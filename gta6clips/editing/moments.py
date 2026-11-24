"""Where a clip gets loud (he yells, something explodes): free hints for the editor's punch-ins."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median

from ..media import measure_loudness


@dataclass(frozen=True)
class LoudMoment:
    start: float
    end: float
    peak: float          # seconds of the loudest instant
    over_db: float       # how far over the clip's typical loudness


def loud_moments(values: list[float], bin_s: float, *, above_db: float = 4.0, min_s: float = 0.5,
                 top: int = 6) -> list[LoudMoment]:
    """Runs of bins at least `above_db` over the clip's median loudness, loudest first. Clips Kitty
    normalises the sound, so a yell is only ~4-6 dB over the rest."""
    if not values:
        return []
    base = median(values)
    out, i = [], 0
    while i < len(values):
        if values[i] - base < above_db:
            i += 1
            continue
        j = i
        while j + 1 < len(values) and (values[j + 1] - base >= above_db or
                                       (j + 2 < len(values) and values[j + 2] - base >= above_db)):
            j += 1
        if (j - i + 1) * bin_s >= min_s:
            k = max(range(i, j + 1), key=lambda n: values[n])
            out.append(LoudMoment(i * bin_s, (j + 1) * bin_s, (k + 0.5) * bin_s, round(values[k] - base, 1)))
        i = j + 1
    return sorted(out, key=lambda m: -m.over_db)[:top]


def measure_moments(ffmpeg: Path, media: Path, bin_s: float = 0.25) -> list[LoudMoment]:
    return loud_moments(measure_loudness(ffmpeg, media, bin_s), bin_s)
