"""Live chat of a YouTube stream: yt-dlp downloads it as it happens; we read the file as it grows."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from ..chat import ChatFileTail, ChatMessage

log = logging.getLogger(__name__)


def _popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


class YtDlpLiveChat:
    """Runs `yt-dlp --write-subs --sub-langs live_chat --skip-download` on a live video, restarting it if it
    stops early. read_new() returns (UTC seconds, text) of the messages that arrived since the last call."""

    def __init__(self, *, ytdlp: Path, url: str, video_id: str, folder: Path,
                 clock: Callable[[], float] = time.time):
        self.ytdlp = ytdlp
        self.url = url
        self.video_id = video_id
        self.folder = folder
        self.clock = clock
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self._next_start = 0.0
        self._tails: list[ChatFileTail] = []

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        run = self.folder / f"chat_{len(self._tails) + 1:03d}"
        run.mkdir(exist_ok=True)
        final = run / f"{self.video_id}.live_chat.json"
        self._tails.append(ChatFileTail(final.with_name(final.name + ".part"), final))
        cmd = [str(self.ytdlp), "--quiet", "--no-warnings", "--skip-download", "--write-subs",
               "--sub-langs", "live_chat", "-o", str(run / "%(id)s.%(ext)s"), self.url]
        err = (run / "yt-dlp.log").open("ab")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                     shell=False, **_popen_kwargs())
        err.close()
        log.info("Reading live chat (yt-dlp pid %s)", self.proc.pid)

    def read_new(self) -> list[ChatMessage]:
        out: list[ChatMessage] = []
        for tail in self._tails:
            out.extend(tail.read_new())
        if self.proc is not None and self.proc.poll() is not None:
            log.warning("Live chat reader stopped (exit %s)", self.proc.returncode)
            self.proc = None
            self.restarts += 1
            self._next_start = self.clock() + min(120.0, 10.0 * self.restarts)
        if self.proc is None and self._next_start and self.clock() >= self._next_start:
            self.start()   # a new file: older messages it repeats are dropped by the session
        out.sort(key=lambda m: m[0])
        return out

    def stop(self) -> list[ChatMessage]:
        self._next_start = 0.0
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        last = [m for tail in self._tails for m in tail.read_new()]
        # The raw files are large (hundreds of MB for a long stream) and the messages are in our database.
        for run in self.folder.glob("chat_*"):
            for f in run.glob(f"{self.video_id}.live_chat.json*"):
                f.unlink(missing_ok=True)
        return last
