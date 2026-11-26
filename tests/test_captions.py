"""Studio stage 5: animated caption styles and highlighted words."""

from __future__ import annotations

import pytest

from gta6clips.editing import PlanError, parse_plan
from gta6clips.editing.captions import (YELLOW, animated, canvas_height, draw_frame, layout, motion, word_step,
                                        write_frames)
from gta6clips.editing.plan import PLAN_SCHEMA, TEXT_STYLES, Text
from gta6clips.editing.render import H, build_graph, build_shots, text_overlays


def _plan(**kw):
    base = {"keep": [], "zooms": [], "replays": [], "freezes": [], "texts": [], "flashes": [], "notes": []}
    base.update(kw)
    return base


def _text(**kw):
    t = {"start": 2, "end": 4, "text": "he did it again", "position": "top", "size": "m", "style": "pop",
         "highlight": ""}
    t.update(kw)
    return t


def test_styles_and_highlights_are_checked():
    plan = parse_plan(_plan(texts=[_text(style="slam", highlight="again, nope"), _text(start=5, end=7, style="")],
                            hook={"text": "Watch the cop", "seconds": 2, "style": "words", "highlight": "cop"}), 20)
    assert plan.texts[0].style == "slam" and plan.texts[0].highlight == "AGAIN"   # "nope" isn't in the text
    assert plan.texts[1].style == "pop"                                          # unset: pop
    assert (plan.hook.style, plan.hook.highlight) == ("words", "COP")
    with pytest.raises(PlanError, match="text style must be one of"):
        parse_plan(_plan(texts=[_text(style="spin")]), 20)
    item = PLAN_SCHEMA["properties"]["texts"]["items"]
    assert item["properties"]["style"]["enum"] == list(TEXT_STYLES) and "highlight" in item["required"]
    assert set(PLAN_SCHEMA["properties"]["hook"]["required"]) == {"text", "seconds", "style", "highlight"}


def test_labels_slam_and_pop_and_only_plain_texts_use_drawtext(tmp_path, monkeypatch):
    import gta6clips.editing.captions as captions

    plan = parse_plan(_plan(freezes=[{"at": 3, "seconds": 0.6, "label": "BRUH"}],
                            texts=[_text(start=8, end=10, style="plain", text="so calm"), _text(start=12, end=14, style="words")],
                            hook={"text": "Watch this", "seconds": 2, "style": "pop", "highlight": ""}), 20)
    shots = build_shots(plan, 20)
    styles = {t.text: t.style for _, _, t in text_overlays(plan, shots)}
    assert styles == {"BRUH": "slam", "so calm": "plain", "he did it again": "words", "Watch this": "pop"}
    calls = []
    monkeypatch.setattr(captions, "write_frames", lambda t, seconds, folder, name: calls.append(name) or
                        captions.TextFrames(folder / name, f"{name}/f%04d.png", 1, 500))
    graph, files = build_graph(plan, shots, tmp_path)
    assert sorted(calls) == ["txt0", "txt1", "txt2"] and len(files) == 1          # the plain one is drawtext
    assert "[1:v]format=rgba" in graph and "overlay=x=0:y=500" in graph
    assert graph.index("overlay=x=0") < graph.index("drawtext")
    assert not animated(Text(0, 1, "x")) and animated(Text(0, 1, "x", highlight="x"))


def test_layout_matches_the_drawtext_places():
    top = layout(Text(0, 1, "no way", "top", "l"))
    assert abs(top.cy - top.block_h / 2 - H * 0.34) < 1
    bottom = layout(Text(0, 1, "taxiing a plane through traffic", "bottom", "l"))
    assert len({y for _, _, y, _ in bottom.words}) == 3                   # three lines, as drawtext wraps it
    assert abs(bottom.cy + bottom.block_h / 2 - H * 0.70) < 1
    xs = [x for _, x, _, _ in top.words]
    assert xs[0] < 540 < xs[1]                                             # centred


def test_motion_curves():
    assert motion("pop", 0)[0] < 0.5 and motion("pop", 1)[0] == 1.0
    assert motion("slam", 0)[0] == pytest.approx(2.2) and motion("slam", 1.0) == (1.0, 0.0, 0.0)
    assert any(abs(motion("shake", t / 30)[1]) > 4 for t in range(30))
    assert motion("plain", 0.1) == (1.0, 0.0, 0.0)
    assert word_step(4, 3.0) == pytest.approx(0.18) and word_step(6, 1.0) == pytest.approx(0.06)


def test_frames_words_and_yellow(tmp_path):
    t = Text(0, 2, "he did it again", "middle", "l", style="words", highlight="again")
    lay = layout(t)
    first = draw_frame(t, lay, 0.0, 2.0)
    done = draw_frame(t, lay, 1.5, 2.0)
    assert first.size == (1080, canvas_height(lay))
    # word by word: the first frame has only the first word in it, small
    assert first.getbbox()[2] - first.getbbox()[0] < (done.getbbox()[2] - done.getbbox()[0]) / 3
    yellow = sum(n for n, c in done.getcolors(1 << 20) if c[:3] == YELLOW[:3] and c[3] == 255)
    assert yellow > 500
    assert draw_frame(t, lay, 2.0, 2.0).getchannel("A").getextrema()[1] == 0     # faded out
    frames = write_frames(Text(0, 1, "no way", style="slam"), 0.5, tmp_path, "txt0")
    assert frames.count == 16 and len(list(frames.folder.glob("*.png"))) == 16
    assert frames.y == round(layout(Text(0, 1, "no way")).cy - canvas_height(layout(Text(0, 1, "no way"))) / 2)
