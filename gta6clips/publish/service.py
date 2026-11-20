"""Phase 5: upload approved clips to YouTube through Clips Kitty, each scheduled for a slot.

Every upload is sent with privacy "private" and a publish_at, so YouTube keeps it private until
the slot and nothing can go public by accident. Before each upload this re-checks the permission
gate, that the clip is approved, and that its description in Clips Kitty still has the credit.
Afterwards Clips Kitty's report is compared with what we asked for; a locked_private upload
(an unaudited Google Cloud project) stops further scheduling, because those never go public.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import db, notify
from ..clipskitty import ClipsKittyClient, ClipsKittyError
from ..config import Config
from ..postprocess import has_credit
from ..review.service import ReviewError, check_title
from ..streamers import PermissionDenied, Streamer
from ..vod import _streamer_named
from .schedule import YOUTUBE_MIN_LEAD, SlotSettings, label, next_slots, parse_rfc3339, rfc3339
from .youtube_direct import DirectUploader, YouTubeError

log = logging.getLogger(__name__)

REFRESH_EVERY_S = 20.0
MAX_TAGS_CHARS = 500


@dataclass
class PublishStatus:
    enabled: bool = False
    connected: bool = False
    channel: str = ""
    used: int = 0
    limit: int = 100
    problems: list[str] = field(default_factory=list)    # scheduling refused while any
    warnings: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {**self.__dict__, "remaining": self.remaining, "ok": self.ok}


@dataclass
class PlanItem:
    clip_id: int
    title: str
    publish_at: str          # RFC 3339 with offset
    when: str                # for people
    channel: str = ""        # the YouTube channel it goes to, for people

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Plan:
    status: PublishStatus
    items: list[PlanItem] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)    # {clip_id, reason}

    def as_dict(self) -> dict:
        return {"status": self.status.as_dict(), "items": [i.as_dict() for i in self.items],
                "skipped": self.skipped}


class PublishService:
    def __init__(self, *, conn: sqlite3.Connection, client: ClipsKittyClient, streamers: list[Streamer],
                 cfg: Config, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 clock: Callable[[], float] = time.monotonic,
                 is_live: Callable[[Streamer], bool | None] | None = None,
                 uploaders: dict[str, DirectUploader] | None = None, work_dir: Path | None = None):
        self.conn = conn
        # Channels other than Clips Kitty's (config.yaml publish.channels), by key.
        self.uploaders = uploaders or {}
        self._clipskitty_channel = ""     # its title, from the last status()
        self.work_dir = work_dir or Path(tempfile.gettempdir())
        # For streamers marked hold_uploads_while_live: True live, False offline, None can't tell.
        self.is_live = is_live
        self.client = client
        self.streamers = streamers
        self.cfg = cfg
        self.slots = SlotSettings.from_config(cfg)
        self.now = now
        self.clock = clock
        self._refreshed_at: float | None = None

    # -- can we publish at all? --------------------------------------------------------
    def status(self) -> PublishStatus:
        st = PublishStatus()
        try:
            yt = self.client.youtube_status()
        except ClipsKittyError as exc:
            st.problems.append(f"Clips Kitty isn't reachable: {exc}")
            return st
        st.enabled = bool(yt.get("enabled"))
        if not st.enabled:
            st.problems.append("YouTube publishing is off in Clips Kitty. Turn on \"Publish to YouTube\" in its "
                               "Settings, add your Google Cloud OAuth client and connect your channel.")
            return st
        st.connected = bool(yt.get("connected"))
        channel = yt.get("channel") or {}
        st.channel = channel.get("title") or channel.get("handle") or ""
        self._clipskitty_channel = st.channel
        quota = yt.get("quota") or {}
        st.used = int(quota.get("uploads_used") or 0) + self._direct_uploads_today()
        st.limit = int(quota.get("uploads_limit") or 100)
        if not st.connected:
            st.problems.append("Clips Kitty isn't connected to a YouTube channel. Connect it in Settings.")
        if st.remaining <= 0:
            st.problems.append(f"Today's YouTube upload limit is used up ({st.used}/{st.limit}). It resets daily.")
        elif st.used >= int(self.cfg.get("publish.quota_warn_at", 80)):
            st.warnings.append(f"{st.used} of today's {st.limit} YouTube uploads are used.")
        locked = db.publishes(self.conn, ("locked_private",))
        if locked and self.cfg.get("publish.accept_locked_private", False) is not True:
            st.problems.append(
                f"YouTube locked {len(locked)} upload(s) to private: your Google Cloud project hasn't passed "
                "YouTube's API audit, so scheduled clips would never go public. Apply for the audit (Google "
                "Cloud console, YouTube API Services audit form) or upload by hand. Scheduling is stopped; "
                "set publish.accept_locked_private: true only if private-only uploads are what you want.")
        settings = yt.get("settings") or {}
        if str(settings.get("privacy", "")).lower() == "public":
            st.warnings.append("Clips Kitty's default upload privacy is Public. Uploads from here are always "
                               "private until their slot, but set the default to Private in its Settings.")
        return st

    # -- planning ------------------------------------------------------------------------
    def plan(self, clip_ids: list[int] | None = None) -> Plan:
        self.refresh(force=True)
        plan = Plan(status=self.status())
        if not plan.status.ok:
            return plan
        rows = self._approved(clip_ids)
        ready: list[sqlite3.Row] = []
        for row in rows:
            reason = self._why_not(row)
            if reason:
                plan.skipped.append({"clip_id": row["ck_clip_id"], "reason": reason})
            else:
                ready.append(row)
        if len(ready) > plan.status.remaining:
            for row in ready[plan.status.remaining:]:
                plan.skipped.append({"clip_id": row["ck_clip_id"], "reason": "over today's upload limit"})
            ready = ready[:plan.status.remaining]
        by_channel: dict[str, list[sqlite3.Row]] = {}
        for row in ready:
            by_channel.setdefault(self._channel_key(row), []).append(row)
        for key, rows in by_channel.items():
            slots = next_slots(self.slots, now=self.now(), taken=self._taken(key), count=len(rows))
            for row, slot in zip(rows, slots):
                plan.items.append(PlanItem(clip_id=row["ck_clip_id"], title=row["title"], publish_at=rfc3339(slot),
                                           when=label(slot, self.slots.tz), channel=self.channel_title(key)))
        plan.items.sort(key=lambda i: i.publish_at)
        return plan

    # -- which channel -------------------------------------------------------------------
    def _channel_key(self, row: sqlite3.Row) -> str:
        streamer = next((s for s in self.streamers if s.name == row["streamer"]), None)
        return streamer.youtube_channel if streamer is not None else ""

    def channel_title(self, key: str) -> str:
        if not key:
            if not self._clipskitty_channel:
                try:   # once; status() keeps it fresh after that
                    channel = (self.client.youtube_status().get("channel") or {})
                    self._clipskitty_channel = channel.get("title") or channel.get("handle") or ""
                except ClipsKittyError:
                    pass
            return self._clipskitty_channel or "the channel connected in Clips Kitty"
        up = self.uploaders.get(key)
        return up.settings.title if up is not None else key

    def channel_for_streamer(self, name: str) -> str:
        streamer = next((s for s in self.streamers if s.name == name), None)
        return self.channel_title(streamer.youtube_channel if streamer else "")

    def _direct_uploads_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return self.conn.execute("SELECT COUNT(*) FROM publish_log WHERE channel != '' AND created_at >= ?",
                                 (today,)).fetchone()[0]

    def _approved(self, clip_ids: list[int] | None) -> list[sqlite3.Row]:
        rows = db.clips_by_status(self.conn, "approved")
        if clip_ids is not None:
            wanted = {int(i) for i in clip_ids}
            rows = [r for r in rows if r["ck_clip_id"] in wanted]
        return [r for r in rows if not self._in_flight(r["ck_clip_id"])]

    def _in_flight(self, clip_id: int) -> bool:
        last = db.latest_publish(self.conn, clip_id)
        return last is not None and last["state"] in db.LIVE_PUBLISH_STATES

    def _taken(self, channel: str | None = None) -> list[datetime]:
        """Slots already used, on one channel (None: all)."""
        return [parse_rfc3339(r["publish_at"]) for r in db.publishes(self.conn, db.LIVE_PUBLISH_STATES)
                if r["publish_at"] and (channel is None or r["channel"] == channel)]

    def _why_not(self, row: sqlite3.Row) -> str:
        """Why this approved clip can't be uploaded now, or ''."""
        try:
            streamer = _streamer_named(self.streamers, row["streamer"])   # the permission gate
        except PermissionDenied as exc:
            return str(exc)
        key = streamer.youtube_channel
        if key:
            up = self.uploaders.get(key)
            if up is None:
                return f"{streamer.name}'s youtube_channel {key!r} isn't in config.yaml publish.channels"
            try:
                if not up.connected():
                    return (f"{up.settings.title} isn't connected yet: run "
                            f"'python -m gta6clips connect-youtube {key}'")
            except YouTubeError as exc:
                return str(exc)
        if streamer.hold_uploads_while_live:
            live = self.is_live(streamer) if self.is_live is not None else None
            if live is True:
                return f"{streamer.name} is live right now; it's uploaded once the stream ends"
            if live is None:
                return f"couldn't check whether {streamer.name} is live, so it waits (it's uploaded once they're "                        "seen offline)"
        try:
            check_title(row["title"])
        except ReviewError as exc:
            return str(exc)
        theirs = self._clips_kitty_clip(row)
        if theirs is None:
            return "Clips Kitty doesn't have this clip any more (re-rendered or deleted): reload the review page."
        if not has_credit(theirs.get("description") or "", streamer):
            return f"its description in Clips Kitty has lost {streamer.name}'s credit line"
        return ""

    def _clips_kitty_clip(self, row: sqlite3.Row) -> dict | None:
        return next((c for c in self.client.video_clips(row["ck_video_id"]) if c["id"] == row["ck_clip_id"]), None)

    def schedule_one(self, clip_id: int) -> dict:
        """Upload one just-approved clip into the next free slot (private until then).

        Returns {"scheduled": {...}} or {"not_scheduled": reason}; never raises for the
        ordinary reasons (YouTube off, quota, a check failing), so an approval always stands.
        """
        plan = self.plan([clip_id])
        if not plan.status.ok:
            return {"not_scheduled": " ".join(plan.status.problems)}
        if not plan.items:
            reason = next((s["reason"] for s in plan.skipped if s["clip_id"] == clip_id), "no free slot found")
            return {"not_scheduled": reason}
        result = self.execute([plan.items[0].as_dict()])
        if result["started"]:
            return {"scheduled": result["started"][0]}
        return {"not_scheduled": result["skipped"][0]["reason"] if result["skipped"] else "it wasn't uploaded"}

    # -- uploading -----------------------------------------------------------------------
    def execute(self, items: list[dict]) -> dict:
        """Upload each planned item. Everything is re-checked: the plan came from a browser."""
        status = self.status()
        if not status.ok:
            raise ReviewError(" ".join(status.problems))
        started, skipped = [], []
        budget = status.remaining
        taken = {(r["channel"], parse_rfc3339(r["publish_at"]).astimezone(timezone.utc))
                 for r in db.publishes(self.conn, db.LIVE_PUBLISH_STATES) if r["publish_at"]}
        for item in items:
            clip_id = int(item.get("clip_id", 0))
            try:
                when = parse_rfc3339(str(item.get("publish_at", "")))
            except ValueError as exc:
                skipped.append({"clip_id": clip_id, "reason": str(exc)})
                continue
            row = db.get_clip(self.conn, clip_id)
            key = self._channel_key(row) if row is not None else ""
            reason = ""
            if row is None or row["status"] != "approved":
                reason = "only approved clips are uploaded"
            elif self._in_flight(clip_id):
                reason = "already uploaded or uploading"
            elif when < self.now() + YOUTUBE_MIN_LEAD:
                reason = "its slot is too close (YouTube needs 15 minutes); plan again"
            elif (key, when.astimezone(timezone.utc)) in taken:
                reason = "another clip already has that slot; plan again"
            elif budget <= 0:
                reason = "over today's upload limit"
            else:
                reason = self._why_not(row)
            if reason:
                skipped.append({"clip_id": clip_id, "reason": reason})
                continue
            body = self._body(row, rfc3339(when))
            if key:
                error = self._upload_direct(key, row, body)
                if error:
                    skipped.append({"clip_id": clip_id, "reason": error})
                    continue
            else:
                try:
                    answer = self.client.publish_clip(clip_id, body)
                except ClipsKittyError as exc:
                    skipped.append({"clip_id": clip_id, "reason": f"Clips Kitty refused it: {exc}"})
                    continue
                db.add_publish(self.conn, ck_clip_id=clip_id, publish_job_id=answer.get("publish_job_id"),
                               title=body["title"], publish_at=body["publish_at"])
                db.add_clip_note(self.conn, clip_id, f"sent to YouTube, scheduled for {label(when, self.slots.tz)}")
            taken.add((key, when.astimezone(timezone.utc)))
            budget -= 1
            started.append({"clip_id": clip_id, "publish_at": body["publish_at"],
                            "when": label(when, self.slots.tz), "channel": self.channel_title(key)})
            log.info("Clip %s uploading to YouTube (%s), scheduled for %s", clip_id, self.channel_title(key),
                     body["publish_at"])
        if started:
            self._refreshed_at = None      # the next look checks on these right away
        return {"started": started, "skipped": skipped}

    def _upload_direct(self, key: str, row: sqlite3.Row, body: dict) -> str:
        """Uploads to a publish.channels channel ourselves. Returns an error for the owner, or ''."""
        up = self.uploaders[key]
        clip_id = row["ck_clip_id"]
        video = self.work_dir / f"upload_{clip_id}.mp4"
        try:
            self.client.download_media(clip_id, video)
            result = up.upload(video, body)
        except (ClipsKittyError, YouTubeError) as exc:
            return f"upload to {up.settings.title} failed: {exc}"
        finally:
            video.unlink(missing_ok=True)
        log_id = db.add_publish(self.conn, ck_clip_id=clip_id, publish_job_id=None, title=body["title"],
                                publish_at=body["publish_at"], channel=key)
        publish_row = self.conn.execute("SELECT * FROM publish_log WHERE id = ?", (log_id,)).fetchone()
        # We uploaded it ourselves, so the answer is already here: settle it like a Clips Kitty upload.
        self._settle(publish_row, {"state": "uploaded", "youtube_id": result["youtube_id"],
                                   "actual_privacy": result["privacy"]})
        return ""

    def _body(self, row: sqlite3.Row, publish_at: str) -> dict:
        theirs = self._clips_kitty_clip(row) or {}
        tags, total = [], 0
        for tag in [t.lstrip("#") for t in (theirs.get("hashtags") or [])] + ["GTA 6", "GTA VI"]:
            if tag and tag.lower() not in {t.lower() for t in tags} and total + len(tag) + 1 <= MAX_TAGS_CHARS:
                tags.append(tag)
                total += len(tag) + 1
        return {
            "title": check_title(row["title"]),
            "description": theirs.get("description") or "",
            "tags": tags,
            "category_id": str(self.cfg.get("publish.category_id", "20")),
            "privacy": "private",                 # never Clips Kitty's default; publish_at makes it public later
            "publish_at": publish_at,
            "made_for_kids": bool(self.cfg.get("publish.made_for_kids", False)),
            "notify_subscribers": bool(self.cfg.get("publish.notify_subscribers", True)),
            "contains_synthetic_media": bool(self.cfg.get("publish.contains_synthetic_media", False)),
        }

    # -- what happened -------------------------------------------------------------------
    def refresh(self, *, force: bool = False) -> list[dict]:
        """Ask Clips Kitty how each upload went. Returns what changed."""
        now = self.clock()
        if not force and self._refreshed_at is not None and now - self._refreshed_at < REFRESH_EVERY_S:
            return []
        self._refreshed_at = now
        changed = []
        for row in db.publishes(self.conn, ("submitted",)):
            if row["channel"]:
                continue   # uploaded by us: settled at upload time
            try:
                answer = self.client.publish_state(row["ck_clip_id"])
            except ClipsKittyError as exc:
                log.warning("Couldn't check upload of clip %s: %s", row["ck_clip_id"], exc)
                continue
            upload, job = answer.get("upload"), answer.get("job") or {}
            if upload and upload.get("state") in ("uploaded", "locked_private", "rejected", "failed"):
                changed.append(self._settle(row, upload))
            elif str(job.get("status") or job.get("state") or "") in ("failed", "cancelled", "interrupted"):
                state = "interrupted" if str(job.get("status") or job.get("state")) == "interrupted" else "failed"
                error = str(job.get("error") or "")
                db.update_publish(self.conn, row["id"], state=state, error=error[:2000])
                note = ("upload interrupted: check your YouTube channel before trying again (it may be there)"
                        if state == "interrupted" else f"upload failed: {error or 'no reason given'}")
                db.add_clip_note(self.conn, row["ck_clip_id"], note)
                changed.append({"clip_id": row["ck_clip_id"], "state": state})
        return changed

    def _settle(self, row: sqlite3.Row, upload: dict) -> dict:
        state = upload["state"]
        yt_id = str(upload.get("youtube_id") or "")
        actual = str(upload.get("actual_privacy") or "")
        db.update_publish(self.conn, row["id"], state=state, youtube_id=yt_id, actual_privacy=actual,
                          error=str(upload.get("error") or "")[:2000])
        clip_id = row["ck_clip_id"]
        when = label(parse_rfc3339(row["publish_at"]), self.slots.tz)
        link = f"https://youtube.com/shorts/{yt_id}" if yt_id else ""
        if state == "uploaded":
            if actual and actual != "private":
                # Scheduled uploads must be private until their slot. Say so loudly.
                db.add_clip_note(self.conn, clip_id, f"WARNING: asked for private until {when}, YouTube reports "
                                                     f"{actual!r}")
                log.warning("Clip %s: asked for private until %s, YouTube reports %r", clip_id, when, actual)
            db.mark_published(self.conn, clip_id, f"on YouTube, scheduled for {when}: {link}")
            notify.current().published(f"📅 Uploaded **{row['title']}**: private until {when}, then public. {link}")
        elif state == "locked_private":
            db.mark_published(self.conn, clip_id, f"on YouTube but LOCKED PRIVATE by YouTube (unaudited Google "
                                                  f"Cloud project); it won't go public at {when}: {link}")
            log.warning("YouTube locked clip %s's upload to private; scheduling is stopped.", clip_id)
        else:
            db.add_clip_note(self.conn, clip_id, f"upload {state}: {upload.get('error') or 'no reason given'}")
        return {"clip_id": clip_id, "state": state, "youtube_id": yt_id}

    def upload_for(self, clip_id: int) -> dict | None:
        row = db.latest_publish(self.conn, clip_id)
        if row is None:
            return None
        return {"state": row["state"], "youtube_id": row["youtube_id"], "publish_at": row["publish_at"],
                "when": label(parse_rfc3339(row["publish_at"]), self.slots.tz),
                "actual_privacy": row["actual_privacy"], "error": row["error"],
                "channel": self.channel_title(row["channel"]),
                "url": f"https://youtube.com/shorts/{row['youtube_id']}" if row["youtube_id"] else ""}


    def held_for_live(self) -> list[int]:
        """Approved clips of hold_uploads_while_live streamers that aren't on YouTube yet."""
        held = {s.name for s in self.streamers if s.hold_uploads_while_live and s.permission}
        return [r["ck_clip_id"] for r in self._approved(None) if r["streamer"] in held]

    def upload_held(self) -> dict:
        """Uploads the held clips whose streamer is offline now (each into the next free slot)."""
        ids = self.held_for_live()
        if not ids:
            return {"started": [], "skipped": []}
        plan = self.plan(ids)
        if not plan.status.ok or not plan.items:
            return {"started": [], "skipped": plan.skipped}
        return self.execute([i.as_dict() for i in plan.items])
