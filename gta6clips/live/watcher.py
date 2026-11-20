"""The watch loop: who's live in GTA 6, start and stop their recordings, keep everything moving.

Every live.poll_seconds each permitted YouTube streamer's <channel>/live is checked
(yt-dlp, no API quota). A streamer who's live with a GTA 6 title gets a session, up to
live.max_concurrent_streams, highest priority (lowest number) first. A session stops
after live.stop_after_misses checks in a row that find them offline or on another game.
Every live.tick_seconds the sessions ingest what was recorded and pick windows, and the
dispatcher sends and collects. The janitor deletes recorded pieces older than
live.retention_hours. Ctrl+C stops the recordings cleanly; windows still in Clips Kitty
are collected the next time `watch` runs.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import db
from ..clipskitty import ClipsKittyClient
from ..clipskitty.readiness import check_readiness
from ..config import Config
from ..llm import TitleWriter
from ..sources import looks_like_gta6
from ..streamers import PermissionDenied, Streamer, permitted_streamer_for, require_permission
from ..twitch_clips import TwitchClipSource
from ..vod import NotReady
from .dispatch import Dispatcher
from .session import LiveSession
from .youtube import LiveCheck, check_youtube_live

log = logging.getLogger(__name__)

SUPPORTED = ("youtube",)
HELD_UPLOADS_EVERY_S = 600.0


@dataclass(frozen=True)
class WatchSettings:
    poll_s: float = 150.0
    tick_s: float = 15.0
    max_concurrent: int = 3
    stop_after_misses: int = 2
    retention_s: float = 2 * 3600.0
    janitor_every_s: float = 600.0

    @classmethod
    def from_config(cls, cfg: Config) -> "WatchSettings":
        s = cfg.get("live", {}) or {}
        d = cls()
        return cls(
            poll_s=float(s.get("poll_seconds", d.poll_s)),
            tick_s=float(s.get("tick_seconds", d.tick_s)),
            max_concurrent=int(s.get("max_concurrent_streams", d.max_concurrent)),
            stop_after_misses=int(s.get("stop_after_misses", d.stop_after_misses)),
            retention_s=float(s.get("retention_hours", d.retention_s / 3600)) * 3600,
        )


SessionFactory = Callable[[Streamer, LiveCheck], LiveSession]


class Watcher:
    def __init__(self, *, cfg: Config, client: ClipsKittyClient, conn: sqlite3.Connection,
                 streamers: list[Streamer], writer: TitleWriter, make_session: SessionFactory,
                 check_live: Callable[[str], LiveCheck] = check_youtube_live, allow_non_gta: bool = False,
                 clip_source: TwitchClipSource | None = None, publisher=None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.client = client
        self.conn = conn
        self.streamers = streamers
        self.settings = WatchSettings.from_config(cfg)
        self.make_session = make_session
        self.check_live = check_live
        self.allow_non_gta = allow_non_gta
        self.clock = clock
        self.sleep = sleep
        self.keywords = list(cfg.get("gta6.keywords", []) or [])
        self.dispatcher = Dispatcher(cfg=cfg, client=client, conn=conn, streamers=streamers, writer=writer,
                                     clock=clock)
        self.sessions: dict[str, LiveSession] = {}      # streamer name -> session
        self.clip_source = clip_source        # Twitch's top GTA 6 clips, every twitch_clips.every_minutes
        self._last_poll = -1e18
        self._last_janitor = -1e18
        self._last_clips = clip_source.last_run() if clip_source is not None else -1e18
        self._said_unsupported: set[str] = set()
        # Uploads the approved clips of hold_uploads_while_live streamers once they're offline.
        self.publisher = publisher
        self._last_held = -1e18

    # -- who we watch ----------------------------------------------------------------
    def watched(self) -> list[Streamer]:
        """Permitted streamers on a supported platform, highest priority first. The gate runs here too."""
        out = []
        for s in sorted(self.streamers, key=lambda s: s.priority):
            if not s.permission:
                continue
            if s.platform not in SUPPORTED:
                if s.name not in self._said_unsupported:
                    self._said_unsupported.add(s.name)
                    how = " (their Twitch clips are picked up instead)" if s.platform == "twitch" and                         self.clip_source is not None else ""
                    log.info("%s is on %s, which the live recorder doesn't support yet%s.", s.name, s.platform, how)
                continue
            out.append(require_permission(s))
        return out

    def preflight(self) -> list[Streamer]:
        readiness = check_readiness(self.client, self.cfg)
        for warning in readiness.warnings:
            log.warning("%s", warning)
        if not readiness.ok:
            raise NotReady("Clips Kitty isn't ready:\n- " + "\n- ".join(readiness.problems))
        watched = self.watched()
        if not watched and self.clip_source is None:
            raise PermissionDenied("No permitted YouTube streamer in streamers.yaml, so there's nothing to watch.")
        return watched

    # -- the loop --------------------------------------------------------------------
    def run(self, *, stop_after_s: float | None = None) -> None:
        watched = self.preflight()
        if watched:
            log.info("Watching %s (every %.0f s). Ctrl+C stops.", ", ".join(s.name for s in watched),
                     self.settings.poll_s)
        if self.clip_source is not None:
            log.info("Checking Twitch's top GTA 6 clips every %.0f min. Ctrl+C stops.",
                     self.clip_source.settings.every_s / 60)
        began = self.clock()
        try:
            while stop_after_s is None or self.clock() - began < stop_after_s:
                self.step()
                self.sleep(self.settings.tick_s)
        except KeyboardInterrupt:
            log.info("Stopping (Ctrl+C)")
        finally:
            self.stop_all()

    def step(self) -> None:
        now = self.clock()
        if now - self._last_poll >= self.settings.poll_s:
            self._last_poll = now
            self.poll_live()
        for name, session in list(self.sessions.items()):
            try:
                session.tick()
            except Exception:  # one stream's problem mustn't stop the others
                log.exception("Problem in %s's recording", name)
        try:
            self.dispatcher.tick()
        except Exception:  # Clips Kitty or the database hiccuped: keep recording, try again next tick
            log.exception("Problem sending or collecting windows")
        if self.clip_source is not None and now - self._last_clips >= self.clip_source.settings.every_s:
            self._last_clips = now
            try:
                run = self.clip_source.run_once()
                if run.queued:
                    log.info("Queued %d Twitch clip(s) for Clips Kitty", len(run.queued))
            except Exception:   # Twitch or a download hiccuped: try again next time
                log.exception("Problem checking Twitch clips")
        if self.publisher is not None and now - self._last_held >= HELD_UPLOADS_EVERY_S:
            self._last_held = now
            try:
                result = self.publisher.upload_held()
                for s in result["started"]:
                    log.info("Clip %s held while its streamer was live is now scheduled for %s", s["clip_id"],
                             s["when"])
            except Exception:
                log.exception("Problem uploading clips held while their streamer was live")
        if now - self._last_janitor >= self.settings.janitor_every_s:
            self._last_janitor = now
            self.janitor(now)

    def poll_live(self) -> None:
        for streamer in self.watched():
            check = self.check_live(streamer.channel_url)
            session = self.sessions.get(streamer.name)
            on_gta = check.live and (self.allow_non_gta or looks_like_gta6(check.title, self.keywords))
            if check.live:
                try:   # the live video must really be this permitted channel's
                    if permitted_streamer_for(self.streamers, "youtube", *check.channel_values) != streamer:
                        log.warning("%s/live points at another channel; not recording it.", streamer.channel_url)
                        on_gta = False
                except PermissionDenied as exc:
                    log.warning("%s", exc)
                    on_gta = False
            if session is not None:
                if check.state == "unknown":
                    continue   # couldn't tell: keep recording
                if on_gta and check.video_id == session.video_id:
                    session.misses = 0
                    if check.title != session.title:
                        session.title = check.title
                    continue
                if on_gta and check.video_id != session.video_id:
                    log.info("%s started a new stream; switching to it.", streamer.name)
                    self._stop(streamer.name)
                else:
                    session.misses += 1
                    why = "offline" if not check.live else f"not GTA 6 now ({check.title!r})"
                    if session.misses < self.settings.stop_after_misses:
                        log.info("%s looks %s; checking again before stopping.", streamer.name, why)
                        continue
                    log.info("%s is %s; stopping the recording.", streamer.name, why)
                    self._stop(streamer.name)
                    continue
            if not on_gta:
                if check.live:
                    log.info("%s is live but not in GTA 6 (%r); not recording.", streamer.name, check.title)
                continue
            if len(self.sessions) >= self.settings.max_concurrent:
                log.warning("%s is live in GTA 6, but %d streams are already recording (live.max_concurrent_"
                            "streams).", streamer.name, len(self.sessions))
                continue
            session = self.make_session(streamer, check)
            session.start()
            self.sessions[streamer.name] = session

    def _stop(self, name: str) -> None:
        session = self.sessions.pop(name, None)
        if session is not None:
            try:
                session.stop()
            except Exception:
                log.exception("Problem stopping %s's recording", name)

    def stop_all(self) -> None:
        for name in list(self.sessions):
            self._stop(name)
        self.dispatcher.tick()   # send what the last moments produced

    def janitor(self, now: float) -> None:
        """Deletes recorded pieces older than live.retention_hours (windows are already cut to their own file)."""
        removed = 0
        for seg in db.old_segments(self.conn, now - self.settings.retention_s):
            Path(seg["path"]).unlink(missing_ok=True)
            db.mark_segment_deleted(self.conn, seg["id"])
            removed += 1
        if removed:
            log.info("Deleted %d recorded pieces older than %.0f h", removed, self.settings.retention_s / 3600)
