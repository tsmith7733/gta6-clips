"""Is a YouTube channel live, and what's on? Read with yt-dlp from <channel>/live: no API quota.

The YouTube Data API's search.list costs 100 units a call, which would use up the daily
10,000 in a few hours of polling, and it doesn't say which game is on anyway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

OFFLINE_HINTS = ("not currently live", "will begin", "premieres in", "is offline", "this live event")


class _QuietLogger:
    """yt-dlp prints "not currently live" as an ERROR even when quiet; we report it our own way."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    info = warning = error = debug


@dataclass(frozen=True)
class LiveCheck:
    state: str                      # live | offline | unknown
    video_id: str = ""
    url: str = ""
    title: str = ""
    channel_values: tuple[str, ...] = ()
    started_utc: float | None = None
    error: str = ""

    @property
    def live(self) -> bool:
        return self.state == "live"


def live_url(channel_url: str) -> str:
    return channel_url.rstrip("/") + "/live"


def check_youtube_live(channel_url: str) -> LiveCheck:
    """`unknown` means the check itself failed (network, a YouTube change): don't stop a recording on it."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "logger": _QuietLogger()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(live_url(channel_url), download=False)
    except Exception as exc:  # yt-dlp raises many types; "not live" is one of them
        text = str(exc)
        if any(h in text.lower() for h in OFFLINE_HINTS):
            return LiveCheck("offline")
        log.warning("Couldn't check whether %s is live: %s", channel_url, text.splitlines()[0][:300])
        return LiveCheck("unknown", error=text[:500])
    if not info or info.get("live_status") != "is_live":
        return LiveCheck("offline")
    video_id = str(info.get("id") or "")
    values = tuple(str(v) for v in (info.get("uploader_url"), info.get("channel_url"), info.get("uploader_id"),
                                    info.get("channel_id")) if v)
    started = info.get("release_timestamp") or info.get("timestamp")
    return LiveCheck("live", video_id=video_id, url=f"https://www.youtube.com/watch?v={video_id}",
                     title=str(info.get("title") or ""), channel_values=values,
                     started_utc=float(started) if started else None)


def resolve_stream_urls(url: str, video_format: str) -> list[str]:
    """The HLS playlist URLs to record: [video, audio] (YouTube live keeps them separate) or [both].

    They expire after a few hours; the recorder asks again when ffmpeg stops.
    """
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "format": video_format, "logger": _QuietLogger()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    formats = info.get("requested_formats") or [info]
    urls = [str(f["url"]) for f in formats if f.get("url")]
    if not urls:
        raise RuntimeError(f"yt-dlp found no recordable format for {url}")
    return urls
