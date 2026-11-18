"""Phase 1: one recorded VOD -> Clips Kitty (with the GTA plugin) -> credited, titled clips for review."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from . import db
from .clipskitty import ClipsKittyClient, ClipsKittyError
from .clipskitty.readiness import check_readiness
from .config import Config
from .llm import TitleWriter
from .postprocess import ClipOutcome, postprocess_video, required_hashtags
from .sources import SourceInfo, looks_like_gta6, probe
from .streamers import PermissionDenied, Streamer, permitted_streamer_for, require_permission

log = logging.getLogger(__name__)


class NotReady(Exception):
    """Clips Kitty isn't safe or able to take work. The message lists what to fix."""


class JobFailed(Exception):
    """Clips Kitty's job failed or timed out. The message includes its log."""


@dataclass
class VodResult:
    streamer: str
    job_id: int | None
    video_id: str | None
    window_id: int
    clips: list[ClipOutcome] = field(default_factory=list)
    waited: bool = True


def job_options(cfg: Config, streamer: Streamer) -> dict:
    job = cfg.get("clipskitty.job", {}) or {}
    options = {
        "gaming": bool(job.get("gaming", True)),
        "captions": bool(job.get("captions", True)),
        "pipeline": {
            "id": cfg.require("clipskitty.gta_plugin_id"),
            "version": str(cfg.require("clipskitty.gta_plugin_version")),
        },
        "hashtags": required_hashtags(cfg, streamer),
    }
    if job.get("max_clips"):
        options["max_clips"] = int(job["max_clips"])
    if job.get("focus"):
        options["focus"] = " ".join(str(job["focus"]).split())[:600]  # Clips Kitty's limit
    return options


def wait_for_job(client: ClipsKittyClient, job_id: int, *, poll_seconds: float, timeout_seconds: float,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> dict:
    deadline = clock() + timeout_seconds
    last_status = None
    while True:
        job = client.job(job_id)
        status = job.get("status")
        if status != last_status:
            log.info("Clips Kitty job %s: %s", job_id, status)
            last_status = status
        if status == "done":
            return job
        if status in ("failed", "cancelled"):
            raise JobFailed(_failure_text(client, job_id, job.get("error") or status))
        if clock() >= deadline:
            raise JobFailed(
                f"Clips Kitty job {job_id} is still '{status}' after {timeout_seconds / 3600:.1f} h. "
                f"It keeps running in Clips Kitty; collect it later with: python -m gta6clips wait {job_id}"
            )
        sleep(poll_seconds)


def _failure_text(client: ClipsKittyClient, job_id: int, error: str) -> str:
    try:
        tail = "\n".join(client.job_log(job_id).splitlines()[-25:])
    except ClipsKittyError:
        tail = "(log unavailable)"
    return f"Clips Kitty job {job_id} failed: {error}\n--- last lines of its log ---\n{tail}"


def run_vod(url: str, *, cfg: Config, client: ClipsKittyClient, conn: sqlite3.Connection,
            streamers: list[Streamer], writer: TitleWriter, wait: bool = True, force: bool = False,
            probe_fn: Callable[[str], SourceInfo] = probe,
            sleep: Callable[[float], None] = time.sleep) -> VodResult:
    info = probe_fn(url)
    # The gate: nothing reaches Clips Kitty unless this channel is permitted.
    streamer = permitted_streamer_for(streamers, info.platform, *info.channel_values)
    log.info("%s by %s: permitted (%s)", info.title or url, streamer.name,
             streamer.permission_source or "no source noted")

    if not looks_like_gta6(f"{info.title} {info.description}", cfg.get("gta6.keywords", [])):
        log.warning("The title and description don't mention GTA 6. Clipping anyway, but check the clips.")

    readiness = check_readiness(client, cfg)
    if not readiness.ok:
        raise NotReady("Clips Kitty isn't ready:\n- " + "\n- ".join(readiness.problems))

    options = job_options(cfg, streamer)
    if force:
        options["force"] = True
    submitted = client.submit_url(url, options)
    job_id = submitted.job_id or submitted.queued_job_id
    video_id = submitted.video_id

    if submitted.already_processed:
        log.info("Clips Kitty already processed this video (%s); collecting its clips. "
                 "Use --force to process it again.", video_id)
        window_id = db.add_window(conn, streamer=streamer.name, source_url=url, source_title=info.title,
                                  ck_job_id=None, ck_video_id=video_id, state="done")
    else:
        if submitted.already_queued:
            log.info("Already in Clips Kitty's queue as job %s", job_id)
        else:
            log.info("Sent to Clips Kitty as job %s", job_id)
        window_id = db.add_window(conn, streamer=streamer.name, source_url=url, source_title=info.title,
                                  ck_job_id=job_id, ck_video_id=video_id)
        if not wait:
            return VodResult(streamer.name, job_id, video_id, window_id, waited=False)
        video_id = _wait_and_record(client, conn, cfg, window_id, job_id, sleep=sleep)

    clips = postprocess_video(client, conn, cfg, streamer, video_id, writer, window_id=window_id,
                              source_title=info.title)
    db.update_window(conn, window_id, state="collected")
    return VodResult(streamer.name, job_id, video_id, window_id, clips)


def _wait_and_record(client: ClipsKittyClient, conn: sqlite3.Connection, cfg: Config, window_id: int,
                     job_id: int, *, sleep: Callable[[float], None]) -> str:
    try:
        job = wait_for_job(
            client, job_id,
            poll_seconds=float(cfg.get("clipskitty.poll_seconds", 30)),
            timeout_seconds=float(cfg.get("clipskitty.job_timeout_hours", 6)) * 3600,
            sleep=sleep,
        )
    except JobFailed as exc:
        db.update_window(conn, window_id, state="failed", error=str(exc)[:2000])
        raise
    video_id = job.get("video_id")
    if not video_id:
        raise JobFailed(f"Clips Kitty job {job_id} finished without a video id")
    db.update_window(conn, window_id, state="done", ck_video_id=video_id)
    return video_id


def resume_job(job_id: int, *, cfg: Config, client: ClipsKittyClient, conn: sqlite3.Connection,
               streamers: list[Streamer], writer: TitleWriter,
               sleep: Callable[[float], None] = time.sleep) -> VodResult:
    """Waits for a job sent earlier (e.g. with --no-wait), then post-processes its clips."""
    row = conn.execute("SELECT * FROM windows WHERE ck_job_id = ? ORDER BY id DESC LIMIT 1",
                       (job_id,)).fetchone()
    if row is None:
        raise PermissionDenied(f"Job {job_id} wasn't sent by this pipeline, so it isn't collected.")
    streamer = _streamer_named(streamers, row["streamer"])
    video_id = _wait_and_record(client, conn, cfg, row["id"], job_id, sleep=sleep)
    clips = postprocess_video(client, conn, cfg, streamer, video_id, writer, window_id=row["id"],
                              source_title=row["source_title"])
    db.update_window(conn, row["id"], state="collected")
    return VodResult(streamer.name, job_id, video_id, row["id"], clips)


def _streamer_named(streamers: list[Streamer], name: str) -> Streamer:
    streamer = next((s for s in streamers if s.name == name), None)
    if streamer is None:
        raise PermissionDenied(f"{name} is no longer in streamers.yaml, so their clips aren't collected.")
    return require_permission(streamer)
