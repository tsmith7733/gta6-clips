"""streamers.yaml and THE permission gate.

Every entry point that records, downloads, sends to Clips Kitty or publishes
must go through `require_permission` (directly, or via `permitted_streamer_for`).
A streamer is permitted only when its entry says `permission: true`, a real
YAML boolean. `false` is a no, and anything that isn't a boolean (missing,
empty, "true" in quotes, 1) makes the file invalid, so nothing runs until it's fixed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

log = logging.getLogger(__name__)

PLATFORMS = ("youtube", "twitch", "kick")


class StreamerFileError(Exception):
    """streamers.yaml is missing or an entry is invalid."""


class PermissionDenied(Exception):
    """The streamer isn't marked `permission: true`, or isn't in streamers.yaml at all."""


@dataclass(frozen=True)
class Streamer:
    name: str
    platform: str
    channel_url: str
    permission: bool
    credit: str
    priority: int = 100
    permission_source: str = ""
    keys: frozenset[str] = field(default=frozenset(), compare=False)
    clipskitty_creator_override: str = ""

    @property
    def clipskitty_creator(self) -> str:
        """The creator name Clips Kitty files this channel under ("TheBurntPeanut").

        Sent as `channel` with local windows: Clips Kitty matches creator profiles,
        including a remembered gaming layout (the webcam/avatar box), by it. Clips Kitty
        names YouTube creators by their @handle without the '@', and Twitch/Kick by login.
        streamers.yaml can set `clipskitty_creator` when that guess is wrong.
        """
        if self.clipskitty_creator_override:
            return self.clipskitty_creator_override
        path = urlparse(self.channel_url).path if "://" in self.channel_url else self.channel_url
        parts = [p for p in path.split("/") if p]
        if self.platform == "youtube":
            handle = next((p for p in parts if p.startswith("@")), "")
            return handle.lstrip("@") or self.name
        return parts[0].lstrip("@") if parts else self.name

    @property
    def hashtag(self) -> str:
        """#TheBurntPeanut for "The Burnt Peanut"."""
        joined = "".join(word[:1].upper() + word[1:] for word in self.name.split())
        return "#" + re.sub(r"[^0-9A-Za-z_]", "", joined)


def channel_keys(platform: str, *values: str | None) -> set[str]:
    """Normalised identities for a channel: '@handle', 'uc…' channel id, or login name.

    Accepts channel URLs, '@handles' and bare ids, so a streamers.yaml entry and
    yt-dlp's metadata about a video can be compared.
    """
    keys: set[str] = set()
    for value in values:
        if not value:
            continue
        value = value.strip()
        path = urlparse(value).path if "://" in value else value
        parts = [p for p in path.split("/") if p]
        candidates = parts or [value]
        if platform == "youtube":
            for i, part in enumerate(candidates):
                if part.startswith("@"):
                    keys.add(part.lower())
                elif part.lower().startswith("uc") and len(part) == 24:
                    keys.add(part.lower())
                elif part in ("c", "user") and i + 1 < len(candidates):
                    keys.add("@" + candidates[i + 1].lower())
        else:  # twitch / kick: the first path segment is the login name
            first = candidates[0]
            if first.lower() not in ("videos", "clip", "clips"):
                keys.add(first.lstrip("@").lower())
    return keys


def _entry_to_streamer(entry: dict, index: int) -> Streamer:
    where = f"streamers.yaml entry #{index + 1}"
    if not isinstance(entry, dict):
        raise StreamerFileError(f"{where} must be a mapping")
    missing = [k for k in ("name", "platform", "channel_url", "permission", "credit") if k not in entry]
    if missing:
        raise StreamerFileError(f"{where} is missing: {', '.join(missing)}")
    name = str(entry["name"]).strip()
    platform = str(entry["platform"]).strip().lower()
    if platform not in PLATFORMS:
        raise StreamerFileError(f"{where} ({name}): platform must be one of {', '.join(PLATFORMS)}")
    permission = entry["permission"]
    if not isinstance(permission, bool):
        raise StreamerFileError(
            f"{where} ({name}): permission must be true or false without quotes, got {permission!r}"
        )
    credit = str(entry["credit"] or "").strip()
    if not credit:
        raise StreamerFileError(f"{where} ({name}): credit can't be empty")
    channel_url = str(entry["channel_url"]).strip()
    keys = channel_keys(platform, channel_url, entry.get("channel_id"))
    if not keys:
        raise StreamerFileError(f"{where} ({name}): can't read a channel from channel_url {channel_url!r}")
    try:
        priority = int(entry.get("priority", 100))
    except (TypeError, ValueError) as exc:
        raise StreamerFileError(f"{where} ({name}): priority must be a whole number") from exc
    return Streamer(
        name=name,
        platform=platform,
        channel_url=channel_url,
        permission=permission,
        credit=credit,
        priority=priority,
        permission_source=str(entry.get("permission_source") or ""),
        keys=frozenset(keys),
        clipskitty_creator_override=str(entry.get("clipskitty_creator") or "").strip(),
    )


def load_streamers(path: Path) -> list[Streamer]:
    if not path.is_file():
        raise StreamerFileError(f"No streamers file at {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise StreamerFileError(f"streamers.yaml is not valid YAML: {exc}") from exc
    entries = data.get("streamers") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise StreamerFileError("streamers.yaml needs a top-level 'streamers:' list")
    streamers = [_entry_to_streamer(e, i) for i, e in enumerate(entries)]
    return sorted(streamers, key=lambda s: s.priority)


def require_permission(streamer: Streamer) -> Streamer:
    """The gate. Returns the streamer when permitted, raises PermissionDenied otherwise."""
    if streamer.permission is not True:
        log.warning("Blocked: %s is not marked permission: true", streamer.name)
        raise PermissionDenied(
            f"{streamer.name} is not marked 'permission: true' in streamers.yaml, so nothing is "
            "recorded, downloaded, clipped or published for this channel."
        )
    return streamer


def find_streamer(streamers: list[Streamer], platform: str, *channel_values: str | None) -> Streamer | None:
    wanted = channel_keys(platform, *channel_values)
    for s in streamers:
        if s.platform == platform and s.keys & wanted:
            return s
    return None


def permitted_streamer_for(streamers: list[Streamer], platform: str, *channel_values: str | None) -> Streamer:
    """Finds the streamers.yaml entry for a channel and applies the gate."""
    streamer = find_streamer(streamers, platform, *channel_values)
    if streamer is None:
        shown = ", ".join(v for v in channel_values if v) or "an unknown channel"
        log.warning("Blocked: %s is not in streamers.yaml", shown)
        raise PermissionDenied(
            f"This video's channel ({shown}) is not in streamers.yaml. Add it with "
            "'permission: true' once the creator allows clipping."
        )
    return require_permission(streamer)
