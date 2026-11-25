"""Checks new clips for a licensed song before the owner reviews them (YouTube blocks those).

Each clip waiting for review is checked once (`music_checks`), from its transcript (Clips Kitty's
captions) and the live chat recorded around it, by `llm/music.py`. A likely song moves the clip to
needs_fix with a note naming it, so it can't be approved by accident: the owner can still send it to
review from the "Needs a fix" tab. A possible one only gets the note. Every call is priced into
`llm_usage` (purpose "music check").
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

from . import db
from .clipskitty.client import ClipsKittyError
from .exact import CHAT_LAG_S
from .llm.music import MusicCheck, MusicCheckError, MusicEvidence, MusicVerdict

log = logging.getLogger(__name__)

CHECKED_STATUSES = ("pending_review", "spoiler_hold", "needs_fix")


@dataclass(frozen=True)
class MusicSettings:
    enabled: bool = True
    model: str = "claude-haiku-5-5"
    per_run: int = 20

    @classmethod
    def from_config(cls, cfg) -> "MusicSettings":
        return cls(enabled=cfg.get("music_check.enabled", True) is True,
                   model=str(cfg.get("music_check.model", cls.model)),
                   per_run=int(cfg.get("music_check.per_run", cls.per_run)))


def note_for(v: MusicVerdict) -> str:
    what = f"a licensed song ({v.song})" if v.song else "a licensed song"
    head = "Likely copyrighted music" if v.verdict == "likely" else "Possibly copyrighted music"
    return f"{head}: {what} may be audible, and YouTube can block the Short. {v.evidence}".strip()


class MusicChecker:
    def __init__(self, *, conn: sqlite3.Connection, client, check: MusicCheck, settings: MusicSettings,
                 clock: Callable[[], float] = time.time):
        self.conn = conn
        self.client = client
        self.check = check
        self.settings = settings
        self.clock = clock

    def unchecked(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.*, COALESCE(w.source_title, '') AS source_title, w.start_utc AS w_start_utc, "
            "w.stream_id AS w_stream_id FROM clips c LEFT JOIN windows w ON w.id = c.window_id "
            "LEFT JOIN music_checks m ON m.ck_clip_id = c.ck_clip_id "
            f"WHERE m.ck_clip_id IS NULL AND c.status IN ({','.join('?' * len(CHECKED_STATUSES))}) "
            "ORDER BY c.created_at DESC", CHECKED_STATUSES).fetchall()

    def _chat(self, row: sqlite3.Row) -> list[str]:
        if row["w_start_utc"] is None or row["w_stream_id"] is None:
            return []
        t0 = float(row["w_start_utc"])
        return [r[0] for r in self.conn.execute(
            "SELECT text FROM chat_messages WHERE stream_id = ? AND t_utc BETWEEN ? AND ? ORDER BY t_utc",
            (row["w_stream_id"], t0 + float(row["start_s"] or 0), t0 + float(row["end_s"] or 0) + CHAT_LAG_S))]

    def evidence(self, row: sqlite3.Row) -> MusicEvidence:
        lines = self.client.captions(row["ck_clip_id"])
        return MusicEvidence(
            streamer=row["streamer"], title=row["title"] or "", source_title=row["source_title"] or "",
            duration_s=float(row["end_s"] or 0) - float(row["start_s"] or 0),
            transcript=[(float(x.get("start") or 0), str(x.get("text") or "")) for x in lines],
            chat=self._chat(row))

    def check_clip(self, row: sqlite3.Row) -> MusicVerdict:
        from .editing.editor import record_usage

        verdict = self.check.check(self.evidence(row))
        record_usage(self.conn, model=verdict.model or self.settings.model, purpose="music check",
                     clip_id=row["ck_clip_id"], input_tokens=verdict.input_tokens,
                     output_tokens=verdict.output_tokens, now=self.clock())
        self.conn.execute("INSERT OR REPLACE INTO music_checks (ck_clip_id, verdict, song, evidence, model, "
                          "checked_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (row["ck_clip_id"], verdict.verdict, verdict.song, verdict.evidence,
                           verdict.model or self.settings.model, db._now()))
        self.conn.commit()
        if verdict.verdict == "none":
            return verdict
        note = note_for(verdict)
        if verdict.flagged and row["status"] in ("pending_review", "spoiler_hold"):
            db.set_clip_status(self.conn, row["ck_clip_id"], "needs_fix", note=note)
            log.info("Clip %s held back: %s", row["ck_clip_id"], note)
        else:
            db.add_clip_note(self.conn, row["ck_clip_id"], note)
            log.info("Clip %s: %s", row["ck_clip_id"], note)
        return verdict

    def run_once(self, limit: int | None = None) -> list[tuple[int, MusicVerdict]]:
        if not self.settings.enabled:
            return []
        done = []
        for row in self.unchecked()[: limit or self.settings.per_run]:
            try:
                done.append((row["ck_clip_id"], self.check_clip(row)))
            except ClipsKittyError as exc:
                log.debug("No transcript for clip %s yet: %s", row["ck_clip_id"], exc)
            except MusicCheckError as exc:
                log.warning("Couldn't check clip %s for music: %s", row["ck_clip_id"], exc)
                if "rejected" in str(exc) or "rate limit" in str(exc):
                    break
        return done


def music_of(conn: sqlite3.Connection, clip_id: int) -> dict | None:
    """The check's verdict for the review page (None: not checked, or no music)."""
    r = conn.execute("SELECT * FROM music_checks WHERE ck_clip_id = ?", (int(clip_id),)).fetchone()
    if r is None or r["verdict"] == "none":
        return None
    return {"verdict": r["verdict"], "song": r["song"], "evidence": r["evidence"]}
