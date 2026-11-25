"""Studio stage 3, effects: glitch, colour split, black and white, vignette, colour pop, and a grade."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gta6clips.editing import PlanError, parse_plan, render
from gta6clips.editing.editor import check_points
from gta6clips.editing.plan import PLAN_SCHEMA
from gta6clips.editing.render import build_graph, build_shots, effect_filters, effect_ranges

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")


def _plan(**kw):
    base = {"keep": [], "zooms": [], "replays": [], "freezes": [], "texts": [], "flashes": [], "notes": []}
    base.update(kw)
    return base


def test_effects_and_grades_are_checked():
    plan = parse_plan(_plan(effects=[{"start": 2, "end": 3, "kind": "glitch"}], grade="punchy",
                            freezes=[{"at": 5, "seconds": 0.6, "label": "", "effect": "bw"}]), 20)
    assert plan.effects[0].kind == "glitch" and plan.grade == "punchy" and plan.freezes[0].effect == "bw"
    assert parse_plan(_plan(), 20).grade == "none"
    with pytest.raises(PlanError, match="effect must be one of"):
        parse_plan(_plan(effects=[{"start": 2, "end": 3, "kind": "sparkles"}]), 20)
    with pytest.raises(PlanError, match="grade must be"):
        parse_plan(_plan(grade="sepia"), 20)
    with pytest.raises(PlanError, match="at most 8 effects"):
        parse_plan(_plan(effects=[{"start": i, "end": i + 1, "kind": "pop"} for i in range(9)]), 20)
    assert {"effects", "grade"} <= set(PLAN_SCHEMA["required"])
    assert PLAN_SCHEMA["properties"]["freezes"]["items"]["properties"]["effect"]["enum"][:2] == ["", "glitch"]


def _all_required(node) -> bool:
    """No optional properties anywhere: they made the API's compiled grammar too large."""
    if isinstance(node, dict):
        if node.get("type") == "object" and set(node.get("required", [])) != set(node.get("properties", {})):
            return False
        return all(_all_required(v) for v in node.values())
    if isinstance(node, list):
        return all(_all_required(v) for v in node)
    return True


def test_the_schema_has_no_optional_fields_and_empty_values_mean_unset():
    from gta6clips.editing.plan import plan_schema

    assert _all_required(plan_schema(["boom"], ["beat"])) and _all_required(PLAN_SCHEMA)
    plan = parse_plan(_plan(zooms=[{"start": 1, "end": 2, "target": "cam", "scale": 2, "box": [], "shake": False,
                                    "style": "punch"}],
                            replays=[{"start": 2, "end": 4, "speed": 0.5, "target": "game", "scale": 1.3, "box": [],
                                      "label": "AGAIN", "after": -1, "effect": "", "sound": ""}],
                            freezes=[{"at": 5, "seconds": 0.5, "label": "", "push": False, "effect": "", "sound": ""}],
                            hook={"text": "", "seconds": 0}, music={"track": "", "volume": 0, "start": 0},
                            sounds=[], effects=[], grade="none"), 20)
    assert plan.zooms[0].box is None and plan.replays[0].after == 4 and not plan.replays[0].effect
    assert plan.hook is None and plan.music is None and not plan.freezes[0].effect


def test_effects_land_at_output_times_under_the_text(tmp_path):
    plan = parse_plan(_plan(keep=[[0, 5], [8, 20]], grade="cinematic",
                            effects=[{"start": 9, "end": 10, "kind": "rgb_split"}],
                            freezes=[{"at": 12, "seconds": 0.5, "label": "OOF", "effect": "bw"}],
                            replays=[{"start": 2, "end": 4, "speed": 0.5, "target": "game", "scale": 1.3,
                                      "label": "AGAIN", "effect": "glitch"}],
                            texts=[{"start": 14, "end": 16, "text": "no way", "position": "top", "size": "m"}]), 20)
    shots = build_shots(plan, 20)
    ranges = [(round(a, 2), round(b, 2), k) for a, b, k in effect_ranges(plan, shots)]
    # 0-4, the 4 s replay (4-8), 4-5 (8-9), then 8-12 lands at 9-13, the freeze at 13-13.5
    assert ranges == [(4.0, 8.0, "glitch"), (10.0, 11.0, "rgb_split"), (13.0, 13.5, "bw")]
    graph, _ = build_graph(plan, shots, tmp_path)
    assert graph.index("colorbalance") < graph.index("chromashift") < graph.index("drawtext")
    assert "rgbashift=rh=-16:bh=16:rv=3:enable='between(t\\,4.000\\,8.000)*lt(mod(t*12\\,1)\\,0.5)'" in graph
    assert "hue=s=0:enable='between(t\\,13.000\\,13.500)'" in graph
    whats = " | ".join(w for _, w in check_points(plan, 20))
    assert "the rgb split effect" in whats and "the bw effect" in whats


@needs_ffmpeg
def test_render_every_effect(tmp_path):
    ffmpeg = Path(shutil.which("ffmpeg"))
    src = tmp_path / "clip.mp4"
    subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:d=8",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=8:sample_rate=48000", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    kinds = ["glitch", "rgb_split", "bw", "vignette", "pop"]
    plan = parse_plan(_plan(grade="warm", effects=[{"start": i * 1.5, "end": i * 1.5 + 1, "kind": k}
                                                   for i, k in enumerate(kinds)]), 8)
    out = render(ffmpeg, src, plan, tmp_path / "edit.mp4", duration_s=8)
    probe = subprocess.run([str(ffmpeg), "-i", str(out)], capture_output=True, text=True).stderr
    assert "1080x1920" in probe
    for kind in kinds:
        assert effect_filters(kind, 0, 1)
