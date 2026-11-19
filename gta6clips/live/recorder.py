"""Records a live stream into 1-minute pieces with ffmpeg, and cuts windows back out of them.

ffmpeg reads the stream's HLS playlists directly (YouTube live keeps video and audio
apart, so there are two inputs) and copies them, without re-encoding, into
`live.segment_seconds` MPEG-TS pieces. Each finished piece is renamed after its start
time in UTC epoch seconds. If ffmpeg stops (the playlist URLs expire after a few hours,
or the network drops) it's started again with a fresh URL, waiting a little longer
after each failure.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

COPY = ("-c", "copy")


@dataclass(frozen=True)
class Segment:
    path: Path
    start_utc: float
    end_utc: float


def _popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


class SegmentRecorder:
    """One ffmpeg at a time, supervised. Call poll() regularly; it returns the pieces finished since."""

    def __init__(self, *, ffmpeg: Path, folder: Path, inputs: Callable[[], Sequence[str]],
                 codec_args: Sequence[str] = COPY, segment_s: float = 60.0,
                 clock: Callable[[], float] = time.time, max_backoff_s: float = 120.0):
        self.ffmpeg = ffmpeg
        self.folder = folder
        self.inputs = inputs            # ffmpeg input arguments (with -map); asked again on every start
        self.codec_args = tuple(codec_args)
        self.segment_s = segment_s
        self.clock = clock
        self.max_backoff_s = max_backoff_s
        self.proc: subprocess.Popen | None = None
        self.runs = 0
        self.failures = 0
        self._run_dir: Path | None = None
        self._run_started = 0.0
        self._lines_done = 0
        self._origin: tuple[float, float] | None = None    # (utc of the run's first piece, its csv start)
        self._next_start = 0.0
        self._stopped = False

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        self._stopped = False
        self._spawn()

    def _spawn(self) -> None:
        self.runs += 1
        self._run_dir = self.folder / f"run_{self.runs:03d}"
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._lines_done, self._origin = 0, None
        self._run_started = self.clock()
        try:
            inputs = list(self.inputs())
        except Exception as exc:  # the stream URL couldn't be read (stream ended, network)
            log.warning("Couldn't start recording: %s", str(exc).splitlines()[0][:300] if str(exc) else exc)
            self.proc = None
            self._schedule_retry()
            return
        cmd = [str(self.ffmpeg), "-hide_banner", "-loglevel", "warning", "-nostats", *inputs,
               *self.codec_args, "-f", "segment", "-segment_time", f"{self.segment_s:g}",
               "-reset_timestamps", "1", "-segment_format", "mpegts",
               "-segment_list", str(self._run_dir / "list.csv"), "-segment_list_type", "csv",
               str(self._run_dir / "seg_%06d.ts")]
        err = (self._run_dir / "ffmpeg.log").open("ab")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err,
                                     shell=False, **_popen_kwargs())
        err.close()
        log.info("Recording to %s (ffmpeg pid %s)", self._run_dir, self.proc.pid)

    def _schedule_retry(self) -> None:
        self.failures += 1
        delay = min(self.max_backoff_s, 5.0 * 2 ** min(self.failures - 1, 6))
        self._next_start = self.clock() + delay
        log.info("Trying to record again in %.0f s", delay)

    def poll(self) -> list[Segment]:
        done = self._collect()
        if self._stopped:
            return done
        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            done += self._collect()
            ran = self.clock() - self._run_started
            log.warning("ffmpeg stopped (exit %s) after %.0f s; see %s", code, ran,
                        self._run_dir / "ffmpeg.log" if self._run_dir else "its log")
            self.proc = None
            if ran > 300:
                self.failures = 0   # it worked for a while: this is an expiry or a blip, not a broken setup
            self._schedule_retry()
        if self.proc is None and self.clock() >= self._next_start:
            self._spawn()
        return done

    def stop(self, timeout_s: float = 15.0) -> list[Segment]:
        """Asks ffmpeg to finish its current piece and exit ('q'), then collects the last pieces."""
        self._stopped = True
        if self.alive:
            assert self.proc is not None
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=10)
        self.proc = None
        return self._collect()

    def _collect(self) -> list[Segment]:
        """Pieces ffmpeg finished (it lists them in list.csv once each is closed)."""
        if self._run_dir is None:
            return []
        listing = self._run_dir / "list.csv"
        if not listing.is_file():
            return []
        lines = [l for l in listing.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        out: list[Segment] = []
        for line in lines[self._lines_done:]:
            try:
                name, start, end = line.rsplit(",", 2)
                start_rel, end_rel = float(start), float(end)
            except ValueError:
                break  # half-written line: next time
            path = self._run_dir / name.strip('"')
            if not path.is_file():
                break
            if self._origin is None:
                # The first piece was closed when its last frame arrived: that's its end, live.
                self._origin = (path.stat().st_mtime - (end_rel - start_rel), start_rel)
            origin_utc, origin_rel = self._origin
            seg_start = origin_utc + (start_rel - origin_rel)
            seg_end = origin_utc + (end_rel - origin_rel)
            final = self.folder / f"{int(seg_start)}.ts"
            if final.exists():
                final = self.folder / f"{int(seg_start)}_{self.runs}.ts"
            path.replace(final)
            out.append(Segment(final, seg_start, seg_end))
            self._lines_done += 1
        return out


def cut_segments(ffmpeg: Path, segments: Sequence[Segment], start_utc: float, end_utc: float,
                 dest: Path) -> Path:
    """Joins the pieces covering [start_utc, end_utc] into one mp4 (no re-encode)."""
    pieces = [s for s in sorted(segments, key=lambda s: s.start_utc) if s.end_utc > start_utc and
              s.start_utc < end_utc and s.path.is_file()]
    if not pieces:
        raise FileNotFoundError("No recorded pieces cover that window (deleted, or never recorded)")
    for a, b in zip(pieces, pieces[1:]):
        if b.start_utc - a.end_utc > 5:
            log.warning("The recording has a %.0f s gap in this window; the clip will jump there",
                        b.start_utc - a.end_utc)
    dest.parent.mkdir(parents=True, exist_ok=True)
    listing = dest.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.path.resolve().as_posix()}'\n" for p in pieces), encoding="utf-8")
    offset = max(0.0, start_utc - pieces[0].start_utc)
    length = min(end_utc, pieces[-1].end_utc) - max(start_utc, pieces[0].start_utc)
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "0",
           "-ss", f"{offset:.3f}", "-i", str(listing), "-t", f"{length:.3f}", "-map", "0", "-c", "copy",
           "-movflags", "+faststart", str(dest)]
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", shell=False)
    listing.unlink(missing_ok=True)
    if result.returncode != 0 or not dest.is_file():
        raise RuntimeError(f"ffmpeg couldn't cut the window (exit {result.returncode}): "
                           f"{result.stderr.strip()[-800:]}")
    return dest
