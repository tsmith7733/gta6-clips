"""Discord phone notifications."""

from __future__ import annotations

import logging

import httpx
import pytest

from gta6clips import notify
from gta6clips.notify import DiscordNotifier, Notifications, NotifyError

HOOK = "https://discord.com/api/webhooks/123/abc"


def _discord(sent: list, status: int = 204, first: int | None = None):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if first is not None and calls["n"] == 1:
            return httpx.Response(first, json={"retry_after": 0.01})
        sent.append(request)
        return httpx.Response(status)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("url", ["http://discord.com/api/webhooks/1/x", "https://evil.example/api/webhooks/1/x",
                                 "https://discord.com/channels/1/2", ""])
def test_only_real_discord_webhooks_are_accepted(url):
    with pytest.raises(NotifyError):
        DiscordNotifier(url)


def test_sends_plain_text_without_pinging_anyone():
    sent = []
    n = DiscordNotifier(HOOK, http=_discord(sent))
    assert n.send("hello @everyone")
    body = sent[0].read().decode()
    assert '"parse":[]' in body.replace(" ", "") and "hello @everyone" in body


def test_rate_limit_is_retried_once():
    sent = []
    n = DiscordNotifier(HOOK, http=_discord(sent, first=429), sleep=lambda s: None)
    assert n.send("x") and len(sent) == 1


def test_discord_failing_never_raises():
    def boom(request):
        raise httpx.ConnectError("offline")
    n = DiscordNotifier(HOOK, http=httpx.Client(transport=httpx.MockTransport(boom)))
    assert n.send("x") is False


class Recorder:
    def __init__(self):
        self.texts = []

    def send(self, text):
        self.texts.append(text)
        return True


def test_events_can_be_turned_off_and_problems_repeat_only_after_a_while():
    t = {"now": 0.0}
    rec = Recorder()
    n = Notifications(rec, events={"live": False}, problem_repeat_s=1800, clock=lambda: t["now"])
    n.live("started")
    n.new_clip(streamer="The Burnt Peanut", title="Soccer Practice", clip_id=30, status="pending_review",
               seconds=15, score=79)
    n.new_clip(streamer="The Burnt Peanut", title="Cutscene", clip_id=36, status="spoiler_hold")
    n.problem("ffmpeg stopped")
    n.problem("ffmpeg stopped")
    t["now"] = 1801
    n.problem("ffmpeg stopped")
    assert len(rec.texts) == 4
    assert "Soccer Practice" in rec.texts[0] and "15s" in rec.texts[0] and "score 79" in rec.texts[0]
    assert "spoiler" in rec.texts[1]
    assert rec.texts[2] == rec.texts[3] == "⚠️ ffmpeg stopped"


def test_nothing_is_sent_without_a_webhook(cfg, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert not notify.setup(cfg).on


def test_warnings_from_the_program_become_problem_notifications(cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    n = notify.setup(cfg)
    rec = Recorder()
    n.notifier = rec
    try:
        logging.getLogger("gta6clips.live.recorder").warning("ffmpeg stopped (exit 1)")
        logging.getLogger("gta6clips.live.recorder").info("Recording to x")       # not a problem
        logging.getLogger("httpx").warning("someone else's warning")              # not ours
        assert rec.texts == ["⚠️ ffmpeg stopped (exit 1)"]
    finally:
        monkeypatch.delenv("DISCORD_WEBHOOK_URL")
        notify.setup(cfg)                                                           # handler off again
