"""Twitch's top GTA 6 clips: picked for permitted streamers only, cut with slack, queued for the dispatcher."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from gta6clips import db
from gta6clips.downloads import DownloadError
from gta6clips.live.dispatch import Dispatcher
from gta6clips.streamers import load_streamers, require_permission
from gta6clips.twitch import Helix, HelixError
from gta6clips.twitch_clips import TwitchClipSource, clip_score

from .conftest import STREAMERS_YAML, FakeWriter

GTA = "1234"
NOW = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc).timestamp()

TWITCH_YAML = STREAMERS_YAML + """
  - name: Kai Racer
    platform: twitch
    channel_url: https://www.twitch.tv/kairacer
    permission: true
    permission_source: test
    credit: "Clip from Kai Racer on Twitch"
    priority: 3
  - name: Secret Sam
    platform: twitch
    channel_url: https://www.twitch.tv/secretsam
    permission: false
    credit: "Clip from Sam"
    priority: 4
"""


def clip(cid, views, *, game=GTA, vod="555", offset=1000, duration=30, created="2026-11-20T10:00:00Z"):
    return {"id": cid, "url": f"https://clips.twitch.tv/{cid}", "title": f"Clip {cid}", "game_id": game,
            "view_count": views, "duration": duration, "video_id": vod, "vod_offset": offset,
            "created_at": created, "broadcaster_name": "kairacer"}


class FakeTwitch:
    def __init__(self, clips_by_user: dict[str, list[dict]]):
        self.clips_by_user = clips_by_user
        self.requests: list[httpx.Request] = []
        self.tokens = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "id.twitch.tv":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": 5000})
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "games":
            return httpx.Response(200, json={"data": [{"id": GTA, "name": "Grand Theft Auto VI"}]})
        if path == "users":
            logins = request.url.params.get_list("login")
            return httpx.Response(200, json={"data": [{"id": f"id_{lg}", "login": lg} for lg in logins]})
        if path == "clips":
            bid = request.url.params["broadcaster_id"]
            return httpx.Response(200, json={"data": self.clips_by_user.get(bid, [])})
        return httpx.Response(404)


class FakeFetcher:
    def __init__(self, fail_vod: bool = False):
        self.cuts: list[tuple[str, float, float]] = []
        self.fail_vod = fail_vod

    def cut(self, info, streamer, start_s, end_s, dest: Path) -> Path:
        require_permission(streamer)
        if self.fail_vod and "/videos/" in info.url:
            raise DownloadError("subscriber-only VOD")
        self.cuts.append((info.url, start_s, end_s))
        out = dest.with_suffix(".mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4" * 2000)
        return out


@pytest.fixture
def twitch_streamers(workdir):
    (workdir / "streamers.yaml").write_text(TWITCH_YAML, encoding="utf-8")
    return load_streamers(workdir / "streamers.yaml")


def make_source(cfg, conn, streamers, fake: FakeTwitch, workdir, fetcher=None):
    helix = Helix("cid", "secret", http=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                  clock=lambda: NOW)
    return TwitchClipSource(cfg=cfg, conn=conn, helix=helix, streamers=streamers, fetcher=fetcher or FakeFetcher(),
                            windows_dir=workdir / "windows", clock=lambda: NOW)


def test_only_permitted_twitch_streamers_are_asked_about(cfg, conn, twitch_streamers, workdir):
    fake = FakeTwitch({"id_kairacer": [clip("a", 500)]})
    src = make_source(cfg, conn, twitch_streamers, fake, workdir)
    run = src.find()
    asked = [r.url.params.get("broadcaster_id") for r in fake.requests if r.url.path.endswith("/clips")]
    assert asked == ["id_kairacer"]                                   # never Secret Sam
    logins = [p for r in fake.requests if r.url.path.endswith("/users") for p in r.url.params.get_list("login")]
    assert logins == ["kairacer"]
    assert [p.clip["id"] for p in run.picks] == ["a"]


def test_picks_gta6_clips_with_views_and_cuts_them_from_the_vod_with_slack(cfg, conn, twitch_streamers, workdir):
    fake = FakeTwitch({"id_kairacer": [
        clip("top", 900), clip("other_game", 800, game="999"), clip("few", 10),
        clip("same_moment", 400, offset=1010),          # viewers clipped the same moment twice
        clip("second", 300, offset=5000),
    ]})
    fetcher = FakeFetcher()
    src = make_source(cfg, conn, twitch_streamers, fake, workdir, fetcher)
    run = src.run_once()
    assert [p.clip["id"] for p in run.picks] == ["top", "second"]
    assert ("https://www.twitch.tv/videos/555", 955.0, 1060.0) in fetcher.cuts   # 45 s before, 30 s after
    rows = db.live_windows(conn, ("planned",))
    assert [r["source_url"] for r in rows] == ["https://clips.twitch.tv/top", "https://clips.twitch.tv/second"]
    top = rows[0]
    assert top["finder"] == "exact" and top["stream_video_id"] == "kairacer@clips"
    # The clip we'll ask for: the 30 s viewers clipped (45-75 s into the cut) plus 12 s each side.
    assert (top["moment_start_s"], top["moment_end_s"]) == (33.0, 87.0)
    assert "0:45-1:15" in top["focus"] and top["score"] == clip_score(900)
    assert Path(top["local_path"]).is_file()
    # A second run queues nothing new.
    assert src.run_once().queued == []


def test_clip_without_a_vod_yet_waits_then_uses_the_clip(cfg, conn, twitch_streamers, workdir):
    fresh = clip("fresh", 300, offset=None, created="2026-11-20T17:00:00Z")    # an hour old
    old = clip("old", 200, offset=None, created="2026-11-20T08:00:00Z")      # ten hours old
    gone = clip("gone", 100, vod="", offset=None)                            # VOD deleted
    fetcher = FakeFetcher()
    src = make_source(cfg, conn, twitch_streamers, FakeTwitch({"id_kairacer": [fresh, old, gone]}), workdir, fetcher)
    run = src.run_once()
    assert [p.clip["id"] for p in run.picks] == ["old", "gone"]
    assert ("https://clips.twitch.tv/old", 0.0, 30.0) in fetcher.cuts
    assert any("waits for Twitch" in s for s in run.skipped)


def test_a_vod_that_cant_be_downloaded_falls_back_to_the_clip(cfg, conn, twitch_streamers, workdir):
    fetcher = FakeFetcher(fail_vod=True)
    src = make_source(cfg, conn, twitch_streamers, FakeTwitch({"id_kairacer": [clip("a", 500)]}), workdir, fetcher)
    assert len(src.run_once().queued) == 1
    assert fetcher.cuts == [("https://clips.twitch.tv/a", 0.0, 30.0)]
    assert "0:00-0:30" in db.live_windows(conn, ("planned",))[0]["focus"]


def test_dry_run_downloads_nothing(cfg, conn, twitch_streamers, workdir):
    fetcher = FakeFetcher()
    src = make_source(cfg, conn, twitch_streamers, FakeTwitch({"id_kairacer": [clip("a", 500)]}), workdir, fetcher)
    run = src.run_once(dry_run=True)
    assert len(run.picks) == 1 and fetcher.cuts == [] and db.live_windows(conn, ("planned",)) == []


def test_game_id_is_looked_up_once_and_cached(cfg, conn, twitch_streamers, workdir):
    fake = FakeTwitch({})
    src = make_source(cfg, conn, twitch_streamers, fake, workdir)
    src.find()
    src.find()
    assert sum(r.url.path.endswith("/games") for r in fake.requests) == 1
    assert db.kv_get(conn, "twitch.game_id:grand theft auto vi") == GTA


def test_expired_token_is_renewed_once():
    calls = {"api": 0}

    def handler(request):
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 5000})
        calls["api"] += 1
        return httpx.Response(401) if calls["api"] == 1 else httpx.Response(200, json={"data": []})

    helix = Helix("cid", "s", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert helix.clips("1", started_at="a", ended_at="b") == []
    bad = Helix("cid", "s", http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400))))
    with pytest.raises(HelixError):
        bad.game_id("Grand Theft Auto VI")


def test_dispatcher_sends_a_twitch_clip_to_clipskittys_finder_with_its_focus(cfg, conn, twitch_streamers, workdir,
                                                                          ck, ck_state):
    src = make_source(cfg, conn, twitch_streamers, FakeTwitch({"id_kairacer": [clip("a", 500)]}), workdir)
    src.run_once()
    d = Dispatcher(cfg=cfg, client=ck, conn=conn, streamers=twitch_streamers, writer=FakeWriter())
    d.tick()
    body = ck_state.submissions[-1]
    assert body["pipeline"] == {"id": "gta6clips/exact-moments", "version": "1.0.0",
                                "settings": {"ranges": "33.0-87.0", "snap_seconds": 4.0}}
    assert body["channel"] == "kairacer" and body["max_clips"] == 1 and "force" not in body
    assert body["title"].endswith("[Twitch clip]") and body["focus"].startswith("Twitch viewers clipped 0:45-1:15")
    d.tick()
    d.tick()
    clips = conn.execute("SELECT * FROM clips").fetchall()
    assert clips and all(c["streamer"] == "Kai Racer" for c in clips)


def test_watcher_checks_twitch_clips_on_its_schedule(cfg, conn, workdir, ck):
    from gta6clips.live.watcher import Watcher

    only_twitch = TWITCH_YAML.split("  - name: Kai Racer")[0].replace("permission: true", "permission: false")
    (workdir / "streamers.yaml").write_text(only_twitch + "  - name: Kai Racer" +
                                            TWITCH_YAML.split("  - name: Kai Racer")[1], encoding="utf-8")
    streamers = load_streamers(workdir / "streamers.yaml")
    t = {"now": NOW}
    runs = []

    class Source:
        settings = type("S", (), {"every_s": 7200.0})()

        def last_run(self):
            return -1e18

        def run_once(self):
            runs.append(t["now"])
            return type("R", (), {"queued": []})()

    w = Watcher(cfg=cfg, client=ck, conn=conn, streamers=streamers, writer=FakeWriter(),
                make_session=lambda s, c: None, check_live=lambda url: None, clip_source=Source(),
                clock=lambda: t["now"])
    assert w.preflight() == []          # no YouTube streamer to record, but Twitch clips keep it useful
    w.step()
    t["now"] += 600
    w.step()
    t["now"] += 7200
    w.step()
    assert runs == [NOW, NOW + 7800]


def test_watcher_uploads_held_clips_every_ten_minutes(cfg, conn, workdir, ck):
    from gta6clips.live.watcher import Watcher

    calls = []

    class Publisher:
        def upload_held(self):
            calls.append(t["now"])
            return {"started": [{"clip_id": 7, "when": "Sat 3 PM"}], "skipped": []}

    t = {"now": NOW}
    w = Watcher(cfg=cfg, client=ck, conn=conn, streamers=[], writer=FakeWriter(), make_session=lambda s, c: None,
                check_live=lambda url: None, publisher=Publisher(), clock=lambda: t["now"])
    w.step()
    t["now"] += 300
    w.step()
    t["now"] += 300
    w.step()
    assert calls == [NOW, NOW + 600]


def test_last_run_is_remembered_so_a_restart_waits(cfg, conn, twitch_streamers, workdir):
    src = make_source(cfg, conn, twitch_streamers, FakeTwitch({}), workdir)
    assert src.last_run() < 0
    src.run_once(dry_run=True)
    assert src.last_run() < 0
    src.run_once()
    assert src.last_run() == NOW


def test_an_empty_vod_cut_falls_back_to_the_clip(cfg, conn, twitch_streamers, workdir):
    class EmptyVod(FakeFetcher):
        def cut(self, info, streamer, start_s, end_s, dest):
            out = super().cut(info, streamer, start_s, end_s, dest)
            if "/videos/" in info.url:
                out.write_bytes(b"x" * 261)          # what a 38-hour VOD gave back
            return out

    fetcher = EmptyVod()
    src = make_source(cfg, conn, twitch_streamers, FakeTwitch({"id_kairacer": [clip("a", 500)]}), workdir, fetcher)
    src.run_once()
    row = db.live_windows(conn, ("planned",))[0]
    assert "clips.twitch.tv" in fetcher.cuts[-1][0] and Path(row["local_path"]).stat().st_size > 10_000


def test_a_refused_window_is_failed_and_the_rest_still_go(cfg, conn, twitch_streamers, workdir, ck, ck_state):
    src = make_source(cfg, conn, twitch_streamers,
                      FakeTwitch({"id_kairacer": [clip("a", 900), clip("b", 500, offset=5000)]}), workdir)
    src.run_once()
    first, second = db.live_windows(conn, ("planned",))
    Path(first["local_path"]).write_bytes(b"x" * 261)            # emptied after queueing
    d = Dispatcher(cfg=cfg, client=ck, conn=conn, streamers=twitch_streamers, writer=FakeWriter())
    d.tick()
    state = conn.execute("SELECT state FROM windows WHERE id = ?", (first["id"],)).fetchone()["state"]
    assert state == "failed"
    assert [w["id"] for w in db.live_windows(conn, ("submitted",))] == [second["id"]]


def test_a_muted_vod_cut_falls_back_to_the_clip(cfg, conn, twitch_streamers, workdir):
    fetcher = FakeFetcher()
    src = make_source(cfg, conn, twitch_streamers, FakeTwitch({"id_kairacer": [clip("a", 500)]}), workdir, fetcher)
    src.silent = lambda path, s, e: s + 10 if "videos" in str(path) or "555" in str(path) else None
    src.run_once()
    row = db.live_windows(conn, ("planned",))[0]
    assert fetcher.cuts[-1][0] == "https://clips.twitch.tv/a" and "clip only" in row["reasons"]
    assert not list((workdir / "windows").glob("twitch_555_*"))       # the silent cut is deleted


def test_clips_under_the_minimum_score_are_skipped(cfg, conn, twitch_streamers, workdir):
    cfg.data["twitch_clips"]["min_score"] = 65           # about 1,800 views
    fake = FakeTwitch({"id_kairacer": [clip("big", 2000), clip("small", 1500, offset=5000)]})
    src = make_source(cfg, conn, twitch_streamers, fake, workdir)
    assert [p.clip["id"] for p in src.find().picks] == ["big"]
