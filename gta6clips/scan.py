"""Phase 2: score a permitted GTA 6 VOD on chat and audio, and send only its best windows to Clips Kitty.

probe -> gate -> GTA 6 title check -> readiness -> chat replay + loudness (cached in our DB)
-> spike scores -> windows -> cut each window -> POST /videos/local -> wait -> post-process
(credit, hashtags, titles, spoiler hold).

Safe to run again on the same VOD: chat and loudness are reused, windows already sent
aren't sent twice, and windows left unsent (queue full, a failed cut) are retried.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import db
from .clipskitty import ClipsKittyClient, QueueFull
from .clipskitty.readiness import check_readiness
from .config import Config
from .downloads import DownloadError, Fetcher
from .llm import TitleWriter
from .postprocess import ClipOutcome, postprocess_video
from .scoring import PlannedWindow, ScoringSettings, WindowSettings, find_spikes, plan_windows, score_stream
from .scoring import clock as _clock
from .sources import SourceInfo, looks_like_gta6, probe
from .spoilers import KeywordSpoilerCheck
from .streamers import Streamer, permitted_streamer_for, require_permission
from .vod import JobFailed, NotGta6, NotReady, _wait_and_record, job_options

log = logging.getLogger(__name__)

SENT_STATES = ("submitted", "done", "collected")
SPOILER_CHAT_MARGIN_S = 30.0


class ScanError(Exception):
    """The VOD can't be scored (it's still live, or there's nothing to score it on)."""


@dataclass
class WindowOutcome:
    window_id: int
    start_s: float
    end_s: float
    score: float | None
    reasons: str
    state: str
    job_id: int | None = None
    clips: list[ClipOutcome] = field(default_factory=list)
    error: str = ""


@dataclass
class ScanResult:
    streamer: str
    stream_id: int
    title: str
    chat_messages: int | None          # None: no chat replay
    audio: bool
    windows: list[WindowOutcome] = field(default_factory=list)
    planned_only: bool = False          # --dry-run
    queue_full: bool = False
    waited: bool = True


def window_job_options(cfg: Config, streamer: Streamer) -> dict:
    options = job_options(cfg, streamer)
    max_clips = (cfg.get("windows", {}) or {}).get("max_clips")
    if max_clips:
        options["max_clips"] = int(max_clips)
    return options


def _load_chat(conn: sqlite3.Connection, stream: sqlite3.Row, info: SourceInfo, streamer: Streamer,
               fetcher: Fetcher) -> list[tuple[float, str]] | None:
    if stream["chat_status"] == "ok":
        return db.chat_for_stream(conn, stream["id"])
    if stream["chat_status"] in ("none", "unsupported"):
        return None
    messages = fetcher.chat(info, streamer)
    if messages is None:
        db.set_stream_status(conn, stream["id"], chat="none")
        return None
    db.replace_chat(conn, stream["id"], stream["start_utc"], messages)
    db.set_stream_status(conn, stream["id"], chat="ok")
    log.info("Chat replay: %d messages", len(messages))
    return messages


def _load_loudness(conn: sqlite3.Connection, stream: sqlite3.Row, info: SourceInfo, streamer: Streamer,
                   fetcher: Fetcher, bin_s: float) -> list[float] | None:
    if stream["audio_status"] == "ok":
        stored = db.signal_for_stream(conn, stream["id"], "loudness")
        if len(stored) > 1 and abs(stored[1][0] - bin_s) < 1e-6:
            return [v for _, v in stored]
        log.info("scoring.bin_seconds changed since the last scan; measuring the audio again")
    try:
        values = fetcher.loudness(info, streamer, bin_s)
    except (DownloadError, RuntimeError) as exc:
        log.warning("Couldn't measure the audio, scoring on chat only: %s", exc)
        return None
    db.replace_signal(conn, stream["id"], "loudness", bin_s, values)
    db.set_stream_status(conn, stream["id"], audio="ok")
    return values


def _overlaps(w: PlannedWindow, row: sqlite3.Row) -> bool:
    return w.start_s < (row["offset_end_s"] or 0) and w.end_s > (row["offset_start_s"] or 0)


def _outcome(row: sqlite3.Row) -> WindowOutcome:
    return WindowOutcome(row["id"], row["offset_start_s"], row["offset_end_s"], row["score"], row["reasons"],
                         row["state"], row["ck_job_id"], error=row["error"])


def run_scan(url: str, *, cfg: Config, client: ClipsKittyClient, conn: sqlite3.Connection,
             streamers: list[Streamer], writer: TitleWriter, fetcher: Fetcher, windows_dir: Path,
             dry_run: bool = False, wait: bool = True, allow_non_gta: bool = False,
             max_windows: int | None = None, probe_fn: Callable[[str], SourceInfo] = probe,
             sleep: Callable[[float], None] = time.sleep) -> ScanResult:
    info = probe_fn(url)
    # The gate, before anything is downloaded.
    streamer = permitted_streamer_for(streamers, info.platform, *info.channel_values)
    log.info("%s by %s: permitted (%s)", info.title or url, streamer.name,
             streamer.permission_source or "no source noted")

    if not looks_like_gta6(info.title, cfg.get("gta6.keywords", [])):
        if not allow_non_gta:
            raise NotGta6(
                f"This video's title doesn't mention GTA 6, so it wasn't scanned:\n  {info.title!r}\n"
                "If it really is GTA 6, run the command again with --allow-non-gta."
            )
        log.warning("The title doesn't mention GTA 6; scanning anyway because of --allow-non-gta.")
    if info.live_status in ("is_live", "is_upcoming", "post_live"):
        raise ScanError("This stream is still live or still being processed by the platform. Scan it once "
                        "the recording is finished (live streams are Phase 3).")
    if not info.duration:
        raise ScanError("The video's length is unknown, so it can't be scored.")

    if not dry_run:
        readiness = check_readiness(client, cfg)
        for warning in readiness.warnings:
            log.warning("%s", warning)
        if not readiness.ok:
            raise NotReady("Clips Kitty isn't ready:\n- " + "\n- ".join(readiness.problems))

    stream = db.upsert_stream(conn, platform=info.platform, video_id=info.video_id, streamer=streamer.name,
                              url=info.url, title=info.title, start_utc=info.start_utc,
                              duration_s=info.duration)
    settings = ScoringSettings.from_config(cfg)
    chat = _load_chat(conn, stream, info, streamer, fetcher)
    loudness = _load_loudness(conn, stream, info, streamer, fetcher, settings.bin_s)
    if chat is None and not loudness:
        raise ScanError("There's no chat replay and the audio couldn't be measured, so there's nothing to "
                        "score this VOD on. Use 'vod' to send the whole video instead.")

    scores = score_stream(settings, duration_s=float(info.duration), chat=chat, loudness=loudness)
    db.replace_signal(conn, stream["id"], "score", settings.bin_s, scores.combined)
    wsettings = WindowSettings.from_config(cfg)
    if max_windows:
        wsettings = WindowSettings(wsettings.lead_in_s, wsettings.tail_s, wsettings.max_s, max_windows)
    spikes = find_spikes(scores, settings.threshold)
    planned = plan_windows(spikes, wsettings, float(info.duration))
    log.info("%d spike(s) over %.1f; %d window(s) planned", len(spikes), settings.threshold, len(planned))

    result = ScanResult(streamer.name, stream["id"], info.title,
                        None if chat is None else len(chat), bool(loudness), planned_only=dry_run)
    existing = db.windows_for_stream(conn, stream["id"])
    if dry_run:
        for w in planned:
            sent = next((r for r in existing if _overlaps(w, r) and r["state"] != "failed"), None)
            result.windows.append(WindowOutcome(0, w.start_s, w.end_s, w.score, w.reasons,
                                                sent["state"] if sent else "planned"))
        return result

    # Record new windows; one overlapping a window we already have is that window
    # (a failed one is retried below rather than duplicated).
    for w in planned:
        if any(_overlaps(w, r) for r in existing):
            continue
        db.add_window(conn, streamer=streamer.name, source_url=info.url, source_title=info.title,
                      ck_job_id=None, ck_video_id=None, state="planned", stream_id=stream["id"],
                      offset_start_s=w.start_s, offset_end_s=w.end_s,
                      start_utc=None if info.start_utc is None else info.start_utc + w.start_s,
                      end_utc=None if info.start_utc is None else info.start_utc + w.end_s,
                      score=w.score, reasons=w.reasons)

    rows = [r for r in db.windows_for_stream(conn, stream["id"]) if r["state"] != "collected"]
    options = window_job_options(cfg, streamer)
    for row in rows:
        if row["state"] not in ("planned", "failed") or result.queue_full:
            continue
        if row["state"] == "failed" and not any(_overlaps(w, row) for w in planned):
            continue  # an old failed window that today's scores no longer pick
        _send_window(conn, client, info, streamer, fetcher, windows_dir, row, options, result)

    rows = [r for r in db.windows_for_stream(conn, stream["id"]) if r["state"] != "collected"]
    if not wait:
        result.waited = False
        result.windows = [_outcome(r) for r in rows]
        return result

    spoilers = KeywordSpoilerCheck.from_config(cfg)
    for row in rows:
        outcome = _outcome(row)
        result.windows.append(outcome)
        if row["state"] not in ("submitted", "done"):
            continue
        try:
            video_id = row["ck_video_id"] if row["state"] == "done" else \
                _wait_and_record(client, conn, cfg, row["id"], row["ck_job_id"], sleep=sleep)
        except JobFailed as exc:
            outcome.state, outcome.error = "failed", str(exc)
            log.error("%s", exc)
            continue
        offset = float(row["offset_start_s"] or 0)

        def chat_for(start_s: float, end_s: float, _offset=offset) -> list[str]:
            return [t for _, t in db.chat_for_stream(conn, stream["id"], _offset + start_s - SPOILER_CHAT_MARGIN_S,
                                                     _offset + end_s + SPOILER_CHAT_MARGIN_S)]

        outcome.clips = postprocess_video(client, conn, cfg, streamer, video_id, writer, window_id=row["id"],
                                          source_title=info.title, spoilers=spoilers, chat_for=chat_for)
        db.update_window(conn, row["id"], state="collected")
        outcome.state = "collected"
    return result


def _send_window(conn: sqlite3.Connection, client: ClipsKittyClient, info: SourceInfo, streamer: Streamer,
                 fetcher: Fetcher, windows_dir: Path, row: sqlite3.Row, options: dict,
                 result: ScanResult) -> None:
    require_permission(streamer)
    start, end = float(row["offset_start_s"]), float(row["offset_end_s"])
    dest = windows_dir / f"{info.platform}_{info.video_id}_{int(start)}-{int(end)}"
    try:
        path = Path(row["local_path"]) if row["local_path"] and Path(row["local_path"]).is_file() else \
            fetcher.cut(info, streamer, start, end, dest)
    except DownloadError as exc:
        db.update_window(conn, row["id"], state="failed", error=str(exc)[:2000])
        log.error("%s", exc)
        return
    db.update_window(conn, row["id"], local_path=str(path))
    title = f"{info.title} [{_clock(start)}]"[:100]
    try:
        # The same creator Clips Kitty made for this channel, so its remembered layout applies.
        submitted = client.submit_local(str(path.resolve()), title=title, channel=streamer.clipskitty_creator,
                                        options=options)
    except QueueFull:
        log.warning("Clips Kitty's queue is full; the remaining windows stay planned. Run the same "
                    "command again later to send them.")
        result.queue_full = True
        return
    job_id = submitted.job_id or submitted.queued_job_id
    if submitted.already_processed:
        if submitted.video_id:
            db.update_window(conn, row["id"], state="done", ck_video_id=submitted.video_id)
            return
        # Clips Kitty has this exact file but didn't say which video it is: nothing to collect.
        db.update_window(conn, row["id"], state="failed",
                         error="Clips Kitty says this window was already processed but gave no video id")
        return
    db.update_window(conn, row["id"], state="submitted", ck_job_id=job_id, ck_video_id=submitted.video_id)
    log.info("Window %s-%s sent to Clips Kitty as job %s", _clock(start), _clock(end), job_id)

