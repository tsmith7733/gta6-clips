"""Our own SQLite state. We never write to Clips Kitty's database: only its API.

Clip statuses: pending_review | spoiler_hold -> approved | rejected -> published. A clip the quality
check finds a problem in waits as needs_fix until it's re-rendered, sent to review anyway, or rejected.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CLIP_STATUSES = ("pending_review", "spoiler_hold", "needs_fix", "approved", "rejected", "published")

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

-- Phase 5: one row per upload we asked Clips Kitty for.
CREATE TABLE IF NOT EXISTS publish_log (
    id              INTEGER PRIMARY KEY,
    ck_clip_id      INTEGER NOT NULL,
    publish_job_id  INTEGER,
    title           TEXT NOT NULL,
    publish_at      TEXT NOT NULL,              -- RFC 3339 with offset, as sent
    state           TEXT NOT NULL,              -- submitted | uploaded | locked_private | rejected | failed | interrupted
    youtube_id      TEXT NOT NULL DEFAULT '',
    privacy         TEXT NOT NULL DEFAULT '',   -- what we asked for (always private)
    actual_privacy  TEXT NOT NULL DEFAULT '',   -- what YouTube set
    error           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS publish_log_clip ON publish_log(ck_clip_id);

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

-- Phase 3: the recorded 1-minute pieces of a live stream.
CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY,
    stream_id  INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    start_utc  REAL NOT NULL,
    end_utc    REAL NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0     -- 1 once the janitor removed the file
);
CREATE INDEX IF NOT EXISTS segments_stream_time ON segments(stream_id, start_utc);

-- Compilations: every clip is checked once for a driving fail (by Claude); the fails are joined
-- into one Short, which goes through Clips Kitty and review like any other clip.
CREATE TABLE IF NOT EXISTS fail_checks (
    ck_clip_id     INTEGER PRIMARY KEY,
    streamer       TEXT NOT NULL,
    window_id      INTEGER REFERENCES windows(id),
    is_fail        INTEGER NOT NULL,
    start_s        REAL,                  -- the fail, in seconds of the window's file
    end_s          REAL,
    strength       INTEGER NOT NULL DEFAULT 0,
    kind           TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    compilation_id INTEGER,               -- the compilation it went into
    excluded       INTEGER NOT NULL DEFAULT 0,   -- the owner dropped it
    checked_at     TEXT NOT NULL
);

-- Driving fails found in finished streams (crash_hunt.py), by where chat reacted (peak_s).
CREATE TABLE IF NOT EXISTS vod_fails (
    id             INTEGER PRIMARY KEY,
    streamer       TEXT NOT NULL,
    video_id       TEXT NOT NULL,
    url            TEXT NOT NULL,
    peak_s         REAL NOT NULL,
    is_fail        INTEGER NOT NULL,
    start_s        REAL,                  -- the fail, in seconds of the stream
    end_s          REAL,
    strength       INTEGER NOT NULL DEFAULT 0,
    kind           TEXT NOT NULL DEFAULT '',
    note           TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    hd_path        TEXT NOT NULL DEFAULT '',  -- its HD cut, once downloaded
    compilation_id INTEGER,
    excluded       INTEGER NOT NULL DEFAULT 0,
    checked_at     TEXT NOT NULL,
    UNIQUE(video_id, peak_s)
);

CREATE TABLE IF NOT EXISTS compilations (
    id          INTEGER PRIMARY KEY,
    streamer    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    window_id   INTEGER REFERENCES windows(id),
    path        TEXT NOT NULL,
    parts       TEXT NOT NULL DEFAULT '[]',   -- JSON: [{clip, start_s, end_s, at_s, kind, note, strength}]
    state       TEXT NOT NULL DEFAULT 'queued',   -- queued | dropped
    created_at  TEXT NOT NULL
);

-- Small cached values (the Twitch GTA 6 game id).
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
        "finder": "TEXT NOT NULL DEFAULT 'gta_plugin'",   # gta_plugin | clipskitty (the fallback)
        "render_retries": "INTEGER NOT NULL DEFAULT 0",     # times sent again after Clips Kitty's render failed
        "not_before": "REAL",                               # don't send before this (UTC seconds)
        "focus": "TEXT NOT NULL DEFAULT ''",                # this window's own focus text (Twitch clips)
        "moment_start_s": "REAL",                           # the clip we ask for, in seconds of the cut file
        "moment_end_s": "REAL",                             # (sent to the exact-moments plugin)
        "widened": "INTEGER NOT NULL DEFAULT 0",            # times the check sent it back wider
    },
    "publish_log": {
        "channel": "TEXT NOT NULL DEFAULT ''",   # '' = Clips Kitty's channel, else a publish.channels key
    },
    "fail_checks": {
        "impact_s": "REAL",          # the crash itself, in seconds of the window's file
    },
    "vod_fails": {
        "impact_s": "REAL",          # the crash itself, in seconds of the stream
        "hd_start_s": "REAL",        # where hd_path starts, in seconds of the stream
    },
    "clips": {
        "review_from": "TEXT NOT NULL DEFAULT ''",   # pending_review | spoiler_hold before the owner decided
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path, *, threads: bool = False) -> sqlite3.Connection:
    """threads=True: the connection may be used from several threads (the caller serialises access)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # The watcher writes all the time while `scan`, `clips` and the review page read: WAL lets
    # readers and the writer work at once, and the timeout waits out a brief lock instead of failing.
    conn = sqlite3.connect(path, timeout=30, check_same_thread=not threads)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
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
    allowed = {"ck_job_id", "ck_video_id", "state", "error", "start_utc", "end_utc", "local_path", "finder",
               "render_retries", "not_before", "focus", "moment_start_s", "moment_end_s", "widened"}
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


# -- Phase 4: review decisions ---------------------------------------------------------

# Which status the owner may move a clip to, from which. Publishing (Phase 5) is the only way
# to `published`, and nothing moves a clip out of `published`.
REVIEW_MOVES = {
    "approved": ("pending_review", "spoiler_hold"),
    "rejected": ("pending_review", "spoiler_hold", "approved", "needs_fix"),
    "pending_review": ("rejected", "approved", "needs_fix"),   # undo, or "send to review anyway"
    "spoiler_hold": ("rejected", "approved", "needs_fix"),      # (a spoiler goes back to the Spoilers tab)
    "needs_fix": ("pending_review", "spoiler_hold"),            # the quality check found a problem
}


class BadMove(ValueError):
    pass


def get_clip(conn: sqlite3.Connection, ck_clip_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM clips WHERE ck_clip_id = ?", (int(ck_clip_id),)).fetchone()


def set_clip_status(conn: sqlite3.Connection, ck_clip_id: int, status: str, *, note: str = "",
                    title: str | None = None) -> sqlite3.Row:
    """The owner's decision on a clip. Refuses moves REVIEW_MOVES doesn't allow."""
    row = get_clip(conn, ck_clip_id)
    if row is None:
        raise BadMove(f"we don't track clip {ck_clip_id}")
    if row["status"] not in REVIEW_MOVES.get(status, ()):
        raise BadMove(f"clip {ck_clip_id} is {row['status']}; it can't be moved to {status}")
    notes = json.loads(row["notes"] or "[]")
    if note:
        notes.append(f"{_now()}: {note}")
    review_from = row["status"] if row["status"] in ("pending_review", "spoiler_hold") else row["review_from"]
    conn.execute("UPDATE clips SET status = ?, notes = ?, title = COALESCE(?, title), review_from = ?, "
                 "updated_at = ? WHERE ck_clip_id = ?",
                 (status, json.dumps(notes), title, review_from, _now(), int(ck_clip_id)))
    conn.commit()
    return get_clip(conn, ck_clip_id)


def set_clip_title(conn: sqlite3.Connection, ck_clip_id: int, title: str) -> None:
    conn.execute("UPDATE clips SET title = ?, updated_at = ? WHERE ck_clip_id = ?", (title, _now(), int(ck_clip_id)))
    conn.commit()


def follow_replaced_clip(conn: sqlite3.Connection, old_id: int, new_id: int, note: str) -> None:
    """Clips Kitty rendered a clip again under a new id: move our row (and its decision) to it."""
    row = get_clip(conn, old_id)
    notes = json.loads(row["notes"] or "[]") + [f"{_now()}: {note}"]
    conn.execute("UPDATE clips SET ck_clip_id = ?, notes = ?, updated_at = ? WHERE ck_clip_id = ?",
                 (int(new_id), json.dumps(notes), _now(), int(old_id)))
    conn.execute("UPDATE publish_log SET ck_clip_id = ? WHERE ck_clip_id = ?", (int(new_id), int(old_id)))
    conn.commit()


# -- Phase 5: publishing ------------------------------------------------------------------

LIVE_PUBLISH_STATES = ("submitted", "uploaded", "locked_private")   # occupy a slot / block a repeat


def add_publish(conn: sqlite3.Connection, *, ck_clip_id: int, publish_job_id: int | None, title: str,
                publish_at: str, channel: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO publish_log (ck_clip_id, publish_job_id, title, publish_at, state, privacy, channel, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'submitted', 'private', ?, ?, ?)",
        (int(ck_clip_id), publish_job_id, title, publish_at, channel, _now(), _now()))
    conn.commit()
    return int(cur.lastrowid)


def update_publish(conn: sqlite3.Connection, log_id: int, **fields) -> None:
    allowed = {"state", "youtube_id", "actual_privacy", "error", "ck_clip_id"}
    if set(fields) - allowed:
        raise ValueError(f"unknown publish fields: {set(fields) - allowed}")
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE publish_log SET {sets} WHERE id = ?", (*fields.values(), log_id))
    conn.commit()


def publishes(conn: sqlite3.Connection, states: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
    if states:
        marks = ",".join("?" * len(states))
        return conn.execute(f"SELECT * FROM publish_log WHERE state IN ({marks}) ORDER BY publish_at",
                            states).fetchall()
    return conn.execute("SELECT * FROM publish_log ORDER BY publish_at").fetchall()


def latest_publish(conn: sqlite3.Connection, ck_clip_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM publish_log WHERE ck_clip_id = ? ORDER BY id DESC LIMIT 1",
                        (int(ck_clip_id),)).fetchone()


def mark_published(conn: sqlite3.Connection, ck_clip_id: int, note: str) -> None:
    """The only way to `published`: Clips Kitty reported the upload on YouTube."""
    row = get_clip(conn, ck_clip_id)
    notes = json.loads(row["notes"] or "[]") + [f"{_now()}: {note}"]
    conn.execute("UPDATE clips SET status = 'published', notes = ?, updated_at = ? WHERE ck_clip_id = ? "
                 "AND status = 'approved'", (json.dumps(notes), _now(), int(ck_clip_id)))
    conn.commit()


def add_clip_note(conn: sqlite3.Connection, ck_clip_id: int, note: str) -> None:
    row = get_clip(conn, ck_clip_id)
    if row is None:
        return
    notes = json.loads(row["notes"] or "[]") + [f"{_now()}: {note}"]
    conn.execute("UPDATE clips SET notes = ?, updated_at = ? WHERE ck_clip_id = ?",
                 (json.dumps(notes), _now(), int(ck_clip_id)))
    conn.commit()


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


# -- Phase 3: live sessions ------------------------------------------------------------

def add_chat(conn: sqlite3.Connection, stream_id: int, start_utc: float,
             messages: list[tuple[float, str]]) -> None:
    """Appends live chat; `messages` are (offset_s from start_utc, text)."""
    conn.executemany(
        "INSERT INTO chat_messages (stream_id, offset_s, t_utc, text) VALUES (?, ?, ?, ?)",
        [(stream_id, off, start_utc + off, text) for off, text in messages],
    )
    conn.commit()


def set_signal_values(conn: sqlite3.Connection, stream_id: int, kind: str,
                      values: list[tuple[float, float]]) -> None:
    """Adds or replaces (offset_s, value) bins of one signal."""
    conn.executemany("INSERT OR REPLACE INTO signals (stream_id, kind, offset_s, value) VALUES (?, ?, ?, ?)",
                     [(stream_id, kind, off, v) for off, v in values])
    conn.commit()


def add_segment(conn: sqlite3.Connection, stream_id: int, path: str, start_utc: float, end_utc: float) -> int:
    cur = conn.execute("INSERT INTO segments (stream_id, path, start_utc, end_utc) VALUES (?, ?, ?, ?)",
                       (stream_id, path, start_utc, end_utc))
    conn.commit()
    return int(cur.lastrowid)


def segments_between(conn: sqlite3.Connection, stream_id: int, start_utc: float,
                     end_utc: float) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM segments WHERE stream_id = ? AND deleted = 0 AND end_utc > ? AND start_utc < ?"
        " ORDER BY start_utc", (stream_id, start_utc, end_utc)).fetchall()


def old_segments(conn: sqlite3.Connection, before_utc: float) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM segments WHERE deleted = 0 AND end_utc < ? ORDER BY start_utc",
                        (before_utc,)).fetchall()


def mark_segment_deleted(conn: sqlite3.Connection, segment_id: int) -> None:
    conn.execute("UPDATE segments SET deleted = 1 WHERE id = ?", (segment_id,))
    conn.commit()


def live_windows(conn: sqlite3.Connection, states: tuple[str, ...]) -> list[sqlite3.Row]:
    """Windows the watcher's dispatcher sends: cut from live recordings (stream video_id '<id>@<epoch>')
    or from Twitch's top clips (stream video_id '<login>@clips')."""
    marks = ",".join("?" * len(states))
    return conn.execute(
        f"SELECT w.*, s.title AS stream_title, s.video_id AS stream_video_id FROM windows w"
        f" JOIN streams s ON s.id = w.stream_id"
        f" WHERE s.video_id LIKE '%@%' AND w.state IN ({marks}) ORDER BY w.score DESC, w.id",
        states).fetchall()


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
                 " value = excluded.value, updated_at = excluded.updated_at", (key, value, _now()))
    conn.commit()


def windows_for_source(conn: sqlite3.Connection, source_url: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM windows WHERE source_url = ?", (source_url,)).fetchall()
