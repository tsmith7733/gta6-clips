"""Turns an EditPlan into one ffmpeg command on a clip Clips Kitty rendered (1080x1920, 30 fps).

The clip is cut into shots: pieces of the kept ranges (split where a zoom starts or ends), frozen
frames, and slow-motion replays inserted after their moment. Each shot gets its own crop (the
punch-in), then they're joined and the text, hook and flashes are drawn on the joined video, at the
output times the source times map to. Texts are drawn with Montserrat Black (fonts/, OFL), white with a
black border and a soft shadow, sized down to fit the frame. Stickers (stickers.py) are drawn by Pillow
as frame runs and overlaid at their points, mapped through any punch-in crop.
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from ..media import _popen_kwargs
from .plan import EditPlan, Speed, Sticker, Text, Zoom

log = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30
FONT_CANDIDATES = (Path(__file__).resolve().parents[2] / "fonts" / "Montserrat-Black.ttf",
                   Path(r"C:\Windows\Fonts\impact.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
# Montserrat Black is ~45% wider than Impact was: these keep a line under ~850 px, and _fit_size
# shrinks a text whose longest line still wouldn't fit TEXT_MAX_W.
FONT_SIZES = {"s": 58, "m": 80, "l": 104}
WRAP = {"s": 22, "m": 16, "l": 12}
TEXT_MAX_W = 960
GAME_CENTER = (0.5, 0.62)
EPS = 0.02


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Shot:
    kind: str                     # play | freeze
    a: float                      # source start (freeze: the frozen moment)
    b: float                      # source end (freeze: a)
    seconds: float                # how long it lasts in the edit
    speed: float = 1.0
    crop: tuple[int, int, int, int] | None = None   # w, h, x, y
    shake: bool = False
    replay: bool = False
    label: str = ""
    zoom: Zoom | None = None      # a smooth or push zoom this shot is part of (animated, not a fixed crop)
    in_fx: str = ""               # a transition into this shot (whip | zoom)
    out_fx: str = ""              # a transition out of it
    push: bool = False            # a frozen frame with a slow push-in
    center: tuple[float, float] = (0.5, 0.5)   # what a moving zoom closes in on (fractions of the frame)
    effect: str = ""              # a freeze's or replay's own effect, over the whole shot

    @property
    def moving(self) -> bool:
        return bool(self.zoom or self.in_fx or self.out_fx or self.push)


def _center(target: str, box, cam_box) -> tuple[float, float]:
    if target == "box" and box:
        return box[0] + box[2] / 2, box[1] + box[3] / 2
    if target == "cam" and cam_box:
        return cam_box[0] + cam_box[2] / 2, cam_box[1] + cam_box[3] / 2
    return GAME_CENTER


def crop_for(target: str, scale: float, box, cam_box) -> tuple[int, int, int, int]:
    """A 9:16 crop, 1/scale of the frame, centred on the target and kept inside the frame."""
    w = int(round(W / scale / 2)) * 2
    h = min(H, int(round(w * 16 / 9 / 2)) * 2)
    cx, cy = _center(target, box, cam_box)
    x = int(max(0, min(W - w, cx * W - w / 2)))
    y = int(max(0, min(H - h, cy * H - h / 2)))
    return w, h, x, y


RAMP_S, RAMP_STEPS = 0.45, 3     # a speed ramp: the first and last 0.45 s of a section, in 3 steps each


def _ramp_points(s: Speed) -> set[float]:
    if not s.ramp:
        return set()
    step = min(RAMP_S, (s.end - s.start) / 3) / RAMP_STEPS
    return {s.start + k * step for k in range(1, RAMP_STEPS + 1)} | {s.end - k * step for k in range(1, RAMP_STEPS + 1)}


def speed_at(plan: EditPlan, t: float) -> float:
    """The playback speed at source time t (inside a ramp: stepped from 1 toward the section's speed)."""
    s = next((s for s in plan.speeds if s.start - EPS <= t < s.end - EPS), None)
    if s is None:
        return 1.0
    if not s.ramp:
        return s.speed
    edge = min(RAMP_S, (s.end - s.start) / 3)
    d = min(t - s.start, s.end - t)
    if d >= edge - EPS:
        return s.speed
    frac = (int(d / edge * RAMP_STEPS) + 0.5) / RAMP_STEPS     # the middle of its step
    return round(1.0 + (s.speed - 1.0) * frac * frac * (3 - 2 * frac), 3)


def build_shots(plan: EditPlan, duration_s: float, cam_box=None) -> list[Shot]:
    keep = list(plan.keep) or [(0.0, duration_s)]
    cuts = {t for z in plan.zooms for t in (z.start, z.end)} | {f.at for f in plan.freezes} | \
        {r.after for r in plan.replays} | {t for s in plan.speeds for t in (s.start, s.end)} | \
        {t for s in plan.speeds for t in _ramp_points(s)}
    segments: list[tuple[float, float]] = []
    for a, b in keep:
        points = sorted({a, b} | {t for t in cuts if a + EPS < t < b - EPS})
        segments += [(x, y) for x, y in zip(points, points[1:]) if y - x > EPS]

    def zoom_at(t: float):
        return next((z for z in plan.zooms if z.start - EPS <= t < z.end - EPS), None)

    def crop(z):
        return crop_for(z.target, z.scale, z.box, cam_box) if z and z.style == "punch" else None

    def moving(z):
        return z if z and z.style != "punch" else None

    # Each replay goes right after the last kept piece that ends by its `after` (or after the first piece).
    replay_after: dict[int, list] = {}
    for r in plan.replays:
        idx = max((i for i, (_, b) in enumerate(segments) if b <= r.after + EPS), default=0)
        replay_after.setdefault(idx, []).append(r)

    shots: list[Shot] = []
    for i, (a, b) in enumerate(segments):
        for f in plan.freezes:
            if abs(f.at - a) <= EPS:
                z = zoom_at(a)
                shots.append(Shot("freeze", a, a, f.seconds, crop=crop_for(z.target, z.scale, z.box, cam_box) if z else None,
                                  label=f.label, push=f.push, effect=f.effect))
        z = zoom_at(a)
        speed = speed_at(plan, (a + b) / 2)
        shots.append(Shot("play", a, b, (b - a) / speed, speed=speed, crop=crop(z), shake=bool(z and z.shake),
                          zoom=moving(z), center=_center(z.target, z.box, cam_box) if moving(z) else (0.5, 0.5)))
        for r in replay_after.get(i, []):
            shots.append(Shot("play", r.start, r.end, (r.end - r.start) / r.speed, speed=r.speed,
                              crop=crop_for(r.target, r.scale, r.box, cam_box) if r.scale > 1.0 else None,
                              replay=True, label=r.label, effect=r.effect))
    # Transitions: out of the piece that ends at the cut, into whatever comes next.
    for t in plan.transitions:
        i = next((i for i, s in enumerate(shots) if s.kind == "play" and not s.replay and abs(s.b - t.at) <= EPS), None)
        if i is None or i + 1 >= len(shots):
            continue
        shots[i] = replace(shots[i], out_fx=t.kind)
        if shots[i + 1].kind == "play":
            shots[i + 1] = replace(shots[i + 1], in_fx=t.kind)
    return shots


def out_time(shots: list[Shot], t: float) -> float:
    """Where source time t lands in the edit (its first, non-replay showing; or the next kept moment)."""
    clock = 0.0
    best = None
    for s in shots:
        if s.kind == "play" and not s.replay and s.a - EPS <= t < s.b:
            return clock + (t - s.a) / s.speed
        if best is None and s.kind == "play" and not s.replay and s.a >= t:
            best = clock
        clock += s.seconds
    return best if best is not None else clock


def _atempo(speed: float) -> str:
    parts, s = [], speed
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    parts.append(f"atempo={s:.4f}")
    return ",".join(parts)


# -- motion: smooth and push zooms, whip and zoom transitions, a push on a freeze ---------------
TRANSITION_S = 0.18
UPSCALE = 2          # zoompan moves in whole input pixels: on a 2x frame that's half an output pixel


def _clip01(e: str) -> str:
    return f"max(0\\,min(1\\,{e}))"


def _ease(u: str) -> str:
    return f"(({u})*({u})*(3-2*({u})))"


def motion_filter(s: Shot) -> str:
    """A zoompan filter for a moving shot: zoom and centre as expressions of the shot's frame number."""
    T = f"(on/{FPS})"
    D = f"{s.seconds:.4f}"
    if s.kind == "freeze":
        z = f"(1+0.12*{_ease(_clip01(f'{T}/{D}'))})"
        cx, cy = 0.5, 0.45
    elif s.zoom is not None:
        zm = s.zoom
        cx, cy = s.center
        src = f"({s.a:.4f}+{T}*{s.speed:.4f})"                         # where in the clip this frame is
        if zm.style == "push":
            u = _clip01(f"({src}-{zm.start:.4f})/{zm.end - zm.start:.4f}")
        else:   # smooth: ease in over the zoom's first moments and back out over its last
            edge = min(0.35, (zm.end - zm.start) / 3)
            u = _clip01(f"min(({src}-{zm.start:.4f})\\,({zm.end:.4f}-{src}))/{edge:.4f}")
        z = f"(1+{zm.scale - 1:.4f}*{_ease(u)})"
    else:
        z, (cx, cy) = "1", (0.5, 0.5)
    out_u = _clip01(f"({T}-({D}-{TRANSITION_S}))/{TRANSITION_S}") if s.out_fx else "0"
    in_u = _clip01(f"1-{T}/{TRANSITION_S}") if s.in_fx else "0"
    for fx, u in ((s.out_fx, out_u), (s.in_fx, in_u)):
        if fx == "zoom":
            z = f"{z}*(1+0.9*({u})*({u}))"                               # punches through and lands
        elif fx == "whip":
            z = f"{z}*(1+0.3*{u})"                                       # room for the pan
    # whip: the picture flies off to the left and the next one comes in from the right
    terms = ([f"+0.5*{out_u}"] if s.out_fx == "whip" else []) + ([f"-0.5*{in_u}"] if s.in_fx == "whip" else [])
    pan = f"(0{''.join(terms)})"
    shake = f"+0.012*iw/zoom*sin(2*PI*{T}*11)" if s.shake else ""
    shake_y = f"+0.012*ih/zoom*cos(2*PI*{T}*13)" if s.shake else ""
    x = f"max(0\\,min(iw-iw/zoom\\,{cx:.4f}*iw-iw/zoom/2+{pan}*(iw-iw/zoom){shake}))"
    y = f"max(0\\,min(ih-ih/zoom\\,{cy:.4f}*ih-ih/zoom/2{shake_y}))"
    blur = []
    if s.out_fx:
        blur.append(f"{_blur(s.out_fx)}:enable='gte(t\\,{s.seconds - TRANSITION_S:.4f})'")
    if s.in_fx:
        blur.append(f"{_blur(s.in_fx)}:enable='lt(t\\,{TRANSITION_S})'")
    return (f"scale={W * UPSCALE}:{H * UPSCALE},zoompan=z='max(1\\,{z})':x='{x}':y='{y}':d=1:s={W}x{H}:fps={FPS}"
            + "".join("," + b for b in blur))


# -- looks: effects over stretches of the edit, and a colour grade over all of it ------------------
GRADE_FILTERS = {
    "none": "",
    "punchy": "eq=contrast=1.08:saturation=1.2",
    "warm": "colorbalance=rs=0.06:gs=0.01:bs=-0.06:rm=0.04:bm=-0.04,eq=saturation=1.1",
    "cool": "colorbalance=rs=-0.05:bs=0.07:bm=0.04,eq=contrast=1.05",
    "cinematic": "eq=contrast=1.12:saturation=0.85,colorbalance=rs=0.03:bs=-0.02:rh=-0.02:bh=0.04,vignette=angle=PI/6",
}
GLITCH_HZ = 12       # the glitch flips between two colour shifts this often, so it flickers


def effect_filters(kind: str, a: float, b: float) -> list[str]:
    """The filters for one effect between output times a and b."""
    on = f"between(t\\,{a:.3f}\\,{b:.3f})"
    if kind == "glitch":
        return [f"rgbashift=rh=-16:bh=16:rv=3:enable='{on}*lt(mod(t*{GLITCH_HZ}\\,1)\\,0.5)'",
                f"rgbashift=rh=10:bh=-10:bv=-5:enable='{on}*gte(mod(t*{GLITCH_HZ}\\,1)\\,0.5)'",
                f"noise=alls=28:allf=t:enable='{on}'"]
    if kind == "rgb_split":
        return [f"chromashift=crh=-14:cbh=14:enable='{on}'"]
    if kind == "bw":
        return [f"hue=s=0:enable='{on}'", f"eq=contrast=1.15:enable='{on}'"]
    if kind == "vignette":
        return [f"vignette=angle=PI/4.5:enable='{on}'"]
    if kind == "pop":
        return [f"eq=contrast=1.12:saturation=1.45:enable='{on}'"]
    raise RenderError(f"unknown effect {kind!r}")


def effect_ranges(plan: EditPlan, shots: list[Shot]) -> list[tuple[float, float, str]]:
    """(output start, output end, kind) for every effect: the plan's own, and freezes' and replays'."""
    out = []
    for e in plan.effects:
        a, b = out_time(shots, e.start), out_time(shots, e.end)
        if b > a:
            out.append((a, b, e.kind))
    clock = 0.0
    for s in shots:
        if s.effect:
            out.append((clock, clock + s.seconds, s.effect))
        clock += s.seconds
    return sorted(out)


def _blur(kind: str) -> str:
    return "dblur=angle=0:radius=40" if kind == "whip" else "gblur=sigma=8"


def _wrap(text: str, width: int) -> str:
    """At most 3 lines: a text that needs more is wrapped wider (and _fit_size shrinks it to fit),
    never cut short."""
    while True:
        lines, line = [], ""
        for word in text.upper().split():
            if line and len(line) + 1 + len(word) > width:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        lines.append(line)
        if len(lines) <= 3 or width > 80:
            return "\n".join(lines[:3])
        width += 2


def _fit_size(text: str, size: int) -> int:
    """The font size that keeps the longest line of `text` within TEXT_MAX_W (measured with Pillow)."""
    try:
        from PIL import ImageFont

        font = ImageFont.truetype(str(find_font()), size)
    except (ImportError, OSError, RenderError):
        return size
    lines = text.split("\n")
    while size > 24 and max((font.getlength(line) for line in lines), default=0) > TEXT_MAX_W:
        size -= 2
        font = ImageFont.truetype(str(find_font()), size)
    return size


def _drawtext(name: str, size: int, y: str, a: float, b: float) -> str:
    border, shadow = max(5, round(size * 0.08)), max(3, round(size * 0.06))
    return (f"drawtext=fontfile=font.ttf:textfile={name}:fontsize={size}:fontcolor=white:borderw={border}:"
            f"bordercolor=black:shadowx=0:shadowy={shadow}:shadowcolor=black@0.5:text_align=C:"
            f"line_spacing={round(size * 0.08)}:x=(w-text_w)/2:y={y}:enable='between(t\\,{a:.3f}\\,{b:.3f})'")


@dataclass(frozen=True)
class StickerRun:
    sticker: Sticker
    start: float                       # output time of its first frame
    end: float
    pieces: tuple[tuple[float, float, float, float], ...]   # (out start, out end, x, y in output pixels)


def _to_output(s: Shot, px: float, py: float) -> tuple[float, float]:
    """A point of the source frame (fractions) in the shot's output frame (pixels), through its crop."""
    crop = s.crop or (crop_for("game", 1.08, None, None) if s.shake else None)
    if not crop:
        return px * W, py * H
    w, h, x, y = crop
    return (px * W - x) * W / w, (py * H - y) * H / h


def sticker_runs(plan: EditPlan, shots: list[Shot]) -> list[StickerRun]:
    """Where each sticker shows in the edit: the pieces of it in each shot (not in replays or moving
    shots, whose picture moves under it), at its point mapped through that shot's crop."""
    runs = []
    for st in plan.stickers:
        pieces, clock = [], 0.0
        for s in shots:
            span = None
            if s.kind == "freeze" and st.start - EPS <= s.a < st.end:
                span = (clock, clock + s.seconds)
            elif s.kind == "play" and not s.replay and st.start < s.b and s.a < st.end:
                a, b = max(st.start, s.a), min(st.end, s.b)
                span = (clock + (a - s.a) / s.speed, clock + (b - s.a) / s.speed)
            if span and span[1] - span[0] > EPS and not (s.zoom or s.push):
                x, y = _to_output(s, st.x, st.y)
                if -20 <= x <= W + 20 and -20 <= y <= H + 20:
                    last = pieces[-1] if pieces else None
                    if last and abs(last[1] - span[0]) < EPS and abs(last[2] - x) < 1 and abs(last[3] - y) < 1:
                        pieces[-1] = (last[0], span[1], x, y)
                    else:
                        pieces.append((span[0], span[1], x, y))
            clock += s.seconds
        if pieces:
            runs.append(StickerRun(st, pieces[0][0], pieces[-1][1], tuple(pieces)))
        else:
            log.warning("The sticker at %.1f s doesn't show anywhere in the edit", st.start)
    return runs


def _text_y(position: str) -> str:
    return {"top": "h*0.34", "middle": "(h-text_h)/2", "bottom": "h*0.70-text_h"}[position]


ATTACHED_VOLUME = 0.7          # a replay's, freeze's or text's own sound
MUSIC_FADE_IN, MUSIC_FADE_OUT = 0.6, 1.5
# Levels are set against the clip's own loudness (Clips Kitty's clips are already loud): an effect at
# volume 0.7 is EFFECT_OVER_DB louder than the clip on average (at the clip's level they got lost:
# measured on clip 171), music at 0.12 sits MUSIC_UNDER_DB under it (and dips further while the clip
# is loud). The clip itself goes down CLIP_DB so the effects have room under the limiter.
EFFECT_REFERENCE, EFFECT_OVER_DB = 0.7, 4.0
MUSIC_REFERENCE, MUSIC_UNDER_DB = 0.12, 12.0
CLIP_DB = -2.0
DEFAULT_CLIP_MEAN_DB, DEFAULT_SOUND_MEAN_DB = -16.0, -20.0


def _gain_db(clip_mean: float, sound, volume: float, reference: float, under: float = 0.0) -> float:
    own = sound.mean_db if sound.mean_db is not None else DEFAULT_SOUND_MEAN_DB
    db = clip_mean - under + 20 * math.log10(volume / reference) - own + 20 * math.log10(sound.gain)
    return max(-30.0, min(30.0, db))


def sound_events(plan: EditPlan, shots: list[Shot], sounds) -> list[tuple[float, object, float]]:
    """The sound effects, as (output time, library Sound, volume), in the order they're inputs 1..n."""
    if sounds is None:
        return []
    events = []
    for e in plan.sounds:
        events.append((out_time(shots, e.at), sounds.get(e.sound), e.volume))
    for t in plan.texts:
        if t.sound:
            events.append((out_time(shots, t.start), sounds.get(t.sound), ATTACHED_VOLUME))
    freezes = [f for f in plan.freezes if f.sound]
    replays = {(r.start, r.end, r.label): r for r in plan.replays if r.sound}
    clock = 0.0
    for s in shots:
        f = next((f for f in freezes if abs(f.at - s.a) <= EPS), None) if s.kind == "freeze" else None
        if f is not None:
            freezes.remove(f)
            events.append((clock, sounds.get(f.sound), ATTACHED_VOLUME))
        elif s.replay and (s.a, s.b, s.label) in replays:
            events.append((clock, sounds.get(replays.pop((s.a, s.b, s.label)).sound), ATTACHED_VOLUME))
        clock += s.seconds
    return sorted((e for e in events if e[1] is not None), key=lambda e: e[0])


def audio_inputs(plan: EditPlan, shots: list[Shot], sounds) -> list[tuple[Path, bool]]:
    """The extra ffmpeg inputs, in order: each sound effect, then the music (looped)."""
    inputs = [(snd.path, False) for _, snd, _ in sound_events(plan, shots, sounds)]
    if plan.music and sounds is not None and sounds.get(plan.music.track):
        inputs.append((sounds.get(plan.music.track).path, True))
    return inputs


def _mix_lines(plan: EditPlan, shots: list[Shot], sounds, clip_mean_db: float | None = None) -> list[str]:
    """Mixes the clip's sound ([acat]) with the effects and the ducked music into [aout]."""
    events = sound_events(plan, shots, sounds)
    music = sounds.get(plan.music.track) if plan.music and sounds is not None else None
    if not events and music is None:
        return ["[acat]anull[aout]"]
    clip_mean = DEFAULT_CLIP_MEAN_DB if clip_mean_db is None else clip_mean_db
    total = sum(s.seconds for s in shots)
    lines, mix = [f"[acat]volume={CLIP_DB}dB[clip]"], []
    if music is not None:
        n = len(events) + 1
        fade_out = max(0.0, total - MUSIC_FADE_OUT)
        db = _gain_db(clip_mean, music, plan.music.volume, MUSIC_REFERENCE, MUSIC_UNDER_DB)
        lines.append(f"[{n}:a]atrim=start={plan.music.start:.3f}:duration={total:.3f},asetpts=PTS-STARTPTS,"
                     f"aresample=48000,aformat=channel_layouts=stereo,volume={db:.1f}dB,"
                     f"afade=t=in:d={MUSIC_FADE_IN}:curve=tri,afade=t=out:st={fade_out:.3f}:d={MUSIC_FADE_OUT}[mraw]")
        # The music dips whenever the clip itself is loud (his voice, a crash), and comes back up between.
        lines.append("[clip]asplit=2[voice][key]")
        lines.append("[mraw][key]sidechaincompress=threshold=0.1:ratio=4:attack=20:release=400[music]")
        mix += ["[voice]", "[music]"]
    else:
        mix.append("[clip]")
    for k, (at, snd, volume) in enumerate(events, start=1):
        ms = int(round(at * 1000))
        db = _gain_db(clip_mean, snd, volume, EFFECT_REFERENCE, -EFFECT_OVER_DB)
        lines.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,volume={db:.1f}dB,"
                     f"adelay=delays={ms}:all=1[fx{k}]")
        mix.append(f"[fx{k}]")
    # normalize=0: every input keeps its own level; the limiter stops a boom on a yell from clipping.
    lines.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0:dropout_transition=0,"
                 "alimiter=limit=0.95:level=disabled[aout]")
    return lines


def build_graph(plan: EditPlan, shots: list[Shot], workdir: Path, sounds=None,
                clip_mean_db: float | None = None) -> tuple[str, list[str]]:
    """The filter graph, and the text files it reads (written into workdir). With `sounds` (the
    SoundLibrary), the plan's effects and music are inputs 1..n, in `audio_inputs` order."""
    plays = [i for i, s in enumerate(shots) if s.kind == "play"]
    lines = [f"[0:v]split={len(shots)}" + "".join(f"[v{i}]" for i in range(len(shots))),
             f"[0:a]asplit={len(plays)}" + "".join(f"[a{i}]" for i in plays)]
    for i, s in enumerate(shots):
        crop = ""
        motion = f"{motion_filter(s)}," if s.moving else ""     # shake then lives in the zoompan
        if s.crop or (s.shake and not s.moving):
            w, h, x, y = s.crop or crop_for("game", 1.08, None, None)
            if s.shake and not s.moving:
                amp = max(4, min(18, x, W - w - x, y, H - h - y))
                xs = f"max(0\\,min({W - w}\\,{x}+{amp}*sin(2*PI*t*11)))"
                ys = f"max(0\\,min({H - h}\\,{y}+{amp}*cos(2*PI*t*13)))"
                crop = f"crop={w}:{h}:{xs}:{ys},scale={W}:{H},"
            else:
                crop = f"crop={w}:{h}:{x}:{y},scale={W}:{H},"
        if s.kind == "freeze":
            frames = max(1, round(s.seconds * FPS))
            lines.append(f"[v{i}]trim=start={s.a:.3f}:end={s.a + 0.1:.3f},setpts=PTS-STARTPTS,{crop}"
                         f"loop=loop={frames}:size=1:start=0,trim=end_frame={frames},setpts=N/{FPS}/TB,"
                         f"fps={FPS},{motion}setsar=1,format=yuv420p[sv{i}]")
            lines.append(f"aevalsrc=0|0:s=48000:d={s.seconds:.3f},aformat=channel_layouts=stereo[sa{i}]")
        else:
            speed = f",setpts=PTS/{s.speed:.4f}" if s.speed != 1.0 else ""
            # fps before the zoompan: it makes one frame per frame in, so slow motion needs its frames first
            lines.append(f"[v{i}]trim=start={s.a:.3f}:end={s.b:.3f},setpts=PTS-STARTPTS{speed},{crop}"
                         f"fps={FPS},{motion}setsar=1,format=yuv420p[sv{i}]")
            tempo = f",{_atempo(s.speed)}" if s.speed != 1.0 else ""
            lines.append(f"[a{i}]atrim=start={s.a:.3f}:end={s.b:.3f},asetpts=PTS-STARTPTS{tempo},"
                         f"aresample=48000,aformat=channel_layouts=stereo[sa{i}]")
    lines.append("".join(f"[sv{i}][sa{i}]" for i in range(len(shots))) + f"concat=n={len(shots)}:v=1:a=1[cv][acat]")
    lines += _mix_lines(plan, shots, sounds, clip_mean_db)

    from .captions import animated

    overlays = text_overlays(plan, shots)
    moving = [o for o in overlays if animated(o[2])]
    chain, files = [], []
    # Colour first, then the effects, then the stickers, so text and labels drawn last stay clean.
    if GRADE_FILTERS.get(plan.grade):
        chain.append(GRADE_FILTERS[plan.grade])
    for a, b, kind in effect_ranges(plan, shots):
        chain += effect_filters(kind, a, b)
    runs = sticker_runs(plan, shots)
    label = "cv"
    if runs or moving:
        from . import captions
        from .stickers import arrow_direction, write_frames

        lines.append("[cv]" + (",".join(chain) if chain else "null") + "[sk0]")
        chain, label, k = [], "sk0", 0
        first = 1 + len(audio_inputs(plan, shots, sounds))
        for n, run in enumerate(runs):
            _, _, x0, y0 = run.pieces[0]
            frames = write_frames(run.sticker, run.end - run.start, workdir, f"stk{n}", arrow_direction(x0, y0, W, H))
            m = len(run.pieces)
            lines.append(f"[{first + n}:v]format=rgba,setpts=PTS-STARTPTS+{run.start:.3f}/TB"
                         + (f",split={m}" if m > 1 else "") + "".join(f"[sp{n}_{j}]" for j in range(m)))
            for j, (a, b, x, y) in enumerate(run.pieces):
                k += 1
                lines.append(f"[{label}][sp{n}_{j}]overlay=x={round(x - frames.size / 2)}:y={round(y - frames.size / 2)}:"
                             f"eof_action=pass:enable='between(t\\,{a:.3f}\\,{b:.3f})'[sk{k}]")
                label = f"sk{k}"
        # Styled captions: frame runs like the stickers, inputs after them.
        for n, (a, b, t) in enumerate(moving):
            frames = captions.write_frames(t, b - a, workdir, f"txt{n}")
            k += 1
            lines.append(f"[{first + len(runs) + n}:v]format=rgba,setpts=PTS-STARTPTS+{a:.3f}/TB[tp{n}]")
            lines.append(f"[{label}][tp{n}]overlay=x=0:y={frames.y}:eof_action=pass:"
                         f"enable='between(t\\,{a:.3f}\\,{b:.3f})'[sk{k}]")
            label = f"sk{k}"
    for n, (a, b, t) in enumerate(o for o in overlays if not animated(o[2])):
        name = f"t{n}.txt"
        text = _wrap(t.text, WRAP[t.size])
        # newline="\n": on Windows text mode writes \r\n, and drawtext draws the \r as a blank line.
        (workdir / name).write_text(text, encoding="utf-8", newline="\n")
        files.append(name)
        chain.append(_drawtext(name, _fit_size(text, FONT_SIZES[t.size]), _text_y(t.position), a, b))
    for f in plan.flashes:
        o = out_time(shots, f)
        chain.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill:enable='between(t\\,{o:.3f}\\,{o + 0.1:.3f})'")
    lines.append(f"[{label}]" + (",".join(chain) if chain else "null") + "[vout]")
    return ";\n".join(lines), files


def text_overlays(plan: EditPlan, shots: list[Shot]) -> list[tuple[float, float, Text]]:
    """The texts, replay and freeze labels and the hook, at output times. A freeze's label slams in,
    a replay's pops."""
    overlays: list[tuple[float, float, Text]] = []
    for t in plan.texts:
        a, b = out_time(shots, t.start), out_time(shots, t.end)
        if b > a:
            overlays.append((a, b, t))
    clock = 0.0
    for s in shots:
        if s.label:
            freeze = s.kind == "freeze"
            overlays.append((clock, clock + s.seconds, Text(0, 0, s.label, "middle" if freeze else "top",
                                                            "l" if freeze else "m", style="slam" if freeze else "pop")))
        clock += s.seconds
    if plan.hook:
        overlays.append((0.0, plan.hook.seconds, Text(0, 0, plan.hook.text, "top", "l", style=plan.hook.style,
                                                      highlight=plan.hook.highlight)))
    return overlays


def find_font() -> Path:
    for f in FONT_CANDIDATES:
        if f.is_file():
            return f
    raise RenderError("No bold font found for the text (fonts/Montserrat-Black.ttf)")


def render(ffmpeg: Path, source: Path, plan: EditPlan, dest: Path, *, duration_s: float, cam_box=None,
           sounds=None) -> Path:
    """Renders the plan onto `source` (a Clips Kitty clip) into `dest` (.mp4). `sounds` is the
    SoundLibrary its sound effects and music come from."""
    shots = build_shots(plan, duration_s, cam_box)
    if not shots:
        raise RenderError("The plan keeps nothing of the clip")
    total = sum(s.seconds for s in shots)
    if total > 90:
        raise RenderError(f"The edit would run {total:.0f} s; Shorts here stay under 90 s")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copy(find_font(), work / "font.ttf")
        inputs = audio_inputs(plan, shots, sounds)
        clip_mean = None
        if inputs:
            from .sounds import mean_volume

            clip_mean = mean_volume(ffmpeg, source)
        graph, _ = build_graph(plan, shots, work, sounds, clip_mean)
        (work / "graph.txt").write_text(graph, encoding="utf-8")
        extra: list[str] = []
        for path, loop in inputs:
            extra += (["-stream_loop", "-1"] if loop else []) + ["-i", str(Path(path).resolve())]
        from .captions import animated

        for n in range(len(sticker_runs(plan, shots))):      # their frames, written by build_graph
            extra += ["-framerate", str(FPS), "-i", f"stk{n}/f%04d.png"]
        for n in range(sum(1 for o in text_overlays(plan, shots) if animated(o[2]))):
            extra += ["-framerate", str(FPS), "-i", f"txt{n}/f%04d.png"]
        args = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(source.resolve()), *extra,
                "-/filter_complex", "graph.txt", "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                str(dest.resolve())]
        proc = subprocess.run(args, cwd=work, capture_output=True, text=True, **_popen_kwargs())
        if proc.returncode != 0 or not dest.is_file():
            raise RenderError(f"ffmpeg couldn't render the edit: {proc.stderr.strip()[-800:]}")
    log.info("Edited %s -> %s (%d shots, %.1f s)", source.name, dest.name, len(shots), total)
    return dest
