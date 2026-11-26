"""Claude as the editor: it writes an edit plan for a clip, we render it, it looks at the result and
fixes what's wrong once, and the edit waits on the review page next to the original.

Every Claude call is priced from its usage and logged in `llm_usage`, and no new edit starts once
the last 7 days' editor spending plus one edit's worth would pass `editor.weekly_budget_usd`.
The owner's notes from the review page (`editor_feedback`) go into every later plan, so the
editor learns the owner's taste. Nothing here uploads anything.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from ..media import _popen_kwargs
from .moments import LoudMoment, measure_moments
from .plan import EditPlan, PlanError, parse_plan, plan_schema
from .render import RenderError, build_shots, effect_ranges, out_time, render, sound_events, sticker_runs
from .sounds import SoundLibrary

log = logging.getLogger(__name__)

# Dollars per million tokens (input, output), from Anthropic's price list (Nov 2026).
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (3.0, 15.0),
          "claude-fable-5-2": (8.0, 40.0)}
EDIT_ESTIMATE_USD = 0.40        # what one edit (plan + check + a fix) may cost, kept back before starting
FRAMES = 12                     # frames of the clip Claude sees when planning
FRAME_WIDTH = 360
STALE_WORK_S = 30 * 60          # an edit still 'working' after this was interrupted


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = next((v for k, v in PRICES.items() if model.startswith(k)), PRICES["claude-opus-5-5"])
    return (input_tokens * inp + output_tokens * out) / 1_000_000


def record_usage(conn: sqlite3.Connection, *, model: str, purpose: str, clip_id: int | None,
                 input_tokens: int, output_tokens: int, now: float | None = None) -> float:
    cost = price(model, input_tokens, output_tokens)
    conn.execute("INSERT INTO llm_usage (at, model, purpose, clip_id, input_tokens, output_tokens, cost_usd) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (now or time.time(), model, purpose, clip_id, input_tokens,
                                                  output_tokens, cost))
    conn.commit()
    return cost


def spent_this_week(conn: sqlite3.Connection, *, purpose_prefix: str = "edit", now: float | None = None) -> float:
    since = (now or time.time()) - 7 * 86400
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE at >= ? AND purpose LIKE ?",
                       (since, purpose_prefix + "%")).fetchone()
    return float(row[0])


EDIT_PROMPT = """\
You are the editor for a YouTube Shorts channel that posts clips of streamers playing GTA 6 (Grand \
Theft Auto VI). Each clip is already a vertical 1080x1920 Short made by another tool: the game fills \
the frame and the streamer's webcam (or avatar) sits in a box, usually in the top part. You turn it \
into something that looks like a professional clipper edited it, and that people watch to the end.

You write an edit plan with these moves (all times are seconds of the clip you were given):
- keep: the ranges to keep, in order. Cut dead air, loading, menus and slow setup, but keep enough \
lead-up that the payoff makes sense. Empty list = keep the whole clip. Viewers swipe away in the first \
2 seconds, so start close to the action.
- zooms: punch-ins. target "cam" (the streamer's face/avatar box, for his reaction: yelling, laughing, \
shock), "game" (the middle of the game), or "box" with a box [x, y, w, h] as fractions of the frame \
(something specific you saw in a frame). scale 1.2-2.2 (cam reactions look best around 1.8-2.2, game \
1.2-1.5). shake: true only for a big impact or a scream. Zooms may not overlap. style: "punch" cuts \
straight in on the beat (a yell, an impact; keep it 0.8-3 s), "smooth" eases in and back out (a \
reaction or a funny line you want to sit on, 1.5-4 s), "push" is a slow push-in over the whole range \
that builds tension before a payoff (2-5 s, ending on the moment; it cuts back out after).
- speeds: parts played slower (0.3-0.8: the impact itself, without stopping the clip like a replay \
does) or faster (1.25-4: fly through dull driving or setup instead of cutting it, so the run still \
makes sense). ramp: true eases into and out of the speed (the classic speed ramp: normal, slow-mo on \
the hit, back to normal). The sound changes speed too, so don't put one over his words. One or two per \
clip at most; speeds may not overlap.
- transitions: on a cut between two kept ranges ("at" = the end of a kept range that isn't the last): \
"whip" (a fast blurred slide, for jumping to another place or later moment) or "zoom" (punches through \
with blur, for jumping to something more intense). Use on 1-3 cuts that jump in time or place; a plain \
cut is better inside one scene, and not every cut needs one.
- replays: a slow-motion "run it back" of the best moment (1-4 s of it, speed 0.4-0.6), inserted right \
after it (`after`, default its end). At most one or two per clip, only for something worth seeing twice.
- freezes: a frame held 0.4-1 s with a short label ("BRUH", "WAIT FOR IT"), on the exact moment before \
or of the payoff (push: true adds a slow push-in while it's frozen). Use sparingly: at most one.
- effects: a look over a stretch of the clip: "glitch" (0.3-1 s of flickering colour shift and grain: \
a bug, a jump scare, a "what just happened"), "rgb_split" (0.3-1 s of split colour on an impact), \
"bw" (black and white: the "it's over" moment), "vignette" (dark edges for a few seconds of tension), \
"pop" (extra colour and contrast on the payoff). A freeze or a replay can take an "effect" over all \
of it: a black-and-white freeze on the fail, a glitch into a replay. 1-3 effects in a clip; they're \
seasoning, and a glitch over talking just looks broken.
- grade: the whole edit's colour. "punchy" (a bit more contrast and colour) suits most action clips; \
"warm", "cool" or "cinematic" (muted, contrasty, dark edges) when they fit the mood; "none" keeps the \
game's own look. Don't stack "cinematic" with a vignette effect.
- stickers: drawn over the picture for 0.8-3 s, each with a point x, y as fractions of the frame (as you see it in the frames you were given, before any zoom; punch-ins are accounted for): kind "emoji" (centred on the point; emoji is one of the names in the schema: laugh, skull, cry, fire, shock, eyes, clown, boom, siren...) for a reaction beat, "arrow" (its tip touches the point, coming from the middle of the frame) to point at the thing that's about to matter (the cop car, the ramp, the guy he's about to hit), "circle" (around the point) to circle that thing. size s, m, l. Never over his face box or a text, and not during a smooth or push zoom or a pushed freeze (the picture moves under them; a punch zoom is fine). 0-3 per clip: an arrow or circle on the setup ("look at this") and an emoji on the reaction is the classic. A sticker only works if its point is exactly on the thing, so only use one where you can see where it is in a frame near that time.
- texts: short captions that add something: a label for what's happening, a punchline, a count \
("FAIL #2"). Not a transcript. 1-5 words, position "top" (over the game, under the webcam), \
"middle" or "bottom"; size s, m or l. Don't cover the streamer's face box. Each shows 1-3 s. \
style: "pop" (scales in with a bounce: the default, fits most), "slam" (drops in big and shakes as it \
lands: an impact, a "BRUH", a verdict), "words" (comes in word by word: a punchline or a line that \
builds, 3-5 words, shown 2 s or more), "shake" (keeps shaking: rage, panic, a scream), "plain" (just \
there). Mix them; not every text should slam. highlight: one or two words of the text drawn in \
yellow (the word that matters: "TWO", "AGAIN", "COPS"), or "" for none.
- flashes: a white flash at the second of an impact.
- hook: the text on screen for the first 1.5-3 seconds, which makes people stay: a tease of the \
payoff, 2-7 words, no clickbait lies. It takes a style and highlight like a text (pop or words \
suit a hook; highlight its key word).
- notes: one short line per choice, saying why (the owner reads these).

Sound, when you're given a sound list (only ids from that list):
- sounds: a sound effect at a second of the clip ("at"), volume 0.2-1.0 (0.7 is normal). Replays, \
freezes and texts can also take a "sound" that plays as they start: a whoosh into a replay, a ding or \
sting on a freeze, a pop on a text.
- music: optionally one background track, volume 0.05-0.3 (0.12 is a good start), "start" = where in \
the track to begin. It fades in and out and dips by itself whenever the clip is loud.
Sound it like a good clipper: an impact sound exactly on the crash (the loud moment's peak, with the \
flash), a whoosh into a replay or a big punch-in, a pop when a text appears. 2-6 effects in a 30-60 s \
clip is plenty; never put one over his punchline's words, and not one on every cut. Music only when it \
helps: a montage or compilation, or a slow clip that needs energy. His voice and the game must stay \
clearly louder, so a talky, funny clip usually gets no music.

Make the edit fit the clip: a funny line gets a punch-in on his face as he says it; a crash gets a \
flash, maybe a shake and a replay; a slow clip gets tightened with keep. Don't do everything at once: \
3-8 moves in a 30-60 s clip is plenty, and a clean clip with a few well-timed punch-ins beats a busy one. \
No story spoilers in any text. Write texts in plain English, without emoji or hashtags.
The edited video must stay under 60 seconds where possible (never over 90).
"""

CHECK_PROMPT = """\
You edited a GTA 6 Short (see the plan) and it has been rendered. You now see frames from the edited \
video, each at a moment where one of your moves should be visible. Check them like a picky editor:
- does each punch-in land on what it should (his face/avatar for "cam", the action for "game"), not on \
empty game or half a face?
- is every text readable, not cut off at the edges, and not covering his face or the action?
- does the hook make sense on its first frame? (Texts animate in: one may still be growing, or in \
"words" style missing its last words, in a frame right at its start.)
- anything that looks broken (black frames, a wrong crop, text over text)?
- is every sticker on its thing: an arrow's tip on what it points at, a circle around it, an emoji not covering his face, the action or a text? Move its x, y in the fix if not (stickers pop in, so one may still be small in a frame right at its start).
Transitions are blurred on purpose, and a smooth or push zoom may be mid-way in a frame. Glitch, \
colour-split, black-and-white and grain are effects you chose, not damage; judge only whether they \
land where they should and don't hide the action.
You can't hear the edit, so keep its sounds and music as they are unless a fix moves the moment they \
belong to (then move them with it).
Answer ok=true if it's good enough to show the owner. Otherwise list the problems and give a fixed \
plan (the whole plan, same moves and rules as before). Only change what's wrong.
"""

# The fixed plan comes back as JSON text, not as a nested schema: the plan schema inside another one
# made the API refuse the check ("compiled grammar is too large", 2026-11-25, with the motion moves).
# parse_plan checks it all the same, and an unusable fix keeps the first render.
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "problems": {"type": "array", "items": {"type": "string"}},
        "fixed_plan_json": {"type": "string"},
    },
    "required": ["ok", "problems", "fixed_plan_json"],
    "additionalProperties": False,
}


def fixed_plan(answer: dict) -> dict | None:
    """The self-check's fixed plan, or None (no fix, or text that isn't a JSON object)."""
    if isinstance(answer.get("fixed_plan"), dict):          # older answers, and the tests' fakes
        return answer["fixed_plan"]
    return json_object(answer.get("fixed_plan_json") or "")


class EditorError(Exception):
    """No usable edit came out (the message says why)."""


@dataclass
class EditBrief:
    clip_id: int
    streamer: str
    title: str
    duration_s: float
    frames: list[tuple[float, bytes]]
    transcript: list[tuple[float, float, str]]
    loud: list[LoudMoment]
    cam_box: list[float] | None
    context: str = ""                       # what Clips Kitty / our notes know (a compilation's parts)
    owner_notes: list[str] = field(default_factory=list)   # the owner's suggestions, newest first
    request: str = ""                       # the owner's note for this clip
    sounds: str = ""                        # the sound library, described (SoundLibrary.describe)
    sfx_ids: tuple[str, ...] = ()
    music_ids: tuple[str, ...] = ()

    def schema(self) -> dict:
        return plan_schema(self.sfx_ids, self.music_ids)


@dataclass
class Answer:
    data: dict
    model: str
    input_tokens: int
    output_tokens: int
    error: str = ""             # a plan that couldn't be read (sent back to Claude like a refused plan)


def json_object(text: str) -> dict | None:
    """The JSON object in an answer, also inside a ```json fence or with words around it; else None."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


class PlanWriter(Protocol):
    def plan(self, brief: EditBrief, problem: str = "") -> Answer: ...

    def check(self, brief: EditBrief, plan: dict, frames: list[tuple[float, str, bytes]]) -> Answer: ...


def _image(jpeg: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpeg).decode("ascii")}}


def brief_content(brief: EditBrief) -> list[dict]:
    content: list[dict] = []
    for t, jpeg in brief.frames:
        content += [{"type": "text", "text": f"Frame at {t:.1f} s:"}, _image(jpeg)]
    lines = "\n".join(f"[{a:.1f}-{b:.1f}] {text}" for a, b, text in brief.transcript) or "(no speech)"
    loud = "\n".join(f"{m.start:.1f}-{m.end:.1f} s (loudest {m.peak:.1f} s), {m.over_db:.0f} dB over normal"
                     for m in sorted(brief.loud, key=lambda m: m.start)) or "(none)"
    cam = (f"[{', '.join(f'{x:.2f}' for x in brief.cam_box)}] (x, y, w, h as fractions of the frame)"
           if brief.cam_box else "unknown (no webcam box: don't use target cam)")
    text = (f"Streamer: {brief.streamer}\nClip title: {brief.title}\nClip length: {brief.duration_s:.1f} s\n"
            f"Webcam box: {cam}\n")
    if brief.context:
        text += f"\nAbout this clip:\n{brief.context}\n"
    text += f"\nTranscript (seconds of the clip):\n{lines}\n\nLoud moments (he yells, something bangs):\n{loud}\n"
    text += (f"\n{brief.sounds}\n" if brief.sounds else "\n(No sound library: don't use sounds or music.)\n")
    if brief.owner_notes:
        text += "\nThe channel owner's suggestions from earlier edits (follow them):\n" + \
            "\n".join(f"- {n}" for n in brief.owner_notes) + "\n"
    if brief.request:
        text += f"\nThe owner's note for THIS clip (most important):\n{brief.request}\n"
    content.append({"type": "text", "text": text})
    return content


class ClaudePlanWriter:
    """PlanWriter backed by the Claude API (Opus by default). Reads ANTHROPIC_API_KEY from the environment."""

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.model = model
        self.effort = effort

    def _ask(self, system: str, content: list[dict], schema: dict, *, strict: bool = True) -> Answer:
        """strict: the answer is held to `schema` (structured output). Otherwise the schema is in the
        prompt and the answer is JSON text: the plan schema grew past what the API compiles ("compiled
        grammar is too large", 2026-11-25), and parse_plan checks every plan anyway."""
        import anthropic

        output = {"effort": self.effort}
        if strict:
            output["format"] = {"type": "json_schema", "schema": schema}
        else:
            content = content + [{"type": "text", "text": "Answer with only the plan: one JSON object, no other "
                                  "text, following this JSON Schema (every field is required; use [] for none, "
                                  "\"\" for an unused effect, sound, track or hook, -1 for a replay's default "
                                  "`after`):\n" + json.dumps(schema, separators=(",", ":"))}]
        try:
            response = self._client.beta.messages.create(
                model=self.model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": content}],
                output_config=output,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        except anthropic.AuthenticationError as exc:
            raise EditorError("The Claude API key in .env (ANTHROPIC_API_KEY) was rejected") from exc
        except anthropic.RateLimitError as exc:
            raise EditorError("Claude API rate limit reached; trying again later") from exc
        except anthropic.APIStatusError as exc:
            raise EditorError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise EditorError("Couldn't reach the Claude API (network)") from exc
        usage = response.usage
        answer = Answer({}, getattr(response, "model", None) or self.model,
                        int(getattr(usage, "input_tokens", 0) or 0) + int(getattr(usage, "cache_read_input_tokens", 0) or 0)
                        + int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
                        int(getattr(usage, "output_tokens", 0) or 0))
        if response.stop_reason == "refusal":
            raise EditorError("Claude declined to edit this clip")
        if response.stop_reason == "max_tokens":
            raise EditorError("Claude's answer was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json_object(text)
        if data is None:
            if strict:
                raise EditorError(f"Claude returned invalid JSON: {text[:200]!r}")
            answer.error = f"your answer wasn't one valid JSON object (it began {text[:80]!r})"
        else:
            answer.data = data
        return answer

    def plan(self, brief: EditBrief, problem: str = "") -> Answer:
        content = brief_content(brief)
        if problem:
            content.append({"type": "text", "text": f"Your last plan was refused: {problem}. Write it again."})
        return self._ask(EDIT_PROMPT, content, brief.schema(), strict=False)

    def check(self, brief: EditBrief, plan: dict, frames: list[tuple[float, str, bytes]]) -> Answer:
        content: list[dict] = [{"type": "text", "text": f"Clip title: {brief.title}\nClip length: "
                                f"{brief.duration_s:.1f} s\nWebcam box: {brief.cam_box}\n\nYour plan:\n"
                                f"{json.dumps(plan)}\n\nFrames of the edited video:"}]
        for t, what, jpeg in frames:
            content += [{"type": "text", "text": f"At {t:.1f} s of the edit (should show: {what}):"}, _image(jpeg)]
        content.append({"type": "text", "text": "If it needs fixing, fixed_plan_json is the whole fixed plan as "
                        "JSON, with the same fields as your plan above; otherwise an empty string."})
        return self._ask(CHECK_PROMPT, content, CHECK_SCHEMA)


def frames_at(ffmpeg: Path, media: Path, times: list[float], width: int = FRAME_WIDTH) -> list[tuple[float, bytes]]:
    out = []
    for t in times:
        proc = subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.2f}",
                               "-i", str(media), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5",
                               "-f", "image2pipe", "-vcodec", "mjpeg", "-"], capture_output=True, **_popen_kwargs())
        if proc.returncode == 0 and proc.stdout:
            out.append((round(t, 1), proc.stdout))
    return out


def check_points(plan: EditPlan, duration_s: float, cam_box=None, limit: int = 10) -> list[tuple[float, str]]:
    """Moments of the edited video worth looking at, with what each should show."""
    shots = build_shots(plan, duration_s, cam_box)
    points: list[tuple[float, str]] = []
    if plan.hook:
        points.append((min(1.0, plan.hook.seconds * 0.6), f"the hook text '{plan.hook.text}'"))
    for z in plan.zooms:
        if z.style == "push":   # look where it ends up
            points.append((out_time(shots, z.end - 0.1), f"the end of a slow push-in ({z.scale:.1f}x) on the {z.target}"))
        else:
            kind = "punch-in" if z.style == "punch" else "smooth zoom"
            points.append((out_time(shots, (z.start + z.end) / 2), f"a {z.scale:.1f}x {kind} on the {z.target}"))
    for t in plan.texts:
        points.append((out_time(shots, (t.start + t.end) / 2), f"the text '{t.text}' at the {t.position}"))
    for a, b, kind in effect_ranges(plan, shots):
        points.append(((a + b) / 2, f"the {kind.replace('_', ' ')} effect"))
    for run in sticker_runs(plan, shots):
        st = run.sticker
        what = f"the {st.emoji} emoji sticker" if st.kind == "emoji" else f"the {st.kind} sticker"
        points.append((min(run.start + 0.6, (run.start + run.end) / 2), f"{what} at x={st.x:.2f}, y={st.y:.2f}"))
    clock = 0.0
    for s in shots:
        if s.replay or s.kind == "freeze":
            what = "a frozen frame" if s.kind == "freeze" else "a slow replay"
            points.append((clock + s.seconds / 2, what + (f" labelled '{s.label}'" if s.label else "")))
        clock += s.seconds
    points.sort()
    merged: list[tuple[float, str]] = []
    for t, what in points:
        if merged and t - merged[-1][0] < 0.4:
            merged[-1] = (merged[-1][0], merged[-1][1] + "; " + what)
        else:
            merged.append((t, what))
    if len(merged) > limit:   # spread the ones we keep over the edit
        step = len(merged) / limit
        merged = [merged[int(i * step)] for i in range(limit)]
    return merged


@dataclass(frozen=True)
class EditorSettings:
    enabled: bool = True
    streamers: tuple[str, ...] = ()        # empty: every streamer
    weekly_budget_usd: float = 10.0
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    per_run: int = 2
    every_s: float = 600.0
    self_check: bool = True
    owner_notes: int = 15

    @classmethod
    def from_config(cls, cfg) -> "EditorSettings":
        g = lambda k, d: cfg.get(f"editor.{k}", d)   # noqa: E731
        return cls(enabled=g("enabled", True) is True, streamers=tuple(g("streamers", []) or ()),
                   weekly_budget_usd=float(g("weekly_budget_usd", 10)), model=str(g("model", cls.model)),
                   effort=str(g("effort", cls.effort)), per_run=int(g("per_run", 2)),
                   every_s=float(g("every_minutes", 10)) * 60, self_check=g("self_check", True) is True,
                   owner_notes=int(g("owner_notes", 15)))


@dataclass
class EditResult:
    edit_id: int
    state: str
    path: str = ""
    cost_usd: float = 0.0
    error: str = ""
    notes: list[str] = field(default_factory=list)


class Editor:
    def __init__(self, *, conn: sqlite3.Connection, client, ffmpeg: Path, writer: PlanWriter, edits_dir: Path,
                 settings: EditorSettings, clock: Callable[[], float] = time.time,
                 sounds: SoundLibrary | None = None):
        self.sounds = sounds
        self.conn = conn
        self.client = client
        self.ffmpeg = ffmpeg
        self.writer = writer
        self.edits_dir = edits_dir
        self.settings = settings
        self.clock = clock

    # -- the budget --------------------------------------------------------------------
    def spent(self) -> float:
        return spent_this_week(self.conn, now=self.clock())

    def can_afford(self) -> bool:
        return self.spent() + EDIT_ESTIMATE_USD <= self.settings.weekly_budget_usd

    def _paid(self, answer: Answer, purpose: str, clip_id: int) -> float:
        return record_usage(self.conn, model=answer.model, purpose=purpose, clip_id=clip_id,
                            input_tokens=answer.input_tokens, output_tokens=answer.output_tokens, now=self.clock())

    # -- which clips -------------------------------------------------------------------
    def busy(self, clip_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM edits WHERE ck_clip_id = ? AND state = 'working' AND updated_at > ?",
                                 (clip_id, self.clock() - STALE_WORK_S)).fetchone() is not None

    def waiting(self) -> list[int]:
        """Clips waiting for review that have never been edited, newest first."""
        names = self.settings.streamers
        where = f" AND c.streamer IN ({','.join('?' * len(names))})" if names else ""
        rows = self.conn.execute(
            "SELECT c.ck_clip_id FROM clips c WHERE c.status IN ('pending_review', 'spoiler_hold')"
            f"{where} AND NOT EXISTS (SELECT 1 FROM edits e WHERE e.ck_clip_id = c.ck_clip_id) "
            "ORDER BY c.created_at DESC", names).fetchall()
        return [int(r[0]) for r in rows]

    def run_once(self) -> list[EditResult]:
        if not self.settings.enabled:
            return []
        done = []
        for clip_id in self.waiting()[: self.settings.per_run]:
            if not self.can_afford():
                log.info("The editor has spent $%.2f of its $%.2f this week; no more edits until some of it "
                         "is 7 days old.", self.spent(), self.settings.weekly_budget_usd)
                break
            done.append(self.edit(clip_id))
        return done

    # -- one edit ----------------------------------------------------------------------
    def _feedback(self) -> list[str]:
        rows = self.conn.execute("SELECT text FROM editor_feedback ORDER BY id DESC LIMIT ?",
                                 (self.settings.owner_notes,)).fetchall()
        return [r[0] for r in rows]

    def brief(self, clip_id: int, source: Path, request: str = "") -> EditBrief:
        row = self.conn.execute("SELECT * FROM clips WHERE ck_clip_id = ?", (clip_id,)).fetchone()
        if row is None:
            raise EditorError(f"We don't track clip {clip_id}.")
        theirs = next((c for c in self.client.video_clips(row["ck_video_id"]) if int(c["id"]) == clip_id), None)
        if theirs is None:
            raise EditorError(f"Clips Kitty doesn't have clip {clip_id} any more.")
        duration = float(theirs["end_s"]) - float(theirs["start_s"])
        if not source.is_file():
            source.parent.mkdir(parents=True, exist_ok=True)
            self.client.download_media(clip_id, source)
        places = ((theirs.get("render_opts") or {}).get("gaming") or {}).get("places") or {}
        lines = self.client.captions(clip_id)
        frames = frames_at(self.ffmpeg, source, [duration * (i + 0.5) / FRAMES for i in range(FRAMES)])
        notes = json.loads(row["notes"] or "[]")
        return EditBrief(clip_id=clip_id, streamer=row["streamer"], title=row["title"], duration_s=duration,
                         frames=frames, cam_box=places.get("cam"),
                         transcript=[(float(x.get("start") or 0), float(x.get("end") or 0), str(x.get("text") or ""))
                                     for x in lines],
                         loud=measure_moments(self.ffmpeg, source), context="\n".join(notes[:8]),
                         owner_notes=self._feedback(), request=request,
                         sounds=self.sounds.describe() if self.sounds else "",
                         sfx_ids=tuple(self.sounds.ids("sfx")) if self.sounds else (),
                         music_ids=tuple(self.sounds.ids("music")) if self.sounds else ())

    def _start(self, clip_id: int, request: str) -> int:
        now = self.clock()
        cur = self.conn.execute("INSERT INTO edits (ck_clip_id, state, request, created_at, updated_at) "
                                "VALUES (?, 'working', ?, ?, ?)", (clip_id, request, now, now))
        self.conn.commit()
        return int(cur.lastrowid)

    def _finish(self, edit_id: int, **cols) -> None:
        cols["updated_at"] = self.clock()
        sets = ", ".join(f"{k} = ?" for k in cols)
        self.conn.execute(f"UPDATE edits SET {sets} WHERE id = ?", (*cols.values(), edit_id))
        self.conn.commit()

    def _plan(self, brief: EditBrief, clip_id: int) -> tuple[dict, EditPlan, float]:
        cost, problem = 0.0, ""
        for _ in range(2):
            answer = self.writer.plan(brief, problem)
            cost += self._paid(answer, "edit plan", clip_id)
            if answer.error:
                problem = answer.error
                continue
            try:
                return answer.data, parse_plan(answer.data, brief.duration_s, self.sounds), cost
            except PlanError as exc:
                problem = str(exc)
        raise EditorError(f"Claude's plan was refused twice: {problem}")

    def _sound_notes(self, plan: EditPlan, brief: EditBrief) -> list[str]:
        """What's heard, for the owner, and the credit line a music track's licence asks for."""
        if self.sounds is None:
            return []
        shots = build_shots(plan, brief.duration_s, brief.cam_box)
        out = []
        events = sound_events(plan, shots, self.sounds)
        if events:
            out.append("Sounds: " + ", ".join(f"{snd.id} at {t:.1f} s" for t, snd, _ in events))
        track = self.sounds.get(plan.music.track) if plan.music else None
        if track is not None:
            out.append(f"Music: {track.id} ({track.description})")
            if track.credit:
                out.append(f"The music needs this credit in the description: {track.credit}")
        return out

    def edit(self, clip_id: int, request: str = "") -> EditResult:
        """Plans, renders and checks one edit of `clip_id`. Never raises for an ordinary failure:
        the edit row says what went wrong."""
        if self.busy(clip_id):
            return EditResult(0, "busy", error="This clip is already being edited.")
        edit_id = self._start(clip_id, request)
        cost, notes = 0.0, []
        try:
            source = self.edits_dir / f"clip{clip_id}_source.mp4"
            brief = self.brief(clip_id, source, request)
            data, plan, cost = self._plan(brief, clip_id)
            dest = self.edits_dir / f"clip{clip_id}_e{edit_id}.mp4"
            render(self.ffmpeg, source, plan, dest, duration_s=brief.duration_s, cam_box=brief.cam_box,
                   sounds=self.sounds)
            notes = list(plan.notes)
            if self.settings.self_check:
                points = check_points(plan, brief.duration_s, brief.cam_box)
                shots = frames_at(self.ffmpeg, dest, [t for t, _ in points])
                frames = [(t, what, jpeg) for (t, what), (_, jpeg) in zip(points, shots)]
                try:
                    answer = self.writer.check(brief, data, frames)
                except EditorError as exc:      # the first render stands
                    answer = None
                    notes.append(f"The self-check couldn't run ({exc}); this is the first version, unchecked.")
                if answer is not None:
                    cost += self._paid(answer, "edit check", clip_id)
                    problems = [str(p) for p in answer.data.get("problems") or []]
                    fix = fixed_plan(answer.data) if not answer.data.get("ok") else None
                    if fix:
                        trial = dest.with_name(dest.stem + "_fix.mp4")
                        try:
                            fixed = parse_plan(fix, brief.duration_s, self.sounds)
                            render(self.ffmpeg, source, fixed, trial, duration_s=brief.duration_s,
                                   cam_box=brief.cam_box, sounds=self.sounds)
                            trial.replace(dest)
                            data, plan = fix, fixed
                            notes = list(fixed.notes) + [f"Self-check of the render: {p}" for p in problems]
                        except (PlanError, RenderError) as exc:
                            trial.unlink(missing_ok=True)
                            notes.append(f"The self-check wanted a fix that didn't work ({exc}); this is the first "
                                         "version.")
                    elif problems:
                        notes += [f"Self-check: {p}" for p in problems]
            notes += self._sound_notes(plan, brief)
            seconds = sum(s.seconds for s in build_shots(plan, brief.duration_s, brief.cam_box))
            dest.with_suffix(".plan.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
            self.conn.execute("UPDATE edits SET state = 'replaced', updated_at = ? WHERE ck_clip_id = ? AND "
                              "state = 'ready' AND id != ?", (self.clock(), clip_id, edit_id))
            self._finish(edit_id, state="ready", path=str(dest), plan=json.dumps(data), notes=json.dumps(notes),
                         cost_usd=cost, seconds=round(seconds, 1))
            log.info("Edited clip %s (edit %s, %.1f s, $%.2f)", clip_id, edit_id, seconds, cost)
            return EditResult(edit_id, "ready", str(dest), cost, notes=notes)
        except (EditorError, PlanError, RenderError, OSError) as exc:
            log.warning("Couldn't edit clip %s: %s", clip_id, exc)
            self._finish(edit_id, state="failed", error=str(exc)[:500], cost_usd=cost)
            return EditResult(edit_id, "failed", cost_usd=cost, error=str(exc))
        except Exception as exc:   # Clips Kitty or ffmpeg hiccuped: the row must not stay 'working'
            self._finish(edit_id, state="failed", error=f"{type(exc).__name__}: {exc}"[:500], cost_usd=cost)
            raise


def latest_edit(conn: sqlite3.Connection, clip_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM edits WHERE ck_clip_id = ? AND state != 'replaced' ORDER BY id DESC LIMIT 1",
                        (clip_id,)).fetchone()


class EditorWorker:
    """Runs edits in a background thread, one at a time, each run with its own database connection
    (the review page's and the watcher's connections belong to their own threads)."""

    def __init__(self, make_editor: Callable[[], Editor]):
        self.make_editor = make_editor
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._queue: list[tuple[int, str]] = []

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request(self, clip_id: int, note: str = "") -> None:
        with self._lock:
            if all(c != clip_id for c, _ in self._queue):
                self._queue.append((clip_id, note))
        self._kick()

    def tick(self) -> None:
        """Edits waiting clips in the background (the watcher calls this every few minutes)."""
        self._kick(auto=True)

    def _kick(self, auto: bool = False) -> None:
        with self._lock:
            if self.running():
                return
            self._thread = threading.Thread(target=self._run, args=(auto,), name="editor", daemon=True)
            self._thread.start()

    def _run(self, auto: bool) -> None:
        editor = None
        try:
            editor = self.make_editor()
            while True:
                with self._lock:
                    job = self._queue.pop(0) if self._queue else None
                    if job is None and not auto:
                        self._thread = None      # a request from now on starts a new thread
                        return
                if job is None:
                    auto = False
                    editor.run_once()
                    continue
                if not editor.can_afford():
                    log.warning("The editor's weekly budget ($%.2f) is used up; clip %s wasn't edited.",
                                editor.settings.weekly_budget_usd, job[0])
                    continue
                editor.edit(*job)
        except Exception:
            log.exception("The editor stopped on a problem")
            with self._lock:
                self._thread = None
        finally:
            if editor is not None:
                editor.conn.close()
