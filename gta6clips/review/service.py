"""What the review page does, without the web part: list clips, approve, reject, retitle.

The decisions live in our database (clips.status). Titles are also written to Clips Kitty with
PATCH /clips/{id}, because that's where publishing (Phase 5) reads them from. Approving re-checks
that the clip still exists in Clips Kitty and that its description still carries the credit.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

from .. import db
from ..clipskitty import ClipsKittyClient, ClipsKittyError
from ..postprocess import has_credit
from ..streamers import PermissionDenied, Streamer
from ..vod import _streamer_named

log = logging.getLogger(__name__)

TABS = ("pending_review", "spoiler_hold", "approved", "rejected", "published")
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
    def counts(self) -> dict[str, int]:
        found = dict(self.conn.execute("SELECT status, COUNT(*) FROM clips GROUP BY status").fetchall())
        return {tab: int(found.get(tab, 0)) for tab in TABS}

    def cards(self, status: str) -> list[Card]:
        if status not in TABS:
            raise ReviewError(f"unknown tab {status!r}")
        rows = self.conn.execute(
            "SELECT c.*, COALESCE(w.source_title, '') AS source_title FROM clips c "
            "LEFT JOIN windows w ON w.id = c.window_id WHERE c.status = ? "
            "ORDER BY c.score DESC, c.created_at DESC", (status,)).fetchall()
        return [self._card(r) for r in rows]

    def card(self, clip_id: int) -> Card:
        row = self.conn.execute(
            "SELECT c.*, COALESCE(w.source_title, '') AS source_title FROM clips c "
            "LEFT JOIN windows w ON w.id = c.window_id WHERE c.ck_clip_id = ?", (int(clip_id),)).fetchone()
        if row is None:
            raise ReviewError(f"We don't track clip {clip_id}.")
        return self._card(row)

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

    def reject(self, clip_id: int) -> Card:
        card = self.card(clip_id)
        if card.status not in ("pending_review", "spoiler_hold", "approved"):
            raise ReviewError(f"This clip is {card.status}; it can't be rejected.")
        db.set_clip_status(self.conn, clip_id, "rejected", note="rejected")
        return self.card(clip_id)

    def undo(self, clip_id: int) -> Card:
        """Approved or rejected → back to where it was reviewed from (a spoiler goes back to Spoilers)."""
        card = self.card(clip_id)
        if card.status not in ("approved", "rejected"):
            raise ReviewError(f"This clip is {card.status}; there's no decision to undo.")
        row = db.get_clip(self.conn, clip_id)
        back = row["review_from"] or "pending_review"
        db.set_clip_status(self.conn, clip_id, back, note=f"decision undone; back to {back}")
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
