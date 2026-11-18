import pytest

from gta6clips.streamers import PermissionDenied
from gta6clips.vod import JobFailed, NotReady, job_options, resume_job, run_vod

from .conftest import source

URL = "https://www.youtube.com/watch?v=CW_rULxbDs0"


def _run(ck, conn, cfg, streamers, writer, info=None, **kwargs):
    return run_vod(URL, cfg=cfg, client=ck, conn=conn, streamers=streamers, writer=writer,
                   probe_fn=lambda url: info or source(), sleep=lambda s: None, **kwargs)


def test_end_to_end_vod(ck, ck_state, conn, cfg, streamers, writer):
    result = _run(ck, conn, cfg, streamers, writer)

    sent = ck_state.submissions[0]
    assert sent["url"] == URL
    assert sent["gaming"] is True
    assert sent["pipeline"] == {"id": "colingpt9/gta-vi-highlights", "version": "0.4.1"}
    assert "#TheBurntPeanut" in sent["hashtags"]
    assert len(sent["focus"]) <= 600

    assert result.video_id == "CW_rULxbDs0" and len(result.clips) == 2
    window = conn.execute("SELECT * FROM windows").fetchone()
    assert window["state"] == "collected" and window["ck_video_id"] == "CW_rULxbDs0"
    assert conn.execute("SELECT COUNT(*) FROM clips WHERE status = 'pending_review'").fetchone()[0] == 2


def test_unpermitted_channel_never_reaches_clipskitty(ck, ck_state, conn, cfg, streamers, writer):
    with pytest.raises(PermissionDenied):
        _run(ck, conn, cfg, streamers, writer, info=source("@NoPermissionPete"))
    with pytest.raises(PermissionDenied):
        _run(ck, conn, cfg, streamers, writer, info=source("@Stranger"))
    assert ck_state.submissions == []
    assert conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 0


def test_not_ready_sends_nothing(ck, ck_state, conn, cfg, streamers, writer):
    ck_state.automation_enabled = True
    with pytest.raises(NotReady, match="automation"):
        _run(ck, conn, cfg, streamers, writer)
    assert ck_state.submissions == []


def test_failed_job_reports_log(ck, ck_state, conn, cfg, streamers, writer):
    ck_state.fail_jobs = True
    with pytest.raises(JobFailed, match="transcription crashed"):
        _run(ck, conn, cfg, streamers, writer)
    assert conn.execute("SELECT state FROM windows").fetchone()["state"] == "failed"


def test_timeout_leaves_job_resumable(ck, ck_state, conn, cfg, streamers, writer):
    ck_state.polls_until_done = 10_000
    cfg.data["clipskitty"]["job_timeout_hours"] = 0
    with pytest.raises(JobFailed, match="wait"):
        _run(ck, conn, cfg, streamers, writer)


def test_already_processed_collects_without_new_job(ck, ck_state, conn, cfg, streamers, writer):
    _run(ck, conn, cfg, streamers, writer)
    jobs_before = len(ck_state.jobs)
    result = _run(ck, conn, cfg, streamers, writer)
    assert len(ck_state.jobs) == jobs_before
    assert all(c.skipped for c in result.clips)


def test_no_wait_then_resume(ck, ck_state, conn, cfg, streamers, writer):
    queued = _run(ck, conn, cfg, streamers, writer, wait=False)
    assert not queued.waited and queued.job_id
    result = resume_job(queued.job_id, cfg=cfg, client=ck, conn=conn, streamers=streamers, writer=writer,
                        sleep=lambda s: None)
    assert len(result.clips) == 2


def test_resume_refuses_jobs_we_did_not_send(ck, conn, cfg, streamers, writer):
    with pytest.raises(PermissionDenied):
        resume_job(999, cfg=cfg, client=ck, conn=conn, streamers=streamers, writer=writer)


def test_job_options_force_flag(ck, ck_state, conn, cfg, streamers, writer):
    _run(ck, conn, cfg, streamers, writer)
    _run(ck, conn, cfg, streamers, writer, force=True)
    assert ck_state.submissions[-1]["force"] is True
    assert "force" not in job_options(cfg, streamers[0])
