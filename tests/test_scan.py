from dataclasses import replace
from pathlib import Path

import pytest

from gta6clips import db
from gta6clips.downloads import DownloadError
from gta6clips.scan import ScanError, run_scan
from gta6clips.streamers import PermissionDenied, require_permission
from gta6clips.vod import NotGta6, NotReady

from .conftest import source

URL = "https://www.youtube.com/watch?v=CW_rULxbDs0"
HOUR = 3600.0
START_UTC = 1_790_000_000.0


def chat_with_bursts(*at: float, text: str = "KEKW CLIP IT") -> list[tuple[float, str]]:
    steady = [(t * 5.0, "nice") for t in range(int(HOUR / 5))]
    bursts = [(a + i * 0.25, text) for a in at for i in range(40)]
    return sorted(steady + bursts)


class FakeFetcher:
    def __init__(self, chat=None, loudness=None, cut_fails=False):
        self._chat = chat_with_bursts(1200, 2400) if chat is None else chat
        self._loudness = loudness
        self.cut_fails = cut_fails
        self.calls: list[str] = []
        self.cuts: list[tuple[float, float]] = []

    def chat(self, info, streamer):
        require_permission(streamer)
        self.calls.append("chat")
        return None if self._chat == "none" else self._chat

    def loudness(self, info, streamer, bin_s):
        require_permission(streamer)
        self.calls.append("loudness")
        if self._loudness == "fail":
            raise DownloadError("no audio")
        return self._loudness or [-30.0 + (i % 2) for i in range(int(HOUR / bin_s))]

    def cut(self, info, streamer, start_s, end_s, dest: Path) -> Path:
        require_permission(streamer)
        self.calls.append("cut")
        if self.cut_fails:
            raise DownloadError("network hiccup")
        self.cuts.append((start_s, end_s))
        out = dest.with_suffix(".mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake mp4")
        return out


def vod_info(**changes):
    return replace(source(), **{"duration": HOUR, "start_utc": START_UTC, "live_status": "was_live", **changes})


@pytest.fixture
def fetcher():
    return FakeFetcher()


def _scan(ck, conn, cfg, streamers, writer, fetcher, workdir, info=None, **kwargs):
    return run_scan(URL, cfg=cfg, client=ck, conn=conn, streamers=streamers, writer=writer, fetcher=fetcher,
                    windows_dir=workdir / "data" / "windows", probe_fn=lambda url: info or vod_info(),
                    sleep=lambda s: None, **kwargs)


def test_scan_sends_only_the_spike_windows(ck, ck_state, conn, cfg, streamers, writer, fetcher, workdir):
    result = _scan(ck, conn, cfg, streamers, writer, fetcher, workdir)

    assert len(result.windows) == 2
    assert len(fetcher.cuts) == 2
    for (start, end), peak in zip(fetcher.cuts, (1200, 2400)):
        assert start < peak - 60 and end > peak + 30 and end - start <= 240

    assert len(ck_state.submissions) == 2
    sent = ck_state.submissions[0]
    assert sent["path"].endswith(".mp4") and Path(sent["path"]).is_absolute()
    assert sent["channel"] == "TheBurntPeanut"   # Clips Kitty's creator name, for the remembered layout
    assert sent["title"].startswith("GTA 6 first day") and "[0:" in sent["title"]
    assert sent["max_clips"] == 2
    assert sent["pipeline"] == {"id": "colingpt9/gta-vi-highlights", "version": "0.4.1"}
    assert sent["gaming"] is True and "#TheBurntPeanut" in sent["hashtags"]

    assert all(w.state == "collected" for w in result.windows)
    assert sum(len(w.clips) for w in result.windows) == 4
    row = db.windows_for_stream(conn, result.stream_id)[0]
    assert row["start_utc"] == pytest.approx(START_UTC + row["offset_start_s"])
    assert "chat" in row["reasons"]
    assert conn.execute("SELECT COUNT(*) FROM clips WHERE status = 'pending_review'").fetchone()[0] == 4


def test_unpermitted_channel_downloads_nothing(ck, ck_state, conn, cfg, streamers, writer, fetcher, workdir):
    for handle in ("@NoPermissionPete", "@Stranger"):
        with pytest.raises(PermissionDenied):
            _scan(ck, conn, cfg, streamers, writer, fetcher, workdir, info=vod_info(
                channel_values=(f"https://www.youtube.com/{handle}", handle)))
    assert fetcher.calls == [] and ck_state.submissions == []
    assert conn.execute("SELECT COUNT(*) FROM streams").fetchone()[0] == 0


def test_not_gta_is_refused_before_downloading(ck, conn, cfg, streamers, writer, fetcher, workdir):
    with pytest.raises(NotGta6):
        _scan(ck, conn, cfg, streamers, writer, fetcher, workdir, info=vod_info(title="Age of Empires 2"))
    assert fetcher.calls == []


def test_live_streams_are_refused(ck, conn, cfg, streamers, writer, fetcher, workdir):
    with pytest.raises(ScanError, match="live"):
        _scan(ck, conn, cfg, streamers, writer, fetcher, workdir, info=vod_info(live_status="is_live"))
    assert fetcher.calls == []


def test_not_ready_downloads_nothing(ck, ck_state, conn, cfg, streamers, writer, fetcher, workdir):
    ck_state.auto_upload = True
    with pytest.raises(NotReady):
        _scan(ck, conn, cfg, streamers, writer, fetcher, workdir)
    assert fetcher.calls == []


def test_dry_run_plans_without_cutting_or_sending(ck, ck_state, conn, cfg, streamers, writer, fetcher, workdir):
    ck_state.automation_enabled = True  # a dry run doesn't need Clips Kitty at all
    result = _scan(ck, conn, cfg, streamers, writer, fetcher, workdir, dry_run=True)
    assert result.planned_only and len(result.windows) == 2
    assert "cut" not in fetcher.calls and ck_state.submissions == []
    assert conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 0


def test_rerun_reuses_signals_and_sends_nothing_twice(ck, ck_state, conn, cfg, streamers, writer, fetcher,
                                                      workdir):
    _scan(ck, conn, cfg, streamers, writer, fetcher, workdir)
    calls = list(fetcher.calls)
    again = _scan(ck, conn, cfg, streamers, writer, fetcher, workdir)
    assert fetcher.calls == calls          # chat and audio came from our database
    assert len(ck_state.submissions) == 2
    assert again.windows == []             # everything was already collected


def test_full_queue_leaves_windows_planned_for_the_next_run(ck, ck_state, conn, cfg, streamers, writer,
                                                             fetcher, workdir):
    ck_state.queue_full = True
    first = _scan(ck, conn, cfg, streamers, writer, fetcher, workdir)
    assert first.queue_full
    assert [r["state"] for r in db.windows_for_stream(conn, first.stream_id)] == ["planned", "planned"]

    ck_state.queue_full = False
    second = _scan(ck, conn, cfg, streamers, writer, fetcher, workdir)
    assert [w.state for w in second.windows] == ["collected", "collected"]
    assert fetcher.calls.count("cut") == 2   # the first window's file was kept, not cut again


def test_failed_cut_is_retried(ck, ck_state, conn, cfg, streamers, writer, workdir):
    broken = FakeFetcher(cut_fails=True)
    first = _scan(ck, conn, cfg, streamers, writer, broken, workdir)
    assert [w.state for w in first.windows] == ["failed", "failed"] and ck_state.submissions == []
    second = _scan(ck, conn, cfg, streamers, writer, FakeFetcher(), workdir)
    assert [w.state for w in second.windows] == ["collected", "collected"]


def test_no_wait_then_rerun_collects(ck, ck_state, conn, cfg, streamers, writer, fetcher, workdir):
    queued = _scan(ck, conn, cfg, streamers, writer, fetcher, workdir, wait=False)
    assert not queued.waited and [w.state for w in queued.windows] == ["submitted", "submitted"]
    done = _scan(ck, conn, cfg, streamers, writer, fetcher, workdir)
    assert [w.state for w in done.windows] == ["collected", "collected"]
    assert len(ck_state.submissions) == 2


def test_no_chat_replay_scores_on_audio(ck, ck_state, conn, cfg, streamers, writer, workdir):
    loud = [-30.0 + (i % 2) for i in range(int(HOUR / 5))]
    for i in range(500, 504):
        loud[i] = -6.0
    result = _scan(ck, conn, cfg, streamers, writer, FakeFetcher(chat="none", loudness=loud), workdir)
    assert result.chat_messages is None and result.audio
    assert len(result.windows) == 1


def test_nothing_to_score_on(ck, conn, cfg, streamers, writer, workdir):
    with pytest.raises(ScanError, match="nothing to score"):
        _scan(ck, conn, cfg, streamers, writer, FakeFetcher(chat="none", loudness="fail"), workdir)


def test_spoiler_chat_holds_only_the_clip_it_surrounds(ck, ck_state, conn, cfg, streamers, writer, workdir):
    # Spoiler chat at 1200-1210 s. The window starts at 1080 s; the fake's clips are at
    # 100-130 s (stream 1180-1210, next to the chat) and 160-230 s (stream 1240-1310, after it).
    chat = chat_with_bursts(1200, text="SPOILERS the ending omg")
    result = _scan(ck, conn, cfg, streamers, writer, FakeFetcher(chat=chat), workdir)
    assert 1075 <= result.windows[0].start_s <= 1090
    assert [c.status for c in result.windows[0].clips] == ["spoiler_hold", "pending_review"]
    assert "chat messages" in result.windows[0].clips[0].notes[0]
