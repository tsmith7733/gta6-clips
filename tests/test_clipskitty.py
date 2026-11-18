import pytest

from gta6clips.clipskitty import ClipsKittyClient, JobRefused, QueueFull
from gta6clips.clipskitty.readiness import check_readiness


def test_refuses_non_loopback_address():
    with pytest.raises(ValueError, match="127.0.0.1"):
        ClipsKittyClient("http://192.168.1.20:8765")


def test_new_job_returns_id(ck):
    result = ck.submit_url("https://www.youtube.com/watch?v=abc", {})
    assert result.created and result.job_id


def test_already_processed_is_not_an_error(ck, ck_state):
    ck_state.processed.add("abc")
    result = ck.submit_url("https://www.youtube.com/watch?v=abc", {})
    assert not result.created
    assert result.already_processed and result.video_id == "abc"


def test_already_queued_returns_existing_job(ck):
    first = ck.submit_url("https://www.youtube.com/watch?v=abc", {})
    second = ck.submit_url("https://www.youtube.com/watch?v=abc", {})
    assert second.already_queued and second.queued_job_id == first.job_id


def test_queue_full_raises(ck, ck_state):
    ck_state.queue_full = True
    with pytest.raises(QueueFull):
        ck.submit_url("https://www.youtube.com/watch?v=abc", {})


def test_missing_plugin_refusal_is_surfaced(ck):
    with pytest.raises(JobRefused, match="isn't installed"):
        ck.submit_url("https://www.youtube.com/watch?v=abc", {"pipeline": {"id": "someone/else"}})


def test_patch_clip_sends_only_given_fields(ck, ck_state):
    ck_state.clips[5] = {"id": 5, "video_id": "v", "title": "t", "description": "d", "hashtags": [],
                         "lines": []}
    ck.patch_clip(5, description="new")
    assert ck_state.patches == [(5, {"description": "new"})]


def test_readiness_ok(ck, cfg):
    assert check_readiness(ck, cfg).ok


def test_readiness_blocks_automation(ck, ck_state, cfg):
    ck_state.automation_enabled = True
    r = check_readiness(ck, cfg)
    assert not r.ok and "automation" in r.problems[0]


def test_readiness_blocks_auto_upload(ck, ck_state, cfg):
    ck_state.auto_upload = True
    assert any("auto upload" in p for p in check_readiness(ck, cfg).problems)


def test_readiness_warns_about_public_default(ck, ck_state, cfg):
    ck_state.privacy = "public"
    r = check_readiness(ck, cfg)
    assert r.ok and any("public" in w for w in r.warnings)


def test_readiness_needs_plugin_at_pinned_version(ck, ck_state, cfg):
    ck_state.plugins[0]["version"] = "0.5.0"
    assert any("pins" in p for p in check_readiness(ck, cfg).problems)
    ck_state.plugins.clear()
    assert any("isn't installed" in p for p in check_readiness(ck, cfg).problems)


def test_readiness_reports_failed_blocking_preflight(ck, ck_state, cfg):
    ck_state.preflight_ok = False
    assert any("Pick a model" in p for p in check_readiness(ck, cfg).problems)


def test_readiness_warns_on_other_version(ck, ck_state, cfg):
    ck_state.app_version = "2.2.0"
    assert any("2.2.0" in w for w in check_readiness(ck, cfg).warnings)
