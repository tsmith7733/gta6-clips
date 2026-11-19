"""Keyword matching shared by chat scoring and the spoiler filter.

Keywords match case-insensitively. A keyword that starts or ends with a letter or
digit only matches whole words ("lol" doesn't match "lollipop"). One written as
`re:<pattern>` is a regular expression (for example `re:\\?{3,}` for "?????").
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _pattern(keyword: str) -> str:
    if keyword.startswith("re:"):
        return f"(?:{keyword[3:]})"
    escaped = re.escape(keyword.strip())
    start = r"(?<!\w)" if keyword[:1].isalnum() else ""
    end = r"(?!\w)" if keyword[-1:].isalnum() else ""
    return start + escaped + end


@dataclass(frozen=True)
class KeywordMatcher:
    keywords: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_regex", re.compile("|".join(_pattern(k) for k in self.keywords), re.I)
                           if self.keywords else None)

    @classmethod
    def of(cls, keywords) -> "KeywordMatcher":
        return cls(tuple(str(k) for k in (keywords or []) if str(k).strip()))

    def search(self, text: str) -> str | None:
        """The first matching text, or None."""
        if self._regex is None or not text:
            return None
        found = self._regex.search(text)
        return found.group(0) if found else None

    def hits(self, text: str) -> bool:
        return self.search(text) is not None
