"""Twitch's most-viewed GTA 6 clips of permitted streamers, queued for Clips Kitty.

Viewers already pick a stream's best moments by clipping them. Every run (from `watch`,
every twitch_clips.every_minutes, or `twitch-clips` by hand) this asks Helix for each
permitted Twitch streamer's clips of the last twitch_clips.hours, keeps those in GTA 6
with enough views, and cuts each one from the VOD with twitch_clips.lead_in_seconds
before and tail_seconds after (so the Short gets its setup and there's slack to trim).
Without a VOD (deleted, subscriber-only) the clip itself is used.

The cut is queued as a window (state planned) on a stream '<login>@clips'; the watcher's
dispatcher sends it to Clips Kitty's own moment finder with a focus naming the clipped
time range, then collects, credits and announces it like any live window. A clip is
never queued twice, nor a second clip of the same moment.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import db
from .config import Config
from .downloads import DownloadError, Fetcher
from .exact import ExactSettings
from .scoring import clock as _clock
from .sources import SourceInfo
from .streamers import Streamer, channel_keys, require_permission
from .twitch import Helix, HelixError

log = logging.getLogger(__name__)

VOD_WAIT_S = 3 * 3600.0     # a clip without a VOD offset yet: wait this long for Twitch before using the clip
MIN_CUT_BYTES = 10_000      # smaller than this, the download came back empty (bad ones were 261 bytes)


@dataclass(frozen=True)
class ClipSettings:
    game_name: str = "Grand Theft Auto VI"
    every_s: float = 7200.0
    hours: float = 24.0
    min_views: int = 50
    max_per_streamer: int = 3
    max_per_run: int = 6
    lead_in_s: float = 45.0
    tail_s: float = 30.0
    focus: str = ""

    @classmethod
    def from_config(cls, cfg: Config) -> "ClipSettings":
        s = cfg.get("twitch_clips", {}) or {}
        d = cls()
        return cls(
            game_name=str(s.get("game_name", d.game_name)),
            every_s=float(s.get("every_minutes", d.every_s / 60)) * 60,
            hours=float(s.get("hours", d.hours)),
            min_views=int(s.get("min_views", d.min_views)),
            max_per_streamer=int(s.get("max_per_streamer", d.max_per_streamer)),
            max_per_run=int(s.get("max_per_run", d.max_per_run)),
            lead_in_s=float(s.get("lead_in_seconds", d.lead_in_s)),
            tail_s=float(s.get("tail_seconds", d.tail_s)),
            focus=" ".join(str(s.get("focus", "") or "").split()),
        )


@dataclass
class ClipPick:
    streamer: Streamer
    clip: dict
    url: str              # what's cut: the VOD, or the clip itself
    video_id: str         # the VOD id, or the clip id
    start_s: float
    end_s: float
    moment_start_s: float  # where the clipped moment sits inside the cut
    moment_end_s: float

    @property
    def views(self) -> int:
        return int(self.clip.get("view_count") or 0)

    @property
    def from_vod(self) -> bool:
        return "/videos/" in self.url


@dataclass
class ClipRun:
    picks: list[ClipPick] = field(default_factory=list)
    queued: list[int] = field(default_factory=list)     # window ids
    skipped: list[str] = field(default_factory=list)


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(text: str) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def clip_score(views: int) -> float:
    """Where a clip sits in the dispatcher's queue next to live windows (scores ~4-9): 100 views = 5."""
    return round(3.0 + math.log10(max(views, 1)), 2)


def twitch_streamers(streamers: list[Streamer]) -> list[Streamer]:
    """Permitted Twitch streamers, highest priority first. The gate runs here."""
    return [require_permission(s) for s in sorted(streamers, key=lambda s: s.priority)
            if s.platform == "twitch" and s.permission]


def _login(streamer: Streamer) -> str:
    keys = channel_keys("twitch", streamer.channel_url)
    return next(iter(sorted(keys)), "")


class TwitchClipSource:
    def __init__(self, *, cfg: Config, conn: sqlite3.Connection, helix: Helix, streamers: list[Streamer],
                 fetcher: Fetcher, windows_dir: Path, clock: Callable[[], float] = time.time,
                 silent: Callable[[Path, float, float], float | None] | None = None):
        self.cfg = cfg
        # Where a cut is silent within [start, end], or None (media.silent_stretch): Twitch mutes VOD
        # stretches with copyrighted music, while the clip viewers made keeps its sound.
        self.silent = silent
        self.conn = conn
        self.helix = helix
        self.streamers = streamers
        self.fetcher = fetcher
        self.windows_dir = windows_dir
        self.clock = clock
        self.settings = ClipSettings.from_config(cfg)

    # -- picking -----------------------------------------------------------------------
    def find(self) -> ClipRun:
        run = ClipRun()
        targets = twitch_streamers(self.streamers)
        if not targets:
            run.skipped.append("No Twitch streamer in streamers.yaml has permission: true.")
            return run
        game = self.helix.game_id(self.settings.game_name, self.conn)
        if not game:
            raise HelixError(f"Twitch doesn't know a game called {self.settings.game_name!r} "
                             "(twitch_clips.game_name in config.yaml).")
        logins = {s.name: _login(s) for s in targets}
        ids = self.helix.user_ids([lg for lg in logins.values() if lg])
        now = self.clock()
        window = dict(started_at=_rfc3339(now - self.settings.hours * 3600), ended_at=_rfc3339(now))
        picks: list[ClipPick] = []
        for streamer in targets:
            bid = ids.get(logins[streamer.name])
            if not bid:
                run.skipped.append(f"{streamer.name}: Twitch has no channel {logins[streamer.name]!r} "
                                   f"(check channel_url in streamers.yaml)")
                continue
            mine: list[ClipPick] = []
            for clip in self.helix.clips(bid, first=40, **window):
                if len(mine) >= self.settings.max_per_streamer:
                    break
                pick, why = self._consider(streamer, clip, game, now, picks + mine)
                if pick is not None:
                    mine.append(pick)
                elif why:
                    run.skipped.append(f"{streamer.name}: {why}")
            picks.extend(mine)
        picks.sort(key=lambda p: p.views, reverse=True)
        run.picks = picks[:self.settings.max_per_run]
        for p in picks[self.settings.max_per_run:]:
            run.skipped.append(f"{p.streamer.name}: {p.clip.get('title')!r} is over twitch_clips.max_per_run")
        return run

    def _consider(self, streamer: Streamer, clip: dict, game: str, now: float,
                  taken: list[ClipPick]) -> tuple[ClipPick | None, str]:
        title = str(clip.get("title") or "")
        if str(clip.get("game_id") or "") != game:
            return None, ""                                    # another game: not worth a line
        views = int(clip.get("view_count") or 0)
        if views < self.settings.min_views:
            return None, ""
        url = str(clip.get("url") or "")
        if not url or db.windows_for_source(self.conn, url):
            return None, ""                                    # queued in an earlier run
        duration = float(clip.get("duration") or 30)
        vod, offset = str(clip.get("video_id") or ""), clip.get("vod_offset")
        lead, tail = self.settings.lead_in_s, self.settings.tail_s
        if vod and offset is not None:
            start = max(0.0, float(offset) - lead)
            end = float(offset) + duration + tail
            pick = ClipPick(streamer, clip, f"https://www.twitch.tv/videos/{vod}", vod, start, end,
                            float(offset) - start, float(offset) - start + duration)
        elif vod and now - _parse_time(clip.get("created_at", "")) < VOD_WAIT_S:
            return None, f"{title!r} waits for Twitch to place it in the VOD"
        else:
            slug = str(clip.get("id") or url.rstrip("/").rsplit("/", 1)[-1])
            pick = ClipPick(streamer, clip, url, slug, 0.0, duration, 0.0, duration)
        if self._same_moment(pick, taken):
            return None, f"{title!r} is the same moment as a clip already picked"
        return pick, ""

    def _same_moment(self, pick: ClipPick, taken: list[ClipPick]) -> bool:
        if not pick.from_vod:
            return False
        for other in taken:
            if other.from_vod and other.video_id == pick.video_id and \
                    pick.start_s < other.end_s and pick.end_s > other.start_s:
                return True
        for row in self.conn.execute("SELECT offset_start_s, offset_end_s FROM windows WHERE reasons LIKE ?",
                                     (f"%(vod {pick.video_id} %",)):
            if pick.start_s < (row["offset_end_s"] or 0) and pick.end_s > (row["offset_start_s"] or 0):
                return True
        return False

    # -- cutting and queueing ----------------------------------------------------------
    def last_run(self) -> float:
        """When the last real (not dry) run happened, so a restarted watcher waits out the interval."""
        try:
            return float(db.kv_get(self.conn, "twitch_clips.last_run") or "-1e18")
        except ValueError:
            return -1e18

    def run_once(self, *, dry_run: bool = False) -> ClipRun:
        run = self.find()
        if dry_run:
            return run
        db.kv_set(self.conn, "twitch_clips.last_run", repr(self.clock()))
        for pick in run.picks:
            window_id = self._queue(pick)
            if window_id is not None:
                run.queued.append(window_id)
        return run

    def _cut(self, pick: ClipPick) -> Path:
        streamer = require_permission(pick.streamer)
        info = SourceInfo(url=pick.url, platform="twitch", video_id=pick.video_id,
                          title=str(pick.clip.get("title") or ""), description="", channel_name=streamer.name,
                          channel_values=(streamer.channel_url,), duration=None)
        dest = self.windows_dir / f"twitch_{pick.video_id}_{int(pick.start_s)}-{int(pick.end_s)}"
        path = self.fetcher.cut(info, streamer, pick.start_s, pick.end_s, dest)
        if path.stat().st_size < MIN_CUT_BYTES:   # long VODs sometimes give back an empty file
            path.unlink(missing_ok=True)
            raise DownloadError(f"the download of {pick.url} came back empty")
        return path

    def _queue(self, pick: ClipPick) -> int | None:
        streamer = require_permission(pick.streamer)
        clip, title = pick.clip, str(pick.clip.get("title") or "Twitch clip")
        try:
            path = self._cut(pick)
            if pick.from_vod and self.silent is not None:
                muted_at = self.silent(path, pick.moment_start_s, pick.moment_end_s)
                if muted_at is not None:
                    path.unlink(missing_ok=True)
                    raise DownloadError(f"Twitch muted the VOD there (silent from {muted_at:.0f} s into the cut)")
        except DownloadError as exc:
            if not pick.from_vod:
                log.warning("Couldn't download the Twitch clip %r: %s", title, exc)
                return None   # not recorded, so the next run tries again
            log.info("Couldn't use the VOD for %r (%s); using the clip itself.", title, exc)
            duration = float(clip.get("duration") or 30)
            pick = ClipPick(streamer, clip, str(clip["url"]), str(clip.get("id") or "clip"), 0.0, duration,
                            0.0, duration)
            try:
                path = self._cut(pick)
            except DownloadError as exc2:
                log.warning("Couldn't download the Twitch clip %r: %s", title, exc2)
                return None
        login = _login(streamer)
        stream = db.upsert_stream(self.conn, platform="twitch", video_id=f"{login}@clips", streamer=streamer.name,
                                  url=streamer.channel_url, title=f"{streamer.name} Twitch clips",
                                  start_utc=None, duration_s=None)
        where = f"(vod {pick.video_id} at {_clock(pick.start_s + pick.moment_start_s)})" if pick.from_vod \
            else "(clip only, no VOD)"
        created = _parse_time(clip.get("created_at", "")) or None
        window_id = db.add_window(
            self.conn, streamer=streamer.name, source_url=str(clip["url"]), source_title=title,
            ck_job_id=None, ck_video_id=None, state="planned", stream_id=stream["id"],
            offset_start_s=pick.start_s, offset_end_s=pick.end_s, start_utc=created, end_utc=None,
            score=clip_score(pick.views), reasons=f"Twitch clip, {pick.views} views {where}")
        a, b = _mmss(pick.moment_start_s), _mmss(pick.moment_end_s)
        focus = f"Twitch viewers clipped {a}-{b} of this video: build the clip around that moment. " \
                + self.settings.focus
        db.update_window(self.conn, window_id, local_path=str(path), finder="clipskitty", focus=focus)
        exact = ExactSettings.from_config(self.cfg)
        if exact.enabled:   # exactly the clipped moment plus slack, not whatever a finder picks
            length = pick.end_s - pick.start_s
            db.update_window(self.conn, window_id, finder="exact",
                             moment_start_s=max(0.0, pick.moment_start_s - exact.pad_s),
                             moment_end_s=min(length, pick.moment_end_s + exact.pad_s))
        log.info("Queued Twitch clip %r by %s (%d views) as window %s", title, streamer.name, pick.views, window_id)
        return window_id
