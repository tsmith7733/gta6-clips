from gta6clips.cli import make_title_writer
from gta6clips.llm import ClaudeTitleWriter, ClipBrief, ClipsKittyTitles
from gta6clips.postprocess import has_credit, postprocess_video


def test_titles_off_by_default_needs_no_key(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cfg.get("llm.enabled") is False
    assert isinstance(make_title_writer(cfg), ClipsKittyTitles)


def test_titles_on_without_key_is_a_setup_error(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg.data["llm"]["enabled"] = True
    assert make_title_writer(cfg) is None


def test_titles_on_with_key_uses_claude(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    cfg.data["llm"]["enabled"] = True
    assert isinstance(make_title_writer(cfg), ClaudeTitleWriter)


def test_clipskitty_titles_keep_draft_and_fall_back_to_streamer_name():
    brief = ClipBrief("The Burnt Peanut", "", "  Car flip into the ocean ", "", "", 30)
    assert ClipsKittyTitles().write(brief).titles == ["Car flip into the ocean"]
    empty = ClipBrief("The Burnt Peanut", "", "", "", "", 30)
    assert ClipsKittyTitles().write(empty).titles == ["The Burnt Peanut GTA 6 moment"]


def test_free_mode_still_credits_and_tags(ck, ck_state, conn, cfg, streamers):
    job_id = ck.submit_url("https://www.youtube.com/watch?v=vid1", {}).job_id
    while ck.job(job_id)["status"] != "done":
        pass
    outcomes = postprocess_video(ck, conn, cfg, streamers[0], "vid1", ClipsKittyTitles())
    assert [o.title for o in outcomes] == ["Draft title 0", "Draft title 1"]
    for _, body in ck_state.patches:
        assert has_credit(body["description"], streamers[0])
        assert "#TheBurntPeanut" in body["hashtags"]


def test_streamer_named_title_comes_first_when_it_fits():
    writer = ClipsKittyTitles(suffix=" | {streamer} GTA 6")
    brief = ClipBrief("The Burnt Peanut", "", "Soccer Practice Emergency?!", "", "", 30)
    assert writer.write(brief).titles == ["Soccer Practice Emergency?! | The Burnt Peanut GTA 6",
                                          "Soccer Practice Emergency?!"]
    named = ClipBrief("The Burnt Peanut", "", "The Burnt Peanut robs a store", "", "", 30)
    assert writer.write(named).titles == ["The Burnt Peanut robs a store"]           # already named
    long = ClipBrief("The Burnt Peanut", "", "x" * 90, "", "", 30)
    assert writer.write(long).titles == ["x" * 90]                                    # over 100 chars


def test_config_turns_on_the_streamer_suffix(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert make_title_writer(cfg).suffix == " | {streamer} GTA 6"
