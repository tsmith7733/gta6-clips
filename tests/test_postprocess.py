import json

import pytest

from gta6clips.llm import TitleWriterError
from gta6clips.postprocess import clean_title, has_credit, merge_hashtags, postprocess_video, with_credit
from gta6clips.streamers import PermissionDenied

from .conftest import FakeWriter


def _make_clips(ck, video_id="vid1"):
    """Queues a job in the fake and polls it until the fake finishes it and makes 2 clips."""
    job_id = ck.submit_url(f"https://www.youtube.com/watch?v={video_id}", {}).job_id
    for _ in range(5):
        if ck.job(job_id)["status"] == "done":
            return
    raise AssertionError("fake job never finished")


def test_credit_is_added_once(streamers):
    peanut = streamers[0]
    once = with_credit("Great moment.", peanut)
    twice = with_credit(once, peanut)
    assert once == twice
    assert has_credit(once, peanut)
    assert once.startswith("Great moment.")
    assert "https://www.youtube.com/@TheBurntPeanut" in once


def test_credit_survives_a_very_long_description(streamers):
    result = with_credit("x" * 6000, streamers[0])
    assert len(result) <= 5000 and has_credit(result, streamers[0])


def test_hashtags_required_first_no_duplicates():
    assert merge_hashtags(["#clip", "gta6", "#Shorts"], ["#GTA6", "#Shorts", "#TheBurntPeanut"]) == [
        "#GTA6", "#Shorts", "#TheBurntPeanut", "#clip"]


def test_postprocess_titles_credits_and_tags_every_clip(ck, ck_state, conn, cfg, streamers, writer):
    _make_clips(ck)
    outcomes = postprocess_video(ck, conn, cfg, streamers[0], "vid1", writer, source_title="GTA 6 day 1")

    assert len(outcomes) == 2 and all(o.status == "pending_review" for o in outcomes)
    for clip_id, body in ck_state.patches:
        assert body["title"].startswith("Title A")
        assert has_credit(body["description"], streamers[0])
        assert body["hashtags"][:4] == ["#GTA6", "#GTAVI", "#Shorts", "#TheBurntPeanut"]
    row = conn.execute("SELECT * FROM clips ORDER BY ck_clip_id").fetchone()
    assert json.loads(row["title_options"])[1:] == ["Title B", "Title C"]
    assert row["hook"] == "He did NOT expect that"
    # The transcript reached the title writer.
    assert "crashed the car" in writer.briefs[0].transcript


def test_long_clip_is_flagged_not_dropped(ck, ck_state, conn, cfg, streamers, writer):
    _make_clips(ck)
    outcomes = postprocess_video(ck, conn, cfg, streamers[0], "vid1", writer)
    long_one = [o for o in outcomes if o.notes]
    assert len(long_one) == 1 and "70s" in long_one[0].notes[0]


def test_second_run_skips_tracked_clips(ck, ck_state, conn, cfg, streamers, writer):
    _make_clips(ck)
    postprocess_video(ck, conn, cfg, streamers[0], "vid1", writer)
    again = postprocess_video(ck, conn, cfg, streamers[0], "vid1", writer)
    assert all(o.skipped for o in again)
    assert len(writer.briefs) == 2  # no extra Claude calls


def test_decided_clips_are_never_touched(ck, ck_state, conn, cfg, streamers, writer):
    _make_clips(ck)
    postprocess_video(ck, conn, cfg, streamers[0], "vid1", writer)
    conn.execute("UPDATE clips SET status = 'approved'")
    patches_before = len(ck_state.patches)
    again = postprocess_video(ck, conn, cfg, streamers[0], "vid1", writer, refresh=True)
    assert all(o.skipped and o.status == "approved" for o in again)
    assert len(ck_state.patches) == patches_before


def test_title_failure_keeps_clip_with_note(ck, ck_state, conn, cfg, streamers):
    _make_clips(ck)
    failing = FakeWriter(fail=TitleWriterError("Claude API error 500"))
    outcomes = postprocess_video(ck, conn, cfg, streamers[0], "vid1", failing)
    assert outcomes[0].title == "Draft title 0"
    assert any("Title options unavailable" in n for n in outcomes[0].notes)
    assert all(has_credit(body["description"], streamers[0]) for _, body in ck_state.patches)


def test_postprocess_refuses_unpermitted_streamer(ck, conn, cfg, streamers, writer):
    pete = next(s for s in streamers if not s.permission)
    with pytest.raises(PermissionDenied):
        postprocess_video(ck, conn, cfg, pete, "vid1", writer)


def test_clean_title_drops_hashtags():
    assert clean_title("WHAT is this?! #gta6 #gtavi #theburntpeanut") == "WHAT is this?!"
    assert clean_title("John Wins?! (Sings) #gta6") == "John Wins?! (Sings)"
    assert clean_title("No #1 fan #gta6") == "No #1 fan"
    assert clean_title("#gta6 #shorts") == ""


def test_titles_with_hashtags_are_cleaned_before_saving(ck, ck_state, conn, cfg, streamers):
    from gta6clips.llm import ClipsKittyTitles

    _make_clips(ck)
    for clip in ck_state.clips.values():
        clip["title"] = "WHAT is this?! #gta6 #gtavi #theburntpeanut"
    outcomes = postprocess_video(ck, conn, cfg, streamers[0], "vid1", ClipsKittyTitles())
    assert all(o.title == "WHAT is this?!" for o in outcomes)
    assert all(body["title"] == "WHAT is this?!" for _, body in ck_state.patches)
    assert all("#GTA6" in body["hashtags"] for _, body in ck_state.patches)
