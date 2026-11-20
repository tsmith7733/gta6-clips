"""Phase 3: live selection, live chat, the recorder, and the watch loop end to end (with fakes)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from gta6clips import db
from gta6clips.chat import ChatFileTail, parse_youtube_chat_utc
from gta6clips.live.recorder import Segment, SegmentRecorder, cut_segments
from gta6clips.live.select import LiveSelector, LiveSettings
from gta6clips.live.session import LiveSession
from gta6clips.live.watcher import Watcher
from gta6clips.live.youtube import LiveCheck
from gta6clips.scoring import ScoringSettings, WindowSettings
from gta6clips.streamers import PermissionDenied
from gta6clips.vod import NotReady

KEYWORDS = ("kekw", "clip it")
SCORING = ScoringSettings(keywords=KEYWORDS)
WINDOWS = WindowSettings()
LIVE = LiveSettings()
START = 1_800_000_000.0
PEANUT = "https://www.youtube.com/@TheBurntPeanut"


# -- the selection rules ------------------------------------------------------------------

def steady(duration: float, every: float = 5.0) -> list[tuple[float, str]]:
    return [(i * every, "nice") for i in range(int(duration / every))]


def burst(at: float, n: int = 40, text: str = "KEKW CLIP IT") -> list[tuple[float, str]]:
    return [(at + i * 0.25, text) for i in range(n)]


def loud(duration: float, *peaks: float) -> list[float]:
    bins = [-30.0 + (i % 3) for i in range(int(duration / SCORING.bin_s))]
    for p in peaks:
        for k in range(int(p / SCORING.bin_s), int(p / SCORING.bin_s) + 3):
            bins[k] = -8.0
    return bins


def replay(chat, loudness, duration: float, live: LiveSettings = LIVE, step: float = 30.0) -> LiveSelector:
    chat = sorted(chat)
    sel = LiveSelector(SCORING, WINDOWS, live)
    t = step
    while t <= duration:
        sel.update(chat=[m for m in chat if m[0] < t], loudness=loudness[: int(t / SCORING.bin_s)],
                   recorded_until_s=t)
        t += step
    return sel


def test_a_clear_moment_is_sent_once_its_tail_is_recorded():
    chat = steady(2400) + burst(1500)
    sel = LiveSelector(SCORING, WINDOWS, LIVE)
    early = sel.update(chat=[m for m in chat if m[0] < 1530], loudness=loud(1530, 1500), recorded_until_s=1530)
    assert not [d for d in early if d.sent]          # the 60 s after the peak isn't recorded yet
    sel = replay(chat, loud(2400, 1500), 2400)
    assert len(sel.sent) == 1
    w = sel.sent[0].window
    assert 1495 <= w.peak_s <= 1515 and w.end_s - w.start_s == 180


def test_one_signal_alone_is_not_enough():
    """A chat flood with no hype words and nothing on stream (a raid saying hi) isn't sent."""
    raid = burst(1500, n=60, text="hi from the raid")
    sel = replay(steady(2400) + raid, loud(2400), 2400, live=replace(LIVE, threshold=4.5))
    assert not sel.sent
    assert any("only 1 signal" in d.why for d in sel.decided)


def test_nothing_is_sent_while_the_baseline_is_learned():
    sel = replay(steady(1200) + burst(120), loud(1200, 120), 1200)
    assert not sel.sent


def test_a_weaker_spike_next_to_a_stronger_one_is_dropped():
    # The stronger one comes before the weaker one's tail is recorded, so the weaker one waits and is dropped.
    chat = steady(3000) + burst(1500, n=20, text="nice") + burst(1545, n=60)
    sel = replay(chat, loud(3000, 1500, 1545), 3000)
    assert len(sel.sent) == 1 and abs(sel.sent[0].peak_s - 1545) < 20


def test_hourly_budget_lets_only_stronger_moments_through():
    at = [900, 1700, 2500, 3300]   # four strong moments inside one hour, the last the strongest
    chat = steady(4200) + [m for a in at[:3] for m in burst(a, text="nice")] + burst(3300)
    sel = replay(chat, loud(4200, *at), 4200, live=replace(LIVE, max_per_hour=2))
    sent = [round(d.peak_s / 100) for d in sel.sent]
    assert sent == [9, 17, 33]     # 2500 was over budget; 3300 is stronger than both, so it goes


def test_windows_never_repeat_footage():
    chat = steady(3000) + burst(1500, n=30) + burst(1640, n=80)
    sel = replay(chat, loud(3000, 1500, 1640), 3000, live=replace(LIVE, min_gap_s=60))
    windows = sorted((d.window for d in sel.sent), key=lambda w: w.start_s)
    assert len(windows) == 2
    assert windows[1].start_s >= windows[0].end_s


# -- live chat ------------------------------------------------------------------------------

def _live_line(utc: float, text: str) -> str:
    """yt-dlp's live (not replay) chat line: offset from the first message, the real time in timestampUsec."""
    return json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"message": {"runs": [{"text": text}]},
                                        "timestampUsec": str(int(utc * 1e6))}}}}]},
        "videoOffsetTimeMsec": "0", "isLive": True})


def test_live_chat_uses_the_message_time():
    lines = [_live_line(START + 2, "second"), _live_line(START + 1, "first"), "junk"]
    assert parse_youtube_chat_utc(lines) == [(START + 1, "first"), (START + 2, "second")]


def test_chat_tail_reads_only_new_whole_lines(tmp_path):
    part = tmp_path / "v.live_chat.json.part"
    final = tmp_path / "v.live_chat.json"
    tail = ChatFileTail(part, final)
    assert tail.read_new() == []
    line = _live_line(START, "hello")
    part.write_text(line + "\n" + line[:20], encoding="utf-8")       # the second line is half written
    assert [t for _, t in tail.read_new()] == ["hello"]
    with part.open("a", encoding="utf-8") as f:
        f.write(line[20:] + "\n")
    part.rename(final)                                                 # yt-dlp finished the file
    assert [t for _, t in tail.read_new()] == ["hello"]
    assert tail.read_new() == []


# -- the recorder (real ffmpeg) --------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")
TEST_SOURCE = ["-re", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=15", "-f", "lavfi", "-i",
               "sine=frequency=440:sample_rate=44100", "-map", "0:v", "-map", "1:a"]
TEST_CODECS = ["-c:v", "libx264", "-preset", "ultrafast", "-g", "15", "-c:a", "aac", "-t", "7"]


@needs_ffmpeg
def test_recorder_makes_timed_pieces_and_cuts_a_window(tmp_path):
    ffmpeg = Path(shutil.which("ffmpeg"))
    rec = SegmentRecorder(ffmpeg=ffmpeg, folder=tmp_path / "seg", inputs=lambda: TEST_SOURCE,
                          codec_args=TEST_CODECS, segment_s=2)
    rec.start()
    rec.proc.wait(timeout=30)     # the test source ends after 7 s
    pieces = rec.poll() + rec.stop()
    assert len(pieces) >= 3
    assert all(p.path.is_file() and p.path.name.split(".")[0].split("_")[0].isdigit() for p in pieces)
    for a, b in zip(pieces, pieces[1:]):
        assert abs(b.start_utc - a.end_utc) < 0.2      # one continuous clock
    out = cut_segments(ffmpeg, pieces, pieces[0].start_utc + 1, pieces[0].start_utc + 5, tmp_path / "w.mp4")
    probe = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(out)], capture_output=True, text=True)
    duration = probe.stderr.split("Duration: ")[1].split(",")[0]
    h, m, s = duration.split(":")
    assert 3.0 <= float(s) <= 5.5 and "Audio" in probe.stderr and "Video" in probe.stderr


def test_recorder_retries_when_the_stream_url_fails(tmp_path):
    now = [START]
    calls = []

    def inputs():
        calls.append(now[0])
        raise RuntimeError("stream not ready")

    rec = SegmentRecorder(ffmpeg=Path(sys.executable), folder=tmp_path, inputs=inputs, clock=lambda: now[0])
    rec.start()
    assert rec.proc is None and len(calls) == 1
    rec.poll()
    assert len(calls) == 1          # waits before trying again
    now[0] += 6
    rec.poll()
    assert len(calls) == 2
    now[0] += 6
    rec.poll()
    assert len(calls) == 2          # and waits longer after each failure


# -- the watch loop, with fakes -------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = START

    def __call__(self) -> float:
        return self.t


class FakeRecorder:
    def __init__(self, clock: Clock, folder: Path, seg_s: float = 60.0):
        self.clock, self.folder, self.seg_s = clock, folder, seg_s
        self.next = None
        self.stopped = False

    def start(self):
        self.next = self.clock()

    def poll(self):
        out = []
        while self.next is not None and not self.stopped and self.next + self.seg_s <= self.clock():
            path = self.folder / f"{int(self.next)}.ts"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"ts")
            out.append(Segment(path, self.next, self.next + self.seg_s))
            self.next += self.seg_s
        return out

    def stop(self):
        out = self.poll()
        self.stopped = True
        return out


class FakeChat:
    def __init__(self, clock: Clock, messages: list[tuple[float, str]]):
        self.clock, self.messages, self.given = clock, sorted(messages), 0

    def start(self):
        pass

    def read_new(self):
        out = []
        while self.given < len(self.messages) and self.messages[self.given][0] <= self.clock():
            out.append(self.messages[self.given])
            self.given += 1
        return out

    def stop(self):
        return self.read_new()


def _loudness_for(moment_utc: float):
    def measure(path: Path) -> list[float]:
        seg_start = float(path.stem)
        bins = []
        for i in range(12):
            t = seg_start + i * 5
            bins.append(-8.0 if moment_utc <= t < moment_utc + 15 else -30.0 + (i % 3))
        return bins
    return measure


def _fake_cut(segments, start_utc, end_utc, dest: Path) -> Path:
    assert any(s.start_utc < end_utc and s.end_utc > start_utc for s in segments)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"mp4")
    return dest


def _check(title="GTA 6 | NEW ZEALAND", vid="live1", handle="@TheBurntPeanut"):
    return LiveCheck("live", video_id=vid, url=f"https://www.youtube.com/watch?v={vid}", title=title,
                     channel_values=(f"https://www.youtube.com/{handle}", handle))


class World:
    """A fake YouTube: what each channel's /live says, plus the chat and loudness of the live stream."""

    def __init__(self, workdir: Path, clock: Clock, moment_after_s: float = 1500.0):
        self.workdir, self.clock = workdir, clock
        self.live: dict[str, LiveCheck] = {}
        self.checks: list[str] = []
        self.made: list[str] = []
        self.moment = START + moment_after_s
        steady_chat = [(START + i * 5.0, "nice") for i in range(1200)]
        self.chat = steady_chat + [(self.moment + 2 + i * 0.25, "KEKW CLIP IT") for i in range(40)]
        self.recorders: list[FakeRecorder] = []

    def check_live(self, channel_url: str) -> LiveCheck:
        self.checks.append(channel_url)
        return self.live.get(channel_url, LiveCheck("offline"))

    def factory(self, conn):
        def make(streamer, check):
            self.made.append(streamer.name)
            rec = FakeRecorder(self.clock, self.workdir / "rec" / check.video_id)
            self.recorders.append(rec)
            return LiveSession(conn=conn, streamer=streamer, check=check, recorder=rec,
                               chat=FakeChat(self.clock, self.chat), measure=_loudness_for(self.moment),
                               cut=_fake_cut, windows_dir=self.workdir / "windows", scoring=SCORING,
                               windows=WINDOWS, live=LIVE)
        return make


def _watcher(cfg, ck, conn, streamers, writer, world: World, clock: Clock, **kw) -> Watcher:
    cfg.data["live"]["poll_seconds"] = 150
    return Watcher(cfg=cfg, client=ck, conn=conn, streamers=streamers, writer=writer,
                   make_session=world.factory(conn), check_live=world.check_live, clock=clock, **kw)


def _run(watcher: Watcher, clock: Clock, seconds: float, step: float = 15.0):
    end = clock.t + seconds
    while clock.t < end:
        watcher.step()
        clock.t += step


def test_live_gta_stream_is_recorded_clipped_and_credited(cfg, ck, ck_state, conn, streamers, writer, workdir):
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check()
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    w.preflight()
    _run(w, clock, 1800)

    assert world.made == ["The Burnt Peanut"]
    sent = ck_state.submissions
    assert len(sent) == 1
    body = sent[0]
    assert body["channel"] == "TheBurntPeanut" and "[live " in body["title"]
    assert body["pipeline"]["id"] == "colingpt9/gta-vi-highlights" and body["max_clips"] == 2
    rows = db.live_windows(conn, ("collected",))
    assert len(rows) == 1
    peak = START + 1500
    assert rows[0]["start_utc"] <= peak <= rows[0]["end_utc"]
    clips = db.clips_by_status(conn, "pending_review")
    assert clips and all("Clip from The Burnt Peanut" in c["description"] for c in clips)
    assert not db.clips_by_status(conn, "approved")          # nothing is approved or published by us


def test_only_permitted_streamers_are_watched(cfg, ck, conn, streamers, writer, workdir):
    clock = Clock()
    world = World(workdir, clock)
    world.live["https://www.youtube.com/@NoPermissionPete"] = _check(handle="@NoPermissionPete", vid="pete")
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    _run(w, clock, 300)
    assert world.made == []
    assert "https://www.youtube.com/@NoPermissionPete" not in world.checks


def test_a_live_link_pointing_at_another_channel_is_not_recorded(cfg, ck, conn, streamers, writer, workdir):
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check(handle="@NoPermissionPete")
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    _run(w, clock, 300)
    assert world.made == []


def test_session_refuses_a_streamer_without_permission(conn, streamers, workdir):
    pete = next(s for s in streamers if not s.permission)
    with pytest.raises(PermissionDenied):
        LiveSession(conn=conn, streamer=pete, check=_check(), recorder=FakeRecorder(Clock(), workdir), chat=None,
                    measure=lambda p: [], cut=_fake_cut, windows_dir=workdir, scoring=SCORING, windows=WINDOWS,
                    live=LIVE)


def test_other_games_are_not_recorded_unless_allowed(cfg, ck, conn, streamers, writer, workdir):
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check(title="Rob Run with John")
    _run(_watcher(cfg, ck, conn, streamers, writer, world, clock), clock, 300)
    assert world.made == []
    _run(_watcher(cfg, ck, conn, streamers, writer, world, clock, allow_non_gta=True), clock, 300)
    assert world.made == ["The Burnt Peanut"]


def test_recording_stops_after_two_misses_but_not_on_a_failed_check(cfg, ck, conn, streamers, writer, workdir):
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check()
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    _run(w, clock, 300)
    assert "The Burnt Peanut" in w.sessions
    world.live[PEANUT] = LiveCheck("unknown", error="network")
    _run(w, clock, 450)
    assert "The Burnt Peanut" in w.sessions           # couldn't tell: keeps recording
    world.live[PEANUT] = LiveCheck("offline")
    _run(w, clock, 150)
    assert "The Burnt Peanut" in w.sessions           # one miss
    _run(w, clock, 150)
    assert "The Burnt Peanut" not in w.sessions and world.recorders[0].stopped


def test_watch_refuses_when_clips_kitty_automation_is_on(cfg, ck, ck_state, conn, streamers, writer, workdir):
    ck_state.automation_enabled = True
    clock = Clock()
    w = _watcher(cfg, ck, conn, streamers, writer, World(workdir, clock), clock)
    with pytest.raises(NotReady):
        w.run(stop_after_s=1)


def test_windows_wait_while_the_queue_is_full(cfg, ck, ck_state, conn, streamers, writer, workdir):
    ck_state.queue_full = True
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check()
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    _run(w, clock, 1800)
    assert [r["state"] for r in db.live_windows(conn, ("planned", "submitted", "collected"))] == ["planned"]
    ck_state.queue_full = False
    _run(w, clock, 600)
    assert len(db.live_windows(conn, ("collected",))) == 1


def test_empty_plugin_result_goes_to_clipskitty_finder_once(cfg, ck, ck_state, conn, streamers, writer, workdir):
    ck_state.plugin_finds_nothing = True
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check()
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    _run(w, clock, 2400)
    first, second = ck_state.submissions
    assert first["pipeline"]["id"] == "colingpt9/gta-vi-highlights"
    assert "pipeline" not in second and second["force"] is True and second["focus"] == first["focus"]
    rows = db.live_windows(conn, ("collected",))
    assert len(rows) == 1 and rows[0]["finder"] == "clipskitty"
    clips = db.clips_by_status(conn, "pending_review")
    assert clips and all("Clip from The Burnt Peanut" in c["description"] for c in clips)


def test_clipskitty_finder_finding_nothing_is_not_retried_again(cfg, ck, ck_state, conn, streamers, writer, workdir):
    ck_state.plugin_finds_nothing = True
    ck_state.clips_per_video = 0
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check()
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    _run(w, clock, 3000)
    assert len(ck_state.submissions) == 2
    assert len(db.live_windows(conn, ("collected",))) == 1


def test_no_fallback_when_turned_off(cfg, ck, ck_state, conn, streamers, writer, workdir):
    cfg.data["clipskitty"]["fallback_to_clipskitty_finder"] = False
    ck_state.plugin_finds_nothing = True
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check()
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    _run(w, clock, 2400)
    assert len(ck_state.submissions) == 1
    assert len(db.live_windows(conn, ("collected",))) == 1


def test_a_failed_render_is_sent_again_after_a_pause(cfg, ck, ck_state, conn, streamers, writer, workdir):
    ck_state.render_fails = 1
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check()
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    _run(w, clock, 1800)
    row = db.live_windows(conn, ("planned",))[0]
    assert row["render_retries"] == 1 and row["not_before"] > clock.t and "_retry1" in row["local_path"]
    assert len(ck_state.submissions) == 1                    # not resent before the pause is over
    _run(w, clock, 600)
    first, second = ck_state.submissions
    assert second["pipeline"] == first["pipeline"]           # same finder, new file
    assert second["path"] != first["path"] and second["path"].endswith("_retry1.mp4")
    rows = db.live_windows(conn, ("collected",))
    assert len(rows) == 1 and db.clips_by_status(conn, "pending_review")


def test_render_retries_stop_and_say_so(cfg, ck, ck_state, conn, streamers, writer, workdir, caplog):
    cfg.data["windows"]["render_retries"] = 1
    ck_state.render_fails = 5
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check()
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    with caplog.at_level("ERROR"):
        _run(w, clock, 3000)
    assert len(ck_state.submissions) == 2
    assert len(db.live_windows(conn, ("collected",))) == 1
    assert any("restart Clips Kitty" in r.getMessage() for r in caplog.records)


def test_janitor_deletes_old_pieces(cfg, ck, conn, streamers, writer, workdir):
    clock = Clock()
    world = World(workdir, clock)
    world.live[PEANUT] = _check()
    cfg.data["live"]["retention_hours"] = 0.25
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    _run(w, clock, 2400)
    files = sorted((workdir / "rec" / "live1").glob("*.ts"))
    oldest = min(float(p.stem) for p in files)
    assert oldest >= clock.t - 0.25 * 3600 - 600 - 60    # janitor runs every 10 min
    deleted = conn.execute("SELECT COUNT(*) FROM segments WHERE deleted = 1").fetchone()[0]
    assert deleted > 0
