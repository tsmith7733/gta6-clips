"""After Clips Kitty finishes: credit line, hashtags and 3 title options for every clip.

Writes the result back to Clips Kitty (PATCH /clips/{id}) and records each clip
in our database as pending_review. Nothing here publishes anything.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from . import db
from .clipskitty import ClipsKittyClient
from .config import Config
from .llm import ClipBrief, TitleWriter, TitleWriterError
from .streamers import Streamer, require_permission

log = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 5000  # YouTube's limit, enforced by Clips Kitty too
DECIDED = ("approved", "rejected", "published")


class CreditError(Exception):
    """A description couldn't be given the streamer's credit."""


def credit_block(streamer: Streamer) -> str:
    """The streamer's credit line, plus their channel link if the line doesn't already have it."""
    credit = streamer.credit.strip()
    if streamer.channel_url.rstrip("/").lower() not in credit.lower():
        credit = f"{credit}\n{streamer.channel_url}"
    return credit


def has_credit(description: str, streamer: Streamer) -> bool:
    lowered = description.lower()
    return (streamer.credit.strip().lower() in lowered
            and streamer.channel_url.rstrip("/").lower() in lowered)


def with_credit(description: str, streamer: Streamer) -> str:
    """Returns the description ending with the credit block, exactly once."""
    block = credit_block(streamer)
    body = (description or "").replace(block, "").rstrip()
    result = f"{body}\n\n{block}" if body else block
    if len(result) > MAX_DESCRIPTION_CHARS:
        keep = MAX_DESCRIPTION_CHARS - len(block) - 3
        if keep < 0:
            raise CreditError(f"{streamer.name}'s credit line is longer than a YouTube description allows")
        result = f"{body[:keep].rstrip()}\n\n{block}"
    if not has_credit(result, streamer):
        raise CreditError(f"Couldn't add {streamer.name}'s credit to the description")
    return result


def merge_hashtags(existing: list[str], required: list[str]) -> list[str]:
    """Required tags first, then Clips Kitty's own; each starts with '#', no duplicates."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in [*required, *existing]:
        tag = str(tag).strip().replace(" ", "")
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag
        if tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def required_hashtags(cfg: Config, streamer: Streamer) -> list[str]:
    return merge_hashtags([], [*cfg.get("metadata.hashtags", []), streamer.hashtag])


@dataclass
class ClipOutcome:
    ck_clip_id: int
    title: str
    status: str
    notes: list[str] = field(default_factory=list)
    skipped: bool = False


def postprocess_video(client: ClipsKittyClient, conn: sqlite3.Connection, cfg: Config, streamer: Streamer,
                      ck_video_id: str, writer: TitleWriter, *, window_id: int | None = None,
                      source_title: str = "", refresh: bool = False) -> list[ClipOutcome]:
    """Credits, tags and titles every clip of one Clips Kitty video.

    Clips already in our database are skipped (titles cost an API call), unless
    refresh=True. Clips the owner already approved, rejected or published are never touched.
    """
    require_permission(streamer)
    tags = required_hashtags(cfg, streamer)
    min_s = float(cfg.get("clips.min_seconds", 15))
    max_s = float(cfg.get("clips.max_seconds", 45))
    outcomes: list[ClipOutcome] = []

    for clip in client.video_clips(ck_video_id):
        clip_id = int(clip["id"])
        row = conn.execute("SELECT status FROM clips WHERE ck_clip_id = ?", (clip_id,)).fetchone()
        if row and (row["status"] in DECIDED or not refresh):
            outcomes.append(ClipOutcome(clip_id, clip.get("title", ""), row["status"], skipped=True))
            continue

        notes: list[str] = []
        start_s, end_s = float(clip.get("start_s") or 0), float(clip.get("end_s") or 0)
        duration = max(0.0, end_s - start_s)
        if duration and not (min_s <= duration <= max_s):
            notes.append(f"Length {duration:.0f}s is outside the {min_s:.0f}-{max_s:.0f}s target")

        transcript = " ".join(line.get("text", "") for line in client.captions(clip_id)).strip()
        brief = ClipBrief(
            streamer=streamer.name,
            source_title=source_title,
            clipskitty_title=str(clip.get("title") or ""),
            clipskitty_hook=str(clip.get("hook") or ""),
            transcript=transcript,
            duration_s=duration,
        )
        try:
            suggestions = writer.write(brief)
            titles, hook = suggestions.titles, suggestions.hook
        except TitleWriterError as exc:
            log.warning("Clip %s: %s. Keeping Clips Kitty's title.", clip_id, exc)
            notes.append(f"Title options unavailable: {exc}")
            titles, hook = [brief.clipskitty_title or f"{streamer.name} GTA 6 moment"], ""

        description = with_credit(str(clip.get("description") or ""), streamer)
        hashtags = merge_hashtags(list(clip.get("hashtags") or []), tags)
        client.patch_clip(clip_id, title=titles[0], description=description, hashtags=hashtags)

        db.upsert_clip(
            conn, ck_clip_id=clip_id, window_id=window_id, ck_video_id=ck_video_id, streamer=streamer.name,
            status="pending_review", score=clip.get("score"), start_s=start_s, end_s=end_s,
            title=titles[0], title_options=titles, hook=hook, description=description,
            hashtags=hashtags, notes=notes,
        )
        log.info("Clip %s ready for review: %s", clip_id, titles[0])
        outcomes.append(ClipOutcome(clip_id, titles[0], "pending_review", notes))
    return outcomes
