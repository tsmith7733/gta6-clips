"""ffmpeg: finding it, and measuring how loud a stream is over time.

ffmpeg is a system dependency. config.yaml -> paths.ffmpeg can point at ffmpeg.exe
or the folder that holds it (a portable build); otherwise it's looked up on PATH.
We don't use the copy that ships inside Clips Kitty.
"""

from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

SILENCE_LUFS = -70.0  # floor for silence (ffmpeg reports -inf / -120)


class MediaToolMissing(Exception):
    """ffmpeg isn't installed or configured."""


def find_ffmpeg(cfg: Config) -> Path:
    configured = str(cfg.get("paths.ffmpeg") or "").strip()
    if configured:
        p = Path(configured)
        if not p.is_absolute():
            p = cfg.base_dir / p
        if p.is_dir():
            p = p / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        if p.is_file():
            return p
        raise MediaToolMissing(f"config.yaml -> paths.ffmpeg points at {p}, but there's no ffmpeg there.")
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    raise MediaToolMissing(
        "ffmpeg isn't installed. Phase 2 needs it to measure audio and cut windows. Either install it "
        "(winget install Gyan.FFmpeg, then open a new PowerShell window) or download a portable build "
        "and set config.yaml -> paths.ffmpeg to its ffmpeg.exe."
    )


def _popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


# ebur128 prints one line per 100 ms:  "t: 12.3   TARGET:-23 LUFS    M: -18.2 S: -20.1 ..."
_EBUR_LINE = re.compile(r"\bt:\s*(?P<t>[0-9.]+)\s.*?\bM:\s*(?P<m>-?inf|nan|-?[0-9.]+)")


def parse_ebur128_line(line: str) -> tuple[float, float] | None:
    """(time_s, momentary loudness in LUFS) from one ebur128 log line, or None."""
    found = _EBUR_LINE.search(line)
    if not found:
        return None
    try:
        t, m = float(found["t"]), float(found["m"])
    except ValueError:
        return None
    if math.isnan(m) or m < SILENCE_LUFS:
        m = SILENCE_LUFS
    return t, m


def bin_loudness(samples: list[tuple[float, float]], bin_s: float) -> list[float]:
    """The loudest momentary reading in each bin; empty bins count as silence."""
    if not samples:
        return []
    n = int(max(t for t, _ in samples) // bin_s) + 1
    bins = [SILENCE_LUFS] * n
    for t, m in samples:
        i = int(t // bin_s)
        if m > bins[i]:
            bins[i] = m
    return bins


def measure_loudness(ffmpeg: Path, media: Path, bin_s: float) -> list[float]:
    """Per-bin loudness of a media file's audio, from ffmpeg's ebur128 filter."""
    cmd = [str(ffmpeg), "-hide_banner", "-nostats", "-nostdin", "-i", str(media), "-vn", "-ac", "1",
           "-af", "ebur128=framelog=info", "-f", "null", "-"]
    log.debug("Measuring loudness of %s", media.name)
    samples: list[tuple[float, float]] = []
    tail: list[str] = []
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", shell=False, **_popen_kwargs())
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            parsed = parse_ebur128_line(line)
            if parsed:
                samples.append(parsed)
            else:
                tail = (tail + [line.rstrip()])[-10:]
        code = proc.wait()
    except BaseException:
        proc.terminate()
        raise
    if code != 0 or not samples:
        raise RuntimeError(f"ffmpeg couldn't measure {media.name} (exit {code}):\n" + "\n".join(tail))
    return bin_loudness(samples, bin_s)


SILENT_DB = -70.0     # a stretch whose loudest moment is quieter than this has no sound (Twitch mutes to -91)


def silent_stretch(ffmpeg: Path, media: Path, start_s: float, end_s: float, chunk_s: float = 8.0) -> float | None:
    """Where the first silent `chunk_s` stretch within [start_s, end_s] of a file begins (seconds), or None.

    Twitch mutes VODs in blocks where it hears copyrighted music, so a cut from a VOD can be silent
    in parts while the clip viewers made still has its sound."""
    t = max(0.0, start_s)
    while t < end_s - 1.0:
        length = min(chunk_s, end_s - t)
        cmd = [str(ffmpeg), "-hide_banner", "-nostats", "-nostdin", "-ss", f"{t:.2f}", "-t", f"{length:.2f}",
               "-i", str(media), "-vn", "-af", "volumedetect", "-f", "null", "-"]
        out = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace", shell=False, **_popen_kwargs())
        m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", out.stderr or "")
        if m is None:
            if "Stream" in (out.stderr or "") and "Audio" not in (out.stderr or ""):
                return t   # no audio track at all
            return None    # couldn't tell: don't block anything on it
        top = float("-inf") if m.group(1) == "-inf" else float(m.group(1))
        if top < SILENT_DB:
            return t
        t += length
    return None
