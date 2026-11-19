"""Spike scoring: which moments of a stream are worth sending to Clips Kitty.

Cheap on purpose: no transcription, no LLM. The stream is cut into bins
(scoring.bin_seconds). For each bin we measure
  - chat:     messages per bin
  - keywords: messages with a hype keyword (CLIP IT, KEKW, ?????...)
  - audio:    the loudest momentary loudness (LUFS)
Each signal is z-scored against that stream's own trailing baseline (median and
MAD of the previous scoring.baseline_seconds), so a big channel's normal chat
speed isn't a spike and a small channel's burst still is. The positive z-scores
are combined with scoring.weights. Bins over scoring.threshold form spikes; each
spike's peak becomes a window [peak - lead_in, peak + tail], and overlapping
windows are merged.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .config import Config
from .textmatch import KeywordMatcher

SIGNALS = ("chat", "keywords", "audio")

# Smallest spread used for a z-score, so a quiet stretch (every bin the same)
# doesn't turn one extra message into a huge spike. In each signal's own units.
MIN_SPREAD = {"chat": 1.0, "keywords": 1.0, "audio": 2.0}


@dataclass(frozen=True)
class ScoringSettings:
    bin_s: float = 5.0
    smooth_bins: int = 3
    baseline_s: float = 600.0
    min_baseline_s: float = 120.0
    z_cap: float = 12.0
    threshold: float = 4.5
    weights: dict[str, float] = field(default_factory=lambda: {"chat": 0.45, "keywords": 0.35, "audio": 0.2})
    keywords: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, cfg: Config) -> "ScoringSettings":
        s = cfg.get("scoring", {}) or {}
        d = cls()
        return cls(
            bin_s=float(s.get("bin_seconds", d.bin_s)),
            smooth_bins=int(s.get("smooth_bins", d.smooth_bins)),
            baseline_s=float(s.get("baseline_seconds", d.baseline_s)),
            min_baseline_s=float(s.get("min_baseline_seconds", d.min_baseline_s)),
            z_cap=float(s.get("z_cap", d.z_cap)),
            threshold=float(s.get("threshold", d.threshold)),
            weights={k: float(v) for k, v in (s.get("weights") or d.weights).items()},
            keywords=tuple(str(k) for k in s.get("chat_keywords", []) or []),
        )


@dataclass(frozen=True)
class WindowSettings:
    lead_in_s: float = 120.0
    tail_s: float = 60.0
    max_s: float = 240.0
    max_per_stream: int = 6

    @classmethod
    def from_config(cls, cfg: Config) -> "WindowSettings":
        w = cfg.get("windows", {}) or {}
        d = cls()
        return cls(
            lead_in_s=float(w.get("lead_in_seconds", d.lead_in_s)),
            tail_s=float(w.get("tail_seconds", d.tail_s)),
            max_s=float(w.get("max_seconds", d.max_s)),
            max_per_stream=int(w.get("max_per_stream", d.max_per_stream)),
        )


# -- turning raw data into per-bin series ------------------------------------------

def chat_series(messages: list[tuple[float, str]], n_bins: int, bin_s: float,
                matcher: KeywordMatcher) -> tuple[list[float], list[float]]:
    """(messages per bin, keyword messages per bin)."""
    counts = [0.0] * n_bins
    hits = [0.0] * n_bins
    for offset, text in messages:
        i = int(offset // bin_s)
        if 0 <= i < n_bins:
            counts[i] += 1
            if matcher.hits(text):
                hits[i] += 1
    return counts, hits


def smooth(values: list[float], width: int) -> list[float]:
    """Centred moving average over `width` bins."""
    if width <= 1 or not values:
        return list(values)
    half = width // 2
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def rolling_z(values: list[float], *, baseline_bins: int, min_bins: int, min_spread: float,
              z_cap: float, counts: bool = False) -> list[float]:
    """z of each bin against the median/MAD of the bins before it. 0 until there's enough history.

    For counts (messages per bin) the spread is at least sqrt(median), the natural
    noise of a count, so a busy but steady chat doesn't look spiky.
    """
    out = []
    for i, x in enumerate(values):
        history = values[max(0, i - baseline_bins):i]
        if len(history) < min_bins:
            out.append(0.0)
            continue
        med = statistics.median(history)
        mad = statistics.median(abs(h - med) for h in history)
        floor = max(min_spread, math.sqrt(max(med, 0.0))) if counts else min_spread
        spread = max(1.4826 * mad, floor)
        out.append(max(-z_cap, min(z_cap, (x - med) / spread)))
    return out


@dataclass
class Scores:
    bin_s: float
    combined: list[float]
    z: dict[str, list[float]]          # per signal; only the signals that were available

    def at(self, i: int) -> dict[str, float]:
        return {k: v[i] for k, v in self.z.items()}


def score_stream(settings: ScoringSettings, *, duration_s: float, chat: list[tuple[float, str]] | None,
                 loudness: list[float] | None) -> Scores:
    """Combined spike score per bin. Missing signals are left out and the weights re-normalised."""
    n = max(1, int(math.ceil(duration_s / settings.bin_s)))
    raw: dict[str, list[float]] = {}
    if chat is not None:
        counts, hits = chat_series(chat, n, settings.bin_s, KeywordMatcher.of(settings.keywords))
        raw["chat"], raw["keywords"] = counts, hits
    if loudness:
        raw["audio"] = (list(loudness) + [loudness[-1]] * n)[:n]

    baseline_bins = max(1, int(settings.baseline_s / settings.bin_s))
    min_bins = max(1, int(settings.min_baseline_s / settings.bin_s))
    z = {k: rolling_z(smooth(v, settings.smooth_bins), baseline_bins=baseline_bins, min_bins=min_bins,
                      min_spread=MIN_SPREAD[k], z_cap=settings.z_cap, counts=k != "audio")
         for k, v in raw.items()}

    weights = {k: settings.weights.get(k, 0.0) for k in z}
    total = sum(weights.values())
    combined = [0.0] * n
    if total > 0:
        for k, series in z.items():
            w = weights[k] / total
            for i, value in enumerate(series):
                combined[i] += w * max(0.0, value)
    return Scores(settings.bin_s, combined, z)


# -- spikes and windows -------------------------------------------------------------

@dataclass
class Spike:
    peak_s: float          # centre of the peak bin, seconds from the stream start
    score: float
    signals: dict[str, float]


def find_spikes(scores: Scores, threshold: float) -> list[Spike]:
    """One spike per run of bins at or over the threshold, at that run's highest bin."""
    spikes: list[Spike] = []
    best: int | None = None
    for i, value in enumerate([*scores.combined, -math.inf]):
        if value >= threshold:
            if best is None or value > scores.combined[best]:
                best = i
        elif best is not None:
            spikes.append(Spike((best + 0.5) * scores.bin_s, scores.combined[best], scores.at(best)))
            best = None
    return spikes


@dataclass
class PlannedWindow:
    start_s: float
    end_s: float
    peak_s: float
    score: float
    reasons: str

    @property
    def length_s(self) -> float:
        return self.end_s - self.start_s


def describe(spike: Spike) -> str:
    parts = [f"{name} +{z:.1f}σ" for name, z in sorted(spike.signals.items(), key=lambda kv: -kv[1]) if z >= 1]
    return f"score {spike.score:.1f} at {clock(spike.peak_s)}" + (f" ({', '.join(parts)})" if parts else "")


def plan_windows(spikes: list[Spike], settings: WindowSettings, duration_s: float) -> list[PlannedWindow]:
    """Strongest spikes first; a spike overlapping a kept window widens it if it still fits max_s,
    otherwise it's dropped. Returns at most max_per_stream windows, in stream order."""
    kept: list[PlannedWindow] = []
    for spike in sorted(spikes, key=lambda s: -s.score):
        start = max(0.0, spike.peak_s - settings.lead_in_s)
        end = min(duration_s, spike.peak_s + settings.tail_s) if duration_s else spike.peak_s + settings.tail_s
        overlapping = [w for w in kept if start < w.end_s and end > w.start_s]
        if overlapping:
            w = overlapping[0]
            lo, hi = min(w.start_s, start), max(w.end_s, end)
            if len(overlapping) == 1 and hi - lo <= settings.max_s:
                w.start_s, w.end_s = lo, hi
                w.reasons += f"; {describe(spike)}"
            continue
        if len(kept) >= settings.max_per_stream:
            continue
        kept.append(PlannedWindow(start, end, spike.peak_s, spike.score, describe(spike)))
    return sorted(kept, key=lambda w: w.start_s)


def clock(seconds: float) -> str:
    """h:mm:ss"""
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
