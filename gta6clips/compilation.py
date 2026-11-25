"""Compilations: a streamer's driving fails, found by Claude and joined into one Short.

Every new clip of a streamer in `compilations.streamers` is checked once (`llm/fails.py`, Haiku):
a few frames of its recording, its transcript and chat. Once there are `min_fails` unused fails,
the strongest are cut from the original (horizontal) recordings, joined with ffmpeg and queued as
a planned window on the stream '<handle>@compilations'. The watcher's dispatcher sends that to
Clips Kitty like any other window, so the compilation gets the split layout, the credit line, the
review page and the usual private, scheduled upload; Opus writes its title and description.
Nothing is published without the owner's approval.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import db
from .clipskitty import ClipsKittyClient
from .clipskitty.client import ClipsKittyError
from .config import Config
from .exact import CHAT_LAG_S
from .llm.fails import ClipEvidence, FailJudge, FailJudgeError
from .media import _popen_kwargs
from .streamers import PermissionDenied, Streamer, require_permission

log = logging.getLogger(__name__)

STREAM_SUFFIX = "@compilations"
KIND = "driving fails"
REVIEWED_ALREADY = ("approved", "published")
HD_PAD_S = 5.0   # a finished stream's fail is downloaded with this much extra each side


@dataclass(frozen=True)
class CompilationSettings:
    enabled: bool = False
    streamers: tuple[str, ...] = ()
    every_s: float = 3600.0
    lookback_days: float = 14.0
    judge_per_run: int = 40      # clips checked per run at most (each is one Claude call)
    min_fails: int = 5
    max_fails: int = 6
    min_strength: int = 6
    lead_s: float = 1.0          # added before the fail Claude found (it already includes the setup)
    tail_s: float = 1.5          # and after it
    before_impact_s: float = 6.0   # with the crash's own second known: the part is this much before it...
    after_impact_s: float = 4.0    # ...and this much after (his reaction)
    max_part_s: float = 10.0
    max_total_s: float = 60.0
    frames: int = 8
    judge_model: str = "claude-haiku-5-5"
    judge_effort: str = "low"
    title_model: str = "claude-opus-5-5"
    title_effort: str = "medium"

    @classmethod
    def from_config(cls, cfg: Config) -> "CompilationSettings":
        s = cfg.get("compilations", {}) or {}
        d = cls()
        return cls(
            enabled=s.get("enabled", d.enabled) is True,
            streamers=tuple(str(n) for n in (s.get("streamers") or [])),
            every_s=float(s.get("every_minutes", d.every_s / 60)) * 60,
            lookback_days=float(s.get("lookback_days", d.lookback_days)),
            judge_per_run=int(s.get("judge_per_run", d.judge_per_run)),
            min_fails=int(s.get("min_fails", d.min_fails)),
            max_fails=int(s.get("max_fails", d.max_fails)),
            min_strength=int(s.get("min_strength", d.min_strength)),
            lead_s=float(s.get("lead_seconds", d.lead_s)),
            tail_s=float(s.get("tail_seconds", d.tail_s)),
            before_impact_s=float(s.get("before_impact_seconds", d.before_impact_s)),
            after_impact_s=float(s.get("after_impact_seconds", d.after_impact_s)),
            max_part_s=float(s.get("max_part_seconds", d.max_part_s)),
            max_total_s=float(s.get("max_seconds", d.max_total_s)),
            frames=int(s.get("frames", d.frames)),
            judge_model=str(s.get("judge_model", d.judge_model)),
            judge_effort=str(s.get("judge_effort", d.judge_effort)),
            title_model=str(s.get("title_model", d.title_model)),
            title_effort=str(s.get("title_effort", d.title_effort)),
        )


@dataclass(frozen=True)
class Part:
    clip_id: int
    path: Path            # the window's recording
    start_s: float        # of that file
    end_s: float
    strength: int
    kind: str
    note: str
    source: str           # the YouTube video it's from (see _same_moment)
    abs_start: float | None
    vod_id: int | None = None   # a fail found in a finished stream (vod_fails); path is None until downloaded

    @property
    def seconds(self) -> float:
        return self.end_s - self.start_s

    @property
    def label(self) -> str:
        """How the owner names it for --drop: the clip id, or v<id> for one found in a finished stream."""
        return f"v{self.vod_id}" if self.vod_id is not None else str(self.clip_id)


@dataclass
class CompileRun:
    judged: int = 0
    fails: int = 0
    queued: list[int] = field(default_factory=list)    # compilation ids


def is_compilation_stream(video_id: str) -> bool:
    return str(video_id).endswith(STREAM_SUFFIX)


def _base(path: str) -> str:
    """A recording's name without the _wide1/_retry1/_fix1 copies' suffixes."""
    return re.sub(r"_(wide|retry|fix)\d+$", "", Path(path).stem)


def plan_parts(fails: list[Part], s: CompilationSettings) -> list[Part]:
    """The strongest distinct fails, within the length limit, strongest first. Over the limit, the weakest
    go while more than min_fails are left; then every part is shortened evenly."""
    chosen: list[Part] = []
    for f in sorted(fails, key=lambda f: (-f.strength, -f.clip_id)):
        if f.strength < s.min_strength or any(_same_moment(f, c) for c in chosen):
            continue
        chosen.append(f)
        if len(chosen) >= s.max_fails:
            break
    while len(chosen) > s.min_fails and sum(p.seconds for p in chosen) > s.max_total_s:
        chosen.pop()   # the weakest goes
    total = sum(p.seconds for p in chosen)
    if total > s.max_total_s:
        k = s.max_total_s / total
        chosen = [replace(p, end_s=p.start_s + p.seconds * k) for p in chosen]
    return chosen


def part_range(start_s, end_s, impact_s, s: CompilationSettings) -> tuple[float, float]:
    """The stretch of a fail that goes in a compilation: around the crash itself when Claude named its
    second, else the end of what it marked (where a chain of wrecks ends), at most max_part_s."""
    if impact_s is not None:
        start = max(0.0, float(impact_s) - s.before_impact_s)
        return start, min(float(impact_s) + s.after_impact_s, start + s.max_part_s)
    end = float(end_s) + s.tail_s
    return max(0.0, float(start_s) - s.lead_s, end - s.max_part_s), end


def _same_moment(a: Part, b: Part) -> bool:
    """Two clips of one moment (a widened or re-rendered copy, or overlapping windows)."""
    if a.abs_start is not None and b.abs_start is not None:
        return a.source == b.source and a.abs_start < b.abs_start + b.seconds and b.abs_start < a.abs_start + a.seconds
    if a.vod_id is not None and b.vod_id is not None:
        return a.source == b.source and a.start_s < b.end_s and b.start_s < a.end_s
    if a.path is None or b.path is None:
        return False
    return _base(str(a.path)) == _base(str(b.path)) and a.start_s < b.end_s and b.start_s < a.end_s


def stitch(ffmpeg: Path, parts: list[Part], dest: Path, *, width: int = 1920, height: int = 1080) -> Path:
    """Cuts each part from its recording and joins them (re-encoded, so mixed recordings line up)."""
    args: list[str] = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error"]
    chains = []
    for i, p in enumerate(parts):
        args += ["-ss", f"{p.start_s:.2f}", "-t", f"{p.seconds:.2f}", "-i", str(p.path)]
        chains.append(f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                      f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}];"
                      f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
    joined = "".join(f"[v{i}][a{i}]" for i in range(len(parts)))
    graph = ";".join(chains) + f";{joined}concat=n={len(parts)}:v=1:a=1[v][a]"
    dest.parent.mkdir(parents=True, exist_ok=True)
    args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dest)]
    proc = subprocess.run(args, capture_output=True, text=True, **_popen_kwargs())
    if proc.returncode != 0 or not dest.is_file():
        raise RuntimeError(f"ffmpeg couldn't join the compilation: {proc.stderr.strip()[-500:]}")
    return dest


def grab_frames(ffmpeg: Path, media: Path, start_s: float, end_s: float, count: int) -> list[tuple[float, bytes]]:
    """`count` JPEG frames spread over [start_s, end_s] of the file, 640 px wide; times from start_s."""
    out = []
    span = max(0.1, end_s - start_s)
    for i in range(count):
        t = span * (i + 0.5) / count
        proc = subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-ss", f"{start_s + t:.2f}",
                               "-i", str(media), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5",
                               "-f", "image2pipe", "-vcodec", "mjpeg", "-"], capture_output=True, **_popen_kwargs())
        if proc.returncode == 0 and proc.stdout:
            out.append((round(t, 1), proc.stdout))
    return out


Cutter = Callable[[list[Part], Path], Path]
Framer = Callable[[Path, float, float, int], list[tuple[float, bytes]]]


class Compiler:
    def __init__(self, *, cfg: Config, conn: sqlite3.Connection, client: ClipsKittyClient,
                 streamers: list[Streamer], judge: FailJudge, frames: Framer, cut: Cutter,
                 windows_dir: Path, clock: Callable[[], float] | None = None,
                 fetch: Callable[[Part, Path], Path] | None = None):
        self.cfg = cfg
        self.conn = conn
        self.client = client
        self.streamers = streamers
        self.judge = judge
        self.frames = frames
        self.cut = cut
        self.windows_dir = windows_dir
        # Downloads a finished stream's fail in HD: (part, dest) -> the file, holding
        # [start_s - HD_PAD_S, end_s + HD_PAD_S] of the stream.
        self.fetch = fetch
        self.settings = CompilationSettings.from_config(cfg)
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())

    def _streamers(self) -> list[Streamer]:
        out = []
        for name in self.settings.streamers:
            s = next((x for x in self.streamers if x.name == name), None)
            if s is None:
                log.warning("compilations.streamers names %r, who isn't in streamers.yaml", name)
                continue
            try:
                out.append(require_permission(s))
            except PermissionDenied as exc:
                log.warning("%s", exc)
        return out

    def _since(self) -> str:
        when = datetime.fromtimestamp(self.clock(), timezone.utc) - timedelta(days=self.settings.lookback_days)
        return when.isoformat(timespec="seconds")

    # -- finding the fails -----------------------------------------------------------------
    def unchecked(self, streamer: Streamer) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT c.*, w.local_path, w.start_utc AS w_start_utc, w.stream_id AS w_stream_id FROM clips c"
            " JOIN windows w ON w.id = c.window_id JOIN streams s ON s.id = w.stream_id"
            " LEFT JOIN fail_checks f ON f.ck_clip_id = c.ck_clip_id"
            " WHERE f.ck_clip_id IS NULL AND c.streamer = ? AND c.status != 'spoiler_hold'"
            " AND c.created_at >= ? AND s.video_id NOT LIKE ? AND w.local_path != ''"
            " ORDER BY c.ck_clip_id DESC",
            (streamer.name, self._since(), "%" + STREAM_SUFFIX)).fetchall()

    def judge_new(self, streamer: Streamer, budget: int) -> tuple[int, int]:
        judged = fails = 0
        for row in self.unchecked(streamer):
            if judged >= budget:
                break
            path = Path(row["local_path"])
            if not path.is_file():
                continue   # the recording is gone: nothing to cut it from
            start, end = float(row["start_s"] or 0), float(row["end_s"] or 0)
            try:
                lines = self.client.captions(row["ck_clip_id"])
            except ClipsKittyError:
                lines = []
            evidence = ClipEvidence(
                streamer=streamer.name, duration_s=end - start,
                frames=self.frames(path, start, end, self.settings.frames),
                transcript=[(float(x.get("start") or 0), str(x.get("text") or "")) for x in lines],
                chat=self._chat(row, start, end))
            try:
                verdict = self.judge.judge(evidence)
            except FailJudgeError as exc:
                log.warning("Couldn't check clip %s for a driving fail: %s", row["ck_clip_id"], exc)
                if "rejected" in str(exc) or "rate limit" in str(exc):
                    break
                continue
            judged += 1
            self.conn.execute(
                "INSERT OR REPLACE INTO fail_checks (ck_clip_id, streamer, window_id, is_fail, start_s, end_s,"
                " impact_s, strength, kind, note, model, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["ck_clip_id"], streamer.name, row["window_id"], int(verdict.is_fail),
                 start + verdict.start_s if verdict.is_fail else None,
                 start + verdict.end_s if verdict.is_fail else None,
                 start + verdict.impact_s if verdict.is_fail and verdict.impact_s is not None else None,
                 verdict.strength, verdict.kind, verdict.note, self.judge.model, db._now()))
            self.conn.commit()
            if verdict.is_fail:
                fails += 1
                log.info("Clip %s has a driving fail (%s, strength %d): %s", row["ck_clip_id"], verdict.kind,
                         verdict.strength, verdict.note)
        return judged, fails

    def refine_clip_fails(self) -> int:
        """Second look at clip fails found before Claude named the crash's own second (see crash_hunt)."""
        from .crash_hunt import refine_frames
        done = 0
        rows = self.conn.execute(
            "SELECT f.*, w.local_path, w.start_utc AS w_start_utc, w.stream_id AS w_stream_id FROM fail_checks f"
            " JOIN windows w ON w.id = f.window_id"
            " WHERE f.is_fail = 1 AND f.impact_s IS NULL AND f.excluded = 0").fetchall()
        for r in rows:
            path = Path(r["local_path"])
            if not path.is_file():
                continue
            start, end = max(0.0, float(r["start_s"]) - 4), float(r["end_s"]) + 4
            try:
                v = self.judge.judge(ClipEvidence(
                    streamer=r["streamer"], duration_s=end - start,
                    frames=self.frames(path, start, end, refine_frames(end - start)), transcript=[],
                    chat=self._chat(r, start, end)))
            except FailJudgeError as exc:
                log.warning("Couldn't look again at clip %s: %s", r["ck_clip_id"], exc)
                continue
            done += 1
            if not v.is_fail or v.impact_s is None:
                self.conn.execute("UPDATE fail_checks SET is_fail = 0, note = ? WHERE ck_clip_id = ?",
                                  (f"second look: no crash found ({v.note})"[:300], r["ck_clip_id"]))
            else:
                self.conn.execute("UPDATE fail_checks SET start_s = ?, end_s = ?, impact_s = ?, strength = ?,"
                                  " kind = ?, note = ? WHERE ck_clip_id = ?",
                                  (start + v.start_s, start + v.end_s, start + v.impact_s, v.strength, v.kind,
                                   v.note, r["ck_clip_id"]))
            self.conn.commit()
        return done

    def unbuild(self, comp_id: int, why: str) -> None:
        """Takes a compilation back: its clip is rejected (unless already approved) and its fails are free."""
        comp = self.conn.execute("SELECT * FROM compilations WHERE id = ?", (comp_id,)).fetchone()
        if comp is None:
            raise ValueError(f"no compilation {comp_id}")
        made = self.conn.execute("SELECT * FROM clips WHERE window_id = ?", (comp["window_id"],)).fetchall()
        if any(c["status"] in REVIEWED_ALREADY for c in made):
            raise ValueError(f"compilation {comp_id} is already approved or published")
        for c in made:
            if c["status"] != "rejected":
                db.set_clip_status(self.conn, c["ck_clip_id"], "rejected", note=why)
        window = db.get_window(self.conn, comp["window_id"])
        if window is not None and window["state"] == "planned":
            db.update_window(self.conn, window["id"], state="failed", error=why)
        self.conn.execute("UPDATE compilations SET state = 'dropped' WHERE id = ?", (comp_id,))
        self.conn.execute("UPDATE fail_checks SET compilation_id = NULL WHERE compilation_id = ?", (comp_id,))
        self.conn.execute("UPDATE vod_fails SET compilation_id = NULL WHERE compilation_id = ?", (comp_id,))
        self.conn.commit()

    def _chat(self, row: sqlite3.Row, start: float, end: float) -> list[str]:
        if row["w_start_utc"] is None or row["w_stream_id"] is None:
            return []
        t0 = float(row["w_start_utc"])
        return [r[0] for r in self.conn.execute(
            "SELECT text FROM chat_messages WHERE stream_id = ? AND t_utc BETWEEN ? AND ? ORDER BY t_utc",
            (row["w_stream_id"], t0 + start, t0 + end + CHAT_LAG_S))]

    # -- building ----------------------------------------------------------------------
    def available(self, streamer: Streamer, include_vods: bool = False) -> list[Part]:
        """Unused fails: from our clips (made in the last lookback_days), and with include_vods also
        the ones `hunt` found in finished streams (any age)."""
        rows = self.conn.execute(
            "SELECT f.*, w.local_path, w.start_utc AS w_start_utc, s.video_id AS s_video_id FROM fail_checks f"
            " JOIN windows w ON w.id = f.window_id JOIN streams s ON s.id = w.stream_id"
            " LEFT JOIN music_checks m ON m.ck_clip_id = f.ck_clip_id"   # a licensed song: YouTube blocks it
            " WHERE f.streamer = ? AND f.is_fail = 1 AND f.excluded = 0 AND f.compilation_id IS NULL"
            " AND COALESCE(m.verdict, '') != 'likely'"
            " AND f.checked_at >= ?", (streamer.name, self._since())).fetchall()
        s = self.settings
        parts = []
        for r in rows:
            path = Path(r["local_path"])
            if not path.is_file():
                continue
            start, end = part_range(r["start_s"], r["end_s"], r["impact_s"], s)
            abs_start = float(r["w_start_utc"]) + start if r["w_start_utc"] is not None else None
            parts.append(Part(clip_id=int(r["ck_clip_id"]), path=path, start_s=start, end_s=end,
                              strength=int(r["strength"]), kind=r["kind"], note=r["note"],
                              source=str(r["s_video_id"]).split("@")[0], abs_start=abs_start))
        if include_vods:
            for r in self.conn.execute(
                    "SELECT f.*, s.start_utc AS s_start_utc FROM vod_fails f"
                    " LEFT JOIN streams s ON s.video_id = f.video_id"
                    " WHERE f.streamer = ? AND f.is_fail = 1 AND f.excluded = 0 AND f.compilation_id IS NULL",
                    (streamer.name,)):
                start, end = part_range(r["start_s"], r["end_s"], r["impact_s"], s)
                abs_start = float(r["s_start_utc"]) + start if r["s_start_utc"] is not None else None
                parts.append(Part(clip_id=0, path=None, start_s=start, end_s=end, strength=int(r["strength"]),
                                  kind=r["kind"], note=r["note"], source=r["video_id"], abs_start=abs_start,
                                  vod_id=int(r["id"])))
        return parts

    def _download(self, part: Part) -> Part | None:
        """A finished stream's fail, downloaded in HD (once), as a part of that file."""
        row = self.conn.execute("SELECT hd_path, hd_start_s FROM vod_fails WHERE id = ?", (part.vod_id,)).fetchone()
        path = Path(row["hd_path"]) if row and row["hd_path"] else None
        hd_start = row["hd_start_s"] if row else None
        covers = hd_start is not None and hd_start <= part.start_s and part.end_s <= hd_start + HD_PAD_S * 2 + part.seconds
        if path is None or not path.is_file() or not covers:
            if self.fetch is None:
                return None
            dest = self.windows_dir / f"vodfail_{part.source}_{int(part.start_s)}"
            try:
                path = self.fetch(part, dest)
            except Exception as exc:   # one download failing mustn't stop the rest
                log.warning("Couldn't download fail v%s (%s at %.0f s): %s", part.vod_id, part.source,
                            part.start_s, exc)
                return None
            hd_start = max(0.0, part.start_s - HD_PAD_S)
            self.conn.execute("UPDATE vod_fails SET hd_path = ?, hd_start_s = ? WHERE id = ?",
                              (str(path), hd_start, part.vod_id))
            self.conn.commit()
        start = part.start_s - float(hd_start)
        return replace(part, path=path, start_s=start, end_s=start + part.seconds)

    def _plan_ready(self, streamer: Streamer, include_vods: bool, settings: CompilationSettings) -> list[Part]:
        """plan_parts, with the finished streams' fails downloaded; one that can't be is planned without."""
        pool = self.available(streamer, include_vods)
        while True:
            parts = plan_parts(pool, settings)
            if len(parts) < settings.min_fails:
                return []
            ready = []
            for p in parts:
                got = self._download(p) if p.vod_id is not None else p
                if got is None:
                    pool = [x for x in pool if not (x.vod_id == p.vod_id and x.clip_id == p.clip_id)]
                    break
                ready.append(got)
            else:
                return ready

    def build(self, streamer: Streamer, include_vods: bool = False, *, settings: CompilationSettings | None = None,
              kind: str = KIND) -> int | None:
        """Queues a compilation when there are enough fails. Returns its id."""
        require_permission(streamer)
        parts = self._plan_ready(streamer, include_vods, settings or self.settings)
        return self._queue(streamer, parts, kind) if parts else None

    def build_top(self, streamer: Streamer, *, shorts: int = 3, per: int = 5, include_vods: bool = True) -> list[int]:
        """'Top 5' Shorts: the best shorts*per distinct fails, dealt out in turn (1st, 4th, 7th... to the
        first Short), so every Short opens with one of the very best. Fewer Shorts when there are fewer fails."""
        require_permission(streamer)
        wide = replace(self.settings, max_fails=shorts * per, min_fails=per, max_total_s=1e9)
        best = self._plan_ready(streamer, include_vods, wide)
        count = min(shorts, len(best) // per)
        out = []
        for i in range(count):
            group = best[i::count][:per]
            total = sum(p.seconds for p in group)
            if total > self.settings.max_total_s:   # shorten every part evenly to fit
                k = self.settings.max_total_s / total
                group = [replace(p, end_s=p.start_s + p.seconds * k) for p in group]
            out.append(self._queue(streamer, group, f"top {per} driving fails"))
        return out

    def _queue(self, streamer: Streamer, parts: list[Part], kind: str) -> int:
        n = 1 + self.conn.execute("SELECT COUNT(*) FROM compilations WHERE streamer = ?",
                                  (streamer.name,)).fetchone()[0]
        handle = streamer.clipskitty_creator
        dest = self.windows_dir / f"compilation_{re.sub(r'[^A-Za-z0-9]+', '', handle)}_{n:03d}.mp4"
        self.cut(parts, dest)
        total = sum(p.seconds for p in parts)
        stream = db.upsert_stream(self.conn, platform=streamer.platform, video_id=handle + STREAM_SUFFIX,
                                  streamer=streamer.name, url=streamer.channel_url,
                                  title=f"{streamer.name} compilations", start_utc=None, duration_s=None)
        strength = sum(p.strength for p in parts) / len(parts)
        title = f"{streamer.name} GTA 6 {kind}"
        window_id = db.add_window(self.conn, streamer=streamer.name, source_url=str(dest), source_title=title,
                                  ck_job_id=None, ck_video_id=None, state="planned", stream_id=stream["id"],
                                  offset_start_s=0.0, offset_end_s=total, score=3 + strength / 2,
                                  reasons=f"Compilation: {kind} ({len(parts)})")
        db.update_window(self.conn, window_id, local_path=str(dest), finder="exact", moment_start_s=0.0,
                         moment_end_s=total)
        parts_json = [{"clip": p.clip_id or None, "vod": p.vod_id, "label": p.label, "video": p.source,
                       "start_s": p.start_s, "end_s": p.end_s, "kind": p.kind, "note": p.note,
                       "strength": p.strength} for p in parts]
        cur = self.conn.execute("INSERT INTO compilations (streamer, kind, window_id, path, parts, created_at)"
                                " VALUES (?, ?, ?, ?, ?, ?)",
                                (streamer.name, kind, window_id, str(dest), json.dumps(parts_json), db._now()))
        comp_id = int(cur.lastrowid)
        self.conn.executemany("UPDATE fail_checks SET compilation_id = ? WHERE ck_clip_id = ?",
                              [(comp_id, p.clip_id) for p in parts if p.vod_id is None])
        self.conn.executemany("UPDATE vod_fails SET compilation_id = ? WHERE id = ?",
                              [(comp_id, p.vod_id) for p in parts if p.vod_id is not None])
        self.conn.commit()
        log.info("Compilation %d of %s: %d %s, %.0f s (clips %s), queued for Clips Kitty", comp_id, streamer.name,
                 len(parts), kind, total, ", ".join(p.label for p in parts))
        return comp_id

    def run_once(self) -> CompileRun:
        run = CompileRun()
        for streamer in self._streamers():
            judged, fails = self.judge_new(streamer, self.settings.judge_per_run - run.judged)
            run.judged += judged
            run.fails += fails
            comp = self.build(streamer)
            if comp is not None:
                run.queued.append(comp)
        return run

    def drop(self, label: str | int) -> int | None:
        """The owner doesn't want this fail (a clip id, or v<id> for one found in a finished stream): it
        leaves its compilation, whose clip is rejected, and the compilation is built again from the rest
        (plus the next fail). Returns the new compilation's id."""
        label = str(label).strip()
        vod = label.lower().startswith("v")
        table, key = ("vod_fails", "id") if vod else ("fail_checks", "ck_clip_id")
        try:
            ident = int(label[1:] if vod else label)
        except ValueError:
            raise ValueError(f"{label!r} isn't a clip id or a v<number> fail") from None
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (ident,)).fetchone()
        if row is None or not row["is_fail"]:
            raise ValueError(f"{label} isn't a driving fail we found")
        comp_id = row["compilation_id"]
        streamer = next((s for s in self.streamers if s.name == row["streamer"]), None)
        if streamer is None:
            raise ValueError(f"{row['streamer']} isn't in streamers.yaml")
        had_vods = False
        if comp_id is not None:
            comp = self.conn.execute("SELECT * FROM compilations WHERE id = ?", (comp_id,)).fetchone()
            made = self.conn.execute("SELECT * FROM clips WHERE window_id = ?", (comp["window_id"],)).fetchall()
            if any(c["status"] in REVIEWED_ALREADY for c in made):
                raise ValueError(f"compilation {comp_id} is already approved or published; it can't change now")
            window = db.get_window(self.conn, comp["window_id"])
            if window is not None and window["state"] in ("submitted", "done"):
                raise ValueError(f"compilation {comp_id} is being made in Clips Kitty right now; try again "
                                 "once it's on the review page")
            for c in made:
                if c["status"] != "rejected":
                    db.set_clip_status(self.conn, c["ck_clip_id"], "rejected",
                                       note=f"compilation rebuilt without {label}")
            if window is not None and window["state"] == "planned":
                db.update_window(self.conn, window["id"], state="failed", error="rebuilt without a fail")
            old_parts = json.loads(comp["parts"] or "[]")
            had_vods = any(p.get("vod") for p in old_parts)
            again = replace(self.settings, max_fails=len(old_parts), min_fails=min(self.settings.min_fails,
                                                                                    len(old_parts)))
            old_kind = comp["kind"]
            self.conn.execute("UPDATE compilations SET state = 'dropped' WHERE id = ?", (comp_id,))
            self.conn.execute("UPDATE fail_checks SET compilation_id = NULL WHERE compilation_id = ?", (comp_id,))
            self.conn.execute("UPDATE vod_fails SET compilation_id = NULL WHERE compilation_id = ?", (comp_id,))
        self.conn.execute(f"UPDATE {table} SET excluded = 1 WHERE {key} = ?", (ident,))
        self.conn.commit()
        if comp_id is None:
            return None
        return self.build(streamer, include_vods=had_vods, settings=again, kind=old_kind)


def parts_context(parts: list[dict]) -> str:
    """The compilation's moments in order, for the title writer."""
    return "\n".join(f"{i}. {p.get('kind') or 'driving fail'}: {p.get('note') or ''}".strip()
                     for i, p in enumerate(parts, 1))


def compilation_parts(conn: sqlite3.Connection, window_id: int) -> list[dict]:
    """The moments of the compilation sent as this window, in order ([] for any other window)."""
    row = conn.execute("SELECT parts FROM compilations WHERE window_id = ?", (window_id,)).fetchone()
    return json.loads(row["parts"]) if row else []


def compilation_kind(conn: sqlite3.Connection, window_id: int) -> str:
    row = conn.execute("SELECT kind FROM compilations WHERE window_id = ?", (window_id,)).fetchone()
    return row["kind"] if row else KIND
