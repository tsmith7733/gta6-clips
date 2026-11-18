"""Our own SQLite state. We never write to Clips Kitty's database: only its API.

Clip statuses: pending_review | spoiler_hold -> approved | rejected -> published.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CLIP_STATUSES = ("pending_review", "spoiler_hold", "approved", "rejected", "published")

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    id            INTEGER PRIMARY KEY,
    streamer      TEXT NOT NULL,
    source_url    TEXT NOT NULL,          -- VOD URL, or local window file
    source_title  TEXT NOT NULL DEFAULT '',
    start_utc     REAL,                   -- wall-clock UTC seconds, when known
    end_utc       REAL,
    ck_job_id     INTEGER,
    ck_video_id   TEXT,
    state         TEXT NOT NULL DEFAULT 'submitted',  -- submitted | done | failed | collected
    error         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS windows_video ON windows(ck_video_id);

CREATE TABLE IF NOT EXISTS clips (
    ck_clip_id     INTEGER PRIMARY KEY,   -- Clips Kitty's clip id
    window_id      INTEGER REFERENCES windows(id),
    ck_video_id    TEXT NOT NULL,
    streamer       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending_review',
    score          INTEGER,
    start_s        REAL,
    end_s          REAL,
    title          TEXT NOT NULL DEFAULT '',
    title_options  TEXT NOT NULL DEFAULT '[]',   -- JSON list of 3
    hook           TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    hashtags       TEXT NOT NULL DEFAULT '[]',   -- JSON list
    notes          TEXT NOT NULL DEFAULT '[]',   -- JSON list of review warnings
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def add_window(conn: sqlite3.Connection, *, streamer: str, source_url: str, source_title: str = "",
               ck_job_id: int | None, ck_video_id: str | None, state: str = "submitted") -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO windows (streamer, source_url, source_title, ck_job_id, ck_video_id, state,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (streamer, source_url, source_title, ck_job_id, ck_video_id, state, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_window(conn: sqlite3.Connection, window_id: int, **fields) -> None:
    allowed = {"ck_job_id", "ck_video_id", "state", "error", "start_utc", "end_utc"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown window fields: {bad}")
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE windows SET {sets} WHERE id = ?", (*fields.values(), window_id))
    conn.commit()


def get_window(conn: sqlite3.Connection, window_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM windows WHERE id = ?", (window_id,)).fetchone()


def latest_window_for_video(conn: sqlite3.Connection, ck_video_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM windows WHERE ck_video_id = ? ORDER BY id DESC LIMIT 1", (ck_video_id,)
    ).fetchone()


def upsert_clip(conn: sqlite3.Connection, *, ck_clip_id: int, window_id: int | None, ck_video_id: str,
                streamer: str, status: str, score: int | None, start_s: float | None, end_s: float | None,
                title: str, title_options: list[str], hook: str, description: str,
                hashtags: list[str], notes: list[str]) -> None:
    """Insert, or refresh the metadata of a clip we already track.

    A clip the owner already decided on (approved / rejected / published) keeps its status.
    """
    if status not in CLIP_STATUSES:
        raise ValueError(f"unknown clip status {status!r}")
    now = _now()
    conn.execute(
        """
        INSERT INTO clips (ck_clip_id, window_id, ck_video_id, streamer, status, score, start_s, end_s,
                           title, title_options, hook, description, hashtags, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ck_clip_id) DO UPDATE SET
            score = excluded.score, start_s = excluded.start_s, end_s = excluded.end_s,
            title = excluded.title, title_options = excluded.title_options, hook = excluded.hook,
            description = excluded.description, hashtags = excluded.hashtags, notes = excluded.notes,
            status = CASE WHEN clips.status IN ('approved', 'rejected', 'published')
                          THEN clips.status ELSE excluded.status END,
            updated_at = excluded.updated_at
        """,
        (ck_clip_id, window_id, ck_video_id, streamer, status, score, start_s, end_s, title,
         json.dumps(title_options), hook, description, json.dumps(hashtags), json.dumps(notes), now, now),
    )
    conn.commit()


def clips_by_status(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return conn.execute("SELECT * FROM clips WHERE status = ? ORDER BY score DESC", (status,)).fetchall()
    return conn.execute("SELECT * FROM clips ORDER BY created_at DESC, score DESC").fetchall()
