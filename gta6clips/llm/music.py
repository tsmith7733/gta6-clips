"""Decides whether a clip carries a licensed song, with Claude (Haiku by default), before it's reviewed.

GTA 6's radio stations play real, licensed songs, and YouTube's Content ID blocks Shorts where one is
audible (clip 107, "A Song Demands a New Car", 2026-11-25: Fergie's "Glamorous" on the car radio, him
singing along). Claude can't hear the clip, so it reads what gives a song away: lyrics in the
transcript (the transcriber writes down sung words too), the streamer naming or singing a song, and
chat reacting to it. An instrumental playing quietly with nobody mentioning it can't be caught here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

VERDICTS = ("likely", "possible", "none")

MUSIC_PROMPT = """\
You check short clips of a streamer playing GTA 6 (Grand Theft Auto VI) for copyrighted music before \
they're posted as YouTube Shorts. GTA 6's in-game radio plays real, licensed songs, and YouTube's \
Content ID blocks a Short where one can be heard. You can't hear the clip: you get its transcript with \
times (the transcriber also writes down sung lyrics), the live chat from that moment, and its titles.

Signs a licensed song is audible:
- lines that read like song lyrics rather than talk: rhymes, a repeated hook ("the glamorous, glamorous, \
baby"), verses that don't fit the conversation, often in short repeated bursts;
- the streamer singing along to a real song, or naming a song, artist or radio station that's playing;
- him reacting to the music ("this song is a banger", "turn it up", "a song like that needs a new car");
- chat naming the song or artist, posting lyrics, or reacting to the music ("song?", "banger", "vibing").
Not a sign: him humming or making up a silly tune of his own, talking about music that isn't playing, \
the game's ordinary sound effects, or a cutscene with dialogue.

Answer:
- verdict: "likely" when lyrics of a real song are in the transcript, or a song is clearly playing and \
being sung along to or named; "possible" when there are signs music is playing but you can't tell it's \
a real song; "none" otherwise.
- song: the song and artist if you recognise them (e.g. "Glamorous by Fergie"), else "".
- evidence: one plain sentence quoting what gave it away (the lyric line, the chat message), under 160 \
characters. "" for none.
"""

MUSIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "song": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["verdict", "song", "evidence"],
    "additionalProperties": False,
}


class MusicCheckError(Exception):
    """No usable answer came back (the clip is tried again next time)."""


@dataclass(frozen=True)
class MusicEvidence:
    streamer: str
    title: str
    source_title: str
    duration_s: float
    transcript: list[tuple[float, str]]     # (seconds from the clip's start, text)
    chat: list[str]


@dataclass(frozen=True)
class MusicVerdict:
    verdict: str            # likely | possible | none
    song: str
    evidence: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def flagged(self) -> bool:
        return self.verdict == "likely"


class MusicCheck(Protocol):
    model: str

    def check(self, evidence: MusicEvidence) -> MusicVerdict: ...


def evidence_text(e: MusicEvidence) -> str:
    lines = "\n".join(f"[{t:.1f}] {text}" for t, text in e.transcript) or "(no speech)"
    chat = "\n".join(e.chat[:80]) or "(none)"
    return (f"Streamer: {e.streamer}\nClip title: {e.title}\nStream title: {e.source_title or '(unknown)'}\n"
            f"Clip length: {e.duration_s:.0f} s\n\nTranscript:\n{lines}\n\nLive chat during the clip:\n{chat}")


def parse_verdict(raw: str) -> MusicVerdict:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise MusicCheckError(f"Claude returned invalid JSON: {raw[:200]!r}") from exc
    verdict = str(data.get("verdict") or "none")
    if verdict not in VERDICTS:
        raise MusicCheckError(f"Claude's verdict {verdict!r} isn't one of {VERDICTS}")
    return MusicVerdict(verdict, " ".join(str(data.get("song") or "").split())[:120],
                        " ".join(str(data.get("evidence") or "").split())[:240])


class ClaudeMusicCheck:
    """MusicCheck backed by the Claude API. Reads ANTHROPIC_API_KEY from the environment."""

    def __init__(self, model: str = "claude-haiku-5-5", effort: str = "low", client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.model = model
        self.effort = effort

    def check(self, evidence: MusicEvidence) -> MusicVerdict:
        import anthropic

        try:
            response = self._client.messages.create(
                model=self.model, max_tokens=2000, system=MUSIC_PROMPT,
                messages=[{"role": "user", "content": evidence_text(evidence)}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": MUSIC_SCHEMA}})
        except anthropic.AuthenticationError as exc:
            raise MusicCheckError("The Claude API key in .env (ANTHROPIC_API_KEY) was rejected") from exc
        except anthropic.RateLimitError as exc:
            raise MusicCheckError("Claude API rate limit reached; trying again later") from exc
        except anthropic.APIStatusError as exc:
            raise MusicCheckError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise MusicCheckError("Couldn't reach the Claude API (network)") from exc
        usage = response.usage
        if response.stop_reason == "refusal":
            raise MusicCheckError("Claude declined to check this clip")
        if response.stop_reason == "max_tokens":
            raise MusicCheckError("Claude's answer was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        v = parse_verdict(text)
        return MusicVerdict(v.verdict, v.song, v.evidence, getattr(response, "model", None) or self.model,
                            int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0))
