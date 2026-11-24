"""Driving-fail compilations: Claude's verdicts, choosing the parts, queueing, sending and dropping."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from gta6clips import db
from gta6clips.compilation import CompilationSettings, Compiler, Part, plan_parts, stitch
from gta6clips.live.dispatch import Dispatcher
from gta6clips.llm import ClipBrief, TitleSuggestions
from gta6clips.llm.fails import FailVerdict, parse_verdict

PEANUT = "The Burnt Peanut"
T0 = 1_795_000_000.0


class FakeJudge:
    model = "fake-haiku"

    def __init__(self, verdicts: dict[int, FailVerdict]):
        self.verdicts = verdicts
        self.seen: list = []

    def judge(self, evidence):
        self.seen.append(evidence)
        clip = len(self.seen)
        return self.verdicts.get(clip, FailVerdict(False, 0, 0, 0, "", "nothing"))


class CompilationWriter:
    def __init__(self):
        self.briefs: list[ClipBrief] = []

    def write(self, brief: ClipBrief) -> TitleSuggestions:
        self.briefs.append(brief)
        return TitleSuggestions(titles=["Every Time Peanut Drove", "B", "C"], hook="He can't drive",
                                description="Six crashes in a row.")


def _fake_cut(parts, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"mp4" * 5000)
    return dest


def _setup(conn, workdir, n=7):
    """A live stream with n clips, each from its own recording, made just now."""
    stream = db.upsert_stream(conn, platform="youtube", video_id=f"live1@{int(T0)}", streamer=PEANUT,
                              url="https://www.youtube.com/watch?v=live1", title="GTA 6 day 5", start_utc=T0,
                              duration_s=None)
    ids = []
    for i in range(n):
        path = workdir / "windows" / f"live_live1_{i}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp4" * 5000)
        w = db.add_window(conn, streamer=PEANUT, source_url=str(path), ck_job_id=None, ck_video_id=f"v{i}",
                          state="collected", stream_id=stream["id"], start_utc=T0 + 600 * i, offset_start_s=600 * i,
                          offset_end_s=600 * i + 180)
        db.update_window(conn, w, local_path=str(path))
        db.upsert_clip(conn, ck_clip_id=500 + i, window_id=w, ck_video_id=f"v{i}", streamer=PEANUT,
                       status="pending_review", score=70, start_s=60.0, end_s=100.0, title=f"clip {i}",
                       title_options=[f"clip {i}"], hook="", description="", hashtags=[], notes=[])
        ids.append(500 + i)
    return ids


def _compiler(cfg, conn, ck, streamers, workdir, judge):
    cfg.data["compilations"]["streamers"] = [PEANUT, "No Permission Pete"]
    return Compiler(cfg=cfg, conn=conn, client=ck, streamers=streamers, judge=judge,
                    frames=lambda path, s, e, n: [(1.0, b"jpeg")], cut=_fake_cut, windows_dir=workdir / "windows")


def _fails(*strengths):
    """Clip order -> verdict: clips are checked newest first, so verdict 1 is the newest clip."""
    return {i + 1: FailVerdict(True, 10.0, 18.0, s, "rolled the car", f"he rolls the car {i}")
            for i, s in enumerate(strengths) if s}


# -- the pieces ---------------------------------------------------------------------------------

def test_verdicts_are_clamped_to_the_clip():
    v = parse_verdict(json.dumps({"is_fail": True, "start_s": -2, "end_s": 90, "strength": 14, "kind": "crash",
                                  "note": "he hits a bus"}), duration_s=40)
    assert (v.is_fail, v.start_s, v.end_s, v.strength) == (True, 0.0, 40.0, 10)
    v = parse_verdict(json.dumps({"is_fail": True, "start_s": 5, "end_s": 5.2, "strength": 7, "kind": "x",
                                  "note": ""}), duration_s=40)
    assert not v.is_fail and v.strength == 0      # under a second isn't a moment


def _part(clip, strength, start=10.0, end=20.0, path="a.mp4", abs_start=None, source="1"):
    return Part(clip_id=clip, path=Path(path), start_s=start, end_s=end, strength=strength, kind="crash",
                note="", source=source, abs_start=abs_start)


def test_parts_are_the_strongest_distinct_fails_within_a_minute():
    s = CompilationSettings(min_strength=6, max_fails=4, max_total_s=35, min_fails=3)
    parts = plan_parts([
        _part(1, 9, path="a.mp4"),
        _part(2, 8, path="a_wide1.mp4", start=12, end=22),   # the same moment, widened
        _part(3, 5, path="b.mp4"),                          # too weak
        _part(4, 7, path="c.mp4"),
        _part(5, 6, path="d.mp4"),
        _part(6, 8, path="e.mp4", abs_start=1000.0),
        _part(7, 6, path="f.mp4", abs_start=1005.0),         # overlaps 6 in the stream
    ], s)
    assert [p.clip_id for p in parts] == [1, 6, 4]           # 4 parts would be 40 s: the weakest goes
    # Never below min_fails: then every part is shortened instead.
    parts = plan_parts([_part(i, 8, path=f"{i}.mp4") for i in range(5)], replace(s, min_fails=5, max_fails=6))
    assert len(parts) == 5 and sum(p.seconds for p in parts) == pytest.approx(35)


# -- the whole flow -----------------------------------------------------------------------------

def test_fails_become_a_compilation_on_the_review_page(cfg, ck, ck_state, conn, streamers, workdir):
    _setup(conn, workdir)
    judge = FakeJudge(_fails(9, 0, 8, 7, 6, 6, 3))
    comp = _compiler(cfg, conn, ck, streamers, workdir, judge)
    run = comp.run_once()
    assert run.judged == 7 and run.fails == 6 and len(run.queued) == 1
    row = conn.execute("SELECT * FROM compilations").fetchone()
    parts = json.loads(row["parts"])
    assert [p["clip"] for p in parts] == [506, 504, 503, 502, 501]   # strongest first; 6+ only; the 3 left out
    assert parts[0]["start_s"] == pytest.approx(60 + 10 - 1) and parts[0]["end_s"] == pytest.approx(69 + 10)   # capped at max_part_seconds
    assert comp.run_once().queued == []                     # its fails are used; nothing is checked twice
    assert len(judge.seen) == 7

    writer = CompilationWriter()
    d = Dispatcher(cfg=cfg, client=ck, conn=conn, streamers=streamers, writer=None, compilation_writer=writer)
    for _ in range(4):
        d.tick()
    body = ck_state.submissions[-1]
    total = sum(p["end_s"] - p["start_s"] for p in parts)
    assert body["pipeline"]["settings"]["ranges"] == f"0.0-{total:.1f}" and body["max_clips"] == 1
    assert body["pipeline"]["settings"]["notes"].startswith("A 'driving fails' compilation of 5 moments")
    assert body["channel"] == "TheBurntPeanut"
    clip = db.clips_by_status(conn, "pending_review")
    clip = next(c for c in clip if c["ck_clip_id"] >= 800)
    assert clip["title"] == "Every Time Peanut Drove"
    assert clip["description"].startswith("Six crashes in a row.") and "Clip from The Burnt Peanut" in clip["description"]
    assert "1. rolled the car: he rolls the car 0" in writer.briefs[0].context
    assert any("compilation of clips 506" in n for n in json.loads(clip["notes"]))
    assert not any("Length" in n for n in json.loads(clip["notes"]))   # not held to the single-clip length


def test_too_few_fails_waits(cfg, ck, conn, streamers, workdir):
    _setup(conn, workdir, n=4)
    comp = _compiler(cfg, conn, ck, streamers, workdir, FakeJudge(_fails(9, 9, 9, 9)))
    assert comp.run_once().queued == []
    assert conn.execute("SELECT COUNT(*) FROM windows WHERE state = 'planned'").fetchone()[0] == 0


def test_dropping_a_fail_rebuilds_the_compilation_without_it(cfg, ck, ck_state, conn, streamers, workdir):
    _setup(conn, workdir)
    comp = _compiler(cfg, conn, ck, streamers, workdir, FakeJudge(_fails(9, 7, 8, 7, 6, 6, 6)))
    first = comp.run_once().queued[0]
    d = Dispatcher(cfg=cfg, client=ck, conn=conn, streamers=streamers, writer=None,
                   compilation_writer=CompilationWriter())
    for _ in range(4):
        d.tick()
    made = next(c for c in db.clips_by_status(conn, "pending_review") if c["ck_clip_id"] >= 800)
    second = comp.drop(506)
    assert second and second != first
    assert db.get_clip(conn, made["ck_clip_id"])["status"] == "rejected"
    parts = json.loads(conn.execute("SELECT parts FROM compilations WHERE id = ?", (second,)).fetchone()[0])
    assert 506 not in [p["clip"] for p in parts] and len(parts) == 6   # the next fail took its place
    db.set_clip_status(conn, made["ck_clip_id"], "pending_review")      # (an undo on the page)
    db.set_clip_status(conn, made["ck_clip_id"], "approved")
    conn.execute("UPDATE compilations SET window_id = ? WHERE id = ?", (made["window_id"], second))
    with pytest.raises(ValueError):
        comp.drop(parts[0]["clip"])                         # an approved compilation stays as it is


def test_only_permitted_streamers_are_compiled(cfg, ck, conn, streamers, workdir):
    comp = _compiler(cfg, conn, ck, streamers, workdir, FakeJudge({}))
    assert [s.name for s in comp._streamers()] == [PEANUT]


# -- ffmpeg -------------------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")


@needs_ffmpeg
def test_stitch_joins_the_parts(tmp_path):
    ffmpeg = Path(shutil.which("ffmpeg"))
    srcs = []
    for i, size in enumerate(("1280x720", "1920x1080")):    # recordings of different sizes line up
        src = tmp_path / f"rec{i}.mp4"
        subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:d=12",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-shortest", "-c:v", "libx264",
                        "-c:a", "aac", str(src)], check=True)
        srcs.append(src)
    parts = [_part(1, 9, start=2, end=6, path=str(srcs[0])), _part(2, 8, start=5, end=8, path=str(srcs[1]))]
    out = stitch(ffmpeg, parts, tmp_path / "comp.mp4", width=640, height=360)
    probe = subprocess.run([str(ffmpeg), "-i", str(out)], capture_output=True, text=True).stderr
    assert "640x360" in probe and "Audio" in probe
    hms = probe.split("Duration: ")[1].split(",")[0]
    h, m, s = hms.split(":")
    assert float(s) + 60 * int(m) == pytest.approx(7.0, abs=0.3)


# -- fails in finished streams (hunt) -----------------------------------------------------------

from gta6clips.crash_hunt import FailHunter, HuntSettings, crash_candidates
from gta6clips.sources import SourceInfo


def test_crash_candidates_need_crash_talk_not_just_laughs():
    chat = [(100 + i, "LMAO") for i in range(30)]                                      # only laughs
    chat += [(500 + i, m) for i, m in enumerate(["he crashed", "worst driver ever", "TOTALLED", "can't drive",
                                                 "KEKW", "#1 driver", "lmao"])]
    chat += [(540, "plane"), (2000, "nice car"), (2001, "crash"), (2002, "flip"), (2003, "wrecked")]   # weaker
    found = crash_candidates(chat, 3000, HuntSettings(min_crash_hits=3))
    assert [round(c.peak_s) for c in found] == [505, 2005]


class FakeFetcher:
    def chat(self, info, streamer):
        return [(600.0 + i, m) for i, m in enumerate(["CRASHED", "worst driver", "he flipped it", "can't drive"] * 2)]


def _vod_info(url):
    return SourceInfo(url=url, platform="youtube", video_id="vod1", title="GTA 6 | DAY 2 | #1 GTA 6 DRIVER",
                      description="", channel_name="TheBurntPeanut",
                      channel_values=("https://www.youtube.com/@TheBurntPeanut",), duration=7200.0,
                      start_utc=T0 - 7200, live_status="was_live")


def test_hunted_fails_join_the_clips_fails_in_a_best_of(cfg, ck, conn, streamers, workdir):
    _setup(conn, workdir, n=4)
    judge = FakeJudge({1: FailVerdict(True, 30.0, 38.0, 10, "missed the jump", "flies off the ramp into a bus")})
    hunter = FailHunter(cfg=cfg, conn=conn, streamers=streamers, judge=judge, fetcher=FakeFetcher(),
                        frames=lambda url, h, s, e, n: [(1.0, b"jpeg")], resolve=lambda info, fmt: ("http://low", {}),
                        probe_fn=_vod_info)
    result = hunter.hunt("https://www.youtube.com/watch?v=vod1")
    assert result.candidates == 1 and len(result.fails) == 1
    assert hunter.hunt("https://www.youtube.com/watch?v=vod1").candidates == 0     # never judged twice
    fetched = []

    def fetch(part, dest):
        fetched.append((part.source, part.start_s))
        return _fake_cut([], dest.with_suffix(".mp4"))

    comp = _compiler(cfg, conn, ck, streamers, workdir, FakeJudge(_fails(9, 8, 7, 6)))
    comp.fetch = fetch
    comp.run_once()
    assert comp.build(comp._streamers()[0]) is None                     # 4 clip fails: not enough alone
    comp_id = comp.build(comp._streamers()[0], include_vods=True)
    parts = json.loads(conn.execute("SELECT parts FROM compilations WHERE id = ?", (comp_id,)).fetchone()[0])
    assert parts[0]["label"].startswith("v") and parts[0]["strength"] == 10      # the best one leads
    assert parts[0]["start_s"] == pytest.approx(5.0)                    # within its HD download
    assert fetched == [("vod1", pytest.approx(605 - 45 + 30 - 1))]   # peak 605 s, 45 s looked at before it
    assert comp.drop(parts[0]["label"]) is None                          # without it: 4 fails, too few again


def test_top_five_shorts_share_out_the_best_fails(cfg, ck, conn, streamers, workdir):
    _setup(conn, workdir, n=16)
    strengths = [10, 10, 9, 9, 9, 8, 8, 8, 8, 7, 7, 7, 7, 6, 6, 6]
    comp = _compiler(cfg, conn, ck, streamers, workdir, FakeJudge(_fails(*strengths)))
    comp.judge_new(comp._streamers()[0], 99)
    made = comp.build_top(comp._streamers()[0], shorts=3, per=5)
    assert len(made) == 3
    rows = [conn.execute("SELECT kind, parts FROM compilations WHERE id = ?", (c,)).fetchone() for c in made]
    groups = [[p["strength"] for p in json.loads(r["parts"])] for r in rows]
    assert groups == [[10, 9, 8, 7, 7], [10, 9, 8, 7, 6], [9, 8, 8, 7, 6]]     # 1st, 4th, 7th... to the first
    assert all(r["kind"] == "top 5 driving fails" for r in rows)
    assert comp.build_top(comp._streamers()[0], shorts=3, per=5) == []         # one fail left: none more
