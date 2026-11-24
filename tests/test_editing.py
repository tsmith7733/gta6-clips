"""The editor's engine: plans, shots, output times, loud moments and a real render."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gta6clips.editing import PlanError, loud_moments, parse_plan, render
from gta6clips.editing.render import build_graph, build_shots, crop_for, out_time

CAM = [0.0, 0.094, 0.38, 0.21]     # where Clips Kitty's circle_cam puts the webcam


def _plan(**kw):
    base = {"keep": [], "zooms": [], "replays": [], "freezes": [], "texts": [], "flashes": [], "notes": []}
    base.update(kw)
    return base


def test_plans_are_checked_against_the_clip():
    with pytest.raises(PlanError):
        parse_plan(_plan(zooms=[{"start": 5, "end": 9, "target": "cam", "scale": 5}]), 30)        # too much zoom
    with pytest.raises(PlanError):
        parse_plan(_plan(zooms=[{"start": 5, "end": 9, "target": "cam", "scale": 2},
                                {"start": 8, "end": 10, "target": "game", "scale": 1.5}]), 30)    # overlapping
    with pytest.raises(PlanError):
        parse_plan(_plan(replays=[{"start": 1, "end": 12, "speed": 0.5, "target": "game", "scale": 1.3,
                                   "label": "x"}]), 30)                                          # 11 s replay
    with pytest.raises(PlanError):
        parse_plan(_plan(texts=[{"start": 40, "end": 45, "text": "hi", "position": "top", "size": "m"}]), 30)
    p = parse_plan(_plan(hook={"text": "He did NOT see that coming 💀 at all, no way, never", "seconds": 9},
                         texts=[{"start": 2, "end": 4, "text": "😂", "position": "top", "size": "m"}]), 30)
    assert p.hook.text == "He did NOT see that coming at all, no" or len(p.hook.text) <= 40
    assert p.hook.seconds == 4.0 and p.texts == ()       # capped; an emoji-only text is dropped


def test_shots_split_at_zooms_and_insert_replays_and_freezes():
    plan = parse_plan(_plan(keep=[[0, 10], [14, 20]],
                            zooms=[{"start": 4, "end": 6, "target": "cam", "scale": 2.0, "shake": True}],
                            replays=[{"start": 4, "end": 6, "speed": 0.5, "target": "game", "scale": 1.3,
                                      "label": "RUN IT BACK"}],
                            freezes=[{"at": 15, "seconds": 0.5, "label": "BRUH"}]), 20)
    shots = build_shots(plan, 20, CAM)
    kinds = [(s.kind, s.a, s.b, s.replay, s.shake) for s in shots]
    assert kinds == [("play", 0, 4, False, False), ("play", 4, 6, False, True), ("play", 4, 6, True, False),
                     ("play", 6, 10, False, False), ("play", 14, 15, False, False),
                     ("freeze", 15, 15, False, False), ("play", 15, 20, False, False)]
    assert shots[2].seconds == pytest.approx(4.0)        # 2 s at half speed
    # Output times: the replay (4 s) is inserted at 6 s, the 10-14 s gap cut, the freeze adds 0.5 s.
    assert out_time(shots, 5) == pytest.approx(5)
    assert out_time(shots, 8) == pytest.approx(12)
    assert out_time(shots, 12) == pytest.approx(14)        # cut away: the next kept moment
    assert out_time(shots, 16) == pytest.approx(16.5)
    w, h, x, y = shots[1].crop
    assert (w, h) == (540, 960) and x == 0                 # 2x on the webcam, kept inside the frame


def test_the_graph_draws_texts_at_output_times(tmp_path):
    plan = parse_plan(_plan(keep=[[0, 10], [14, 20]], texts=[{"start": 15, "end": 17, "text": "no way",
                                                              "position": "bottom", "size": "l"}],
                            flashes=[16], hook={"text": "Watch this", "seconds": 2}), 20)
    graph, files = build_graph(plan, build_shots(plan, 20, CAM), tmp_path)
    assert "between(t\\,11.000\\,13.000)" in graph       # 15-17 s lands at 11-13 s once 10-14 s is cut
    assert "between(t\\,0.000\\,2.000)" in graph          # the hook
    assert "drawbox" in graph and len(files) == 2
    assert (tmp_path / files[0]).read_text(encoding="utf-8") == "NO WAY"


def test_crop_targets():
    assert crop_for("game", 1.5, None, None) == (720, 1280, 180, 550)
    assert crop_for("box", 2.0, (0.6, 0.8, 0.2, 0.2), CAM) == (540, 960, 486, 960)   # y pushed back inside


def test_loud_moments_find_the_yell():
    values = [-16.0] * 40
    values[20:23] = [-10.0, -9.0, -11.0]           # a yell at 5-5.75 s
    values[30] = -11.0                              # a short bang: too short
    found = loud_moments(values, 0.25)
    assert len(found) == 1 and found[0].peak == pytest.approx(5.375) and found[0].over_db == 7.0


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")


@needs_ffmpeg
def test_render_a_full_edit(tmp_path):
    ffmpeg = Path(shutil.which("ffmpeg"))
    src = tmp_path / "clip.mp4"
    subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:d=12",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=12:sample_rate=96000", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    plan = parse_plan(_plan(keep=[[0, 5], [7, 12]],
                            zooms=[{"start": 2, "end": 4, "target": "cam", "scale": 2.0, "shake": True}],
                            replays=[{"start": 2, "end": 4, "speed": 0.5, "target": "game", "scale": 1.3,
                                      "label": "RUN IT BACK"}],
                            freezes=[{"at": 8, "seconds": 0.5, "label": "BRUH"}],
                            texts=[{"start": 9, "end": 11, "text": "no way", "position": "bottom", "size": "m"}],
                            flashes=[3], hook={"text": "Watch this", "seconds": 2}), 12)
    out = render(ffmpeg, src, plan, tmp_path / "edit.mp4", duration_s=12, cam_box=CAM)
    probe = subprocess.run([str(ffmpeg), "-i", str(out)], capture_output=True, text=True).stderr
    assert "1080x1920" in probe and "Audio" in probe
    hms = probe.split("Duration: ")[1].split(",")[0]
    seconds = float(hms.split(":")[2]) + 60 * int(hms.split(":")[1])
    assert seconds == pytest.approx(10 + 4 + 0.5, abs=0.3)   # kept 10 s, replay 4 s, freeze 0.5 s
