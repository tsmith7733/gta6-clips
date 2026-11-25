"""Stickers drawn over an edit: emoji, arrows and circles, the usual clipper overlays.

Each sticker is drawn with Pillow as a short run of PNG frames on a fixed transparent canvas (a pop
in, then a little life: an emoji breathes and tilts, an arrow pokes at its point, a circle draws
itself on, and all fade out at the end), which ffmpeg overlays with the canvas centre on the
sticker's point. Emoji come from the system's colour emoji font (Segoe UI Emoji on Windows), so
nothing is downloaded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .plan import EMOJI, Sticker

FPS = 30
SS = 2                                   # drawn at 2x and scaled down: smooth edges
EMOJI_PX = {"s": 150, "m": 210, "l": 290}
ARROW_PX = {"s": 190, "m": 260, "l": 340}       # length, tip to tail
CIRCLE_PX = {"s": 170, "m": 260, "l": 380}      # diameter
RED, WHITE, SHADOW = (255, 45, 45, 255), (255, 255, 255, 255), (0, 0, 0, 150)
POP_S, FADE_S, DRAW_ON_S = 0.22, 0.12, 0.3
EMOJI_FONTS = (Path(r"C:\Windows\Fonts\seguiemj.ttf"), Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
               Path("/System/Library/Fonts/Apple Color Emoji.ttc"))


class StickerError(RuntimeError):
    pass


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except ImportError as e:   # pragma: no cover - Pillow is in requirements.txt
        raise StickerError("Stickers need Pillow (pip install pillow)") from e
    return Image, ImageDraw, ImageFilter, ImageFont


def pop(t: float) -> float:
    """The scale over the pop-in: from small, a little over, settling at 1."""
    if t >= POP_S:
        return 1.0
    if t < POP_S * 0.55:
        return 0.25 + (1.18 - 0.25) * (t / (POP_S * 0.55))
    return 1.18 - 0.18 * ((t - POP_S * 0.55) / (POP_S * 0.45))


def fade(t: float, seconds: float) -> float:
    return max(0.0, min(1.0, (seconds - t) / FADE_S))


def canvas_size(s: Sticker) -> int:
    if s.kind == "emoji":
        return int(EMOJI_PX[s.size] * 1.5) // 2 * 2
    if s.kind == "arrow":
        return (ARROW_PX[s.size] + 60) * 2 // 2 * 2
    return int(CIRCLE_PX[s.size] * 1.25 + 30) // 2 * 2


def _emoji_image(name: str, px: int):
    Image, ImageDraw, _, ImageFont = _pil()
    font_path = next((f for f in EMOJI_FONTS if f.is_file()), None)
    if font_path is None:
        raise StickerError("No colour emoji font found (Segoe UI Emoji on Windows)")
    font = ImageFont.truetype(str(font_path), 109 if "Noto" in font_path.name else px * SS)
    big = Image.new("RGBA", (px * SS * 3, px * SS * 3), (0, 0, 0, 0))
    ImageDraw.Draw(big).text((px * SS, px * SS), EMOJI[name], font=font, embedded_color=True)
    box = big.getbbox()
    if not box:
        raise StickerError(f"The emoji font has no {name!r}")
    glyph = big.crop(box)
    k = px * SS / max(glyph.size)
    return glyph.resize((max(1, round(glyph.width * k)), max(1, round(glyph.height * k))), Image.LANCZOS)


def _with_shadow(img):
    """A soft dark shadow under the sticker, so it reads on any background."""
    Image, _, ImageFilter, _ = _pil()
    alpha = img.getchannel("A").point(lambda a: a * 150 // 255)
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow.putalpha(alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(6 * SS))
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.alpha_composite(shadow, (0, 4 * SS))
    out.alpha_composite(img)
    return out


def _arrow(draw, tip, d, length, poke):
    """A thick arrow whose tip is at `tip`, pointing along unit vector d."""
    n = (-d[1], d[0])
    head_l, head_w, shaft_w = length * 0.38, length * 0.30, length * 0.11
    tx, ty = tip[0] - d[0] * poke, tip[1] - d[1] * poke

    def p(u, v):
        return (tx + u * d[0] + v * n[0], ty + u * d[1] + v * n[1])

    pts = [p(0, 0), p(-head_l, head_w), p(-head_l, shaft_w / 2), p(-length, shaft_w / 2), p(-length, -shaft_w / 2),
           p(-head_l, -shaft_w / 2), p(-head_l, -head_w)]
    draw.polygon(pts, fill=RED, outline=WHITE, width=7 * SS)


def arrow_direction(x: float, y: float, frame_w: int, frame_h: int) -> tuple[float, float]:
    """Arrows come from the middle of the frame toward their point (the side with room for them)."""
    dx, dy = x - frame_w * 0.5, y - frame_h * 0.55
    n = math.hypot(dx, dy)
    if n < 40:
        return (0.0, 1.0)                # in the middle: point down at it
    return (dx / n, dy / n)


def draw_frame(s: Sticker, t: float, seconds: float, emoji_img=None, direction=(0.0, 1.0)):
    """The sticker t seconds into its `seconds` on screen, as an RGBA canvas of canvas_size(s)."""
    Image, ImageDraw, _, _ = _pil()
    size = canvas_size(s) * SS
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    c = size / 2
    k = pop(t)
    if s.kind == "emoji":
        breathe = 1 + 0.05 * math.sin(2 * math.pi * 1.4 * t)
        tilt = 7 * math.sin(2 * math.pi * 0.9 * t)
        g = emoji_img.resize((max(1, round(emoji_img.width * k * breathe)), max(1, round(emoji_img.height * k * breathe))),
                             Image.LANCZOS).rotate(tilt, resample=Image.BICUBIC, expand=True)
        img.alpha_composite(g, (round(c - g.width / 2), round(c - g.height / 2)))
    elif s.kind == "arrow":
        length = ARROW_PX[s.size] * SS * k
        poke = 14 * SS * (0.5 - 0.5 * math.cos(2 * math.pi * 2.2 * t))
        _arrow(ImageDraw.Draw(img), (c, c), direction, length, poke)
    else:
        r = CIRCLE_PX[s.size] * SS / 2 * (1 + 0.04 * math.sin(2 * math.pi * 1.6 * t))
        sweep = 360 * min(1.0, t / DRAW_ON_S)
        box = [c - r, c - r, c + r, c + r]
        draw = ImageDraw.Draw(img)
        draw.arc(box, -90, -90 + sweep, fill=WHITE, width=24 * SS)
        inner = [box[0] + 5 * SS, box[1] + 5 * SS, box[2] - 5 * SS, box[3] - 5 * SS]
        draw.arc(inner, -90, -90 + sweep, fill=RED, width=14 * SS)
    img = _with_shadow(img)
    a = fade(t, seconds)
    if a < 1:
        img.putalpha(img.getchannel("A").point(lambda v: int(v * a)))
    return img.resize((size // SS, size // SS), Image.LANCZOS)


@dataclass(frozen=True)
class Frames:
    folder: Path
    pattern: str                         # for ffmpeg's image2 input, relative to the work folder
    count: int
    size: int                            # the square canvas, in output pixels; its centre goes on the point


def write_frames(s: Sticker, seconds: float, folder: Path, name: str, direction=(0.0, 1.0)) -> Frames:
    """Writes the sticker's frames for `seconds` on screen into folder/name/."""
    out = folder / name
    out.mkdir(parents=True, exist_ok=True)
    emoji_img = _emoji_image(s.emoji, EMOJI_PX[s.size]) if s.kind == "emoji" else None
    count = max(1, math.ceil(seconds * FPS)) + 1
    for i in range(count):
        draw_frame(s, i / FPS, seconds, emoji_img, direction).save(out / f"f{i:04d}.png", compress_level=1)
    return Frames(out, f"{name}/f%04d.png", count, canvas_size(s))
