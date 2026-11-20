"""Our exact-moments plugin (plugins/exact-moments): we name the clip's range, Clips Kitty renders it.

config.yaml → clipskitty.exact_moments. A window with moment_start_s/moment_end_s (seconds of its
cut file) is sent with this plugin as its pipeline, so the clip covers exactly that range (ends
moved to the nearest sentence break) instead of whatever 12-25 s a finder would pick.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class ExactSettings:
    enabled: bool = True
    plugin_id: str = "gta6clips/exact-moments"
    version: str = "1.0.0"
    pad_s: float = 12.0          # around a Twitch clip's range
    before_peak_s: float = 50.0  # live: before the chat spike's peak
    after_peak_s: float = 18.0   # live: after it
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

    def pipeline(self, start_s: float, end_s: float) -> dict:
        """The job's `pipeline` option for one clip covering [start_s, end_s] of the file."""
        return {"id": self.plugin_id, "version": self.version,
                "settings": {"ranges": f"{start_s:.1f}-{end_s:.1f}", "snap_seconds": self.snap_s}}
