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
    state         TEXT NOT NULL DEFAULT 'submitted',  -- planned | submitted | done | failed | collected
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

-- Phase 2: one row per recorded stream (a VOD now, a live recording later).
CREATE TABLE IF NOT EXISTS streams (
    id           INTEGER PRIMARY KEY,
    platform     TEXT NOT NULL,
    video_id     TEXT NOT NULL,
    streamer     TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    start_utc    REAL,                      -- when the stream started, wall-clock UTC seconds
    duration_s   REAL,
    chat_status  TEXT NOT NULL DEFAULT '',  -- '' (not fetched) | ok | none | unsupported
    audio_status TEXT NOT NULL DEFAULT '',  -- '' (not measured) | ok
    created_at   TEXT NOT NULL,
    UNIQUE(platform, video_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    stream_id  INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    offset_s   REAL NOT NULL,               -- seconds from the stream's start
    t_utc      REAL,                        -- start_utc + offset_s, when start_utc is known
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_stream_time ON chat_messages(stream_id, offset_s);

-- Per-bin measurements: kind = 'loudness' (LUFS) or 'score' (combined spike score).
CREATE TABLE IF NOT EXISTS signals (
    stream_id  INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    offset_s   REAL NOT NULL,               -- start of the bin
    value      REAL NOT NULL,
    PRIMARY KEY (stream_id, kind, offset_s)
);
"""

# Columns added to tables that already exist in an older orchestrator.db.
MIGRATIONS = {
    "windows": {
        "stream_id": "INTEGER REFERENCES streams(id)",
        "offset_start_s": "REAL",
        "offset_end_s": "REAL",
        "score": "REAL",
        "reasons": "TEXT NOT NULL DEFAULT ''",
        "local_path": "TEXT NOT NULL DEFAULT ''",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def add_window(conn: sqlite3.Connection, *, streamer: str, source_url: str, source_title: str = "",
               ck_job_id: int | None, ck_video_id: str | None, state: str = "submitted",
               stream_id: int | None = None, offset_start_s: float | None = None,
               offset_end_s: float | None = None, start_utc: float | None = None,
               end_utc: float | None = None, score: float | None = None, reasons: str = "") -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO windows (streamer, source_url, source_title, ck_job_id, ck_video_id, state,"
        " stream_id, offset_start_s, offset_end_s, start_utc, end_utc, score, reasons,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (streamer, source_url, source_title, ck_job_id, ck_video_id, state, stream_id, offset_start_s,
         offset_end_s, start_utc, end_utc, score, reasons, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_window(conn: sqlite3.Connection, window_id: int, **fields) -> None:
    allowed = {"ck_job_id", "ck_video_id", "state", "error", "start_utc", "end_utc", "local_path"}
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


# -- Phase 2: streams, chat and signals ---------------------------------------------

def upsert_stream(conn: sqlite3.Connection, *, platform: str, video_id: str, streamer: str, url: str,
                  title: str, start_utc: float | None, duration_s: float | None) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO streams (platform, video_id, streamer, url, title, start_utc, duration_s, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(platform, video_id) DO UPDATE SET
            title = excluded.title, url = excluded.url,
            start_utc = COALESCE(excluded.start_utc, streams.start_utc),
            duration_s = COALESCE(excluded.duration_s, streams.duration_s)
        """,
        (platform, video_id, streamer, url, title, start_utc, duration_s, _now()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM streams WHERE platform = ? AND video_id = ?",
                        (platform, video_id)).fetchone()


def set_stream_status(conn: sqlite3.Connection, stream_id: int, *, chat: str | None = None,
                      audio: str | None = None) -> None:
    if chat is not None:
        conn.execute("UPDATE streams SET chat_status = ? WHERE id = ?", (chat, stream_id))
    if audio is not None:
        conn.execute("UPDATE streams SET audio_status = ? WHERE id = ?", (audio, stream_id))
    conn.commit()


def replace_chat(conn: sqlite3.Connection, stream_id: int, start_utc: float | None,
                 messages: list[tuple[float, str]]) -> None:
    conn.execute("DELETE FROM chat_messages WHERE stream_id = ?", (stream_id,))
    conn.executemany(
        "INSERT INTO chat_messages (stream_id, offset_s, t_utc, text) VALUES (?, ?, ?, ?)",
        [(stream_id, off, None if start_utc is None else start_utc + off, text) for off, text in messages],
    )
    conn.commit()


def chat_for_stream(conn: sqlite3.Connection, stream_id: int, start_s: float | None = None,
                    end_s: float | None = None) -> list[tuple[float, str]]:
    sql, args = "SELECT offset_s, text FROM chat_messages WHERE stream_id = ?", [stream_id]
    if start_s is not None:
        sql, args = sql + " AND offset_s >= ?", [*args, start_s]
    if end_s is not None:
        sql, args = sql + " AND offset_s < ?", [*args, end_s]
    return [(r["offset_s"], r["text"]) for r in conn.execute(sql + " ORDER BY offset_s", args)]


def replace_signal(conn: sqlite3.Connection, stream_id: int, kind: str, bin_s: float,
                   values: list[float]) -> None:
    conn.execute("DELETE FROM signals WHERE stream_id = ? AND kind = ?", (stream_id, kind))
    conn.executemany("INSERT INTO signals (stream_id, kind, offset_s, value) VALUES (?, ?, ?, ?)",
                     [(stream_id, kind, i * bin_s, v) for i, v in enumerate(values)])
    conn.commit()


def signal_for_stream(conn: sqlite3.Connection, stream_id: int, kind: str) -> list[tuple[float, float]]:
    return [(r["offset_s"], r["value"]) for r in conn.execute(
        "SELECT offset_s, value FROM signals WHERE stream_id = ? AND kind = ? ORDER BY offset_s",
        (stream_id, kind))]


def windows_for_stream(conn: sqlite3.Connection, stream_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM windows WHERE stream_id = ? ORDER BY offset_start_s",
                        (stream_id,)).fetchall()
