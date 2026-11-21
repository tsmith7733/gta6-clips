"""A second YouTube channel, uploaded to directly: sign-in, routing per streamer, slots per channel."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from gta6clips import db
from gta6clips.publish.service import PublishService
from gta6clips.publish.youtube_direct import (ChannelSettings, DirectUploader, OAuthClient, TokenStore,
                                              YouTubeError, connect, make_uploaders)
from gta6clips.streamers import load_streamers

from .test_review import _clip

CHI = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 19, 14, 50, tzinfo=CHI)
SETTINGS = ChannelSettings("cutscene_skipped", "Cutscene Skipped", "@CutsceneSkipped")
OAUTH = OAuthClient("cid.apps.googleusercontent.com", "csecret")


def plain_store(folder: Path) -> TokenStore:
    return TokenStore(folder, protect=lambda b: b[::-1], unprotect=lambda b: b[::-1])


class FakeGoogle:
    def __init__(self, handle="@cutsceneskipped", privacy="private"):
        self.handle = handle
        self.privacy = privacy
        self.uploads: list[dict] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
        if "/youtube/v3/channels" in url:
            return httpx.Response(200, json={"items": [{"id": "UCcs", "snippet": {
                "title": "Cutscene Skipped", "customUrl": self.handle}}]})
        if url.startswith("https://www.googleapis.com/upload/youtube/v3/videos"):
            self.uploads.append(json.loads(request.content))
            return httpx.Response(200, headers={"location": "https://upload.example/session/1"})
        if url.startswith("https://upload.example/session/"):
            return httpx.Response(200, json={"id": "abcDEF12345", "snippet": {"channelId": "UCcs"},
                                             "status": {"privacyStatus": self.privacy}})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class Receiver:
    redirect_uri = "http://127.0.0.1:5555"

    def wait(self):
        return "the-code"


def test_connect_saves_the_sign_in_for_the_right_channel(tmp_path):
    google, store = FakeGoogle(), plain_store(tmp_path)
    opened = []
    channel = connect(SETTINGS, OAUTH, store, http=google.client(), open_browser=opened.append, receiver=Receiver())
    assert channel["title"] == "Cutscene Skipped"
    assert "code_challenge" in opened[0] and "youtube.upload" in opened[0] and "prompt=consent" in opened[0]
    assert store.load("cutscene_skipped")["refresh_token"] == "rt"
    assert store.path("cutscene_skipped").read_bytes() != json.dumps(store.load("cutscene_skipped")).encode()


def test_connect_refuses_another_channel(tmp_path):
    store = plain_store(tmp_path)
    with pytest.raises(YouTubeError, match="not @CutsceneSkipped"):
        connect(SETTINGS, OAUTH, store, http=FakeGoogle(handle="@RoastedClipz").client(),
                open_browser=lambda u: None, receiver=Receiver())
    assert store.load("cutscene_skipped") is None


def test_the_client_file_must_exist(tmp_path):
    with pytest.raises(YouTubeError, match="Download JSON"):
        OAuthClient.from_file(tmp_path / "youtube_client.json")


def test_uploads_must_be_private_with_a_time(tmp_path):
    up = DirectUploader(SETTINGS, OAUTH, plain_store(tmp_path), http=FakeGoogle().client())
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(ValueError):
        up.upload(video, {"title": "t", "privacy": "public", "publish_at": "2026-11-20T15:00:00-06:00"})
    with pytest.raises(ValueError):
        up.upload(video, {"title": "t", "privacy": "private"})


# -- routing --------------------------------------------------------------------------------

@pytest.fixture
def routed(workdir, cfg):
    text = (workdir / "streamers.yaml").read_text(encoding="utf-8") + """
  - name: Kai Racer
    platform: twitch
    channel_url: https://www.twitch.tv/kairacer
    permission: true
    credit: "Clip from Kai Racer"
    priority: 3
    youtube_channel: cutscene_skipped
"""
    (workdir / "streamers.yaml").write_text(text, encoding="utf-8")
    cfg.data["publish"]["channels"] = {"cutscene_skipped": {"title": "Cutscene Skipped",
                                                             "handle": "@CutsceneSkipped"}}
    return load_streamers(workdir / "streamers.yaml")


def _kai_clip(ck_state, conn, clip_id, **kw):
    _clip(ck_state, conn, clip_id, streamer="Kai Racer", credit=False, **kw)
    ck_state.clips[clip_id]["description"] += "\n\nClip from Kai Racer\nhttps://www.twitch.tv/kairacer"


def _service(conn, ck, cfg, streamers, workdir, google, *, signed_in=True):
    store = plain_store(workdir / "secrets")
    if signed_in:
        store.save("cutscene_skipped", {"refresh_token": "rt", "channel_id": "UCcs"})
    up = DirectUploader(SETTINGS, OAUTH, store, http=google.client())
    return PublishService(conn=conn, client=ck, streamers=streamers, cfg=cfg, now=lambda: NOW,
                          uploaders={"cutscene_skipped": up}, work_dir=workdir / "work")


def test_each_streamers_clips_go_to_their_channel_with_its_own_slots(conn, ck, ck_state, cfg, routed, workdir):
    google = FakeGoogle()
    pub = _service(conn, ck, cfg, routed, workdir, google)
    _clip(ck_state, conn, 801, start=101, end=121)                              # The Burnt Peanut
    _kai_clip(ck_state, conn, 802, start=300, end=330)                          # a Twitch streamer
    for cid in (801, 802):
        db.set_clip_status(conn, cid, "approved", note="approved")
    plan = pub.plan()
    by_clip = {i.clip_id: i for i in plan.items}
    assert by_clip[801].channel == "GTA Clips" and by_clip[802].channel == "Cutscene Skipped"
    # Two channels, so both get tonight's 8 PM slot.
    assert by_clip[801].publish_at == by_clip[802].publish_at == "2026-11-19T20:00:00-06:00"
    result = pub.execute([i.as_dict() for i in plan.items])
    assert sorted(s["clip_id"] for s in result["started"]) == [801, 802]
    assert [c for c, _ in ck_state.publishes] == [801]                          # Clips Kitty only got its own
    meta = google.uploads[0]
    assert meta["status"] == {"privacyStatus": "private", "publishAt": "2026-11-19T20:00:00-06:00",
                              "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False}
    assert meta["snippet"]["title"] == "Soccer Practice Rush!" and "Clip from" in meta["snippet"]["description"]
    assert db.get_clip(conn, 802)["status"] == "published"
    assert pub.upload_for(802)["channel"] == "Cutscene Skipped" and pub.upload_for(802)["youtube_id"]
    assert not list((workdir / "work").glob("*.mp4"))                           # the download is cleaned up


def test_a_channel_not_signed_in_yet_waits(conn, ck, ck_state, cfg, routed, workdir):
    pub = _service(conn, ck, cfg, routed, workdir, FakeGoogle(), signed_in=False)
    _kai_clip(ck_state, conn, 802)
    db.set_clip_status(conn, 802, "approved", note="approved")
    plan = pub.plan()
    assert not plan.items and "connect-youtube cutscene_skipped" in plan.skipped[0]["reason"]


def test_a_non_private_answer_is_flagged(conn, ck, ck_state, cfg, routed, workdir):
    pub = _service(conn, ck, cfg, routed, workdir, FakeGoogle(privacy="public"))
    _kai_clip(ck_state, conn, 802)
    db.set_clip_status(conn, 802, "approved", note="approved")
    pub.execute([i.as_dict() for i in pub.plan().items])
    assert any("WARNING" in n for n in json.loads(db.get_clip(conn, 802)["notes"]))


def test_without_the_client_file_the_reason_says_how_to_get_it(conn, ck, ck_state, cfg, routed, workdir):
    ups = make_uploaders(cfg, workdir / "secrets")
    pub = PublishService(conn=conn, client=ck, streamers=routed, cfg=cfg, now=lambda: NOW, uploaders=ups)
    _kai_clip(ck_state, conn, 802)
    db.set_clip_status(conn, 802, "approved", note="approved")
    assert "Download JSON" in pub.plan().skipped[0]["reason"]


def test_review_page_shows_each_channels_clips_apart(conn, ck, ck_state, cfg, routed, workdir):
    from fastapi.testclient import TestClient

    from gta6clips.review.app import create_app
    from gta6clips.review.service import ReviewService

    shared = db.connect(workdir / "data" / "orchestrator.db", threads=True)
    try:
        review = ReviewService(conn=shared, client=ck, streamers=routed)
        pub = _service(shared, ck, cfg, routed, workdir, FakeGoogle())
        page = TestClient(create_app(review, ck, pub), base_url="http://127.0.0.1:8770")
        _clip(ck_state, conn, 801)                     # The Burnt Peanut -> Clips Kitty's channel
        _kai_clip(ck_state, conn, 802)                 # Kai Racer -> Cutscene Skipped
        channels = page.get("/api/channels").json()["channels"]
        assert channels == [{"key": "", "title": "GTA Clips"},
                            {"key": "cutscene_skipped", "title": "Cutscene Skipped"}]
        main = page.get("/api/clips?status=pending_review&channel=").json()
        second = page.get("/api/clips?status=pending_review&channel=cutscene_skipped").json()
        both = page.get("/api/clips?status=pending_review").json()
        assert [c["id"] for c in main["clips"]] == [801] and main["counts"]["pending_review"] == 1
        assert [c["id"] for c in second["clips"]] == [802] and second["clips"][0]["channel"] == "Cutscene Skipped"
        assert both["counts"]["pending_review"] == 2
    finally:
        shared.close()
