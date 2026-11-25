"""An edit plan: the moves the editor may make on a clip Clips Kitty rendered (1080x1920).

Every time is in seconds of the Clips Kitty clip ("source time"), except the hook, which is always
the first seconds of the edited video. The moves are deliberately few and checked here, so a plan
from Claude can't make ffmpeg do something odd; `render.py` turns a plan into one ffmpeg command.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

TARGETS = ("cam", "game", "box")
POSITIONS = ("top", "middle", "bottom")
SIZES = ("s", "m", "l")
MAX_OUTPUT_S = 90.0
MAX_SOUNDS = 10
ZOOM_STYLES = ("punch", "smooth", "push")   # cut straight in | ease in and out | slow push-in over the zoom
TRANSITIONS = ("whip", "zoom")
MAX_TRANSITIONS = 6


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Zoom:
    start: float
    end: float
    target: str = "cam"                     # cam: the streamer's webcam; game: the middle of the game
    scale: float = 1.8                      # 1.1-3.0
    box: tuple[float, float, float, float] | None = None   # target "box": x, y, w, h as fractions of the frame
    shake: bool = False
    style: str = "punch"


@dataclass(frozen=True)
class Speed:
    start: float
    end: float
    speed: float                            # 0.3-0.8 slow motion, 1.25-4 fast forward
    ramp: bool = True                       # ease into and out of the speed instead of jumping


@dataclass(frozen=True)
class Transition:
    at: float                               # a cut: the end of a kept range (source time)
    kind: str = "whip"


@dataclass(frozen=True)
class Replay:
    start: float
    end: float                              # the moment shown again, 1-6 s
    speed: float = 0.5                      # 0.25-1.0
    target: str = "game"
    scale: float = 1.3
    box: tuple[float, float, float, float] | None = None
    label: str = "REPLAY"
    after: float | None = None              # where it's inserted (default: right after `end`)
    sound: str = ""                         # a sound effect id, played as the replay starts


@dataclass(frozen=True)
class Freeze:
    at: float
    seconds: float = 0.7                    # 0.2-1.5
    label: str = ""
    sound: str = ""
    push: bool = False                      # a slow push-in while it's frozen


@dataclass(frozen=True)
class Text:
    start: float
    end: float
    text: str
    position: str = "top"
    size: str = "m"
    sound: str = ""                         # played as the text appears


@dataclass(frozen=True)
class Sound:
    at: float                               # source time
    sound: str                              # a sound effect id from the library
    volume: float = 0.7                     # 0.1-1.0


@dataclass(frozen=True)
class Music:
    track: str                              # a music id from the library
    volume: float = 0.12                    # 0.03-0.35, before it's ducked under the clip's sound
    start: float = 0.0                      # where in the track to begin


@dataclass(frozen=True)
class Hook:
    text: str
    seconds: float = 2.0                    # shown from the edited video's start


@dataclass(frozen=True)
class EditPlan:
    keep: tuple[tuple[float, float], ...] = ()     # empty: the whole clip
    zooms: tuple[Zoom, ...] = ()
    replays: tuple[Replay, ...] = ()
    freezes: tuple[Freeze, ...] = ()
    texts: tuple[Text, ...] = ()
    flashes: tuple[float, ...] = ()
    hook: Hook | None = None
    notes: tuple[str, ...] = field(default=())     # why: one line per idea, shown on the review page
    sounds: tuple[Sound, ...] = ()
    music: Music | None = None
    speeds: tuple[Speed, ...] = ()
    transitions: tuple[Transition, ...] = ()


def _num(d: dict, key: str, default=None) -> float:
    v = d.get(key, default)
    if v is None:
        raise PlanError(f"{key} is missing")
    try:
        return float(v)
    except (TypeError, ValueError):
        raise PlanError(f"{key} must be a number, not {v!r}") from None


def _box(d: dict) -> tuple[float, float, float, float] | None:
    b = d.get("box")
    if b is None:
        return None
    if len(b) != 4 or not all(0 <= float(x) <= 1 for x in b) or float(b[2]) <= 0 or float(b[3]) <= 0:
        raise PlanError(f"box must be [x, y, w, h] fractions of the frame, not {b!r}")
    return tuple(float(x) for x in b)


_DANGLING = {"a", "an", "the", "of", "to", "in", "on", "and", "or", "with", "for", "at", "his", "her"}


def _clean_text(text: str, limit: int = 60) -> str:
    """One or two short lines; characters the font can't draw (emoji) are dropped."""
    text = " ".join("".join(ch for ch in str(text) if ord(ch) < 0x2000 or ch in "’‘“”…").split())
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(" ", 1)[0]      # whole words only
    cut = (cut if cut and len(cut) < len(text) else text[:limit]).rstrip(" ,;:-")
    words = cut.split()
    while len(words) > 1 and words[-1].lower() in _DANGLING:
        words.pop()
    return " ".join(words)


def parse_plan(data: dict, duration_s: float, sounds=None) -> EditPlan:
    """Checks a plan (from JSON) against a clip of `duration_s` seconds. Raises PlanError.
    `sounds` is the SoundLibrary the plan's sound effects and music must come from."""
    def sfx(value) -> str:
        sid = str(value or "").strip()
        if not sid:
            return ""
        if sounds is None or sounds.get(sid, "sfx") is None:
            raise PlanError(f"there's no sound effect {sid!r} in the sound library")
        return sid

    def within(a: float, b: float, what: str, min_len: float = 0.2) -> tuple[float, float]:
        a, b = max(0.0, a), min(duration_s, b)
        if b - a < min_len:
            raise PlanError(f"{what} {a:.2f}-{b:.2f} s is outside the clip ({duration_s:.1f} s) or too short")
        return a, b

    keep = []
    for k in data.get("keep") or []:
        keep.append(within(float(k[0]), float(k[1]), "keep range", 0.5))
    keep.sort()
    zooms = []
    for z in data.get("zooms") or []:
        a, b = within(_num(z, "start"), _num(z, "end"), "zoom")
        target = str(z.get("target", "cam"))
        if target not in TARGETS:
            raise PlanError(f"zoom target must be one of {TARGETS}")
        scale = _num(z, "scale", 1.8)
        if not 1.1 <= scale <= 3.0:
            raise PlanError("zoom scale must be 1.1-3.0")
        box = _box(z)
        if target == "box" and box is None:
            raise PlanError("a zoom on a box needs its box")
        style = str(z.get("style") or "punch")
        if style not in ZOOM_STYLES:
            raise PlanError(f"zoom style must be one of {ZOOM_STYLES}")
        zooms.append(Zoom(a, b, target, scale, box, bool(z.get("shake", False)), style))
    zooms.sort(key=lambda z: z.start)
    for x, y in zip(zooms, zooms[1:]):
        if y.start < x.end:
            raise PlanError(f"zooms at {x.start:.1f} s and {y.start:.1f} s overlap")
    replays = []
    for r in data.get("replays") or []:
        a, b = within(_num(r, "start"), _num(r, "end"), "replay", 0.5)
        if b - a > 6:
            raise PlanError("a replay shows at most 6 s")
        speed = _num(r, "speed", 0.5)
        if not 0.25 <= speed <= 1.0:
            raise PlanError("replay speed must be 0.25-1.0")
        target = str(r.get("target", "game"))
        if target not in TARGETS:
            raise PlanError(f"replay target must be one of {TARGETS}")
        after = r.get("after")
        after = b if after is None else min(duration_s, max(0.0, float(after)))
        replays.append(Replay(a, b, speed, target, max(1.0, min(3.0, _num(r, "scale", 1.3))), _box(r),
                              _clean_text(r.get("label", "REPLAY"), 20), after, sfx(r.get("sound"))))
    freezes = []
    for f in data.get("freezes") or []:
        at = _num(f, "at")
        if not 0 <= at < duration_s:
            raise PlanError(f"freeze at {at:.1f} s is outside the clip")
        freezes.append(Freeze(at, max(0.2, min(1.5, _num(f, "seconds", 0.7))), _clean_text(f.get("label", ""), 30),
                              sfx(f.get("sound")), bool(f.get("push", False))))
    texts = []
    for t in data.get("texts") or []:
        a, b = within(_num(t, "start"), _num(t, "end"), "text", 0.4)
        text = _clean_text(t.get("text", ""))
        if not text:
            continue
        pos, size = str(t.get("position", "top")), str(t.get("size", "m"))
        if pos not in POSITIONS or size not in SIZES:
            raise PlanError(f"text position must be {POSITIONS} and size {SIZES}")
        texts.append(Text(a, b, text, pos, size, sfx(t.get("sound"))))
    flashes = tuple(sorted(float(x) for x in data.get("flashes") or [] if 0 <= float(x) < duration_s))
    hook = None
    if data.get("hook"):
        h = data["hook"]
        text = _clean_text(h.get("text", "") if isinstance(h, dict) else h, 40)
        if text:
            hook = Hook(text, max(1.0, min(4.0, float(h.get("seconds", 2.0)) if isinstance(h, dict) else 2.0)))
    effects = []
    for x in data.get("sounds") or []:
        at = _num(x, "at")
        if not 0 <= at < duration_s:
            raise PlanError(f"sound at {at:.1f} s is outside the clip")
        effects.append(Sound(at, sfx(x.get("sound")), max(0.1, min(1.0, _num(x, "volume", 0.7)))))
    effects = sorted((e for e in effects if e.sound), key=lambda e: e.at)
    count = len(effects) + sum(1 for m in (*replays, *freezes, *texts) if m.sound)
    if count > MAX_SOUNDS:
        raise PlanError(f"{count} sound effects is too many (at most {MAX_SOUNDS})")
    music = None
    m = data.get("music")
    if isinstance(m, dict) and str(m.get("track") or "").strip():
        track = str(m["track"]).strip()
        if sounds is None or sounds.get(track, "music") is None:
            raise PlanError(f"there's no music track {track!r} in the sound library")
        start = max(0.0, _num(m, "start", 0.0))
        if start >= max(0.0, sounds.get(track).seconds - 10):
            start = 0.0                      # too near the track's end: from the top
        music = Music(track, max(0.03, min(0.35, _num(m, "volume", 0.12))), start)
    speeds = []
    for x in data.get("speeds") or []:
        a, b = within(_num(x, "start"), _num(x, "end"), "speed section", 0.5)
        speed = _num(x, "speed")
        if not (0.3 <= speed <= 0.8 or 1.25 <= speed <= 4.0):
            raise PlanError("a speed section is slow motion (0.3-0.8) or fast forward (1.25-4)")
        speeds.append(Speed(a, b, speed, bool(x.get("ramp", True))))
    speeds.sort(key=lambda s: s.start)
    for x, y in zip(speeds, speeds[1:]):
        if y.start < x.end:
            raise PlanError(f"speed sections at {x.start:.1f} s and {y.start:.1f} s overlap")
    cuts = [k[1] for k in keep[:-1]]
    transitions = []
    for x in data.get("transitions") or []:
        at, kind = _num(x, "at"), str(x.get("kind") or "whip")
        if kind not in TRANSITIONS:
            raise PlanError(f"transition kind must be one of {TRANSITIONS}")
        cut = min(cuts, key=lambda c: abs(c - at), default=None)
        if cut is None or abs(cut - at) > 0.5:
            raise PlanError(f"a transition goes on a cut (the end of a kept range that isn't the last); "
                            f"{at:.1f} s isn't one" + (f" (cuts: {', '.join(f'{c:.1f}' for c in cuts)})" if cuts else ""))
        if all(t.at != cut for t in transitions):
            transitions.append(Transition(cut, kind))
    if len(transitions) > MAX_TRANSITIONS:
        raise PlanError(f"at most {MAX_TRANSITIONS} transitions")
    plan = EditPlan(tuple(keep), tuple(zooms), tuple(replays), tuple(freezes), tuple(texts), flashes, hook,
                    tuple(str(n)[:200] for n in data.get("notes") or []), tuple(effects), music, tuple(speeds),
                    tuple(sorted(transitions, key=lambda t: t.at)))
    return plan


# The JSON schema Claude's plans follow (structured output), same moves as parse_plan.
_BOX = {"type": "array", "items": {"type": "number"}}
_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
        "zooms": {"type": "array", "items": {"type": "object", "properties": {
            "start": {"type": "number"}, "end": {"type": "number"}, "target": {"type": "string", "enum": list(TARGETS)},
            "scale": {"type": "number"}, "box": _BOX, "shake": {"type": "boolean"},
            "style": {"type": "string", "enum": list(ZOOM_STYLES)}},
            "required": ["start", "end", "target", "scale", "style"], "additionalProperties": False}},
        "speeds": {"type": "array", "items": {"type": "object", "properties": {
            "start": {"type": "number"}, "end": {"type": "number"}, "speed": {"type": "number"},
            "ramp": {"type": "boolean"}}, "required": ["start", "end", "speed", "ramp"], "additionalProperties": False}},
        "transitions": {"type": "array", "items": {"type": "object", "properties": {
            "at": {"type": "number"}, "kind": {"type": "string", "enum": list(TRANSITIONS)}},
            "required": ["at", "kind"], "additionalProperties": False}},
        "replays": {"type": "array", "items": {"type": "object", "properties": {
            "start": {"type": "number"}, "end": {"type": "number"}, "speed": {"type": "number"},
            "target": {"type": "string", "enum": list(TARGETS)}, "scale": {"type": "number"}, "box": _BOX,
            "label": {"type": "string"}, "after": {"type": "number"}},
            "required": ["start", "end", "speed", "target", "scale", "label"], "additionalProperties": False}},
        "freezes": {"type": "array", "items": {"type": "object", "properties": {
            "at": {"type": "number"}, "seconds": {"type": "number"}, "label": {"type": "string"},
            "push": {"type": "boolean"}},
            "required": ["at", "seconds", "label"], "additionalProperties": False}},
        "texts": {"type": "array", "items": {"type": "object", "properties": {
            "start": {"type": "number"}, "end": {"type": "number"}, "text": {"type": "string"},
            "position": {"type": "string", "enum": list(POSITIONS)}, "size": {"type": "string", "enum": list(SIZES)}},
            "required": ["start", "end", "text", "position", "size"], "additionalProperties": False}},
        "flashes": {"type": "array", "items": {"type": "number"}},
        "hook": {"type": "object", "properties": {"text": {"type": "string"}, "seconds": {"type": "number"}},
                 "required": ["text", "seconds"], "additionalProperties": False},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["keep", "zooms", "replays", "freezes", "texts", "flashes", "notes", "speeds", "transitions"],
    "additionalProperties": False,
}


def plan_schema(sfx_ids=(), music_ids=()) -> dict:
    """The plan schema, with the sound moves when the library has sounds (their ids as enums, so
    Claude can't name a file that isn't there)."""
    schema = copy.deepcopy(_PLAN_SCHEMA)
    props = schema["properties"]
    if sfx_ids:
        sound = {"type": "string", "enum": list(sfx_ids)}
        for move in ("replays", "freezes", "texts"):
            props[move]["items"]["properties"]["sound"] = sound
        props["sounds"] = {"type": "array", "items": {"type": "object", "properties": {
            "at": {"type": "number"}, "sound": sound, "volume": {"type": "number"}},
            "required": ["at", "sound", "volume"], "additionalProperties": False}}
        schema["required"].append("sounds")
    if music_ids:
        props["music"] = {"type": "object", "properties": {
            "track": {"type": "string", "enum": list(music_ids)}, "volume": {"type": "number"},
            "start": {"type": "number"}}, "required": ["track", "volume", "start"], "additionalProperties": False}
    return schema


PLAN_SCHEMA = plan_schema()
