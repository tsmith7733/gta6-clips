"""Sends the windows cut from live recordings to Clips Kitty and collects their clips, without blocking.

Works from our database, not from the sessions, so windows left over when the watcher
stopped (queued, or still in Clips Kitty) are picked up the next time it starts.
Strongest windows go first: Clips Kitty runs one job at a time.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import db, notify
from ..clipskitty import ClipsKittyClient, ClipsKittyError, QueueFull
from ..clipskitty.readiness import check_readiness
from ..config import Config
from ..llm import TitleWriter
from ..postprocess import postprocess_video
from ..scan import SPOILER_CHAT_MARGIN_S, window_job_options
from ..spoilers import KeywordSpoilerCheck
from ..streamers import PermissionDenied, Streamer
from ..vod import _failure_text, _streamer_named

log = logging.getLogger(__name__)

READINESS_EVERY_S = 600.0


def clipskitty_finder_options(options: dict) -> dict:
    """Job options for Clips Kitty's own moment finder: no plugin pipeline, and force, because
    Clips Kitty already processed this video once (with the plugin)."""
    out = {k: v for k, v in options.items() if k != "pipeline"}
    out["force"] = True
    return out


class Dispatcher:
    def __init__(self, *, cfg: Config, client: ClipsKittyClient, conn: sqlite3.Connection,
                 streamers: list[Streamer], writer: TitleWriter, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.client = client
        self.conn = conn
        self.streamers = streamers
        self.writer = writer
        self.clock = clock
        self.spoilers = KeywordSpoilerCheck.from_config(cfg)
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
            try:
                streamer = _streamer_named(self.streamers, row["streamer"])   # the permission gate again
            except PermissionDenied as exc:
                db.update_window(self.conn, row["id"], state="failed", error=str(exc))
                log.error("%s", exc)
                continue
            started = datetime.fromtimestamp(row["start_utc"] or 0, timezone.utc).strftime("%H:%M")
            title = f"{row['source_title']} [live {started} UTC]"[:100]
            options = window_job_options(self.cfg, streamer)
            if row["finder"] == "clipskitty":
                options = clipskitty_finder_options(options)
            try:
                submitted = self.client.submit_local(str(path.resolve()), title=title,
                                                     channel=streamer.clipskitty_creator, options=options)
            except QueueFull:
                log.info("Clips Kitty's queue is full; the next windows wait a few minutes.")
                self._queue_full_until = self.clock() + 180
                return
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
                                         spoilers=self.spoilers, chat_for=chat_for)
            if not outcomes:
                if self._render_failed(row):
                    if self._retry_render(row):
                        continue
                elif self._retry_with_clipskitty_finder(row):
                    continue
            db.update_window(self.conn, row["id"], state="collected")
            self.collected.append(row["id"])
            for o in outcomes:
                if not o.skipped:
                    log.info("Clip %s ready for review (%s): %s", o.ck_clip_id, o.status, o.title)
                    clip = db.get_clip(self.conn, o.ck_clip_id)
                    seconds = (clip["end_s"] or 0) - (clip["start_s"] or 0) if clip else None
                    notify.current().new_clip(streamer=row["streamer"], title=o.title, clip_id=o.ck_clip_id,
                                              status=o.status, seconds=seconds,
                                              score=clip["score"] if clip else None)
