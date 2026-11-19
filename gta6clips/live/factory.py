"""Builds real live sessions: ffmpeg recorder + yt-dlp live chat + loudness + cutting."""

from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

from ..config import Config
from ..media import MediaToolMissing, measure_loudness
from ..scoring import ScoringSettings, WindowSettings
from ..streamers import Streamer, require_permission
from .chatfeed import YtDlpLiveChat
from .recorder import SegmentRecorder, cut_segments
from .select import LiveSettings
from .session import LiveSession
from .watcher import SessionFactory
from .youtube import LiveCheck, resolve_stream_urls

DEFAULT_FORMAT = "bv*[height<=1080][vcodec^=avc1]+ba/b[height<=1080]/b"


def find_ytdlp() -> Path:
    """The yt-dlp that pip installed next to this Python (the venv's), else the one on PATH."""
    exe = Path(sys.executable).parent / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
    if exe.is_file():
        return exe
    found = shutil.which("yt-dlp")
    if found:
        return Path(found)
    raise MediaToolMissing("yt-dlp isn't installed in the venv: run .venv\\Scripts\\python.exe -m pip install "
                           "-r requirements.txt")


def ffmpeg_inputs(urls: list[str]) -> list[str]:
    args: list[str] = []
    for url in urls:
        args += ["-i", url]
    if len(urls) >= 2:   # YouTube live: video and audio are separate playlists
        return args + ["-map", "0:v:0", "-map", "1:a:0"]
    return args + ["-map", "0:v:0?", "-map", "0:a:0?"]


def session_factory(cfg: Config, conn: sqlite3.Connection, *, ffmpeg: Path, ytdlp: Path) -> SessionFactory:
    scoring = ScoringSettings.from_config(cfg)
    windows = WindowSettings.from_config(cfg)
    live = LiveSettings.from_config(cfg)
    video_format = str(cfg.get("live.video_format") or DEFAULT_FORMAT)
    segment_s = float(cfg.get("live.segment_seconds", 60))
    root = cfg.path("paths.recordings")
    windows_dir = cfg.path("paths.windows")

    def make(streamer: Streamer, check: LiveCheck) -> LiveSession:
        require_permission(streamer)
        folder = root / f"{check.video_id}_{int(time.time())}"

        def inputs() -> list[str]:
            require_permission(streamer)   # every (re)start of the recording goes through the gate
            return ffmpeg_inputs(resolve_stream_urls(check.url, video_format))

        recorder = SegmentRecorder(ffmpeg=ffmpeg, folder=folder / "segments", inputs=inputs, segment_s=segment_s)
        chat = YtDlpLiveChat(ytdlp=ytdlp, url=check.url, video_id=check.video_id, folder=folder)
        return LiveSession(conn=conn, streamer=streamer, check=check, recorder=recorder, chat=chat,
                           measure=lambda p: measure_loudness(ffmpeg, p, scoring.bin_s),
                           cut=lambda segs, s, e, dest: cut_segments(ffmpeg, segs, s, e, dest),
                           windows_dir=windows_dir, scoring=scoring, windows=windows, live=live)

    return make
