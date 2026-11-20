"""Phone notifications through a Discord webhook (DISCORD_WEBHOOK_URL in .env).

What's sent is set in config.yaml → notify. New clips, a stream starting or ending, uploads
going through, and problems (anything the program logs as a warning or an error; the same
problem at most once per notify.problem_repeat_minutes). Only titles and short status lines
go to Discord: never keys, paths or descriptions. A Discord failure never stops the program.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable
from urllib.parse import urlparse

import httpx

from .config import Config, env

log = logging.getLogger(__name__)

WEBHOOK_HOSTS = ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")
MAX_CHARS = 1900   # Discord allows 2000


class NotifyError(ValueError):
    pass


def check_webhook_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.hostname not in WEBHOOK_HOSTS or \
            not parsed.path.startswith("/api/webhooks/"):
        raise NotifyError("DISCORD_WEBHOOK_URL should look like https://discord.com/api/webhooks/<id>/<token> "
                          "(Discord: channel settings → Integrations → Webhooks → Copy Webhook URL)")
    return url.strip()


class DiscordNotifier:
    def __init__(self, webhook_url: str, *, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.url = check_webhook_url(webhook_url)
        self.http = http or httpx.Client(timeout=10)
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()

    def send(self, text: str) -> bool:
        body = {"content": text[:MAX_CHARS], "allowed_mentions": {"parse": []}, "username": "GTA 6 clips"}
        with self._lock:
            for attempt in range(2):
                try:
                    r = self.http.post(self.url, json=body)
                except httpx.HTTPError as exc:
                    log.debug("Discord notification failed: %s", exc)
                    return False
                if r.status_code == 429 and attempt == 0:   # rate limited: wait as told, once
                    try:
                        wait = float(r.json().get("retry_after", 1))
                    except ValueError:
                        wait = 1.0
                    self.sleep(min(wait, 10.0))
                    continue
                if r.status_code >= 400:
                    log.debug("Discord answered %s: %s", r.status_code, r.text[:200])
                    return False
                return True
        return False


class Notifications:
    """What the program calls. Does nothing when no webhook is set."""

    def __init__(self, notifier: DiscordNotifier | None = None, *, events: dict | None = None,
                 problem_repeat_s: float = 1800.0, clock: Callable[[], float] = time.monotonic):
        self.notifier = notifier
        self.events = {"new_clips": True, "live": True, "published": True, "problems": True, **(events or {})}
        self.problem_repeat_s = problem_repeat_s
        self.clock = clock
        self._last_problem: dict[str, float] = {}

    @property
    def on(self) -> bool:
        return self.notifier is not None

    def _send(self, event: str, text: str) -> None:
        if self.notifier is not None and self.events.get(event, False):
            self.notifier.send(text)

    def new_clip(self, *, streamer: str, title: str, clip_id: int, status: str, seconds: float | None = None,
                 score: int | None = None) -> None:
        bits = [f"{seconds:.0f}s" if seconds else "", f"score {score}" if score is not None else ""]
        extra = ", ".join(b for b in bits if b)
        if status == "spoiler_hold":
            head = "🙈 New clip held as a possible spoiler"
        else:
            head = "🎬 New clip ready for review"
        self._send("new_clips", f"{head}: **{title}** ({streamer}{', ' + extra if extra else ''}, clip {clip_id}). "
                                "Open the review page on your PC to approve it.")

    def live(self, text: str) -> None:
        self._send("live", text)

    def published(self, text: str) -> None:
        self._send("published", text)

    def problem(self, text: str) -> None:
        key = text[:120]
        now = self.clock()
        last = self._last_problem.get(key)
        if last is not None and now - last < self.problem_repeat_s:
            return
        self._last_problem[key] = now
        self._send("problems", f"⚠️ {text}")


class _ProblemHandler(logging.Handler):
    """Forwards warnings and errors from the program's own loggers as problem notifications."""

    def __init__(self, notifications: Notifications):
        super().__init__(level=logging.WARNING)
        self.notifications = notifications

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(__name__) or not record.name.startswith("gta6clips"):
            return
        try:
            text = record.getMessage().splitlines()[0][:400]
            self.notifications.problem(text)
        except Exception:   # never let a notification break logging
            pass


_current = Notifications()


def current() -> Notifications:
    return _current


def setup(cfg: Config) -> Notifications:
    """Turn notifications on when DISCORD_WEBHOOK_URL is set (and notify.discord isn't false)."""
    global _current
    root = logging.getLogger("gta6clips")
    for h in list(root.handlers):
        if isinstance(h, _ProblemHandler):
            root.removeHandler(h)
    url = env("DISCORD_WEBHOOK_URL")
    settings = cfg.get("notify", {}) or {}
    if not url or settings.get("discord", True) is False:
        _current = Notifications()
        return _current
    try:
        notifier = DiscordNotifier(url)
    except NotifyError as exc:
        log.warning("Phone notifications are off: %s", exc)
        _current = Notifications()
        return _current
    events = {k: bool(settings.get(k, True)) for k in ("new_clips", "live", "published", "problems")}
    _current = Notifications(notifier, events=events,
                             problem_repeat_s=float(settings.get("problem_repeat_minutes", 30)) * 60)
    root.addHandler(_ProblemHandler(_current))
    return _current
