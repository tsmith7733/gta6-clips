"""What the review page does, without the web part: list clips, approve, reject, retitle.

The decisions live in our database (clips.status). Titles are also written to Clips Kitty with
PATCH /clips/{id}, because that's where publishing (Phase 5) reads them from. Approving re-checks
that the clip still exists in Clips Kitty and that its description still carries the credit.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import db
from ..clipskitty import ClipsKittyClient, ClipsKittyError
from ..postprocess import has_credit
from ..streamers import PermissionDenied, Streamer
from ..vod import _streamer_named

log = logging.getLogger(__name__)

TABS = ("pending_review", "needs_fix", "spoiler_hold", "approved", "rejected", "published")
MAX_TITLE_CHARS = 100          # YouTube's limit
RECONCILE_EVERY_S = 20.0


class ReviewError(Exception):
    """The owner asked for something we won't do; the message says why."""


@dataclass
class Card:
    id: int
    streamer: str
    status: str
    score: int | None
    seconds: float
    title: str
    title_options: list[str]
    hook: str
    description: str
    hashtags: list[str]
    notes: list[str]
    source_title: str
    created_at: str
    edit: dict | None = None        # the Claude editor's latest edit of it (state, seconds, notes, ...)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def check_title(title: str) -> str:
    title = " ".join((title or "").split())
    if not title:
        raise ReviewError("The title is empty.")
    if len(title) > MAX_TITLE_CHARS:
        raise ReviewError(f"The title is {len(title)} characters; YouTube allows {MAX_TITLE_CHARS}.")
    if "<" in title or ">" in title:
        raise ReviewError("YouTube doesn't allow < or > in titles.")
    return title


class ReviewService:
    def __init__(self, *, conn: sqlite3.Connection, client: ClipsKittyClient, streamers: list[Streamer],
                 clock: Callable[[], float] = time.monotonic):
        self.conn = conn
        self.client = client
        self.streamers = streamers
        self.clock = clock
        self._reconciled_at: float | None = None

    # -- reading ---------------------------------------------------------------------
    def channels(self) -> list[str]:
        """The YouTube channel keys clips can go to: '' (Clips Kitty's channel) first, then streamers' own."""
        keys = {s.youtube_channel for s in self.streamers}
        return [""] + sorted(k for k in keys if k)

    def _streamers_on(self, channel: str | None) -> list[str] | None:
        """Names of the streamers whose clips go to `channel` (None: every channel)."""
        if channel is None:
            return None
        return [s.name for s in self.streamers if s.youtube_channel == channel]

    def counts(self, channel: str | None = None) -> dict[str, int]:
        names = self._streamers_on(channel)
        if names is None:
            found = dict(self.conn.execute("SELECT status, COUNT(*) FROM clips GROUP BY status").fetchall())
        else:
            marks = ",".join("?" * len(names)) or "NULL"
            found = dict(self.conn.execute(f"SELECT status, COUNT(*) FROM clips WHERE streamer IN ({marks}) "
                                           "GROUP BY status", names).fetchall())
        return {tab: int(found.get(tab, 0)) for tab in TABS}

    def cards(self, status: str, channel: str | None = None) -> list[Card]:
        if status not in TABS:
            raise ReviewError(f"unknown tab {status!r}")
        names = self._streamers_on(channel)
        where, args = "c.status = ?", [status]
        if names is not None:
            where += f" AND c.streamer IN ({','.join('?' * len(names)) or 'NULL'})"
            args += names
        rows = self.conn.execute(
            "SELECT c.*, COALESCE(w.source_title, '') AS source_title FROM clips c "
            f"LEFT JOIN windows w ON w.id = c.window_id WHERE {where} "
            "ORDER BY c.score DESC, c.created_at DESC", args).fetchall()
        return [self._with_edit(self._card(r)) for r in rows]

    def card(self, clip_id: int) -> Card:
        row = self.conn.execute(
            "SELECT c.*, COALESCE(w.source_title, '') AS source_title FROM clips c "
            "LEFT JOIN windows w ON w.id = c.window_id WHERE c.ck_clip_id = ?", (int(clip_id),)).fetchone()
        if row is None:
            raise ReviewError(f"We don't track clip {clip_id}.")
        return self._with_edit(self._card(row))

    def _with_edit(self, card: Card) -> Card:
        e = self.conn.execute("SELECT * FROM edits WHERE ck_clip_id = ? AND state != 'replaced' "
                              "ORDER BY id DESC LIMIT 1", (card.id,)).fetchone()
        if e is not None:
            card.edit = {"id": e["id"], "state": e["state"], "seconds": e["seconds"],
                         "notes": json.loads(e["notes"] or "[]"), "request": e["request"], "error": e["error"],
                         "cost_usd": round(e["cost_usd"] or 0, 2)}
        return card

    # -- the Claude editor -------------------------------------------------------------
    def edit_file(self, edit_id: int) -> Path:
        e = self.conn.execute("SELECT * FROM edits WHERE id = ?", (int(edit_id),)).fetchone()
        if e is None or not e["path"]:
            raise ReviewError(f"There's no edited video {edit_id}.")
        self.card(e["ck_clip_id"])     # only clips we track
        path = Path(e["path"])
        if not path.is_file():
            raise ReviewError(f"The edited video {path.name} isn't on disk any more.")
        return path

    def note_for_editor(self, clip_id: int, text: str) -> str:
        """Keeps the owner's suggestion: it goes into every later edit plan."""
        card = self.card(clip_id)
        text = " ".join((text or "").split())[:500]
        if text:
            self.conn.execute("INSERT INTO editor_feedback (ck_clip_id, edit_id, text, created_at) VALUES (?, ?, ?, ?)",
                              (clip_id, (card.edit or {}).get("id"), text, db._now()))
            self.conn.commit()
        return text

    def drop_edit(self, clip_id: int) -> Card:
        """The owner prefers the original: the edit stays on disk but won't be used."""
        card = self.card(clip_id)
        if not card.edit or card.edit["state"] != "ready":
            raise ReviewError("This clip has no finished edit.")
        self.conn.execute("UPDATE edits SET state = 'dropped' WHERE id = ?", (card.edit["id"],))
        self.conn.commit()
        return self.card(clip_id)

    @staticmethod
    def _card(r: sqlite3.Row) -> Card:
        return Card(id=r["ck_clip_id"], streamer=r["streamer"], status=r["status"], score=r["score"],
                    seconds=round((r["end_s"] or 0) - (r["start_s"] or 0), 1), title=r["title"],
                    title_options=json.loads(r["title_options"] or "[]"), hook=r["hook"],
                    description=r["description"], hashtags=json.loads(r["hashtags"] or "[]"),
                    notes=json.loads(r["notes"] or "[]"), source_title=r["source_title"],
                    created_at=r["created_at"])

    def captions(self, clip_id: int) -> list[dict]:
        self.card(clip_id)   # only clips we track
        return self.client.captions(clip_id)

    # -- following re-rendered clips -------------------------------------------------
    def reconcile(self, *, force: bool = False) -> list[tuple[int, int]]:
        """Clips Kitty gives a clip a new id when it's rendered again (an edit in its studio).

        For each clip we track that has disappeared from its video, follow it when exactly one
        clip we don't track overlaps its old time range. Returns the (old, new) moves.
        """
        now = self.clock()
        if not force and self._reconciled_at is not None and now - self._reconciled_at < RECONCILE_EVERY_S:
            return []
        self._reconciled_at = now
        rows = self.conn.execute("SELECT * FROM clips WHERE status != 'published'").fetchall()
        tracked = {r["ck_clip_id"] for r in self.conn.execute("SELECT ck_clip_id FROM clips")}
        moves: list[tuple[int, int]] = []
        try:
            known_videos = {v.get("video_id") for v in self.client.videos()}
        except ClipsKittyError as exc:
            log.warning("Couldn't check for re-rendered clips: %s", exc)
            return []
        by_video: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_video.setdefault(r["ck_video_id"], []).append(r)
        for video_id, ours in by_video.items():
            if video_id not in known_videos:
                continue   # an unknown video lists no clips: that's not "they were all replaced"
            theirs = self.client.video_clips(video_id)
            present = {c["id"] for c in theirs}
            for r in ours:
                if r["ck_clip_id"] in present:
                    continue
                candidates = [c for c in theirs if c["id"] not in tracked
                              and _overlap(c, r["start_s"], r["end_s"]) > 0.5]
                if len(candidates) != 1:
                    continue
                new = candidates[0]["id"]
                db.follow_replaced_clip(self.conn, r["ck_clip_id"], new,
                                        f"re-rendered in Clips Kitty; clip {r['ck_clip_id']} became {new}")
                tracked.add(new)
                moves.append((r["ck_clip_id"], new))
                log.info("Clip %s was rendered again as clip %s; following it.", r["ck_clip_id"], new)
        return moves

    # -- decisions -------------------------------------------------------------------
    def set_title(self, clip_id: int, title: str) -> Card:
        card = self.card(clip_id)
        if card.status == "published":
            raise ReviewError("This clip is already published; change its title on YouTube.")
        title = check_title(title)
        self._streamer(card)
        self.client.patch_clip(clip_id, title=title)
        db.set_clip_title(self.conn, clip_id, title)
        return self.card(clip_id)

    def approve(self, clip_id: int, title: str, *, confirm_spoiler: bool = False) -> Card:
        card = self.card(clip_id)
        if card.status == "spoiler_hold" and not confirm_spoiler:
            raise ReviewError("This clip is held as a possible spoiler. Confirm that it isn't one to approve it.")
        if card.status not in ("pending_review", "spoiler_hold"):
            raise ReviewError(f"This clip is {card.status}; only clips waiting for review can be approved.")
        title = check_title(title)
        streamer = self._streamer(card)
        theirs = next((c for c in self.client.video_clips(self._video_id(clip_id)) if c["id"] == clip_id), None)
        if theirs is None:
            raise ReviewError("Clips Kitty doesn't have this clip any more (rendered again under a new id, or "
                              "deleted). Reload the page.")
        if not has_credit(theirs.get("description") or "", streamer):
            raise ReviewError(f"The description in Clips Kitty has lost {streamer.name}'s credit line. Put it back "
                              "(or run the clip through post-processing again) before approving.")
        if (theirs.get("title") or "") != title:
            self.client.patch_clip(clip_id, title=title)
        what = "approved" + (" (owner confirmed it isn't a spoiler)" if card.status == "spoiler_hold" else "")
        db.set_clip_status(self.conn, clip_id, "approved", note=what, title=title)
        return self.card(clip_id)

    def _not_uploading(self, clip_id: int) -> None:
        last = db.latest_publish(self.conn, clip_id)
        if last is not None and last["state"] in db.LIVE_PUBLISH_STATES:
            raise ReviewError("This clip has been sent to YouTube; change or delete it in YouTube Studio.")

    def reject(self, clip_id: int) -> Card:
        card = self.card(clip_id)
        self._not_uploading(clip_id)
        if card.status not in ("pending_review", "spoiler_hold", "approved", "needs_fix"):
            raise ReviewError(f"This clip is {card.status}; it can't be rejected.")
        db.set_clip_status(self.conn, clip_id, "rejected", note="rejected")
        return self.card(clip_id)

    def undo(self, clip_id: int) -> Card:
        """Approved or rejected → back to where it was reviewed from (a spoiler goes back to Spoilers)."""
        card = self.card(clip_id)
        self._not_uploading(clip_id)
        if card.status not in ("approved", "rejected"):
            raise ReviewError(f"This clip is {card.status}; there's no decision to undo.")
        row = db.get_clip(self.conn, clip_id)
        back = row["review_from"] or "pending_review"
        db.set_clip_status(self.conn, clip_id, back, note=f"decision undone; back to {back}")
        return self.card(clip_id)

    def send_to_review(self, clip_id: int) -> Card:
        """A clip the quality check held, judged fine by the owner: on to the review queue."""
        card = self.card(clip_id)
        if card.status != "needs_fix":
            raise ReviewError(f"This clip is {card.status}, not waiting for a fix.")
        back = db.get_clip(self.conn, clip_id)["review_from"] or "pending_review"
        db.set_clip_status(self.conn, clip_id, back, note="sent to review as it is")
        return self.card(clip_id)

    def rerender(self, clip_id: int, *, widen_s: float = 15.0) -> Card:
        """Sends the clip's recording to Clips Kitty again as a new video (after the owner drew the
        webcam box for this streamer in Clips Kitty, say). The running watcher sends it; the new clip
        goes through the quality check again, and this one is rejected as replaced."""
        card = self.card(clip_id)
        if card.status != "needs_fix":
            raise ReviewError(f"This clip is {card.status}; only clips waiting for a fix are re-rendered here.")
        self._streamer(card)                                        # the permission gate
        clip = db.get_clip(self.conn, clip_id)
        window = self.conn.execute("SELECT * FROM windows WHERE id = ?", (clip["window_id"],)).fetchone()             if clip["window_id"] else None
        src = Path(window["local_path"]) if window is not None and window["local_path"] else None
        stream = self.conn.execute("SELECT video_id FROM streams WHERE id = ?", (window["stream_id"],)).fetchone()             if window is not None and window["stream_id"] else None
        if stream is None or "@" not in stream["video_id"]:
            raise ReviewError("Only clips from live recordings and Twitch clips are sent again from here (the "
                              "watcher sends them). Fix this one in the Clips Kitty studio, or reject it.")
        if src is None or not src.is_file():
            raise ReviewError("This clip's recording isn't on this PC any more, so it can't be sent again. "
                              "Fix it in the Clips Kitty studio, or reject it.")
        file_s = float((window["offset_end_s"] or 0) - (window["offset_start_s"] or 0))
        start, end = window["moment_start_s"], window["moment_end_s"]
        if start is None or end is None:   # Clips Kitty picked this one: keep its moment, with some slack
            start = max(0.0, float(clip["start_s"] or 0) - widen_s)
            end = float(clip["end_s"] or 0) + widen_s
            if file_s:
                end = min(file_s, end)
        n = 1 + sum(1 for _ in src.parent.glob(f"{src.stem.split('_fix')[0]}_fix*{src.suffix}"))
        dest = src.with_name(f"{src.stem.split('_fix')[0]}_fix{n}{src.suffix}")   # a new video for Clips Kitty
        shutil.copy2(src, dest)
        db.update_window(self.conn, window["id"], state="planned", ck_job_id=None, local_path=str(dest),
                         finder="exact", moment_start_s=float(start), moment_end_s=float(end), not_before=None)
        db.set_clip_status(self.conn, clip_id, "rejected",
                           note="sent to Clips Kitty again; the new clip replaces this one")
        return self.card(clip_id)

    # -- helpers ---------------------------------------------------------------------
    def _streamer(self, card: Card) -> Streamer:
        try:
            return _streamer_named(self.streamers, card.streamer)   # the permission gate
        except PermissionDenied as exc:
            raise ReviewError(str(exc)) from exc

    def _video_id(self, clip_id: int) -> str:
        return db.get_clip(self.conn, clip_id)["ck_video_id"]


def _overlap(clip: dict, start_s: float | None, end_s: float | None) -> float:
    """How much of the old clip's time range the new clip covers (0-1)."""
    if start_s is None or end_s is None or end_s <= start_s:
        return 0.0
    shared = min(end_s, float(clip.get("end_s") or 0)) - max(start_s, float(clip.get("start_s") or 0))
    return max(0.0, shared) / (end_s - start_s)
