"""Our exact-moments plugin (plugins/exact-moments): we name the clip's range, Clips Kitty renders it.

config.yaml → clipskitty.exact_moments. A window with moment_start_s/moment_end_s (seconds of its
cut file) is sent with this plugin as its pipeline, so the clip covers exactly that range (ends
moved to the nearest sentence break) instead of whatever 12-25 s a finder would pick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class ExactSettings:
    enabled: bool = True
    plugin_id: str = "gta6clips/exact-moments"
    version: str = "1.1.0"
    pad_s: float = 2.0           # around a Twitch clip's range
    before_peak_s: float = 30.0  # live: before the chat spike's peak
    after_peak_s: float = 8.0    # live: after it
    snap_s: float = 4.0

    @classmethod
    def from_config(cls, cfg: Config) -> "ExactSettings":
        s = cfg.get("clipskitty.exact_moments", {}) or {}
        d = cls()
        return cls(
            enabled=s.get("enabled", d.enabled) is True,
            plugin_id=str(s.get("id", d.plugin_id)),
            version=str(s.get("version", d.version)),
            pad_s=float(s.get("pad_seconds", d.pad_s)),
            before_peak_s=float(s.get("live_before_peak", d.before_peak_s)),
            after_peak_s=float(s.get("live_after_peak", d.after_peak_s)),
            snap_s=float(s.get("snap_seconds", d.snap_s)),
        )

    def pipeline(self, start_s: float, end_s: float, notes: list[str] | tuple[str, ...] = ()) -> dict:
        """The job's `pipeline` option for one clip covering [start_s, end_s] of the file.

        `notes` say what happens in the moment; the plugin hands them to the AI that writes the
        clip's title, so it names the event instead of "WHAT IS HAPPENING?!".
        """
        settings: dict = {"ranges": f"{start_s:.1f}-{end_s:.1f}", "snap_seconds": self.snap_s}
        text = " || ".join(n.replace("|", "/") for n in notes if n)[:NOTES_MAX]
        if text:
            settings["notes"] = text
        return {"id": self.plugin_id, "version": self.version, "settings": settings}


NOTES_MAX = 800          # the plugin's `notes` setting
NOTE_CHARS = 160         # Clips Kitty cuts each note here
LAUGH = re.compile(r"(l+m+f*a+o+|l+o+l+[lo]*|(ha)+h?|(he)+|x+d+|k+e+k+w+|w+t+f+|o+m+g+|w+)")
CHAT_LAG_S = 20.0        # YouTube live chat reaches us about this late


def twitch_notes(clip_title: str) -> list[str]:
    """Twitch viewers already named the moment when they clipped it."""
    title = " ".join(str(clip_title or "").split())
    return [f'Twitch viewers clipped this moment and called it "{title}"'[:NOTE_CHARS]] if title else []


def chat_notes(messages: list[str], top: int = 4) -> list[str]:
    """The live chat lines repeated most during the moment (laughs and emotes left out): what
    viewers thought happened, in their words."""
    counts: dict[str, int] = {}
    shown: dict[str, str] = {}
    for m in messages:
        text = " ".join(str(m).split())
        key = text.lower().strip("!?. ")
        if len(key) < 4 or len(key) > 90 or key.startswith(":") or key.startswith("@"):
            continue
        letters = [c for c in key if c.isalpha()]
        if len(letters) < 4 or len(set(letters)) < 3:                  # "LMAOOOO", "?????", "kekw"
            continue
        if LAUGH.fullmatch(key.replace(" ", "")) or key in ("clip it", "clip that", "omegalul", "bruh"):
            continue
        counts[key] = counts.get(key, 0) + 1
        shown.setdefault(key, text)
    best = sorted(counts, key=lambda k: (-counts[k], len(k)))[:top]
    if not best:
        return []
    note = "Live chat during this moment: " + "; ".join(f'"{shown[k]}"' for k in best)
    return [note[:NOTE_CHARS]]
