import pytest

from gta6clips.vod import NotGta6, run_vod

from .conftest import source

URL = "https://www.youtube.com/watch?v=CW_rULxbDs0"
AOE = "🔴LIVE | AOE 2 | GETTING DRUNK ON MONDAY | ME AND JOHN VS HARD BOTS"


def _run(ck, conn, cfg, streamers, writer, title, **kwargs):
    return run_vod(URL, cfg=cfg, client=ck, conn=conn, streamers=streamers, writer=writer,
                   probe_fn=lambda url: source(title=title), sleep=lambda s: None, **kwargs)


def test_non_gta_video_is_not_sent(ck, ck_state, conn, cfg, streamers, writer):
    with pytest.raises(NotGta6, match="AOE 2"):
        _run(ck, conn, cfg, streamers, writer, AOE)
    assert ck_state.submissions == []


def test_override_sends_it_anyway(ck, ck_state, conn, cfg, streamers, writer):
    _run(ck, conn, cfg, streamers, writer, AOE, allow_non_gta=True)
    assert len(ck_state.submissions) == 1


@pytest.mark.parametrize("title", ["GTA 6 DAY ONE", "first time in gta vi!!", "Grand Theft Auto VI ragdolls"])
def test_gta_titles_pass(ck, ck_state, conn, cfg, streamers, writer, title):
    _run(ck, conn, cfg, streamers, writer, title)
    assert len(ck_state.submissions) == 1
