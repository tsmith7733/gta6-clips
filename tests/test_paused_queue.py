import logging

from gta6clips.clipskitty.readiness import check_readiness
from gta6clips.vod import wait_for_job


def test_check_warns_when_queue_stopped(ck, ck_state, cfg):
    ck_state.paused = True
    r = check_readiness(ck, cfg)
    assert r.ok and any("Start queue" in w for w in r.warnings)


def test_wait_tells_owner_once_then_finishes_after_start(ck, ck_state, caplog):
    ck_state.paused = True
    job_id = ck.submit_url("https://www.youtube.com/watch?v=abc", {}).job_id
    polls = {"n": 0}

    def sleep(_):
        polls["n"] += 1
        if polls["n"] == 3:
            ck_state.paused = False  # the owner presses Start queue

    with caplog.at_level(logging.WARNING):
        job = wait_for_job(ck, job_id, poll_seconds=0, timeout_seconds=60, sleep=sleep)
    assert job["status"] == "done"
    assert sum("Start queue" in r.message for r in caplog.records) == 1
