"""Sends the windows cut from live recordings to Clips Kitty and collects their clips, without blocking.

Works from our database, not from the sessions, so windows left over when the watcher
stopped (queued, or still in Clips Kitty) are picked up the next time it starts.
Strongest windows go first: Clips Kitty runs one job at a time.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import db, notify
from ..clipskitty import ClipsKittyClient, ClipsKittyError, JobRefused, QueueFull
from ..clipskitty.readiness import check_readiness
from ..config import Config
from ..exact import CHAT_LAG_S, ExactSettings, chat_notes, twitch_notes
from ..llm import TitleWriter
from ..postprocess import postprocess_video
from ..scoring import review_score
from ..scan import SPOILER_CHAT_MARGIN_S, window_job_options
from ..spoilers import KeywordSpoilerCheck
from ..streamers import PermissionDenied, Streamer
from ..vod import _failure_text, _streamer_named

log = logging.getLogger(__name__)

READINESS_EVERY_S = 600.0
QA_WIDEN_TIMES = 1          # a window is sent back wider at most this often
MIN_WINDOW_BYTES = 10_000    # a cut smaller than this came out empty (bad ones were 261 bytes)


def window_clip_score(row) -> int | None:
    """A 1-99 clip score from our window's own score, for windows our exact-moments plugin cut.

    That plugin only renders the range we name, so the score Clips Kitty gets back from it is the
    same 90 for every clip. Our window score (chat spike strength live, 3 + log10(views) for a
    Twitch clip; both ~4-9) is what actually ranks the moment: 100 views -> 40, 1,000 -> 60,
    10,000 -> 80. Other windows keep the score Clips Kitty's finder gave (None).
    """
    if row["finder"] != "exact" or row["score"] is None:
        return None
    return review_score(row["score"])


@dataclass(frozen=True)
class QualitySettings:
    enabled: bool = True
    min_s: float = 15.0
    edge_s: float = 1.5
    widen_s: float = 8.0
    require_split: bool = True

    @classmethod
    def from_config(cls, cfg: Config) -> "QualitySettings":
        s = cfg.get("qa", {}) or {}
        d = cls()
        return cls(enabled=s.get("enabled", d.enabled) is True, min_s=float(s.get("min_seconds", d.min_s)),
                   edge_s=float(s.get("edge_seconds", d.edge_s)), widen_s=float(s.get("widen_seconds", d.widen_s)),
                   require_split=s.get("require_split", d.require_split) is True)


def quality_problems(clip: dict, file_s: float, q: QualitySettings, *, check_edges: bool = True
                     ) -> tuple[list[str], bool]:
    """What's wrong with a clip Clips Kitty made, and whether a wider range could fix it. `check_edges`
    is off for a Twitch clip used as it is (no VOD): the clip itself is the whole recording, so neither
    its length nor its edges can be helped."""
    problems, widen = [], False
    start, end = float(clip.get("start_s") or 0), float(clip.get("end_s") or 0)
    if check_edges and end - start < q.min_s:
        problems.append(f"only {end - start:.0f} s long, too short to show the whole story")
        widen = True
    if check_edges and file_s and (start <= q.edge_s or end >= file_s - q.edge_s):
        problems.append("cut off at the " + ("start" if start <= q.edge_s else "end") + " of its recording")
        widen = True
    gaming = (clip.get("render_opts") or {}).get("gaming") or {}
    if q.require_split and gaming and gaming.get("layout") != "split":
        problems.append("no split screen: Clips Kitty didn't find the streamer's webcam")
    return problems, widen


def clipskitty_finder_options(options: dict) -> dict:
    """Job options for Clips Kitty's own moment finder: no plugin pipeline, and force, because
    Clips Kitty already processed this video once (with the plugin)."""
    out = {k: v for k, v in options.items() if k != "pipeline"}
    out["force"] = True
    return out


class Dispatcher:
    def __init__(self, *, cfg: Config, client: ClipsKittyClient, conn: sqlite3.Connection,
                 streamers: list[Streamer], writer: TitleWriter, clock: Callable[[], float] = time.time,
                 silent: Callable[[Path, float, float], float | None] | None = None):
        self.cfg = cfg
        self.silent = silent   # media.silent_stretch: where a recording is silent within a range, or None
        self.client = client
        self.conn = conn
        self.streamers = streamers
        self.writer = writer
        self.clock = clock
        self.spoilers = KeywordSpoilerCheck.from_config(cfg)
        self.exact = ExactSettings.from_config(cfg)
        self.qa = QualitySettings.from_config(cfg)
        self._ready_checked = 0.0
        self._ready = True
        self._queue_full_until = 0.0
        self.collected: list[int] = []        # window ids, for reporting

    def ready(self) -> bool:
        """Re-checks every 10 minutes that Clips Kitty's automation is still off and the plugin is there."""
        now = self.clock()
        if now - self._ready_checked >= READINESS_EVERY_S or not self._ready_checked:
            self._ready_checked = now
            try:
                readiness = check_readiness(self.client, self.cfg)
            except ClipsKittyError as exc:
                if self._ready:
                    log.warning("Clips Kitty isn't reachable (%s); windows wait until it is.", exc)
                self._ready = False
                return False
            if not readiness.ok and self._ready:
                log.warning("Clips Kitty isn't ready, so windows wait (recording goes on):\n- %s",
                            "\n- ".join(readiness.problems))
            elif readiness.ok and not self._ready:
                log.info("Clips Kitty is ready again; sending the waiting windows.")
            self._ready = readiness.ok
        return self._ready

    def tick(self) -> None:
        try:
            self._collect()
            if self.ready():
                self._send()
        except ClipsKittyError as exc:
            log.warning("Clips Kitty: %s", exc)

    # -- sending ---------------------------------------------------------------------
    def _send(self) -> None:
        if self.clock() < self._queue_full_until:
            return
        for row in db.live_windows(self.conn, ("planned",)):
            if row["not_before"] and self.clock() < float(row["not_before"]):
                continue   # a render retry, waiting for the GPU to free up
            path = Path(row["local_path"]) if row["local_path"] else None
            if path is None or not path.is_file():
                continue  # still being cut, or the cut failed (then it's marked failed)
            if path.stat().st_size < MIN_WINDOW_BYTES:
                db.update_window(self.conn, row["id"], state="failed", error="the cut came out empty")
                log.error("Window %s's file is empty (%s), so it isn't sent.", row["id"], path.name)
                continue
            try:
                streamer = _streamer_named(self.streamers, row["streamer"])   # the permission gate again
            except PermissionDenied as exc:
                db.update_window(self.conn, row["id"], state="failed", error=str(exc))
                log.error("%s", exc)
                continue
            if str(row["stream_video_id"]).endswith("@clips"):
                title = f"{row['source_title']} [Twitch clip]"[:100]
            else:
                started = datetime.fromtimestamp(row["start_utc"] or 0, timezone.utc).strftime("%H:%M")
                title = f"{row['source_title']} [live {started} UTC]"[:100]
            options = window_job_options(self.cfg, streamer)
            if row["finder"] == "clipskitty":
                options = clipskitty_finder_options(options)
            if row["focus"]:
                options["focus"] = " ".join(str(row["focus"]).split())[:600]   # Clips Kitty's limit
            if row["finder"] == "exact" and row["moment_start_s"] is not None and self.exact.enabled:
                options["pipeline"] = self.exact.pipeline(float(row["moment_start_s"]), float(row["moment_end_s"]),
                                                          notes=self._title_notes(row))
                options["max_clips"] = 1
                options.pop("force", None)
            try:
                submitted = self.client.submit_local(str(path.resolve()), title=title,
                                                     channel=streamer.clipskitty_creator, options=options)
            except QueueFull:
                log.info("Clips Kitty's queue is full; the next windows wait a few minutes.")
                self._queue_full_until = self.clock() + 180
                return
            except JobRefused as exc:   # this window only: the others still go
                db.update_window(self.conn, row["id"], state="failed", error=f"Clips Kitty refused it: {exc}"[:2000])
                log.error("Clips Kitty refused window %s: %s", row["id"], exc)
                continue
            job_id = submitted.job_id or submitted.queued_job_id
            if submitted.already_processed and submitted.video_id:
                db.update_window(self.conn, row["id"], state="done", ck_video_id=submitted.video_id)
            elif job_id is None:
                db.update_window(self.conn, row["id"], state="failed",
                                 error="Clips Kitty accepted the window but returned no job id")
            else:
                db.update_window(self.conn, row["id"], state="submitted", ck_job_id=job_id,
                                 ck_video_id=submitted.video_id)
                log.info("Window %s (score %.1f) sent to Clips Kitty as job %s%s", row["id"], row["score"] or 0,
                         job_id, " (Clips Kitty's own moment finder)" if row["finder"] == "clipskitty" else "")

    def _render_failed(self, row: sqlite3.Row) -> bool:
        """Clips Kitty found moments but couldn't render them (its log says so; the job itself is 'done')."""
        if not row["ck_job_id"]:
            return False
        try:
            text = self.client.job_log(row["ck_job_id"], tail=400)
        except ClipsKittyError:
            return False
        return "Render failed" in text

    def _retry_render(self, row: sqlite3.Row) -> bool:
        """Send the window again under a new file name, after a pause. False once the tries are used up."""
        tries = int(row["render_retries"] or 0)
        limit = int(self.cfg.get("windows.render_retries", 2))
        src = Path(row["local_path"]) if row["local_path"] else None
        if tries >= limit or src is None or not src.is_file():
            log.error("Clips Kitty couldn't render the clips of window %s (%d tries). Its GPU is probably out of "
                      "memory: restart Clips Kitty, then ask me to resend it.", row["id"], tries + 1)
            return False
        # A new name makes it a new video in Clips Kitty, so it's transcribed again too (a failing
        # GPU often means the transcript was made on the CPU, with weaker captions).
        stem = src.stem.split("_retry")[0]
        dest = src.with_name(f"{stem}_retry{tries + 1}{src.suffix}")
        shutil.copy2(src, dest)
        wait_s = float(self.cfg.get("windows.render_retry_minutes", 5)) * 60
        db.update_window(self.conn, row["id"], state="planned", ck_job_id=None, local_path=str(dest),
                         render_retries=tries + 1, not_before=self.clock() + wait_s)
        log.info("Clips Kitty's render failed for window %s; sending it again in %.0f min (try %d of %d).",
                 row["id"], wait_s / 60, tries + 1, limit)
        return True

    def _check_quality(self, row: sqlite3.Row, video_id: str, outcomes: list) -> list:
        """The check every new clip gets before the review page. Returns the outcomes, with the status
        each ended up in. A clip that's too short or cut off is sent back once with a wider range (the
        window goes back to 'planned'; the caller sees that from the database)."""
        fresh = [o for o in outcomes if not o.skipped and o.status in ("pending_review", "spoiler_hold")]
        if not self.qa.enabled or not fresh:
            return outcomes
        try:
            theirs = {c["id"]: c for c in self.client.video_clips(video_id)}
        except ClipsKittyError as exc:
            log.warning("Couldn't check window %s's clips: %s", row["id"], exc)
            return outcomes
        file_s = float((row["offset_end_s"] or 0) - (row["offset_start_s"] or 0))
        for o in fresh:
            clip = theirs.get(o.ck_clip_id)
            if clip is None:
                continue
            problems, widen = quality_problems(clip, file_s, self.qa,
                                               check_edges="clip only" not in (row["reasons"] or ""))
            muted = self._silent_at(row, clip)
            if muted is not None:
                problems.append(f"no sound from {muted - float(clip['start_s']):.0f} s into the clip (Twitch mutes "
                                "parts of a broadcast with copyrighted music)")
                widen = False
            if not problems:
                continue
            why = "; ".join(problems)
            if widen and self._send_wider(row, clip, file_s, why):
                db.set_clip_status(self.conn, o.ck_clip_id, "rejected",
                                   note=f"quality check: {why}. Sent back to Clips Kitty with a wider range; "
                                        "the new clip replaces this one")
                o.status = "rejected"
                continue
            db.set_clip_status(self.conn, o.ck_clip_id, "needs_fix", note=f"quality check: {why}")
            o.status = "needs_fix"
        return outcomes

    def _silent_at(self, row: sqlite3.Row, clip: dict) -> float | None:
        """Where the clip's stretch of its recording has no sound, or None (also when it can't be checked)."""
        path = Path(row["local_path"]) if row["local_path"] else None
        if self.silent is None or path is None or not path.is_file():
            return None
        try:
            return self.silent(path, float(clip["start_s"]), float(clip["end_s"]))
        except Exception as exc:   # a check that can't run mustn't hold clips back
            log.debug("Couldn't check the sound of window %s: %s", row["id"], exc)
            return None

    def _send_wider(self, row: sqlite3.Row, clip: dict, file_s: float, why: str) -> bool:
        """Queues the window again for one clip covering the old one plus widen_s each side, when the
        recording has that room and it hasn't been widened already."""
        if int(row["widened"] or 0) >= QA_WIDEN_TIMES or not self.exact.enabled or file_s <= 0:
            return False
        src = Path(row["local_path"]) if row["local_path"] else None
        if src is None or not src.is_file():
            return False
        start = max(0.0, float(clip["start_s"]) - self.qa.widen_s)
        end = min(file_s, float(clip["end_s"]) + self.qa.widen_s)
        if (end - start) - (float(clip["end_s"]) - float(clip["start_s"])) < 5:
            return False   # no room left in the recording
        n = int(row["widened"] or 0) + 1
        dest = src.with_name(f"{src.stem.split('_wide')[0]}_wide{n}{src.suffix}")   # a new video for Clips Kitty
        shutil.copy2(src, dest)
        db.update_window(self.conn, row["id"], state="planned", ck_job_id=None, local_path=str(dest), finder="exact",
                         moment_start_s=start, moment_end_s=end, widened=n, not_before=None)
        log.info("Window %s's clip was %s; sending it again as %.0f-%.0f s.", row["id"], why, start, end)
        return True

    def _title_notes(self, row: sqlite3.Row) -> list[str]:
        """What happens in the moment, for the title: the Twitch clip's own title, or live chat."""
        if str(row["stream_video_id"]).endswith("@clips"):
            return twitch_notes(row["source_title"])
        if row["start_utc"] is None or row["moment_start_s"] is None:
            return []
        start = float(row["start_utc"]) + float(row["moment_start_s"])
        end = float(row["start_utc"]) + float(row["moment_end_s"]) + CHAT_LAG_S
        texts = [r[0] for r in self.conn.execute(
            "SELECT text FROM chat_messages WHERE stream_id = ? AND t_utc BETWEEN ? AND ?",
            (row["stream_id"], start, end))]
        return chat_notes(texts)

    def _retry_with_clipskitty_finder(self, row: sqlite3.Row) -> bool:
        """The GTA plugin found nothing: queue the window again for Clips Kitty's own finder (once)."""
        if row["finder"] != "gta_plugin" or not self.cfg.get("clipskitty.fallback_to_clipskitty_finder", False):
            return False
        path = Path(row["local_path"]) if row["local_path"] else None
        if path is None or not path.is_file():
            return False
        db.update_window(self.conn, row["id"], state="planned", finder="clipskitty", ck_job_id=None)
        log.info("The GTA plugin found no moment in window %s; sending it to Clips Kitty's own finder.", row["id"])
        return True

    # -- collecting ------------------------------------------------------------------
    def _collect(self) -> None:
        for row in db.live_windows(self.conn, ("submitted", "done")):
            video_id = row["ck_video_id"]
            if row["state"] == "submitted":
                job = self.client.job(row["ck_job_id"])
                status = job.get("status")
                if status in ("failed", "cancelled"):
                    error = _failure_text(self.client, row["ck_job_id"], job.get("error") or status)
                    db.update_window(self.conn, row["id"], state="failed", error=error[:2000])
                    log.error("%s", error)
                    continue
                if status != "done":
                    continue
                video_id = job.get("video_id") or video_id
                if not video_id:
                    db.update_window(self.conn, row["id"], state="failed",
                                     error=f"job {row['ck_job_id']} finished without a video id")
                    continue
                db.update_window(self.conn, row["id"], state="done", ck_video_id=video_id)
            try:
                streamer = _streamer_named(self.streamers, row["streamer"])
            except PermissionDenied as exc:
                log.error("%s", exc)
                continue
            stream_id, offset = row["stream_id"], float(row["offset_start_s"] or 0)

            def chat_for(start_s: float, end_s: float, _offset=offset, _sid=stream_id) -> list[str]:
                return [t for _, t in db.chat_for_stream(self.conn, _sid, _offset + start_s - SPOILER_CHAT_MARGIN_S,
                                                         _offset + end_s + SPOILER_CHAT_MARGIN_S)]

            outcomes = postprocess_video(self.client, self.conn, self.cfg, streamer, video_id, self.writer,
                                         window_id=row["id"], source_title=row["source_title"],
                                         spoilers=self.spoilers, chat_for=chat_for,
                                         score=window_clip_score(row))
            if not outcomes:
                if self._render_failed(row):
                    if self._retry_render(row):
                        continue
                elif self._retry_with_clipskitty_finder(row):
                    continue
            outcomes = self._check_quality(row, video_id, outcomes)
            sent_back = self.conn.execute("SELECT state FROM windows WHERE id = ?", (row["id"],)).fetchone()[0] \
                == "planned"
            if not sent_back:
                db.update_window(self.conn, row["id"], state="collected")
                self.collected.append(row["id"])
            for o in outcomes:
                if not o.skipped and o.status != "rejected":
                    log.info("Clip %s ready for review (%s): %s", o.ck_clip_id, o.status, o.title)
                    clip = db.get_clip(self.conn, o.ck_clip_id)
                    seconds = (clip["end_s"] or 0) - (clip["start_s"] or 0) if clip else None
                    notify.current().new_clip(streamer=row["streamer"], title=o.title, clip_id=o.ck_clip_id,
                                              status=o.status, seconds=seconds,
                                              score=clip["score"] if clip else None)
