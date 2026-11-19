import pytest

from gta6clips.streamers import (
    PermissionDenied,
    StreamerFileError,
    channel_keys,
    load_streamers,
    permitted_streamer_for,
    require_permission,
)


def test_permitted_streamer_passes_gate(streamers):
    peanut = permitted_streamer_for(streamers, "youtube", "https://www.youtube.com/@TheBurntPeanut")
    assert peanut.name == "The Burnt Peanut"


def test_streamer_without_permission_is_blocked(streamers):
    with pytest.raises(PermissionDenied, match="permission: true"):
        permitted_streamer_for(streamers, "youtube", "@NoPermissionPete")


def test_unknown_channel_is_blocked(streamers):
    with pytest.raises(PermissionDenied, match="not in streamers.yaml"):
        permitted_streamer_for(streamers, "youtube", "https://www.youtube.com/@SomeoneElse")


def test_same_handle_on_another_platform_is_blocked(streamers):
    with pytest.raises(PermissionDenied):
        permitted_streamer_for(streamers, "twitch", "https://www.twitch.tv/theburntpeanut")


def test_require_permission_rejects_false(streamers):
    pete = next(s for s in streamers if s.name == "No Permission Pete")
    with pytest.raises(PermissionDenied):
        require_permission(pete)


@pytest.mark.parametrize("value", ['"true"', "yes", "1", "null"])
def test_permission_must_be_a_real_boolean(tmp_path, value):
    f = tmp_path / "streamers.yaml"
    f.write_text(
        "streamers:\n  - {name: X, platform: youtube, channel_url: 'https://www.youtube.com/@x',"
        f" permission: {value}, credit: c}}\n", encoding="utf-8")
    if value == "yes":
        # YAML 1.1 reads bare yes as a boolean; PyYAML follows it, so this one is a real true.
        assert load_streamers(f)[0].permission is True
        return
    with pytest.raises(StreamerFileError, match="permission"):
        load_streamers(f)


def test_empty_credit_is_rejected(tmp_path):
    f = tmp_path / "streamers.yaml"
    f.write_text("streamers:\n  - {name: X, platform: youtube, channel_url: 'https://www.youtube.com/@x',"
                 " permission: true, credit: ''}\n", encoding="utf-8")
    with pytest.raises(StreamerFileError, match="credit"):
        load_streamers(f)


def test_channel_keys_normalise_urls_handles_and_ids():
    assert channel_keys("youtube", "https://www.youtube.com/@TheBurntPeanut/") == {"@theburntpeanut"}
    assert channel_keys("youtube", "@TheBurntPeanut") == {"@theburntpeanut"}
    assert channel_keys("youtube", "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv") == {
        "ucabcdefghijklmnopqrstuv"}
    assert channel_keys("twitch", "https://www.twitch.tv/SomeStreamer") == {"somestreamer"}


def test_hashtag_keeps_inner_capitals(streamers):
    assert streamers[0].hashtag == "#TheBurntPeanut"


def test_streamers_sorted_by_priority(streamers):
    assert [s.priority for s in streamers] == sorted(s.priority for s in streamers)


def test_repo_streamers_file_is_valid():
    from gta6clips.config import REPO_ROOT

    assert load_streamers(REPO_ROOT / "streamers.yaml")


def test_clipskitty_creator_name(tmp_path):
    from gta6clips.streamers import load_streamers
    path = tmp_path / "streamers.yaml"
    path.write_text("""
streamers:
  - {name: The Burnt Peanut, platform: youtube, channel_url: "https://www.youtube.com/@TheBurntPeanut",
     permission: true, credit: c}
  - {name: Some One, platform: twitch, channel_url: "https://www.twitch.tv/someone", permission: true, credit: c}
  - {name: Over Ride, platform: youtube, channel_url: "https://www.youtube.com/@x", permission: true, credit: c,
     clipskitty_creator: "Custom Name"}
""", encoding="utf-8")
    names = [s.clipskitty_creator for s in load_streamers(path)]
    assert names == ["TheBurntPeanut", "someone", "Custom Name"]
