"""The spoiler filter: clips that may show major story missions or the ending get `spoiler_hold`.

Held clips never appear in the main review queue (`clips` lists pending_review);
they wait in their own list (`clips --status spoiler_hold`, the Spoilers tab later).

This check is free and needs no API key: keywords in the stream title, the clip's
title and transcript, and the chat around the clip. When in doubt it holds, since
a held clip only costs the owner a look, and a leaked ending can't be taken back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import Config
from .textmatch import KeywordMatcher


@dataclass(frozen=True)
class SpoilerVerdict:
    hold: bool
    reason: str = ""


class SpoilerCheck(Protocol):
    def check(self, *, source_title: str, clip_title: str, transcript: str,
              chat: list[str]) -> SpoilerVerdict: ...


class KeywordSpoilerCheck:
    def __init__(self, keywords, chat_keywords, chat_min_hits: int = 3):
        self.words = KeywordMatcher.of(keywords)
        self.chat_words = KeywordMatcher.of(chat_keywords)
        self.chat_min_hits = max(1, int(chat_min_hits))

    @classmethod
    def from_config(cls, cfg: Config) -> "KeywordSpoilerCheck":
        s = cfg.get("spoilers", {}) or {}
        return cls(s.get("keywords", []), s.get("chat_keywords", s.get("keywords", [])),
                   s.get("chat_min_hits", 3))

    def check(self, *, source_title: str, clip_title: str, transcript: str,
              chat: list[str]) -> SpoilerVerdict:
        for where, text in (("stream title", source_title), ("clip title", clip_title),
                            ("transcript", transcript)):
            word = self.words.search(text)
            if word:
                return SpoilerVerdict(True, f"Possible spoiler: {where} mentions {word!r}")
        hits = [m for m in chat if self.chat_words.hits(m)]
        if len(hits) >= self.chat_min_hits:
            sample = self.chat_words.search(hits[0])
            return SpoilerVerdict(True, f"Possible spoiler: {len(hits)} chat messages around it mention "
                                        f"spoilers (e.g. {sample!r})")
        return SpoilerVerdict(False)


class NoSpoilerCheck:
    def check(self, **_) -> SpoilerVerdict:
        return SpoilerVerdict(False)
