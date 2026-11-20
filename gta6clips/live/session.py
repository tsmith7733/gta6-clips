"""One live stream being recorded: pieces + live chat + loudness -> scores -> windows cut and queued.

Everything is on one wall clock. The session's timeline starts when its recording
started (`origin`); chat offsets, loudness bins and windows are seconds from there. The
session is stored as a stream whose video_id is '<YouTube id>@<origin epoch>', so a later
scan of the finished VOD (plain '<id>') stays separate.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .. import db, notify
from ..chat import ChatMessage
from ..scoring import ScoringSettings, WindowSettings
from ..scoring import clock as _clock
from ..streamers import Streamer, require_permission
from .recorder import Segment
from .select import Decision, LiveSelector, LiveSettings
from .youtube import LiveCheck

log = logging.getLogger(__name__)


class Recorder(Protocol):
    def start(self) -> None: ...
    def poll(self) -> list[Segment]: ...
    def stop(self) -> list[Segment]: ...


class ChatFeed(Protocol):
    def start(self) -> None: ...
    def read_new(self) -> list[ChatMessage]: ...   # (UTC seconds, text)
    def stop(self) -> list[ChatMessage]: ...


class LiveSession:
    def __init__(self, *, conn: sqlite3.Connection, streamer: Streamer, check: LiveCheck, recorder: Recorder,
                 chat: ChatFeed | None, measure: Callable[[Path], list[float]],
                 cut: Callable[[Sequence[Segment], float, float, Path], Path], windows_dir: Path,
                 scoring: ScoringSettings, windows: WindowSettings, live: LiveSettings,
                 clock: Callable[[], float] = time.time):
        self.conn = conn
        self.streamer = require_permission(streamer)
        self.check = check
        self.recorder = recorder
        self.chat_feed = chat
        self.measure = measure
        self.cut = cut
        self.windows_dir = windows_dir
        self.bin_s = scoring.bin_s
        self.windows = windows
        self.selector = LiveSelector(scoring, windows, live)
        self.clock = clock
        self.origin = 0.0
        self.stream_id = 0
        self.chat: list[ChatMessage] | None = [] if chat is not None else None   # (offset_s, text)
        self.loudness: list[float | None] = []
        self.segments: list[Segment] = []
        self._last_chat_utc = 0.0
        self.title = check.title
        self.misses = 0
        self.stopped = False

    @property
    def video_id(self) -> str:
        return self.check.video_id

    def start(self) -> None:
        require_permission(self.streamer)
        self.origin = self.clock()
        stream = db.upsert_stream(self.conn, platform="youtube", video_id=f"{self.video_id}@{int(self.origin)}",
                                  streamer=self.streamer.name, url=self.check.url, title=self.title,
                                  start_utc=self.origin, duration_s=None)
        self.stream_id = int(stream["id"])
        db.set_stream_status(self.conn, self.stream_id, chat="ok" if self.chat_feed else "unsupported",
                             audio="ok")
        log.info("%s is live in GTA 6: recording %r", self.streamer.name, self.title)
        notify.current().live(f"🔴 {self.streamer.name} is live in GTA 6: recording and watching for moments. "
                              f"({self.title[:120]})")
        self.recorder.start()
        if self.chat_feed is not None:
            self.chat_feed.start()

    # -- the regular step --------------------------------------------------------------
    def tick(self) -> list[Decision]:
        if self.stopped:
            return []
        self._ingest(self.recorder.poll(), self.chat_feed.read_new() if self.chat_feed else [])
        return self._select()

    def stop(self) -> list[Decision]:
        if self.stopped:
            return []
        self.stopped = True
        segments = self.recorder.stop()
        messages = self.chat_feed.stop() if self.chat_feed else []
        self._ingest(segments, messages)
        decisions = self._select()
        if self.segments:
            self.conn.execute("UPDATE streams SET duration_s = ? WHERE id = ?",
                              (self.segments[-1].end_utc - self.origin, self.stream_id))
            self.conn.commit()
        log.info("Stopped recording %s (%d windows sent from this stream)", self.streamer.name,
                 len(self.selector.sent))
        notify.current().live(f"⏹️ Stopped recording {self.streamer.name} ({len(self.selector.sent)} moment(s) sent "
                              "to Clips Kitty from this stream).")
        return decisions

    def _ingest(self, segments: list[Segment], messages: list[ChatMessage]) -> None:
        for seg in segments:
            db.add_segment(self.conn, self.stream_id, str(seg.path), seg.start_utc, seg.end_utc)
            self.segments.append(seg)
            try:
                bins = self.measure(seg.path)
            except Exception as exc:  # one unreadable piece shouldn't stop the recording
                log.warning("Couldn't measure loudness of %s: %s", seg.path.name, exc)
                continue
            first = int((seg.start_utc - self.origin) // self.bin_s)
            rows = []
            for i, value in enumerate(bins):
                k = first + i
                if k < 0:
                    continue
                if k >= len(self.loudness):
                    self.loudness.extend([None] * (k + 1 - len(self.loudness)))
                current = self.loudness[k]
                self.loudness[k] = value if current is None else max(current, value)
                rows.append((k * self.bin_s, self.loudness[k]))
            db.set_signal_values(self.conn, self.stream_id, "loudness", rows)
        if self.chat is not None and messages:
            # A restarted chat reader sends some messages again; only take ones newer than what we have.
            fresh = [(utc - self.origin, text) for utc, text in messages
                     if utc >= self.origin and utc > self._last_chat_utc]
            if fresh:
                self._last_chat_utc = max(self._last_chat_utc, max(t for t, _ in fresh) + self.origin)
                self.chat.extend(fresh)
                self.chat.sort(key=lambda m: m[0])
                db.add_chat(self.conn, self.stream_id, self.origin, fresh)

    def _filled_loudness(self) -> list[float] | None:
        """Bins with no measurement (a gap in the recording) take the previous value: neutral, not silent."""
        if not any(v is not None for v in self.loudness):
            return None
        first = next(v for v in self.loudness if v is not None)
        out, last = [], first
        for v in self.loudness:
            last = last if v is None else v
            out.append(last)
        return out

    def _select(self) -> list[Decision]:
        if not self.segments:
            return []
        recorded_from = self.segments[0].start_utc - self.origin
        recorded_until = self.segments[-1].end_utc - self.origin
        decisions = self.selector.update(chat=self.chat, loudness=self._filled_loudness(),
                                         recorded_until_s=recorded_until, recorded_from_s=recorded_from)
        for d in decisions:
            if d.sent:
                log.info("%s: moment at %s (%s); cutting the window", self.streamer.name,
                         _clock(d.peak_s), d.why)
                self._queue_window(d)
            else:
                log.debug("%s: spike at %s not sent: %s", self.streamer.name, _clock(d.peak_s), d.why)
        return decisions

    def _queue_window(self, d: Decision) -> None:
        w = d.window
        assert w is not None
        require_permission(self.streamer)
        start_utc, end_utc = self.origin + w.start_s, self.origin + w.end_s
        window_id = db.add_window(self.conn, streamer=self.streamer.name, source_url=self.check.url,
                                  source_title=self.title, ck_job_id=None, ck_video_id=None, state="planned",
                                  stream_id=self.stream_id, offset_start_s=w.start_s, offset_end_s=w.end_s,
                                  start_utc=start_utc, end_utc=end_utc, score=w.score, reasons=w.reasons)
        dest = self.windows_dir / f"live_{self.video_id}_{int(start_utc)}-{int(end_utc)}.mp4"
        try:
            path = self.cut(self.segments, start_utc, end_utc, dest)
        except Exception as exc:
            db.update_window(self.conn, window_id, state="failed", error=f"cut failed: {exc}"[:2000])
            log.error("Couldn't cut the window at %s: %s", _clock(w.peak_s), exc)
            return
        db.update_window(self.conn, window_id, local_path=str(path))
