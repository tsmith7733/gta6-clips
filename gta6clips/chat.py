"""Chat replay for a recorded stream, as (seconds from the stream start, text) pairs.

YouTube: yt-dlp's `live_chat` subtitle track (chat replay), one JSON action per line.
Twitch and Kick replays aren't supported yet; those streams are scored on audio alone.
Anything platform-specific stays in this module so the source can be swapped.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

ChatMessage = tuple[float, str]

SUPPORTED_PLATFORMS = ("youtube",)

_MESSAGE_RENDERERS = ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer")


def _runs_text(message: dict) -> str:
    parts: list[str] = []
    for run in message.get("runs", []) or []:
        if "text" in run:
            parts.append(str(run["text"]))
        elif "emoji" in run:
            emoji = run["emoji"] or {}
            emoji_id = str(emoji.get("emojiId") or "")
            shortcuts = emoji.get("shortcuts") or []
            # Standard emoji have the character itself as their id; channel emoji have a long id.
            if emoji_id and len(emoji_id) <= 8:
                parts.append(emoji_id)
            elif shortcuts:
                parts.append(str(shortcuts[0]))
    return "".join(parts).strip()


def parse_youtube_live_chat(lines: Iterable[str]) -> list[ChatMessage]:
    """Parses yt-dlp's .live_chat.json (JSON lines) into (offset_s, text), sorted by time."""
    out: list[ChatMessage] = []
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            bad += 1
            continue
        replay = entry.get("replayChatItemAction") or {}
        try:
            offset = int(replay.get("videoOffsetTimeMsec", entry.get("videoOffsetTimeMsec"))) / 1000.0
        except (TypeError, ValueError):
            continue
        for action in replay.get("actions", []) or []:
            item = (action.get("addChatItemAction") or {}).get("item") or {}
            for renderer in _MESSAGE_RENDERERS:
                if renderer in item:
                    text = _runs_text(item[renderer].get("message") or {})
                    if text:
                        out.append((max(0.0, offset), text))
    if bad:
        log.debug("Skipped %d unreadable chat lines", bad)
    out.sort(key=lambda m: m[0])
    return out


def read_youtube_live_chat(path: Path) -> list[ChatMessage]:
    with path.open(encoding="utf-8", errors="replace") as f:
        return parse_youtube_live_chat(f)


# -- Live chat: yt-dlp keeps appending to the file while the stream is live ------------

def parse_youtube_chat_utc(lines: Iterable[str]) -> list[ChatMessage]:
    """(UTC seconds the message was sent, text) from yt-dlp live chat lines.

    Live, yt-dlp's offsets count from the first message it saw, so the message's own
    timestampUsec is used instead; it puts chat on the same wall clock as the recording.
    """
    out: list[ChatMessage] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        replay = entry.get("replayChatItemAction") or {}
        actions = replay.get("actions") or ([entry] if "addChatItemAction" in entry else [])
        for action in actions:
            item = (action.get("addChatItemAction") or {}).get("item") or {}
            for renderer in _MESSAGE_RENDERERS:
                if renderer not in item:
                    continue
                body = item[renderer]
                try:
                    utc = int(body.get("timestampUsec")) / 1e6
                except (TypeError, ValueError):
                    continue
                text = _runs_text(body.get("message") or {})
                if text:
                    out.append((utc, text))
    out.sort(key=lambda m: m[0])
    return out


class ChatFileTail:
    """Reads the lines added to a growing chat file since the last read (whole lines only)."""

    def __init__(self, *candidates: Path):
        self.candidates = candidates   # e.g. the .part file yt-dlp writes, then the finished file
        self._path: Path | None = None
        self._pos = 0
        self._partial = ""

    def read_new(self) -> list[ChatMessage]:
        path = self._path if self._path and self._path.is_file() else \
            next((p for p in self.candidates if p.is_file()), None)
        if path is None:
            return []
        if path != self._path:
            # yt-dlp renamed .part to the finished name: same content, carry on from the same place.
            self._path = path
        with path.open("rb") as f:
            f.seek(self._pos)
            data = f.read()
            self._pos = f.tell()
        text = self._partial + data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._partial = lines.pop()  # an unfinished last line waits for the next read
        return parse_youtube_chat_utc(lines)
