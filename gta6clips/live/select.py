"""Which live moments become windows. Pure functions, so the rules can be replayed on a recorded VOD.

A VOD scan sees the whole stream and keeps its best N moments. Live, we have to decide
as we go, so a moment is sent only when it is clearly exceptional for this stream:
  - its combined score is over live.selection.threshold (higher than the VOD threshold), or over
    quiet_threshold once quiet_minutes passed with nothing sent (so a quieter stream still gets
    its best moments),
  - at least live.min_signals signals agree (chat speed, hype words, loudness each at
    least live.min_signal_z): a raid or a donation train moves one signal, a real moment
    usually moves several,
  - it's the strongest moment within live.min_gap_seconds: a weaker spike next to a stronger one
    waits for it and is dropped, and a window never repeats footage an earlier one sent,
  - at most live.max_windows_per_hour, so a hyped hour doesn't flood Clips Kitty with
    mediocre windows; when the budget is spent only a moment stronger than everything
    sent in that hour gets through.
A spike is decided once its peak is followed by windows.tail_seconds of recording, so the
window holds the payoff and the reaction.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field, replace

from ..config import Config
from ..scoring import PlannedWindow, Scores, ScoringSettings, Spike, WindowSettings, describe, score_stream

# Bins kept before the part of the stream we decide on, so the rolling baseline is complete.
_HISTORY_MARGIN_S = 60.0
# A window needs at least this much before its peak to show what led up to the moment.
MIN_LEAD_IN_S = 30.0


@dataclass(frozen=True)
class LiveSettings:
    threshold: float = 7.0
    quiet_threshold: float = 6.0       # the bar after quiet_s with nothing sent (a quieter stream)
    quiet_s: float = 5400.0
    min_signals: int = 2
    min_signal_z: float = 2.0
    min_gap_s: float = 480.0
    max_per_hour: int = 3
    min_baseline_s: float = 300.0
    horizon_s: float = 1500.0          # how much recent stream is re-scored each time (baseline + margin)

    @classmethod
    def from_config(cls, cfg: Config) -> "LiveSettings":
        s = cfg.get("live.selection", {}) or {}
        d = cls()
        return cls(
            threshold=float(s.get("threshold", d.threshold)),
            quiet_threshold=float(s.get("quiet_threshold", d.quiet_threshold)),
            quiet_s=float(s.get("quiet_minutes", d.quiet_s / 60)) * 60,
            min_signals=int(s.get("min_signals", d.min_signals)),
            min_signal_z=float(s.get("min_signal_z", d.min_signal_z)),
            min_gap_s=float(s.get("min_gap_seconds", d.min_gap_s)),
            max_per_hour=int(s.get("max_windows_per_hour", d.max_per_hour)),
            min_baseline_s=float(s.get("min_baseline_seconds", d.min_baseline_s)),
        )


def score_recent(scoring: ScoringSettings, *, chat: list[tuple[float, str]] | None,
                 loudness: list[float] | None, start_s: float, end_s: float) -> tuple[Scores, float]:
    """Scores for [start_s, end_s] only (chat offsets and loudness bins are from the stream start).

    Returns the scores and the offset of their first bin. The rolling baseline only looks back,
    so bins after start_s + baseline match a full-stream score.
    """
    bin_s = scoring.bin_s
    first = max(0, int(start_s // bin_s))
    origin = first * bin_s
    duration = max(bin_s, end_s - origin)
    sub_chat = None
    if chat is not None:  # sorted by time: slice instead of scanning a long stream's whole chat
        lo = bisect.bisect_left(chat, (origin,))
        hi = bisect.bisect_left(chat, (end_s,))
        sub_chat = [(t - origin, text) for t, text in chat[lo:hi]]
    sub_loud = None
    if loudness:
        last = int(math.ceil(end_s / bin_s))
        sub_loud = loudness[first:last] or None
    return score_stream(scoring, duration_s=duration, chat=sub_chat, loudness=sub_loud), origin


@dataclass
class Decision:
    peak_s: float
    score: float
    sent: bool
    why: str
    window: PlannedWindow | None = None


def _closed_spikes(scores: Scores, threshold: float, origin: float) -> list[tuple[Spike, bool]]:
    """(spike, run_closed) for each run of bins over the threshold; peaks shifted by origin."""
    out: list[tuple[Spike, bool]] = []
    best: int | None = None
    values = scores.combined
    for i, value in enumerate(values):
        if value >= threshold:
            if best is None or value > values[best]:
                best = i
        elif best is not None:
            out.append((Spike(origin + (best + 0.5) * scores.bin_s, values[best], scores.at(best)), True))
            best = None
    if best is not None:
        out.append((Spike(origin + (best + 0.5) * scores.bin_s, values[best], scores.at(best)), False))
    return out


@dataclass
class LiveSelector:
    """Remembers what it already decided for one stream; call update() as the stream grows."""
    scoring: ScoringSettings
    windows: WindowSettings
    live: LiveSettings
    decided: list[Decision] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Live, the baseline starts when we start recording, so give it longer to settle.
        self.scoring = replace(self.scoring,
                               min_baseline_s=max(self.scoring.min_baseline_s, self.live.min_baseline_s))

    @property
    def sent(self) -> list[Decision]:
        return [d for d in self.decided if d.sent]

    def update(self, *, chat: list[tuple[float, str]] | None, loudness: list[float] | None,
               recorded_until_s: float, recorded_from_s: float = 0.0) -> list[Decision]:
        """New decisions (sent or not) for spikes whose tail has been recorded."""
        start = max(recorded_from_s, recorded_until_s - self.live.horizon_s)
        scores, origin = score_recent(self.scoring, chat=chat, loudness=loudness,
                                      start_s=start - _HISTORY_MARGIN_S, end_s=recorded_until_s)
        # Before this, the slice's baseline is still warming up; those spikes were decided earlier.
        trusted_from = origin + self.scoring.baseline_s if start > recorded_from_s else recorded_from_s
        new: list[Decision] = []
        spikes = _closed_spikes(scores, self.scoring.threshold, origin)
        for spike, closed in spikes:
            if spike.peak_s < trusted_from or spike.peak_s + self.windows.tail_s > recorded_until_s:
                continue
            if not closed and spike.peak_s + self.windows.max_s > recorded_until_s:
                continue  # still going: wait for the peak to settle
            if self._seen(spike.peak_s):
                continue  # the same spike, seen again
            # A stronger moment right next to this one, not decided yet: let it go first, so the
            # weaker one doesn't take its place.
            if any(other.score > spike.score and abs(other.peak_s - spike.peak_s) < self.live.min_gap_s
                   and other.peak_s >= trusted_from and not self._seen(other.peak_s) for other, _ in spikes):
                continue
            decision = self._decide(spike, recorded_from_s, recorded_until_s)
            self.decided.append(decision)
            new.append(decision)
        return new

    def _seen(self, peak_s: float) -> bool:
        return any(abs(d.peak_s - peak_s) < 4 * self.scoring.bin_s for d in self.decided)

    def _decide(self, spike: Spike, recorded_from_s: float, recorded_until_s: float) -> Decision:
        def no(why: str) -> Decision:
            return Decision(spike.peak_s, spike.score, False, why)

        last = max((d.peak_s for d in self.sent), default=recorded_from_s)
        bar = self.live.quiet_threshold if spike.peak_s - last >= self.live.quiet_s else self.live.threshold
        if spike.score < bar:
            return no(f"score {spike.score:.1f} under live threshold {bar:g}")
        strong = [k for k, z in spike.signals.items() if z >= self.live.min_signal_z]
        needed = min(self.live.min_signals, len(spike.signals))
        if len(strong) < needed:
            return no(f"only {len(strong)} signal(s) agree ({', '.join(strong) or 'none'})")
        sent = self.sent
        nearby = [d for d in sent if abs(d.peak_s - spike.peak_s) < self.live.min_gap_s]
        if nearby and spike.score <= max(d.score for d in nearby):
            return no(f"{min(abs(d.peak_s - spike.peak_s) for d in nearby):.0f} s from a stronger window "
                      "already sent")
        hour = [d for d in sent if spike.peak_s - d.peak_s < 3600]
        if len(hour) >= self.live.max_per_hour and spike.score <= max(d.score for d in hour):
            return no(f"{len(hour)} windows already sent this hour, and this one isn't stronger")
        start = max(recorded_from_s, spike.peak_s - self.windows.lead_in_s)
        end = min(recorded_until_s, spike.peak_s + self.windows.tail_s)
        # Don't send the same footage twice: start after an earlier window that overlaps.
        for d in sent:
            if d.window and d.window.start_s < end and d.window.end_s > start and d.window.end_s < spike.peak_s:
                start = max(start, d.window.end_s)
        if spike.peak_s - start < MIN_LEAD_IN_S:
            return no("too close to the previous window to show the lead-up")
        window = PlannedWindow(start, end, spike.peak_s, spike.score, describe(spike))
        return Decision(spike.peak_s, spike.score, True, window.reasons, window)
