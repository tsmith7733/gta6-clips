"""Turns an EditPlan into one ffmpeg command on a clip Clips Kitty rendered (1080x1920, 30 fps).

The clip is cut into shots: pieces of the kept ranges (split where a zoom starts or ends), frozen
frames, and slow-motion replays inserted after their moment. Each shot gets its own crop (the
punch-in), then they're joined and the text, hook and flashes are drawn on the joined video, at the
output times the source times map to. Texts are drawn with Impact, white with a black border, the
usual clipper look.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..media import _popen_kwargs
from .plan import EditPlan, Text

log = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30
FONT_CANDIDATES = (Path(r"C:\Windows\Fonts\impact.ttf"), Path("/usr/share/fonts/truetype/msttcorefonts/Impact.ttf"),
                   Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
FONT_SIZES = {"s": 64, "m": 88, "l": 118}
WRAP = {"s": 24, "m": 18, "l": 14}
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


def crop_for(target: str, scale: float, box, cam_box) -> tuple[int, int, int, int]:
    """A 9:16 crop, 1/scale of the frame, centred on the target and kept inside the frame."""
    w = int(round(W / scale / 2)) * 2
    h = min(H, int(round(w * 16 / 9 / 2)) * 2)
    if target == "box" and box:
        cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
    elif target == "cam" and cam_box:
        cx, cy = cam_box[0] + cam_box[2] / 2, cam_box[1] + cam_box[3] / 2
    else:
        cx, cy = GAME_CENTER
    x = int(max(0, min(W - w, cx * W - w / 2)))
    y = int(max(0, min(H - h, cy * H - h / 2)))
    return w, h, x, y


def build_shots(plan: EditPlan, duration_s: float, cam_box=None) -> list[Shot]:
    keep = list(plan.keep) or [(0.0, duration_s)]
    cuts = {t for z in plan.zooms for t in (z.start, z.end)} | {f.at for f in plan.freezes} | \
        {r.after for r in plan.replays}
    segments: list[tuple[float, float]] = []
    for a, b in keep:
        points = sorted({a, b} | {t for t in cuts if a + EPS < t < b - EPS})
        segments += [(x, y) for x, y in zip(points, points[1:]) if y - x > EPS]

    def zoom_at(t: float):
        return next((z for z in plan.zooms if z.start - EPS <= t < z.end - EPS), None)

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
                                  label=f.label))
        z = zoom_at(a)
        shots.append(Shot("play", a, b, b - a, crop=crop_for(z.target, z.scale, z.box, cam_box) if z else None,
                          shake=bool(z and z.shake)))
        for r in replay_after.get(i, []):
            shots.append(Shot("play", r.start, r.end, (r.end - r.start) / r.speed, speed=r.speed,
                              crop=crop_for(r.target, r.scale, r.box, cam_box) if r.scale > 1.0 else None,
                              replay=True, label=r.label))
    return shots


def out_time(shots: list[Shot], t: float) -> float:
    """Where source time t lands in the edit (its first, non-replay showing; or the next kept moment)."""
    clock = 0.0
    best = None
    for s in shots:
        if s.kind == "play" and not s.replay and s.a - EPS <= t < s.b:
            return clock + (t - s.a)
        if best is None and s.kind == "play" and not s.replay and s.a >= t:
            best = clock
        clock += s.seconds
    return best if best is not None else clock


def _atempo(speed: float) -> str:
    parts, s = [], speed
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.4f}")
    return ",".join(parts)


def _wrap(text: str, width: int) -> str:
    lines, line = [], ""
    for word in text.upper().split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    lines.append(line)
    return "\n".join(lines[:3])


def _text_y(position: str) -> str:
    return {"top": "h*0.34", "middle": "(h-text_h)/2", "bottom": "h*0.70-text_h"}[position]


def build_graph(plan: EditPlan, shots: list[Shot], workdir: Path) -> tuple[str, list[str]]:
    """The filter graph, and the text files it reads (written into workdir)."""
    plays = [i for i, s in enumerate(shots) if s.kind == "play"]
    lines = [f"[0:v]split={len(shots)}" + "".join(f"[v{i}]" for i in range(len(shots))),
             f"[0:a]asplit={len(plays)}" + "".join(f"[a{i}]" for i in plays)]
    for i, s in enumerate(shots):
        crop = ""
        if s.crop or s.shake:
            w, h, x, y = s.crop or crop_for("game", 1.08, None, None)
            if s.shake:
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
                         f"setsar=1,fps={FPS},format=yuv420p[sv{i}]")
            lines.append(f"aevalsrc=0|0:s=48000:d={s.seconds:.3f},aformat=channel_layouts=stereo[sa{i}]")
        else:
            speed = f",setpts=PTS/{s.speed:.4f}" if s.speed != 1.0 else ""
            lines.append(f"[v{i}]trim=start={s.a:.3f}:end={s.b:.3f},setpts=PTS-STARTPTS{speed},{crop}"
                         f"setsar=1,fps={FPS},format=yuv420p[sv{i}]")
            tempo = f",{_atempo(s.speed)}" if s.speed != 1.0 else ""
            lines.append(f"[a{i}]atrim=start={s.a:.3f}:end={s.b:.3f},asetpts=PTS-STARTPTS{tempo},"
                         f"aresample=48000,aformat=channel_layouts=stereo[sa{i}]")
    lines.append("".join(f"[sv{i}][sa{i}]" for i in range(len(shots))) + f"concat=n={len(shots)}:v=1:a=1[cv][aout]")

    # Text, labels and the hook, at output times.
    overlays: list[tuple[float, float, Text]] = []
    for t in plan.texts:
        a, b = out_time(shots, t.start), out_time(shots, t.end)
        if b > a:
            overlays.append((a, b, t))
    clock = 0.0
    for s in shots:
        if s.label:
            overlays.append((clock, clock + s.seconds, Text(0, 0, s.label, "middle" if s.kind == "freeze" else "top",
                                                            "l" if s.kind == "freeze" else "m")))
        clock += s.seconds
    if plan.hook:
        overlays.append((0.0, plan.hook.seconds, Text(0, 0, plan.hook.text, "top", "l")))
    chain, files = [], []
    for n, (a, b, t) in enumerate(overlays):
        name = f"t{n}.txt"
        # newline="\n": on Windows text mode writes \r\n, and drawtext draws the \r as a blank line.
        (workdir / name).write_text(_wrap(t.text, WRAP[t.size]), encoding="utf-8", newline="\n")
        files.append(name)
        chain.append(f"drawtext=fontfile=font.ttf:textfile={name}:fontsize={FONT_SIZES[t.size]}:fontcolor=white:"
                     f"borderw=7:bordercolor=black:text_align=C:line_spacing=6:x=(w-text_w)/2:y={_text_y(t.position)}:"
                     f"enable='between(t\\,{a:.3f}\\,{b:.3f})'")
    for f in plan.flashes:
        o = out_time(shots, f)
        chain.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill:enable='between(t\\,{o:.3f}\\,{o + 0.1:.3f})'")
    lines.append("[cv]" + (",".join(chain) if chain else "null") + "[vout]")
    return ";\n".join(lines), files


def find_font() -> Path:
    for f in FONT_CANDIDATES:
        if f.is_file():
            return f
    raise RenderError("No bold font found for the text (Impact on Windows)")


def render(ffmpeg: Path, source: Path, plan: EditPlan, dest: Path, *, duration_s: float, cam_box=None) -> Path:
    """Renders the plan onto `source` (a Clips Kitty clip) into `dest` (.mp4)."""
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
        graph, _ = build_graph(plan, shots, work)
        (work / "graph.txt").write_text(graph, encoding="utf-8")
        args = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(source.resolve()),
                "-/filter_complex", "graph.txt", "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                str(dest.resolve())]
        proc = subprocess.run(args, cwd=work, capture_output=True, text=True, **_popen_kwargs())
        if proc.returncode != 0 or not dest.is_file():
            raise RenderError(f"ffmpeg couldn't render the edit: {proc.stderr.strip()[-800:]}")
    log.info("Edited %s -> %s (%d shots, %.1f s)", source.name, dest.name, len(shots), total)
    return dest
