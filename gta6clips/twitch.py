"""Twitch Helix API: an app access token, the GTA 6 game id, user ids and clips.

Needs TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env (a free app at dev.twitch.tv).
Only reads public data. The game id is looked up by name at runtime, never hardcoded,
and cached in our database.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Callable

import httpx

from . import db
from .config import env

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.twitch.tv/helix"


class HelixError(Exception):
    """Twitch answered with an error, or couldn't be reached."""


class TwitchSetupError(HelixError):
    """The Twitch app keys aren't in .env."""


class Helix:
    def __init__(self, client_id: str, client_secret: str, *, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time):
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http or httpx.Client(timeout=20)
        self.clock = clock
        self._token: str | None = None
        self._expires = 0.0

    @classmethod
    def from_env(cls) -> "Helix":
        cid, secret = env("TWITCH_CLIENT_ID"), env("TWITCH_CLIENT_SECRET")
        if not cid or not secret:
            raise TwitchSetupError(
                "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET aren't set in .env. Create a free app at "
                "https://dev.twitch.tv/console/apps (Register Your Application; OAuth Redirect URL "
                "http://localhost; Category: Application Integration; Client Type: Confidential), then put "
                "its Client ID and a New Secret in .env.")
        return cls(cid, secret)

    def _bearer(self, *, renew: bool = False) -> str:
        if renew or self._token is None or self.clock() >= self._expires - 60:
            try:
                r = self.http.post(TOKEN_URL, data={"client_id": self.client_id,
                                                    "client_secret": self.client_secret,
                                                    "grant_type": "client_credentials"})
            except httpx.HTTPError as exc:
                raise HelixError(f"Couldn't reach Twitch: {exc}") from exc
            if r.status_code >= 400:
                raise HelixError(f"Twitch refused the app keys in .env ({r.status_code}): {r.text[:200]}")
            data = r.json()
            self._token = str(data["access_token"])
            self._expires = self.clock() + float(data.get("expires_in", 3600))
        return self._token

    def get(self, path: str, params: dict | list) -> dict:
        for attempt in range(2):
            headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {self._bearer(renew=attempt > 0)}"}
            try:
                r = self.http.get(f"{API}/{path}", params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise HelixError(f"Couldn't reach Twitch: {exc}") from exc
            if r.status_code == 401 and attempt == 0:
                continue   # the token expired early: get a new one, once
            if r.status_code >= 400:
                raise HelixError(f"Twitch answered {r.status_code} for {path}: {r.text[:200]}")
            return r.json()
        raise HelixError("Twitch keeps refusing the access token")

    def game_id(self, name: str, conn: sqlite3.Connection | None = None) -> str | None:
        key = f"twitch.game_id:{name.lower()}"
        if conn is not None:
            cached = db.kv_get(conn, key)
            if cached:
                return cached
        data = self.get("games", {"name": name}).get("data") or []
        if not data:
            return None
        gid = str(data[0]["id"])
        if conn is not None:
            db.kv_set(conn, key, gid)
        log.info("Twitch game id for %s: %s", name, gid)
        return gid

    def user_ids(self, logins: list[str]) -> dict[str, str]:
        """login (lower case) -> broadcaster id, for those that exist."""
        out: dict[str, str] = {}
        for i in range(0, len(logins), 100):
            chunk = logins[i:i + 100]
            data = self.get("users", [("login", lg) for lg in chunk]).get("data") or []
            out.update({str(u["login"]).lower(): str(u["id"]) for u in data})
        return out

    def clips(self, broadcaster_id: str, *, started_at: str, ended_at: str, first: int = 20) -> list[dict]:
        """A channel's clips made in [started_at, ended_at], most viewed first."""
        return list(self.get("clips", {"broadcaster_id": broadcaster_id, "started_at": started_at,
                                       "ended_at": ended_at, "first": min(int(first), 100)}).get("data") or [])


class TwitchLiveCheck:
    """Is a Twitch streamer live right now (any game)? Cached for a couple of minutes.

    Returns None when it can't tell (not a Twitch channel, or Twitch can't be reached),
    so callers that hold something while a streamer is live keep holding it.
    """

    def __init__(self, helix: Helix, *, ttl_s: float = 120.0, clock: Callable[[], float] = time.time):
        self.helix = helix
        self.ttl_s = ttl_s
        self.clock = clock
        self._cache: dict[str, tuple[float, bool]] = {}

    def __call__(self, streamer) -> bool | None:
        if streamer.platform != "twitch":
            return None
        from .streamers import channel_keys
        login = next(iter(sorted(channel_keys("twitch", streamer.channel_url))), "")
        hit = self._cache.get(login)
        if hit and self.clock() - hit[0] < self.ttl_s:
            return hit[1]
        try:
            live = bool(self.helix.get("streams", {"user_login": login}).get("data"))
        except HelixError as exc:
            log.warning("Couldn't check whether %s is live on Twitch: %s", streamer.name, exc)
            return None
        self._cache[login] = (self.clock(), live)
        return live
