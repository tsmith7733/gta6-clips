"""Phase 5: scheduled uploads through Clips Kitty."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from gta6clips import db
from gta6clips.publish.schedule import SlotSettings, next_slots, rfc3339
from gta6clips.publish.service import PublishService
from gta6clips.review.app import create_app
from gta6clips.review.service import ReviewError, ReviewService

from .test_review import _clip

CHI = ZoneInfo("America/Chicago")
# Thursday 2026-11-19, 2:50 PM in Chicago (CST, UTC-6).
NOW = datetime(2026, 11, 19, 14, 50, tzinfo=CHI).astimezone(timezone.utc)


@pytest.fixture
def pub(conn, ck, streamers, cfg):
    return PublishService(conn=conn, client=ck, streamers=streamers, cfg=cfg, now=lambda: NOW)


def _approved(ck_state, conn, clip_id, **kw):
    _clip(ck_state, conn, clip_id, start=100 + clip_id, end=120 + clip_id, **kw)
    db.set_clip_status(conn, clip_id, "approved", note="approved")


# -- slots -----------------------------------------------------------------------------

def test_slots_skip_ones_too_close_and_follow_weekdays_and_weekends(cfg):
    s = SlotSettings.from_config(cfg)
    got = [rfc3339(t) for t in next_slots(s, now=NOW, taken=[], count=5)]
    assert got == ["2026-11-19T20:00:00-06:00",          # 3 PM is only 10 minutes away
                   "2026-11-20T15:00:00-06:00", "2026-11-20T20:00:00-06:00",
                   "2026-11-21T12:00:00-06:00", "2026-11-21T15:00:00-06:00"]   # Saturday


def test_taken_slots_are_skipped_and_summer_times_carry_their_offset(cfg):
    s = SlotSettings.from_config(cfg)
    taken = [datetime(2026, 11, 19, 20, 0, tzinfo=CHI)]
    assert rfc3339(next_slots(s, now=NOW, taken=taken, count=1)[0]) == "2026-11-20T15:00:00-06:00"
    july = datetime(2026, 7, 1, 9, 0, tzinfo=CHI)
    assert rfc3339(next_slots(s, now=july, taken=[], count=1)[0]) == "2026-07-01T15:00:00-05:00"


# -- can we publish ----------------------------------------------------------------------

def test_publishing_off_in_clips_kitty_is_a_problem(pub, ck_state, conn):
    ck_state.youtube_enabled = False
    _approved(ck_state, conn, 801)
    plan = pub.plan()
    assert not plan.status.ok and "Publish to YouTube" in plan.status.problems[0] and not plan.items


def test_quota_warning_and_limit(pub, ck_state, conn):
    ck_state.uploads_used = 85
    assert pub.status().ok and "85 of today's 100" in pub.status().warnings[0]
    ck_state.uploads_used = 100
    assert not pub.status().ok


# -- planning ----------------------------------------------------------------------------

def test_plan_takes_approved_clips_best_first_into_free_slots(pub, ck_state, conn):
    _approved(ck_state, conn, 801)
    _clip(ck_state, conn, 802, start=300, end=320)          # not approved
    _approved(ck_state, conn, 803, credit=False)             # lost its credit
    plan = pub.plan()
    assert [i.clip_id for i in plan.items] == [801]
    assert plan.items[0].publish_at == "2026-11-19T20:00:00-06:00" and "Nov 19, 8:00 PM" in plan.items[0].when
    assert plan.skipped == [{"clip_id": 803, "reason": "its description in Clips Kitty has lost The Burnt "
                                                       "Peanut's credit line"}]


def test_plan_stops_at_the_days_upload_limit(pub, ck_state, conn):
    ck_state.uploads_used = 99
    _approved(ck_state, conn, 801)
    _approved(ck_state, conn, 802)
    plan = pub.plan()
    assert len(plan.items) == 1 and plan.skipped[0]["reason"] == "over today's upload limit"


def test_streamer_without_permission_is_never_uploaded(pub, ck_state, conn):
    _approved(ck_state, conn, 801, streamer="No Permission Pete")
    plan = pub.plan()
    assert not plan.items and "permission" in plan.skipped[0]["reason"].lower()
    assert pub.execute([{"clip_id": 801, "publish_at": "2026-11-20T15:00:00-06:00"}])["started"] == []
    assert ck_state.publishes == []


# -- uploading ---------------------------------------------------------------------------

def test_upload_is_private_scheduled_and_credited_then_marked_published(pub, ck_state, conn):
    _approved(ck_state, conn, 801)
    plan = pub.plan()
    result = pub.execute([i.as_dict() for i in plan.items])
    assert [s["clip_id"] for s in result["started"]] == [801]
    clip_id, body = ck_state.publishes[0]
    assert clip_id == 801
    assert body["privacy"] == "private" and body["publish_at"] == "2026-11-19T20:00:00-06:00"
    assert "Clip from The Burnt Peanut" in body["description"] and "@TheBurntPeanut" in body["description"]
    assert body["category_id"] == "20" and body["made_for_kids"] is False and "GTA6" in body["tags"]
    assert db.get_clip(conn, 801)["status"] == "approved"           # until Clips Kitty reports it
    assert pub.refresh(force=True) == [{"clip_id": 801, "state": "uploaded", "youtube_id": "yt00000801"}]
    row = db.get_clip(conn, 801)
    assert row["status"] == "published" and "youtube.com/shorts/yt00000801" in row["notes"]
    assert pub.plan().items == []                                     # not uploaded twice


def test_two_clips_never_share_a_slot(pub, ck_state, conn):
    _approved(ck_state, conn, 801)
    _approved(ck_state, conn, 802)
    items = [i.as_dict() for i in pub.plan().items]
    assert len({i["publish_at"] for i in items}) == 2
    clash = [{"clip_id": 801, "publish_at": items[0]["publish_at"]},
             {"clip_id": 802, "publish_at": items[0]["publish_at"]}]
    result = pub.execute(clash)
    assert len(result["started"]) == 1 and "slot" in result["skipped"][0]["reason"]


def test_execute_rechecks_what_the_browser_sent(pub, ck_state, conn):
    _clip(ck_state, conn, 801)                                         # not approved
    _approved(ck_state, conn, 802)
    result = pub.execute([{"clip_id": 801, "publish_at": "2026-11-20T15:00:00-06:00"},
                          {"clip_id": 802, "publish_at": "2026-11-19T14:55:00-06:00"},   # 5 min away
                          {"clip_id": 802, "publish_at": "2026-11-20T15:00:00"}])          # no offset
    assert result["started"] == [] and len(result["skipped"]) == 3
    assert ck_state.publishes == []


def test_a_locked_private_upload_stops_scheduling(pub, ck_state, conn):
    ck_state.lock_uploads = True
    _approved(ck_state, conn, 801)
    _approved(ck_state, conn, 802)
    pub.execute([pub.plan().items[0].as_dict()])
    pub.refresh(force=True)
    assert "LOCKED PRIVATE" in db.get_clip(conn, 801)["notes"]
    plan = pub.plan()
    assert not plan.status.ok and "audit" in plan.status.problems[0] and not plan.items
    with pytest.raises(ReviewError):
        pub.execute([{"clip_id": 802, "publish_at": "2026-11-20T15:00:00-06:00"}])


def test_a_failed_upload_keeps_the_clip_approved_and_frees_its_slot(pub, ck_state, conn):
    ck_state.publish_fails = True
    _approved(ck_state, conn, 801)
    pub.execute([pub.plan().items[0].as_dict()])
    pub.refresh(force=True)
    assert db.get_clip(conn, 801)["status"] == "approved" and "upload failed" in db.get_clip(conn, 801)["notes"]
    ck_state.publish_fails = False
    assert pub.plan().items[0].publish_at == "2026-11-19T20:00:00-06:00"


def test_client_refuses_anything_but_private():
    from gta6clips.clipskitty import ClipsKittyClient
    with pytest.raises(ValueError):
        ClipsKittyClient().publish_clip(1, {"title": "x", "privacy": "public"})
    with pytest.raises(ValueError):
        ClipsKittyClient().publish_clip(1, {"title": "x", "publish_at": "2026-11-20T15:00:00-06:00"})


# -- on the review page ------------------------------------------------------------------

@pytest.fixture
def page(workdir, ck, streamers, cfg):
    shared = db.connect(workdir / "data" / "orchestrator.db", threads=True)
    review = ReviewService(conn=shared, client=ck, streamers=streamers)
    publisher = PublishService(conn=shared, client=ck, streamers=streamers, cfg=cfg, now=lambda: NOW)
    yield TestClient(create_app(review, ck, publisher), base_url="http://127.0.0.1:8770")
    shared.close()


def test_page_plans_uploads_and_shows_the_upload(page, ck_state, conn):
    _approved(ck_state, conn, 801)
    st = page.get("/api/publish/status").json()
    assert st["ok"] and st["channel"] == "GTA Clips"
    plan = page.post("/api/publish/plan", json={}).json()
    assert [i["clip_id"] for i in plan["items"]] == [801] and ck_state.publishes == []   # planning uploads nothing
    r = page.post("/api/publish/execute", json={"items": plan["items"]}).json()
    assert [s["clip_id"] for s in r["started"]] == [801]
    # Reject / undo can't touch a clip that's on its way to YouTube.
    assert page.post("/api/clips/801/reject", json={}).status_code == 400
    clips = page.get("/api/clips?status=published").json()["clips"]
    assert clips[0]["upload"]["state"] == "uploaded" and clips[0]["upload"]["url"].endswith("yt00000801")
    assert page.post("/api/publish/execute", json={"items": []}).status_code == 400
