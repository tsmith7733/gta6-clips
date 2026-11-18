"""What a video link is: its platform, channel and title, read with yt-dlp (no download)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class SourceError(Exception):
    """The link couldn't be read."""


@dataclass(frozen=True)
class SourceInfo:
    url: str
    platform: str
    video_id: str
    title: str
    description: str
    channel_name: str
    channel_values: tuple[str, ...]   # everything that identifies the channel (urls, @handle, id)
    duration: float | None


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("youtube.com") or host == "youtu.be":
        return "youtube"
    if host.endswith("twitch.tv"):
        return "twitch"
    if host.endswith("kick.com"):
        return "kick"
    raise SourceError(f"Not a YouTube, Twitch or Kick link: {url}")


def probe(url: str) -> SourceInfo:
    """Reads a video's metadata with yt-dlp. Downloads nothing."""
    platform = platform_of(url)
    import yt_dlp  # imported here so tests and other commands don't need it loaded

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises many types; report them all the same way
        raise SourceError(f"Couldn't read {url}: {exc}") from exc
    if not info:
        raise SourceError(f"yt-dlp returned nothing for {url}")
    channel_values = tuple(
        str(v) for v in (info.get("uploader_url"), info.get("channel_url"), info.get("uploader_id"),
                         info.get("channel_id"))
        if v
    )
    log.debug("Probed %s: %r by %s", url, info.get("title"), channel_values)
    return SourceInfo(
        url=url,
        platform=platform,
        video_id=str(info.get("id") or ""),
        title=str(info.get("title") or ""),
        description=str(info.get("description") or ""),
        channel_name=str(info.get("channel") or info.get("uploader") or ""),
        channel_values=channel_values,
        duration=info.get("duration"),
    )


def looks_like_gta6(text: str, keywords: list[str]) -> bool:
    lowered = " ".join(text.lower().split())
    return any(k.lower() in lowered for k in keywords)
