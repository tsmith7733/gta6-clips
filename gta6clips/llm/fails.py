"""Decides whether a clip holds a driving fail, with Claude (Haiku by default), for compilations.

It sees a few frames of the clip's recording (a crash is often silent, or he only says "oh no"),
the clip's transcript with times, and what live chat said. The prompt lives here with the others.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

FAIL_PROMPT = """\
You look at short clips of a streamer playing GTA 6 (Grand Theft Auto VI) and decide whether each \
one holds a driving fail, for a compilation of driving fails.

A driving fail: he crashes or wrecks a vehicle, flips or rolls it, loses control, misses a jump or \
landing, plows through people or things by mistake, drives off a cliff or into water, blows the car up, \
or ruins a getaway or chase by his own bad driving. Funny or dramatic is best.
Not a driving fail: ordinary driving, a cutscene, a shootout or fight without a vehicle mishap, \
something another character does to him, or a crash he talks about but that isn't in the clip.

You get frames from the clip (each labelled with its second), the transcript with times, and chat \
lines from that moment. Times are seconds from the clip's start.

Answer:
- is_fail: true only if the frames or transcript clearly show one.
- start_s / end_s: where the fail itself happens, from the setup (a second or two before) to just \
after his immediate reaction. Keep it short: usually 4-10 seconds. 0 and 0 when there's no fail.
- strength: 1-10, how good it is for the compilation (10: spectacular or hilarious; 5: an ordinary \
crash; under 4: barely a fail).
- kind: two or three words (e.g. "head-on crash", "rolled the car", "missed the jump").
- note: one plain sentence of what happens, under 120 characters, for the person writing the title. \
Describe only what you can see or hear.
"""

FAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "is_fail": {"type": "boolean"},
        "start_s": {"type": "number"},
        "end_s": {"type": "number"},
        "strength": {"type": "integer"},
        "kind": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["is_fail", "start_s", "end_s", "strength", "kind", "note"],
    "additionalProperties": False,
}


class FailJudgeError(Exception):
    """No usable answer came back (the clip is tried again next time)."""


@dataclass(frozen=True)
class ClipEvidence:
    streamer: str
    duration_s: float
    frames: list[tuple[float, bytes]]       # (seconds from the clip's start, JPEG)
    transcript: list[tuple[float, str]]     # (seconds from the clip's start, text)
    chat: list[str]


@dataclass(frozen=True)
class FailVerdict:
    is_fail: bool
    start_s: float      # within the clip
    end_s: float
    strength: int
    kind: str
    note: str


class FailJudge(Protocol):
    model: str

    def judge(self, evidence: ClipEvidence) -> FailVerdict: ...


def parse_verdict(raw: str, duration_s: float) -> FailVerdict:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise FailJudgeError(f"Claude returned invalid JSON: {raw[:200]!r}") from exc
    start = max(0.0, min(float(data.get("start_s") or 0), duration_s))
    end = max(0.0, min(float(data.get("end_s") or 0), duration_s))
    is_fail = bool(data.get("is_fail")) and end - start >= 1.0
    return FailVerdict(is_fail=is_fail, start_s=start if is_fail else 0.0, end_s=end if is_fail else 0.0,
                       strength=max(0, min(10, int(data.get("strength") or 0))) if is_fail else 0,
                       kind=" ".join(str(data.get("kind") or "").split())[:40],
                       note=" ".join(str(data.get("note") or "").split())[:160])


def evidence_content(evidence: ClipEvidence) -> list[dict]:
    content: list[dict] = []
    for t, jpeg in evidence.frames:
        content.append({"type": "text", "text": f"Frame at {t:.1f} s:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(jpeg).decode("ascii")}})
    lines = "\n".join(f"[{t:.1f}] {text}" for t, text in evidence.transcript) or "(no speech)"
    chat = "\n".join(evidence.chat[:40]) or "(none)"
    content.append({"type": "text", "text": f"Streamer: {evidence.streamer}\nClip length: {evidence.duration_s:.0f} s"
                                            f"\n\nTranscript:\n{lines}\n\nLive chat during the clip:\n{chat}"})
    return content


class ClaudeFailJudge:
    """FailJudge backed by the Claude API. Reads ANTHROPIC_API_KEY from the environment."""

    def __init__(self, model: str = "claude-haiku-5-5", effort: str = "low", client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.model = model
        self.effort = effort

    def judge(self, evidence: ClipEvidence) -> FailVerdict:
        import anthropic

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=FAIL_PROMPT,
                messages=[{"role": "user", "content": evidence_content(evidence)}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": FAIL_SCHEMA}},
            )
        except anthropic.AuthenticationError as exc:
            raise FailJudgeError("The Claude API key in .env (ANTHROPIC_API_KEY) was rejected") from exc
        except anthropic.RateLimitError as exc:
            raise FailJudgeError("Claude API rate limit reached; trying again later") from exc
        except anthropic.APIStatusError as exc:
            raise FailJudgeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise FailJudgeError("Couldn't reach the Claude API (network)") from exc
        if response.stop_reason == "refusal":
            return FailVerdict(False, 0.0, 0.0, 0, "", "Claude declined to look at this clip")
        if response.stop_reason == "max_tokens":
            raise FailJudgeError("Claude's answer was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        log.debug("Fail check (request %s): %s", getattr(response, "_request_id", "?"), text)
        return parse_verdict(text, evidence.duration_s)
