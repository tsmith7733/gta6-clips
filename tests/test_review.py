"""Phase 4: the review page and the decisions behind it."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from gta6clips import db
from gta6clips.review.app import create_app
from gta6clips.review.service import ReviewError, ReviewService

CREDIT = "\n\nClip from The Burnt Peanut\nhttps://www.youtube.com/@TheBurntPeanut"
VIDEO = "local_abc"


def _clip(ck_state, conn, clip_id: int, *, status="pending_review", streamer="The Burnt Peanut",
          start=100.0, end=120.0, credit=True, video=VIDEO):
    description = "He robbed the jewelry store." + (CREDIT if credit else "")
    ck_state.processed.add(video)
    ck_state.clips[clip_id] = {"id": clip_id, "video_id": video, "start_s": start, "end_s": end, "score": 80,
                               "title": "Soccer Practice Rush!", "description": description,
                               "hashtags": ["#GTA6"], "hook": "", "status": "queued",
                               "lines": [{"start": 0.0, "end": 1.0, "text": "soccer practice in an hour"}]}
    db.upsert_clip(conn, ck_clip_id=clip_id, window_id=None, ck_video_id=video, streamer=streamer,
                   status=status, score=80, start_s=start, end_s=end, title="Soccer Practice Rush!",
                   title_options=["Soccer Practice Rush!", "Lady, It's A Robbery", "Pick Up Your Kids!"],
                   hook="", description=description, hashtags=["#GTA6"], notes=[])


@pytest.fixture
def service(workdir, conn, ck, streamers):
    # The page's worker threads share one connection, as in `review`.
    shared = db.connect(workdir / "data" / "orchestrator.db", threads=True)
    yield ReviewService(conn=shared, client=ck, streamers=streamers)
    shared.close()


@pytest.fixture
def page(service, ck):
    return TestClient(create_app(service, ck), base_url="http://127.0.0.1:8770")


def test_tabs_list_clips_by_status(page, ck_state, conn):
    _clip(ck_state, conn, 801)
    _clip(ck_state, conn, 802, status="spoiler_hold", start=300, end=320)
    data = page.get("/api/clips?status=pending_review").json()
    assert [c["id"] for c in data["clips"]] == [801]
    assert data["counts"]["pending_review"] == 1 and data["counts"]["spoiler_hold"] == 1
    assert [c["id"] for c in page.get("/api/clips?status=spoiler_hold").json()["clips"]] == [802]
    assert "Clip review" in page.get("/").text


def test_approving_sets_the_chosen_title_in_clips_kitty(page, ck_state, conn):
    _clip(ck_state, conn, 801)
    r = page.post("/api/clips/801/approve", json={"title": "Lady, It's A Robbery"})
    assert r.status_code == 200, r.text
    row = db.get_clip(conn, 801)
    assert row["status"] == "approved" and row["title"] == "Lady, It's A Robbery"
    assert (801, {"title": "Lady, It's A Robbery"}) in ck_state.patches
    assert ck_state.clips[801]["title"] == "Lady, It's A Robbery"
    assert any("approved" in n for n in json.loads(row["notes"]))


def test_approving_refuses_a_clip_that_lost_its_credit(page, ck_state, conn):
    _clip(ck_state, conn, 801, credit=False)
    r = page.post("/api/clips/801/approve", json={"title": "Soccer Practice Rush!"})
    assert r.status_code == 400 and "credit" in r.json()["detail"]
    assert db.get_clip(conn, 801)["status"] == "pending_review"


def test_a_spoiler_needs_confirming_and_undo_puts_it_back_in_spoilers(page, ck_state, conn):
    _clip(ck_state, conn, 801, status="spoiler_hold")
    r = page.post("/api/clips/801/approve", json={"title": "Soccer Practice Rush!"})
    assert r.status_code == 400 and "spoiler" in r.json()["detail"]
    r = page.post("/api/clips/801/approve", json={"title": "Soccer Practice Rush!", "confirm_spoiler": True})
    assert r.json()["status"] == "approved"
    assert page.post("/api/clips/801/undo", json={}).json()["status"] == "spoiler_hold"
    assert page.post("/api/clips/801/reject", json={}).json()["status"] == "rejected"
    assert page.post("/api/clips/801/undo", json={}).json()["status"] == "spoiler_hold"   # never the main queue


def test_reject_and_undo(page, ck_state, conn):
    _clip(ck_state, conn, 801)
    assert page.post("/api/clips/801/reject", json={}).json()["status"] == "rejected"
    r = page.post("/api/clips/801/approve", json={"title": "Soccer Practice Rush!"})
    assert r.status_code == 400                                  # decide again from the review tab
    assert page.post("/api/clips/801/undo", json={}).json()["status"] == "pending_review"


def test_published_clips_stay_published(page, ck_state, conn):
    _clip(ck_state, conn, 801)
    conn.execute("UPDATE clips SET status = 'published' WHERE ck_clip_id = 801")
    conn.commit()
    for what in ("reject", "undo"):
        assert page.post(f"/api/clips/801/{what}", json={}).status_code == 400
    assert page.post("/api/clips/801/title", json={"title": "New"}).status_code == 400
    assert db.get_clip(conn, 801)["status"] == "published"


def test_clips_of_a_streamer_without_permission_cant_be_approved(page, ck_state, conn):
    _clip(ck_state, conn, 801, streamer="No Permission Pete")
    r = page.post("/api/clips/801/approve", json={"title": "Soccer Practice Rush!"})
    assert r.status_code == 400 and "permission" in r.json()["detail"].lower()
    assert db.get_clip(conn, 801)["status"] == "pending_review"


@pytest.mark.parametrize("title", ["", "x" * 101, "Look <here>"])
def test_bad_titles_are_refused(page, ck_state, conn, title):
    _clip(ck_state, conn, 801)
    assert page.post("/api/clips/801/approve", json={"title": title}).status_code == 400
    assert db.get_clip(conn, 801)["status"] == "pending_review"


def test_saving_a_title_on_an_approved_clip(page, ck_state, conn):
    _clip(ck_state, conn, 801)
    page.post("/api/clips/801/approve", json={"title": "Soccer Practice Rush!"})
    r = page.post("/api/clips/801/title", json={"title": "  You Can Pick Up   Your Kids  "})
    assert r.json()["title"] == "You Can Pick Up Your Kids" and r.json()["status"] == "approved"
    assert ck_state.clips[801]["title"] == "You Can Pick Up Your Kids"


def test_a_re_rendered_clip_is_followed_with_its_decision(service, ck_state, conn):
    _clip(ck_state, conn, 801)
    service.approve(801, "Soccer Practice Rush!")
    clip = ck_state.clips.pop(801)                      # Clips Kitty renders it again as 845
    ck_state.clips[845] = {**clip, "id": 845, "start_s": 101.0, "end_s": 121.0}
    _clip(ck_state, conn, 830, start=400, end=420)       # an unrelated clip in the same video
    assert service.reconcile(force=True) == [(801, 845)]
    assert db.get_clip(conn, 801) is None
    row = db.get_clip(conn, 845)
    assert row["status"] == "approved" and "clip 801 became 845" in row["notes"]


def test_a_vanished_clip_with_no_clear_successor_is_left_alone(service, ck_state, conn):
    _clip(ck_state, conn, 801)
    clip = ck_state.clips.pop(801)
    ck_state.clips[845] = {**clip, "id": 845}
    ck_state.clips[846] = {**clip, "id": 846}            # two candidates: don't guess
    assert service.reconcile(force=True) == []
    assert db.get_clip(conn, 801)["status"] == "pending_review"


def test_video_previews_come_straight_from_clips_kitty(page, ck_state, conn):
    _clip(ck_state, conn, 801)
    r = page.get("/media/801", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "http://127.0.0.1:8765/media/801"
    assert page.get("/media/999", follow_redirects=False).status_code == 400     # not one of ours


def test_only_loopback_and_only_this_page_can_change_things(service, ck, ck_state, conn):
    _clip(ck_state, conn, 801)
    app = create_app(service, ck)
    evil = TestClient(app, base_url="http://evil.example:8770")
    assert evil.get("/api/clips").status_code == 400
    local = TestClient(app, base_url="http://127.0.0.1:8770")
    r = local.post("/api/clips/801/reject", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = local.post("/api/clips/801/reject", content="x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert db.get_clip(conn, 801)["status"] == "pending_review"


def test_status_moves_are_checked_in_the_database_too(conn, ck_state):
    _clip(ck_state, conn, 801)
    with pytest.raises(db.BadMove):
        db.set_clip_status(conn, 801, "published")
    with pytest.raises(db.BadMove):
        db.set_clip_status(conn, 999, "approved")


def test_service_errors_are_plain_messages(service, ck_state, conn):
    with pytest.raises(ReviewError):
        service.approve(999, "x")
