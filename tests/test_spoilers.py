from gta6clips import db
from gta6clips.postprocess import postprocess_video
from gta6clips.spoilers import KeywordSpoilerCheck

from .test_postprocess import _make_clips


def _check(cfg, **kwargs):
    args = {"source_title": "GTA 6 day 3", "clip_title": "Car flips", "transcript": "oh no the car", "chat": []}
    return KeywordSpoilerCheck.from_config(cfg).check(**{**args, **kwargs})


def test_spoiler_keywords(cfg):
    assert not _check(cfg).hold
    assert _check(cfg, source_title="GTA 6 ENDING reaction").hold
    assert _check(cfg, transcript="wait is this the final mission").hold
    assert "transcript" in _check(cfg, transcript="okay spoilers ahead").reason
    assert not _check(cfg, transcript="I keep spending money").hold   # whole words only


def test_spoiler_chat_needs_several_messages(cfg):
    assert not _check(cfg, chat=["spoiler!", "lol", "nice"]).hold
    verdict = _check(cfg, chat=["SPOILERS", "no spoilers pls", "is this the ending??", "lol"])
    assert verdict.hold and "3 chat messages" in verdict.reason


def test_held_clips_stay_out_of_the_main_queue(ck, ck_state, conn, cfg, streamers, writer):
    _make_clips(ck)
    first = min(ck_state.clips)
    ck_state.clips[first]["lines"] = [{"start": 0, "end": 1, "text": "no way, the end credits"}]

    outcomes = postprocess_video(ck, conn, cfg, streamers[0], "vid1", writer, source_title="GTA 6 day 1")

    statuses = {o.ck_clip_id: o.status for o in outcomes}
    assert statuses[first] == "spoiler_hold"
    assert sorted(statuses.values()) == ["pending_review", "spoiler_hold"]
    assert [r["ck_clip_id"] for r in db.clips_by_status(conn, "pending_review")] != [first]
    held = db.clips_by_status(conn, "spoiler_hold")
    assert [r["ck_clip_id"] for r in held] == [first]
    assert "end credits" in held[0]["notes"]
    # Held clips still get their credit, like every clip.
    assert "The Burnt Peanut" in held[0]["description"]


def test_chat_around_a_clip_can_hold_it(ck, conn, cfg, streamers, writer):
    _make_clips(ck)
    outcomes = postprocess_video(ck, conn, cfg, streamers[0], "vid1", writer, source_title="GTA 6",
                                 chat_for=lambda s, e: ["SPOILER", "spoiler!!", "spoilers omg"] if s < 150 else [])
    assert [o.status for o in outcomes] == ["spoiler_hold", "pending_review"]
