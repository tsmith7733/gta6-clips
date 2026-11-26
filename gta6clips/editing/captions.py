"""Animated captions (studio stage 5): texts that pop in, slam down, come in word by word or shake,
with chosen words in yellow.

A styled text is drawn with Pillow as a run of transparent PNG frames the width of the video, which
ffmpeg overlays at the text's place, the same way as the stickers. A "plain" text without a
highlighted word is still drawn by ffmpeg's drawtext. Same font, sizes, wrapping, border and shadow
either way, so a caption looks the same whichever path draws it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .plan import Text
from .stickers import _pil, fade, pop

FPS = 30
YELLOW = (255, 221, 0, 255)
WHITE = (255, 255, 255, 255)
SLAM_S = 0.14                     # the drop from big to its size
SLAM_SHAKE_S = 0.3                # then a short shake as it lands
WORD_STEP_S = 0.18                # words style: one word every this, or faster for long texts


def _norm(word: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", word.upper())


def animated(t: Text) -> bool:
    """Whether the text is drawn as frames (any style but plain, or a highlighted word)."""
    return t.style != "plain" or bool(t.highlight)


@lru_cache(maxsize=64)
def _font(size: int):
    from .render import find_font

    _, _, _, ImageFont = _pil()
    return ImageFont.truetype(str(find_font()), size)


@dataclass(frozen=True)
class Layout:
    size: int                                     # font size
    words: tuple[tuple[str, float, float, bool], ...]   # (word, centre x, centre y, highlighted), output pixels
    cy: float                                     # the block's centre, output pixels
    block_h: float


def layout(t: Text) -> Layout:
    """Where each word of the caption goes: wrapped and sized like drawtext's, at the same place."""
    from .render import FONT_SIZES, H, W, WRAP, _fit_size, _wrap

    wrapped = _wrap(t.text, WRAP[t.size])
    size = _fit_size(wrapped, FONT_SIZES[t.size])
    font = _font(size)
    lines = wrapped.split("\n")
    asc, desc = font.getmetrics()
    step = asc + desc + round(size * 0.08) - size * 0.2
    block_h = step * len(lines)
    cy = {"top": H * 0.34 + block_h / 2, "middle": H / 2, "bottom": H * 0.70 - block_h / 2}[t.position]
    marked = {_norm(w) for w in t.highlight.split()} - {""}
    space = font.getlength(" ")
    words = []
    for i, line in enumerate(lines):
        y = cy + (i - (len(lines) - 1) / 2) * step
        parts = line.split()
        widths = [font.getlength(w) for w in parts]
        x = (W - (sum(widths) + space * (len(parts) - 1))) / 2
        for w, wd in zip(parts, widths):
            words.append((w, x + wd / 2, y, _norm(w) in marked))
            x += wd + space
    return Layout(size, tuple(words), cy, block_h)


def canvas_height(lay: Layout) -> int:
    return int(lay.block_h * 2.4 + 160) // 2 * 2       # room for a slam's start at 2.2x


def word_step(count: int, seconds: float) -> float:
    """Words style: the gap between words, so the last one is in by 30% of the text's time."""
    return min(WORD_STEP_S, 0.3 * seconds / max(1, count - 1))


def motion(style: str, t: float) -> tuple[float, float, float]:
    """The whole caption's scale and offset (x, y in pixels) t seconds in."""
    if style == "pop":
        return pop(t), 0.0, 0.0
    if style == "slam":
        if t < SLAM_S:
            u = t / SLAM_S
            return 2.2 - 1.2 * u * u, 0.0, 0.0
        amp = 16 * max(0.0, 1 - (t - SLAM_S) / SLAM_SHAKE_S)
        return 1.0, amp * math.sin(2 * math.pi * 19 * t), amp * math.cos(2 * math.pi * 23 * t)
    if style == "shake":
        return (1.0, 6 * math.sin(2 * math.pi * 13 * t) + 3 * math.sin(2 * math.pi * 29 * t),
                5 * math.cos(2 * math.pi * 11 * t) + 2 * math.cos(2 * math.pi * 31 * t))
    return 1.0, 0.0, 0.0


def draw_frame(t: Text, lay: Layout, at: float, seconds: float):
    """The caption `at` seconds into its `seconds` on screen, as an RGBA image W x canvas_height."""
    from .render import W

    Image, ImageDraw, _, _ = _pil()
    ch = canvas_height(lay)
    shadow = Image.new("RGBA", (W, ch), (0, 0, 0, 0))
    text = Image.new("RGBA", (W, ch), (0, 0, 0, 0))
    ds, dt = ImageDraw.Draw(shadow), ImageDraw.Draw(text)
    k, dx, dy = motion(t.style, at)
    step = word_step(len(lay.words), seconds)
    for i, (word, wx, wy, marked) in enumerate(lay.words):
        kw = 1.0
        if t.style == "words":
            since = at - i * step
            if since < 0:
                continue
            kw = pop(since)
        size = max(8, round(lay.size * k * kw))
        font = _font(size)
        border, drop = max(5, round(size * 0.08)), max(3, round(size * 0.06))
        x = W / 2 + (wx - W / 2) * k + dx
        y = ch / 2 + (wy - lay.cy) * k + dy
        ds.text((x, y + drop), word, font=font, anchor="mm", fill=(0, 0, 0, 128), stroke_width=border,
                stroke_fill=(0, 0, 0, 128))
        dt.text((x, y), word, font=font, anchor="mm", fill=YELLOW if marked else WHITE, stroke_width=border,
                stroke_fill=(0, 0, 0, 255))
    shadow.alpha_composite(text)
    a = fade(at, seconds)
    if a < 1:
        shadow.putalpha(shadow.getchannel("A").point(lambda v: int(v * a)))
    return shadow


@dataclass(frozen=True)
class TextFrames:
    folder: Path
    pattern: str                    # for ffmpeg's image2 input, relative to the work folder
    count: int
    y: int                          # where the canvas's top goes in the video


def write_frames(t: Text, seconds: float, folder: Path, name: str) -> TextFrames:
    """Writes the caption's frames for `seconds` on screen into folder/name/."""
    lay = layout(t)
    out = folder / name
    out.mkdir(parents=True, exist_ok=True)
    count = max(1, math.ceil(seconds * FPS)) + 1
    for i in range(count):
        draw_frame(t, lay, i / FPS, seconds).save(out / f"f{i:04d}.png", compress_level=1)
    return TextFrames(out, f"{name}/f%04d.png", count, round(lay.cy - canvas_height(lay) / 2))
