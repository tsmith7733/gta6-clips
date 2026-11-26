"""Studio stage 4, overlays: emoji, arrow and circle stickers, and the caption font."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gta6clips.editing import PlanError, parse_plan, render
from gta6clips.editing.editor import check_points
from gta6clips.editing.plan import EMOJI, PLAN_SCHEMA
from gta6clips.editing.render import (FONT_SIZES, TEXT_MAX_W, _fit_size, build_graph, build_shots, crop_for,
                                      find_font, sticker_runs)
from gta6clips.editing.stickers import arrow_direction, canvas_size, draw_frame, pop, write_frames

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")
needs_emoji = pytest.mark.skipif(not Path(r"C:\Windows\Fonts\seguiemj.ttf").is_file(), reason="no Segoe UI Emoji")


def _plan(**kw):
    base = {"keep": [], "zooms": [], "replays": [], "freezes": [], "texts": [], "flashes": [], "notes": []}
    base.update(kw)
    return base


def _sticker(**kw):
    s = {"start": 2, "end": 4, "kind": "arrow", "emoji": "", "x": 0.4, "y": 0.6, "size": "m"}
    s.update(kw)
    return s


def test_stickers_are_checked():
    plan = parse_plan(_plan(stickers=[_sticker(kind="emoji", emoji="skull"), _sticker(start=5, end=6)]), 20)
    assert [s.kind for s in plan.stickers] == ["emoji", "arrow"] and plan.stickers[0].emoji == "skull"
    assert plan.stickers[1].emoji == ""         # only emoji stickers keep a name
    with pytest.raises(PlanError, match="an emoji sticker names one of"):
        parse_plan(_plan(stickers=[_sticker(kind="emoji", emoji="unicorn")]), 20)
    with pytest.raises(PlanError, match="kind must be one of"):
        parse_plan(_plan(stickers=[_sticker(kind="star")]), 20)
    with pytest.raises(PlanError, match="fractions of the frame"):
        parse_plan(_plan(stickers=[_sticker(x=540)]), 20)
    with pytest.raises(PlanError, match="smooth zoom"):
        parse_plan(_plan(zooms=[{"start": 3, "end": 5, "target": "cam", "scale": 1.8, "style": "smooth"}],
                         stickers=[_sticker()]), 20)
    with pytest.raises(PlanError, match="pushed|push"):
        parse_plan(_plan(freezes=[{"at": 3, "seconds": 0.6, "label": "", "push": True}], stickers=[_sticker()]), 20)
    with pytest.raises(PlanError, match="at most 6 stickers"):
        parse_plan(_plan(stickers=[_sticker(start=i * 2, end=i * 2 + 1) for i in range(7)]), 20)
    # a punch zoom over a sticker is fine
    parse_plan(_plan(zooms=[{"start": 3, "end": 5, "target": "game", "scale": 1.4, "style": "punch"}],
                     stickers=[_sticker()]), 20)
    item = PLAN_SCHEMA["properties"]["stickers"]["items"]
    assert set(item["required"]) == {"start", "end", "kind", "emoji", "x", "y", "size"}
    assert item["properties"]["emoji"]["enum"] == ["", *EMOJI]


def test_stickers_follow_cuts_and_punch_ins_but_skip_replays():
    plan = parse_plan(_plan(keep=[[0, 5], [8, 20]],
                            zooms=[{"start": 10, "end": 12, "target": "game", "scale": 1.5, "style": "punch"}],
                            replays=[{"start": 9, "end": 11, "speed": 0.5, "target": "game", "scale": 1.0, "label": ""}],
                            stickers=[_sticker(start=4, end=11, x=0.5, y=0.62)]), 20)
    shots = build_shots(plan, 20)
    (run,) = sticker_runs(plan, shots)
    # 4-5 lands at 4-5, the cut skips 5-8, 8-10 lands at 5-7, then the punch-in from 10 (7-8), and the
    # replay after 11 isn't drawn on.
    spans = [(round(a, 2), round(b, 2)) for a, b, _, _ in run.pieces]
    assert spans == [(4.0, 7.0), (7.0, 8.0)]
    w, h, cx, cy = crop_for("game", 1.5, None, None)
    _, _, x, y = run.pieces[1]
    assert (round(x), round(y)) == (round((0.5 * 1080 - cx) * 1080 / w), round((0.62 * 1920 - cy) * 1920 / h))
    assert (run.start, run.end) == (4.0, 8.0)


def test_the_graph_overlays_each_piece(tmp_path, monkeypatch):
    import gta6clips.editing.stickers as stickers

    plan = parse_plan(_plan(keep=[[0, 5], [8, 20]], stickers=[_sticker(start=4, end=9), _sticker(start=12, end=13)],
                            texts=[{"start": 12, "end": 14, "text": "no way", "position": "top", "size": "m",
                                   "style": "plain"}]), 20)
    shots = build_shots(plan, 20)
    calls = []
    monkeypatch.setattr(stickers, "write_frames",
                        lambda s, seconds, folder, name, direction: calls.append((name, round(seconds, 2))) or
                        stickers.Frames(folder / name, f"{name}/f%04d.png", 1, 200))
    graph, _ = build_graph(plan, shots, tmp_path)
    assert calls == [("stk0", 2.0), ("stk1", 1.0)]
    assert "[1:v]format=rgba,setpts=PTS-STARTPTS+4.000/TB[sp0_0]" in graph
    assert "[2:v]format=rgba,setpts=PTS-STARTPTS+9.000/TB[sp1_0]" in graph
    assert graph.index("overlay=") < graph.index("drawtext")     # text stays on top of stickers
    assert graph.rstrip().endswith("[vout]") and "[sk2]" in graph
    whats = " | ".join(w for _, w in check_points(plan, 20))
    assert "the arrow sticker at x=0.40, y=0.60" in whats


def test_animation_curves_and_directions():
    assert pop(0) < 0.5 and max(pop(t / 100) for t in range(30)) > 1.1 and pop(1) == 1.0
    assert arrow_direction(100, 1500, 1080, 1920)[0] < 0        # point on the left: the arrow comes from the right
    assert arrow_direction(540, 1056, 1080, 1920) == (0.0, 1.0)  # in the middle: points down


def test_arrow_and_circle_frames(tmp_path):
    for kind in ("arrow", "circle"):
        s = parse_plan(_plan(stickers=[_sticker(kind=kind)]), 20).stickers[0]
        frames = write_frames(s, 0.5, tmp_path, kind, (0.6, 0.8))
        assert frames.count == 16 and len(list(frames.folder.glob("*.png"))) == 16
        img = draw_frame(s, 0.3, 2.0, None, (0.6, 0.8))
        assert img.size == (canvas_size(s), canvas_size(s)) and img.getbbox()
        assert draw_frame(s, 2.0, 2.0, None, (0.6, 0.8)).getchannel("A").getextrema()[1] == 0   # faded out


@needs_emoji
def test_emoji_frames_are_in_colour(tmp_path):
    s = parse_plan(_plan(stickers=[_sticker(kind="emoji", emoji="fire")]), 20).stickers[0]
    img = draw_frame(s, 0.5, 2.0, __import__("gta6clips.editing.stickers", fromlist=["x"])._emoji_image("fire", 210))
    colours = {c[:3] for _, c in img.getcolors(maxcolors=1 << 20) if c[3] > 200}
    assert len(colours) > 50


def test_captions_use_montserrat_and_fit_the_frame():
    from gta6clips.editing.render import WRAP, _wrap

    assert find_font().name == "Montserrat-Black.ttf"
    # 4 lines at 12 a line: wrapped wider instead of losing "TRAFFIC" (edit 46's hook)
    assert _wrap("Taxiing a plane through traffic", WRAP["l"]) == "TAXIING A\nPLANE THROUGH\nTRAFFIC"
    assert _fit_size("NO WAY", FONT_SIZES["l"]) == FONT_SIZES["l"]
    small = _fit_size("ABSOLUTELYUNBELIEVABLE", FONT_SIZES["l"])
    assert small < FONT_SIZES["l"]
    from PIL import ImageFont

    assert ImageFont.truetype(str(find_font()), small).getlength("ABSOLUTELYUNBELIEVABLE") <= TEXT_MAX_W


@needs_ffmpeg
def test_render_with_stickers(tmp_path):
    ffmpeg = Path(shutil.which("ffmpeg"))
    src = tmp_path / "clip.mp4"
    subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:d=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6:sample_rate=48000", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    plan = parse_plan(_plan(zooms=[{"start": 3, "end": 5, "target": "game", "scale": 1.4, "style": "punch"}],
                            stickers=[_sticker(start=1, end=2.5), _sticker(start=3, end=5, kind="circle", x=0.5, y=0.6)],
                            texts=[{"start": 1, "end": 3, "text": "look at him go", "position": "top", "size": "m"}]), 6)
    out = render(ffmpeg, src, plan, tmp_path / "edit.mp4", duration_s=6)
    probe = subprocess.run([str(ffmpeg), "-i", str(out)], capture_output=True, text=True).stderr
    assert "1080x1920" in probe and "Duration: 00:00:06" in probe
