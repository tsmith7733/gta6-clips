"""Studio stage 2, motion: zoom styles, speed sections and ramps, transitions, a push on a freeze."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gta6clips.editing import PlanError, parse_plan, render
from gta6clips.editing.editor import check_points
from gta6clips.editing.plan import PLAN_SCHEMA
from gta6clips.editing.render import build_graph, build_shots, motion_filter, out_time, speed_at

CAM = [0.0, 0.094, 0.38, 0.21]
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")


def _plan(**kw):
    base = {"keep": [], "zooms": [], "replays": [], "freezes": [], "texts": [], "flashes": [], "notes": []}
    base.update(kw)
    return base


def test_motion_moves_are_checked():
    plan = parse_plan(_plan(keep=[[0, 9], [12, 20]],
                            zooms=[{"start": 2, "end": 4, "target": "cam", "scale": 2, "style": "smooth"}],
                            speeds=[{"start": 14, "end": 17, "speed": 0.5, "ramp": True}],
                            transitions=[{"at": 9.3, "kind": "whip"}]), 20)
    assert plan.zooms[0].style == "smooth" and plan.speeds[0].ramp
    assert plan.transitions[0].at == 9                       # snapped onto the cut
    assert parse_plan(_plan(zooms=[{"start": 2, "end": 4, "scale": 2}]), 20).zooms[0].style == "punch"
    with pytest.raises(PlanError, match="zoom style"):
        parse_plan(_plan(zooms=[{"start": 2, "end": 4, "scale": 2, "style": "spin"}]), 20)
    with pytest.raises(PlanError, match="slow motion"):
        parse_plan(_plan(speeds=[{"start": 2, "end": 4, "speed": 1.1}]), 20)
    with pytest.raises(PlanError, match="overlap"):
        parse_plan(_plan(speeds=[{"start": 2, "end": 5, "speed": 2}, {"start": 4, "end": 6, "speed": 0.5}]), 20)
    with pytest.raises(PlanError, match="goes on a cut"):
        parse_plan(_plan(keep=[[0, 9], [12, 20]], transitions=[{"at": 15, "kind": "zoom"}]), 20)
    with pytest.raises(PlanError, match="goes on a cut"):
        parse_plan(_plan(transitions=[{"at": 5, "kind": "zoom"}]), 20)        # no cuts at all
    assert {"speeds", "transitions"} <= set(PLAN_SCHEMA["required"])
    assert PLAN_SCHEMA["properties"]["zooms"]["items"]["properties"]["style"]["enum"] == ["punch", "smooth", "push"]


def test_speed_sections_ramp_and_move_output_times():
    plan = parse_plan(_plan(speeds=[{"start": 4, "end": 8, "speed": 0.5, "ramp": True},
                                    {"start": 10, "end": 14, "speed": 2.0, "ramp": False}]), 20)
    assert speed_at(plan, 6) == 0.5 and speed_at(plan, 12) == 2.0 and speed_at(plan, 2) == 1.0
    ramp = [speed_at(plan, 4.0 + 0.075 + 0.15 * k) for k in range(3)]
    assert ramp == sorted(ramp, reverse=True) and 1.0 > ramp[0] and ramp[-1] > 0.5   # easing down toward 0.5
    assert speed_at(plan, 7.95) == ramp[0]                                 # and back up at the end
    shots = build_shots(plan, 20)
    total = sum(s.seconds for s in shots)
    ramped = total - 4 - 2 - 2 - 6                                          # 0-4, 8-10, 10-14 at 2x, 14-20
    assert 4 < ramped < 4 / 0.5                                             # 4 s of clip, slowed less at its ends
    assert out_time(shots, 12) == pytest.approx(total - 6 - 1)            # 2 s into the 2x part: 1 s of output
    assert out_time(shots, 3) == pytest.approx(3)


def test_transitions_and_zoom_styles_make_moving_shots():
    plan = parse_plan(_plan(keep=[[0, 9], [12, 20]],
                            zooms=[{"start": 2, "end": 4, "target": "cam", "scale": 2, "style": "smooth"},
                                   {"start": 5, "end": 7, "target": "cam", "scale": 2, "style": "punch"},
                                   {"start": 14, "end": 18, "target": "game", "scale": 1.4, "style": "push"}],
                            freezes=[{"at": 19, "seconds": 0.6, "label": "", "push": True}],
                            transitions=[{"at": 9, "kind": "whip"}]), 20)
    shots = build_shots(plan, 20, CAM)
    smooth = next(s for s in shots if s.a == 2)
    punch = next(s for s in shots if s.a == 5)
    assert smooth.zoom and smooth.crop is None and smooth.center == pytest.approx((0.19, 0.199))
    assert punch.zoom is None and punch.crop and not punch.moving
    assert next(s for s in shots if s.b == 9).out_fx == "whip" and next(s for s in shots if s.a == 12).in_fx == "whip"
    assert next(s for s in shots if s.kind == "freeze").push
    f = motion_filter(next(s for s in shots if s.b == 9))
    assert f.startswith("scale=2160:3840,zoompan=") and "dblur" in f and "s=1080x1920" in f
    graph, _ = build_graph(plan, shots, Path("."))
    assert graph.count("zoompan") == 5                     # smooth, push, whip out, whip in, the freeze's push
    assert "fps=30,scale=2160:3840,zoompan" in graph       # frames first, then the zoom
    whats = " | ".join(w for _, w in check_points(plan, 20, CAM))
    assert "smooth zoom on the cam" in whats and "end of a slow push-in" in whats and "punch-in on the cam" in whats


@needs_ffmpeg
def test_render_with_motion(tmp_path):
    ffmpeg = Path(shutil.which("ffmpeg"))
    src = tmp_path / "clip.mp4"
    subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:d=12",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=12:sample_rate=48000", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    plan = parse_plan(_plan(keep=[[0, 5], [7, 12]],
                            zooms=[{"start": 1, "end": 3, "target": "cam", "scale": 2, "style": "smooth",
                                    "shake": True}],
                            speeds=[{"start": 8, "end": 10, "speed": 0.5, "ramp": False},
                                    {"start": 10, "end": 12, "speed": 4.0, "ramp": False}],
                            freezes=[{"at": 7.5, "seconds": 0.5, "label": "", "push": True}],
                            transitions=[{"at": 5, "kind": "zoom"}]), 12)
    out = render(ffmpeg, src, plan, tmp_path / "edit.mp4", duration_s=12, cam_box=CAM)
    probe = subprocess.run([str(ffmpeg), "-i", str(out)], capture_output=True, text=True).stderr
    assert "1080x1920" in probe
    hms = probe.split("Duration: ")[1].split(",")[0]
    # 5 s + 1 s (7-8) + the 0.5 s freeze + 4 s (8-10 at half speed) + 0.5 s (10-12 at 4x)
    assert float(hms.split(":")[2]) == pytest.approx(11.0, abs=0.15)
