"""Everything Phase 2 downloads from a VOD, through yt-dlp: chat replay, audio, and window cuts.

Every method applies the permission gate first: nothing is downloaded for a
channel that isn't marked `permission: true`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from .chat import SUPPORTED_PLATFORMS, ChatMessage, read_youtube_live_chat
from .media import measure_loudness
from .scoring import clock as _clock
from .sources import SourceInfo
from .streamers import Streamer, require_permission

log = logging.getLogger(__name__)


class DownloadError(Exception):
    """yt-dlp couldn't fetch something from the video."""


class Fetcher(Protocol):
    def chat(self, info: SourceInfo, streamer: Streamer) -> list[ChatMessage] | None:
        """The chat replay, or None when the video has none (or the platform isn't supported)."""

    def loudness(self, info: SourceInfo, streamer: Streamer, bin_s: float) -> list[float]:
        """Per-bin loudness (LUFS) of the whole video."""

    def cut(self, info: SourceInfo, streamer: Streamer, start_s: float, end_s: float, dest: Path) -> Path:
        """Downloads [start_s, end_s] of the video as an mp4 next to `dest` (no extension) and returns it."""


class YtDlpFetcher:
    def __init__(self, *, ffmpeg: Path, cache_dir: Path, video_format: str, keep_audio: bool = False):
        self.ffmpeg = ffmpeg
        self.cache_dir = cache_dir
        self.video_format = video_format
        self.keep_audio = keep_audio

    def _ydl(self, extra: dict):
        import yt_dlp  # loaded only when something is downloaded

        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True,
                "ffmpeg_location": str(self.ffmpeg), **extra}
        return yt_dlp.YoutubeDL(opts)

    def _folder(self, info: SourceInfo) -> Path:
        folder = self.cache_dir / f"{info.platform}_{info.video_id}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def chat(self, info: SourceInfo, streamer: Streamer) -> list[ChatMessage] | None:
        require_permission(streamer)
        if info.platform not in SUPPORTED_PLATFORMS:
            log.warning("Chat replay for %s isn't supported yet; scoring on audio only.", info.platform)
            return None
        folder = self._folder(info)
        target = folder / f"{info.video_id}.live_chat.json"
        if not target.is_file():
            log.info("Downloading the chat replay (this can take a few minutes for a long stream)")
            opts = {"skip_download": True, "writesubtitles": True, "subtitleslangs": ["live_chat"],
                    "outtmpl": str(folder / "%(id)s.%(ext)s")}
            try:
                with self._ydl(opts) as ydl:
                    ydl.download([info.url])
            except Exception as exc:  # yt-dlp raises many types
                raise DownloadError(f"Couldn't download the chat replay: {exc}") from exc
        if not target.is_file():
            log.warning("This video has no chat replay; scoring on audio only.")
            return None
        return read_youtube_live_chat(target)

    def loudness(self, info: SourceInfo, streamer: Streamer, bin_s: float) -> list[float]:
        require_permission(streamer)
        folder = self._folder(info)
        existing = [p for p in folder.glob(f"{info.video_id}.audio.*") if not p.name.endswith(".part")]
        if existing:
            audio = existing[0]
        else:
            log.info("Downloading the audio track")
            opts = {"format": "bestaudio[ext=m4a]/bestaudio/best",
                    "outtmpl": str(folder / f"{info.video_id}.audio.%(ext)s")}
            try:
                with self._ydl(opts) as ydl:
                    ydl.download([info.url])
            except Exception as exc:
                raise DownloadError(f"Couldn't download the audio: {exc}") from exc
            found = [p for p in folder.glob(f"{info.video_id}.audio.*") if not p.name.endswith(".part")]
            if not found:
                raise DownloadError("yt-dlp finished but the audio file isn't there")
            audio = found[0]
        try:
            return measure_loudness(self.ffmpeg, audio, bin_s)
        finally:
            if not self.keep_audio:
                audio.unlink(missing_ok=True)

    def cut(self, info: SourceInfo, streamer: Streamer, start_s: float, end_s: float, dest: Path) -> Path:
        require_permission(streamer)
        from yt_dlp.utils import download_range_func

        dest.parent.mkdir(parents=True, exist_ok=True)
        done = dest.with_suffix(".mp4")
        if done.is_file():
            return done
        log.info("Downloading %s-%s of the video", _clock(start_s), _clock(end_s))
        opts = {"format": self.video_format, "merge_output_format": "mp4",
                "download_ranges": download_range_func(None, [(start_s, end_s)]),
                "force_keyframes_at_cuts": False, "outtmpl": str(dest) + ".%(ext)s"}
        try:
            with self._ydl(opts) as ydl:
                ydl.download([info.url])
        except Exception as exc:
            raise DownloadError(f"Couldn't download {_clock(start_s)}-{_clock(end_s)}: {exc}") from exc
        if not done.is_file():
            others = [p for p in dest.parent.glob(dest.name + ".*") if p.suffix != ".part"]
            if not others:
                raise DownloadError("yt-dlp finished but the window file isn't there")
            return others[0]
        return done

