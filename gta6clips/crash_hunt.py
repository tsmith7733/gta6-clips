"""Finds driving fails in a streamer's finished GTA 6 streams (VODs), for compilations.

1. The chat replay (cached in `chat_messages`, the same one `scan` uses) is scored for crash talk
   ("can't drive", "#1 driver", "crashed", plane...) together with laughs; the strongest stretches,
   at least `min_gap_seconds` apart, are the candidates.
2. Each candidate's frames are read straight from the VOD's low-resolution stream with ffmpeg (one
   yt-dlp lookup per VOD, no full download: lookups in bulk get the PC flagged as a bot).
3. Claude (Haiku, `llm/fails.py`) judges each candidate; the fails go into `vod_fails`.
`Compiler.build(..., include_vods=True)` then downloads the chosen ones in HD and joins them with
the fails found in our own clips.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import db
from .config import Config
from .downloads import Fetcher
from .exact import LAUGH
from .llm.fails import ClipEvidence, FailJudge, FailJudgeError
from .media import _popen_kwargs
from .scan import ScanError, _load_chat
from .sources import SourceInfo, looks_like_gta6, probe
from .streamers import Streamer, permitted_streamer_for
from .vod import NotGta6

log = logging.getLogger(__name__)

CRASH = re.compile(
    r"(crash|wreck|totall?ed|can'?t drive|cannot drive|#1 (gta 6 )?driver|number one driver|driv(e|er|ing)\b|"
    r"licen[cs]e|flip|rolled|seat ?belt|brakes|cliff|bridge|canal|into the water|plane|pilot|landing|"
    r"road rage|hit and run|💥|🚗|🚙|✈|🛬)", re.IGNORECASE)


class HuntError(Exception):
    pass


@dataclass(frozen=True)
class HuntSettings:
    bin_s: float = 10.0
    max_candidates: int = 40        # per stream, strongest first
    min_crash_hits: int = 4         # crash messages within the 30 s around a candidate
    min_gap_s: float = 90.0
    before_s: float = 45.0          # what's looked at around a candidate: the event comes before chat
    after_s: float = 10.0
    frames: int = 10
    low_format: str = "bv*[height<=360][ext=mp4]/bv*[height<=360]/worst"

    @classmethod
    def from_config(cls, cfg: Config) -> "HuntSettings":
        s = cfg.get("compilations.hunt", {}) or {}
        d = cls()
        return cls(bin_s=float(s.get("bin_seconds", d.bin_s)),
                   max_candidates=int(s.get("max_candidates", d.max_candidates)),
                   min_crash_hits=int(s.get("min_crash_hits", d.min_crash_hits)),
                   min_gap_s=float(s.get("min_gap_seconds", d.min_gap_s)),
                   before_s=float(s.get("before_seconds", d.before_s)),
                   after_s=float(s.get("after_seconds", d.after_s)),
                   frames=int(s.get("frames", d.frames)),
                   low_format=str(s.get("low_format", d.low_format)))


@dataclass(frozen=True)
class Candidate:
    peak_s: float          # where chat reacts most (VOD seconds)
    crash_hits: int
    score: float


@dataclass
class HuntResult:
    title: str
    candidates: int = 0
    judged: int = 0
    fails: list[tuple[float, int, str]] = field(default_factory=list)   # (start_s, strength, note)


def crash_candidates(chat: list[tuple[float, str]], duration_s: float, s: HuntSettings) -> list[Candidate]:
    """The stretches where chat talks about crashing (and laughs), strongest first, min_gap_s apart."""
    n = int(duration_s // s.bin_s) + 1
    crash = [0] * n
    laugh = [0] * n
    for t, text in chat:
        i = int(t // s.bin_s)
        if not 0 <= i < n:
            continue
        if CRASH.search(text):
            crash[i] += 1
        elif LAUGH.fullmatch(text.strip().lower().rstrip("!?. ")):
            laugh[i] += 1
    out = []
    for i in range(n):
        hits = sum(crash[max(0, i - 1):i + 2])           # 30 s around the bin
        if hits < s.min_crash_hits:
            continue
        laughs = sum(laugh[max(0, i - 1):i + 2])
        # Ties go to the bin where chat itself says most (the middle of the reaction).
        out.append(Candidate(peak_s=(i + 0.5) * s.bin_s, crash_hits=hits,
                             score=hits + 0.3 * laughs + 0.01 * crash[i]))
    chosen: list[Candidate] = []
    for c in sorted(out, key=lambda c: -c.score):
        if all(abs(c.peak_s - x.peak_s) >= s.min_gap_s for x in chosen):
            chosen.append(c)
        if len(chosen) >= s.max_candidates:
            break
    return chosen


def resolve_low_url(info: SourceInfo, video_format: str) -> tuple[str, dict]:
    """The low-resolution video stream's URL and the headers it needs (one yt-dlp lookup)."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "format": video_format}
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(info.url, download=False)
    fmt = (data.get("requested_formats") or [data])[0]
    if not fmt.get("url"):
        raise HuntError(f"yt-dlp found no low-resolution stream for {info.url}")
    return str(fmt["url"]), dict(fmt.get("http_headers") or {})


def frames_from_url(ffmpeg: Path, url: str, headers: dict, start_s: float, end_s: float,
                    count: int) -> list[tuple[float, bytes]]:
    """`count` frames spread over [start_s, end_s] of a remote video, read with HTTP range requests."""
    span = max(1.0, end_s - start_s)
    with tempfile.TemporaryDirectory() as tmp:
        args = [str(ffmpeg), "-hide_banner", "-loglevel", "error"]
        if headers:
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        args += ["-ss", f"{start_s:.2f}", "-t", f"{span:.2f}", "-i", url, "-an",
                 "-vf", f"fps={count / span:.4f},scale=640:-2", "-q:v", "5", str(Path(tmp) / "f%03d.jpg")]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180, **_popen_kwargs())
        files = sorted(Path(tmp).glob("f*.jpg"))
        if proc.returncode != 0 and not files:
            raise HuntError(f"ffmpeg couldn't read the stream: {proc.stderr.strip()[-300:]}")
        step = span / count
        return [(round(step * (i + 0.5), 1), f.read_bytes()) for i, f in enumerate(files[:count])]


FrameReader = Callable[[str, dict, float, float, int], list[tuple[float, bytes]]]


class FailHunter:
    def __init__(self, *, cfg: Config, conn: sqlite3.Connection, streamers: list[Streamer], judge: FailJudge,
                 fetcher: Fetcher, frames: FrameReader,
                 resolve: Callable[[SourceInfo, str], tuple[str, dict]] = resolve_low_url,
                 probe_fn: Callable[[str], SourceInfo] = probe):
        self.cfg = cfg
        self.conn = conn
        self.streamers = streamers
        self.judge = judge
        self.fetcher = fetcher
        self.frames = frames
        self.resolve = resolve
        self.probe = probe_fn
        self.settings = HuntSettings.from_config(cfg)

    def hunt(self, url: str, *, allow_non_gta: bool = False) -> HuntResult:
        info = self.probe(url)
        streamer = permitted_streamer_for(self.streamers, info.platform, *info.channel_values)   # the gate
        if not allow_non_gta and not looks_like_gta6(info.title, self.cfg.get("gta6.keywords", [])):
            raise NotGta6(f"Not a GTA 6 stream, so it wasn't searched: {info.title!r}")
        if info.live_status in ("is_live", "is_upcoming", "post_live"):
            raise HuntError("This stream is still live or still being processed; try again once it's finished.")
        if not info.duration:
            raise HuntError("The video's length is unknown.")
        stream = db.upsert_stream(self.conn, platform=info.platform, video_id=info.video_id,
                                  streamer=streamer.name, url=info.url, title=info.title,
                                  start_utc=info.start_utc, duration_s=info.duration)
        chat = _load_chat(self.conn, stream, info, streamer, self.fetcher)
        if not chat:
            raise HuntError("This stream has no chat replay to find the crashes with.")
        result = HuntResult(info.title)
        done = {round(r[0]) for r in self.conn.execute("SELECT peak_s FROM vod_fails WHERE video_id = ?",
                                                       (info.video_id,))}
        todo = [c for c in crash_candidates(chat, float(info.duration), self.settings) if round(c.peak_s) not in done]
        result.candidates = len(todo)
        log.info("%s: %d moment(s) where chat talks about crashing", info.title, len(todo))
        if not todo:
            return result
        low_url, headers = self.resolve(info, self.settings.low_format)
        for c in todo:
            start = max(0.0, c.peak_s - self.settings.before_s)
            end = min(float(info.duration), c.peak_s + self.settings.after_s)
            try:
                frames = self.frames(low_url, headers, start, end, self.settings.frames)
            except (HuntError, subprocess.TimeoutExpired) as exc:
                log.warning("Couldn't read the frames at %.0f s: %s", start, exc)
                continue
            lines = [text for t, text in chat if start <= t <= end + 10]
            evidence = ClipEvidence(streamer=streamer.name, duration_s=end - start, frames=frames, transcript=[],
                                    chat=lines[-40:])
            try:
                v = self.judge.judge(evidence)
            except FailJudgeError as exc:
                log.warning("Couldn't check %.0f s: %s", start, exc)
                if "rejected" in str(exc) or "rate limit" in str(exc):
                    break
                continue
            result.judged += 1
            self.conn.execute(
                "INSERT OR REPLACE INTO vod_fails (streamer, video_id, url, peak_s, is_fail, start_s, end_s,"
                " impact_s, strength, kind, note, model, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (streamer.name, info.video_id, info.url, round(c.peak_s), int(v.is_fail),
                 start + v.start_s if v.is_fail else None, start + v.end_s if v.is_fail else None,
                 start + v.impact_s if v.is_fail and v.impact_s is not None else None,
                 v.strength, v.kind, v.note, self.judge.model, db._now()))
            self.conn.commit()
            if v.is_fail:
                result.fails.append((start + v.start_s, v.strength, v.note))
                log.info("Driving fail at %s (%s, strength %d): %s", _clock(start + v.start_s), v.kind,
                         v.strength, v.note)
        return result


def refine_frames(span_s: float) -> int:
    """Frames for a second look at a marked fail: one every 2 s, 8 to 20."""
    return int(max(8, min(20, span_s / 2)))


def refine_vod_fails(conn: sqlite3.Connection, judge: FailJudge, frames: FrameReader,
                     resolve: Callable[[SourceInfo, str], tuple[str, dict]], low_format: str) -> int:
    """Second look at stream fails found before Claude named the crash's own second: with denser frames
    over what it marked, the crash is found (or the fail is dropped). Returns how many were looked at."""
    done = 0
    rows = conn.execute("SELECT * FROM vod_fails WHERE is_fail = 1 AND impact_s IS NULL AND excluded = 0"
                        " ORDER BY video_id").fetchall()
    urls: dict[str, tuple[str, dict]] = {}
    for r in rows:
        if r["video_id"] not in urls:
            urls[r["video_id"]] = resolve(stream_source(conn, r["video_id"]), low_format)
        url, headers = urls[r["video_id"]]
        start, end = max(0.0, float(r["start_s"]) - 4), float(r["end_s"]) + 4
        try:
            shots = frames(url, headers, start, end, refine_frames(end - start))
            chat = [t for (t,) in conn.execute(
                "SELECT text FROM chat_messages m JOIN streams s ON s.id = m.stream_id"
                " WHERE s.video_id = ? AND m.offset_s BETWEEN ? AND ? ORDER BY m.offset_s",
                (r["video_id"], start, end + 15))]
            v = judge.judge(ClipEvidence(streamer=r["streamer"], duration_s=end - start, frames=shots,
                                         transcript=[], chat=chat[-40:]))
        except (HuntError, FailJudgeError, subprocess.TimeoutExpired) as exc:
            log.warning("Couldn't look again at fail v%s: %s", r["id"], exc)
            continue
        done += 1
        if not v.is_fail or v.impact_s is None:
            conn.execute("UPDATE vod_fails SET is_fail = 0, note = ? WHERE id = ?",
                         (f"second look: no crash found ({v.note})"[:300], r["id"]))
            log.info("Fail v%s dropped on a second look: %s", r["id"], v.note)
        else:
            conn.execute("UPDATE vod_fails SET start_s = ?, end_s = ?, impact_s = ?, strength = ?, kind = ?,"
                         " note = ?, hd_path = '', hd_start_s = NULL WHERE id = ?",
                         (start + v.start_s, start + v.end_s, start + v.impact_s, v.strength, v.kind, v.note,
                          r["id"]))
        conn.commit()
    return done


def _clock(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def stream_source(conn: sqlite3.Connection, video_id: str) -> SourceInfo:
    """Enough of a SourceInfo to download a range of a stream we already looked at (no lookup)."""
    row = conn.execute("SELECT * FROM streams WHERE video_id = ?", (video_id,)).fetchone()
    if row is None:
        raise HuntError(f"unknown stream {video_id}")
    return SourceInfo(url=row["url"], platform=row["platform"], video_id=row["video_id"], title=row["title"],
                      description="", channel_name="", channel_values=(), duration=row["duration_s"],
                      start_utc=row["start_utc"])
