"""Writes 3 title options and an on-screen hook for each clip, with Claude.

All prompts for this pipeline live in this package. The spoiler check joins it
in Phase 2.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

MAX_TITLE_CHARS = 100   # YouTube's hard limit
MAX_HOOK_CHARS = 60

SYSTEM_PROMPT = """\
You write YouTube Shorts metadata for clips of streamers playing GTA 6 (Grand Theft Auto VI).

For the clip you're given, write:
- titles: exactly 3 different title options. Each under 70 characters, punchy and specific to \
what happens in this clip. Vary the angle: one about the moment itself, one about the streamer's \
reaction, one curiosity-driven. Use the streamer's name in at most one of them.
- hook: one short line (under 40 characters) shown on screen in the first 2 seconds that teases \
the payoff, e.g. "He did NOT expect that 💀". At most one emoji.

Rules:
- Only describe what the transcript and clip details support. Never invent events, quotes or outcomes.
- No story spoilers: don't name story missions, plot twists or how the story ends.
- No hashtags, no quotation marks around the whole title, no ALL-CAPS titles.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"}},
        "hook": {"type": "string"},
    },
    "required": ["titles", "hook"],
    "additionalProperties": False,
}


class TitleWriterError(Exception):
    """No usable titles came back."""


@dataclass(frozen=True)
class ClipBrief:
    streamer: str
    source_title: str
    clipskitty_title: str
    clipskitty_hook: str
    transcript: str
    duration_s: float


@dataclass(frozen=True)
class TitleSuggestions:
    titles: list[str]   # exactly 3
    hook: str


class TitleWriter(Protocol):
    def write(self, brief: ClipBrief) -> TitleSuggestions: ...


def _clean(text: str, limit: int) -> str:
    text = " ".join(str(text).split()).strip().strip('"').strip()
    return text[:limit].rstrip()


def parse_suggestions(raw: str, fallback_title: str) -> TitleSuggestions:
    """Validates Claude's JSON. Pads with Clips Kitty's own title if fewer than 3 came back."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise TitleWriterError(f"Claude returned invalid JSON: {raw[:200]!r}") from exc
    titles: list[str] = []
    for t in data.get("titles") or []:
        cleaned = _clean(t, MAX_TITLE_CHARS)
        if cleaned and cleaned.lower() not in {x.lower() for x in titles}:
            titles.append(cleaned)
    fallback = _clean(fallback_title, MAX_TITLE_CHARS)
    if fallback and len(titles) < 3 and fallback.lower() not in {x.lower() for x in titles}:
        titles.append(fallback)
    if not titles:
        raise TitleWriterError("Claude returned no usable titles")
    return TitleSuggestions(titles=titles[:3], hook=_clean(data.get("hook", ""), MAX_HOOK_CHARS))


def brief_prompt(brief: ClipBrief) -> str:
    return (
        f"Streamer: {brief.streamer}\n"
        f"Stream title: {brief.source_title or '(unknown)'}\n"
        f"Clip length: {brief.duration_s:.0f} seconds\n"
        f"Clips Kitty's draft title: {brief.clipskitty_title or '(none)'}\n"
        f"Line the clip was picked for: {brief.clipskitty_hook or '(none)'}\n\n"
        f"Transcript of the clip:\n{brief.transcript or '(no speech)'}"
    )


class ClaudeTitleWriter:
    """TitleWriter backed by the Claude API. Reads ANTHROPIC_API_KEY from the environment."""

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.model = model
        self.effort = effort

    def write(self, brief: ClipBrief) -> TitleSuggestions:
        import anthropic

        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": brief_prompt(brief)}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
                # If the model declines, the API retries on a fallback model in the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise TitleWriterError("The Claude API key in .env (ANTHROPIC_API_KEY) was rejected") from exc
        except anthropic.RateLimitError as exc:
            raise TitleWriterError("Claude API rate limit reached; try again in a minute") from exc
        except anthropic.APIStatusError as exc:
            raise TitleWriterError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise TitleWriterError("Couldn't reach the Claude API (network)") from exc

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise TitleWriterError(f"Claude declined to write titles for this clip ({category})")
        if response.stop_reason == "max_tokens":
            raise TitleWriterError("Claude's answer was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        log.debug("Claude titles (request %s): %s", getattr(response, "_request_id", "?"), text)
        return parse_suggestions(text, brief.clipskitty_title)
