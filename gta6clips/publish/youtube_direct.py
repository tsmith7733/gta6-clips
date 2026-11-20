"""Uploads to a YouTube channel that isn't the one connected in Clips Kitty.

Clips Kitty connects one channel; this is for a second channel under the same Google login
(config.yaml → publish.channels, chosen per streamer with `youtube_channel` in streamers.yaml).
It uses the owner's own Google Cloud OAuth client: its JSON (Desktop app) goes in
secrets/youtube_client.json, which git ignores. `connect-youtube <key>` signs in once through
Google's own page in the browser (we never see the password), checks that the channel picked
there is the one config.yaml names, and keeps its refresh token in secrets/, encrypted with
Windows DPAPI (only this Windows user on this PC can read it).

Uploads are always private with a publishAt, the same rule as the Clips Kitty path, and the
answer is checked: anything but private is reported loudly.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets as pysecrets
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger(__name__)

SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTubeError(Exception):
    """Google refused something, or the channel isn't connected."""


class NotConnected(YouTubeError):
    pass


# -- secrets on disk -----------------------------------------------------------------------

def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Windows DPAPI: encrypt/decrypt for the current Windows user."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))  # UI_FORBIDDEN
    if not ok:
        raise YouTubeError("Windows couldn't " + ("encrypt" if protect else "decrypt") + " the YouTube token")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class TokenStore:
    """One encrypted file per channel key in secrets/. `protect`/`unprotect` are swappable for tests."""

    def __init__(self, folder: Path, *, protect: Callable[[bytes], bytes] | None = None,
                 unprotect: Callable[[bytes], bytes] | None = None):
        self.folder = folder
        if protect is None or unprotect is None:
            if sys.platform != "win32":
                raise YouTubeError("Saving the YouTube token needs Windows (DPAPI)")
            protect = lambda b: _dpapi(b, protect=True)       # noqa: E731
            unprotect = lambda b: _dpapi(b, protect=False)    # noqa: E731
        self.protect, self.unprotect = protect, unprotect

    def path(self, key: str) -> Path:
        return self.folder / f"youtube_{key}.token"

    def save(self, key: str, data: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path(key).write_bytes(self.protect(json.dumps(data).encode()))

    def load(self, key: str) -> dict | None:
        p = self.path(key)
        if not p.is_file():
            return None
        return json.loads(self.unprotect(p.read_bytes()).decode())


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str

    @classmethod
    def from_file(cls, path: Path) -> "OAuthClient":
        if not path.is_file():
            raise NotConnected(
                f"No Google OAuth client file at {path}. In the Google Cloud console (the project you made for "
                "Clips Kitty): APIs & Services → Credentials → your OAuth client (Desktop app) → Download JSON, "
                f"and save it as {path}.")
        data = json.loads(path.read_text(encoding="utf-8"))
        inner = data.get("installed") or data.get("web") or {}
        if not inner.get("client_id") or not inner.get("client_secret"):
            raise NotConnected(f"{path} doesn't look like a Google OAuth client file")
        return cls(inner["client_id"], inner["client_secret"])


@dataclass(frozen=True)
class ChannelSettings:
    key: str
    title: str
    handle: str            # "@CutsceneSkipped": the sign-in must land on this channel


def channel_settings(cfg) -> dict[str, ChannelSettings]:
    out = {}
    for key, c in ((cfg.get("publish.channels", {}) or {}).items()):
        c = c or {}
        out[key] = ChannelSettings(key=key, title=str(c.get("title") or key), handle=str(c.get("handle") or ""))
    return out


# -- signing in ------------------------------------------------------------------------------

def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class LoopbackReceiver:
    """A one-shot server on 127.0.0.1 that receives Google's redirect after sign-in."""

    def __init__(self, state: str):
        got: dict[str, str] = {}
        self.got = got

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):   # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("state", [""])[0] == state:
                    got["code"] = q.get("code", [""])[0]
                    got["error"] = q.get("error", [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                msg = "Signed in. You can close this tab." if got.get("code") else "Sign-in didn't finish."
                self.wfile.write(f"<p style='font:16px sans-serif'>{msg}</p>".encode())

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 1.0
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_port}"

    def wait(self, timeout_s: float = 300.0) -> str:
        deadline = time.monotonic() + timeout_s
        try:
            while not self.got.get("code") and not self.got.get("error") and time.monotonic() < deadline:
                self.server.handle_request()
        finally:
            self.server.server_close()
        if not self.got.get("code"):
            raise YouTubeError("Google sign-in didn't finish" +
                               (f": {self.got['error']}" if self.got.get("error") else " (timed out)"))
        return self.got["code"]


def connect(settings: ChannelSettings, oauth: OAuthClient, store: TokenStore, *,
            http: httpx.Client | None = None, open_browser: Callable[[str], object] = webbrowser.open,
            receiver: LoopbackReceiver | None = None) -> dict:
    """Signs in through Google's page, checks the channel picked there, saves its token."""
    http = http or httpx.Client(timeout=60)
    verifier, challenge = _pkce()
    state = pysecrets.token_urlsafe(16)
    receiver = receiver or LoopbackReceiver(state)
    params = {"client_id": oauth.client_id, "redirect_uri": receiver.redirect_uri, "response_type": "code",
              "scope": SCOPES, "access_type": "offline", "prompt": "consent select_account", "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print(f"Opening Google's sign-in. Pick the channel {settings.title} ({settings.handle}) when it asks.\n"
          f"If no browser opens, visit:\n{url}\n")
    open_browser(url)
    code = receiver.wait()
    r = http.post(TOKEN_URL, data={"client_id": oauth.client_id, "client_secret": oauth.client_secret,
                                   "code": code, "code_verifier": verifier, "grant_type": "authorization_code",
                                   "redirect_uri": receiver.redirect_uri})
    if r.status_code >= 400:
        raise YouTubeError(f"Google refused the sign-in ({r.status_code}): {r.text[:300]}")
    tokens = r.json()
    if not tokens.get("refresh_token"):
        raise YouTubeError("Google gave no refresh token; run connect-youtube again")
    channel = _my_channel(http, tokens["access_token"])
    _check_handle(settings, channel)
    store.save(settings.key, {"refresh_token": tokens["refresh_token"], "channel_id": channel["id"],
                              "channel_title": channel["title"], "handle": channel["handle"]})
    return channel


def _my_channel(http: httpx.Client, access_token: str) -> dict:
    r = http.get(f"{API}/channels", params={"part": "snippet", "mine": "true"},
                 headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code >= 400:
        raise YouTubeError(f"Couldn't read the signed-in channel ({r.status_code}): {r.text[:300]}")
    items = r.json().get("items") or []
    if not items:
        raise YouTubeError("That Google account has no YouTube channel selected")
    snip = items[0].get("snippet") or {}
    return {"id": items[0]["id"], "title": snip.get("title", ""), "handle": snip.get("customUrl", "")}


def _check_handle(settings: ChannelSettings, channel: dict) -> None:
    want = settings.handle.lstrip("@").lower()
    have = str(channel.get("handle") or "").lstrip("@").lower()
    if want and have != want:
        raise YouTubeError(f"Google signed in as {channel.get('title')!r} ({channel.get('handle') or 'no handle'}), "
                           f"not {settings.handle}. Run connect-youtube again and pick {settings.title} in "
                           "Google's account picker.")


# -- uploading -------------------------------------------------------------------------------

class DirectUploader:
    def __init__(self, settings: ChannelSettings, oauth: OAuthClient | None, store: TokenStore, *,
                 http: httpx.Client | None = None, clock: Callable[[], float] = time.time,
                 oauth_problem: str = ""):
        self.settings = settings
        self.oauth = oauth
        self.oauth_problem = oauth_problem   # why there's no OAuth client (its file is missing)
        self.store = store
        self.http = http or httpx.Client(timeout=httpx.Timeout(60, write=600))
        self.clock = clock
        self._access: str | None = None
        self._expires = 0.0

    def connected(self) -> dict | None:
        """The saved sign-in, or None. Raises NotConnected when the OAuth client file is missing."""
        if self.oauth is None:
            raise NotConnected(self.oauth_problem or "No Google OAuth client")
        return self.store.load(self.settings.key)

    def _token(self) -> str:
        if self._access and self.clock() < self._expires - 60:
            return self._access
        saved = self.connected()
        if not saved:
            raise NotConnected(f"{self.settings.title} isn't connected yet: run "
                               f"'python -m gta6clips connect-youtube {self.settings.key}'")
        r = self.http.post(TOKEN_URL, data={"client_id": self.oauth.client_id,
                                            "client_secret": self.oauth.client_secret,
                                            "refresh_token": saved["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code >= 400:
            raise NotConnected(f"Google no longer accepts {self.settings.title}'s sign-in ({r.status_code}); run "
                               f"connect-youtube {self.settings.key} again")
        data = r.json()
        self._access, self._expires = data["access_token"], self.clock() + float(data.get("expires_in", 3600))
        return self._access

    def upload(self, video: Path, body: dict) -> dict:
        """Uploads `video` private with body['publish_at']. Returns {'youtube_id', 'privacy'}."""
        if body.get("privacy") != "private" or not body.get("publish_at"):
            raise ValueError("uploads must be private with a publish_at")
        saved = self.connected() or {}
        meta = {
            "snippet": {"title": body["title"], "description": body.get("description", ""),
                        "tags": body.get("tags", []), "categoryId": str(body.get("category_id", "20"))},
            "status": {"privacyStatus": "private", "publishAt": body["publish_at"],
                       "selfDeclaredMadeForKids": bool(body.get("made_for_kids", False)),
                       "containsSyntheticMedia": bool(body.get("contains_synthetic_media", False))},
        }
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json; charset=UTF-8",
                   "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(video.stat().st_size)}
        notify = "true" if body.get("notify_subscribers", True) else "false"
        r = self.http.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status",
                                               "notifySubscribers": notify}, headers=headers, json=meta)
        if r.status_code >= 400 or "location" not in r.headers:
            raise YouTubeError(f"YouTube refused the upload ({r.status_code}): {r.text[:300]}")
        with video.open("rb") as f:
            put = self.http.put(r.headers["location"], content=f.read(),
                                headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "video/mp4"})
        if put.status_code >= 400:
            raise YouTubeError(f"The upload failed ({put.status_code}): {put.text[:300]}")
        video_res = put.json()
        privacy = str((video_res.get("status") or {}).get("privacyStatus") or "")
        channel_id = str((video_res.get("snippet") or {}).get("channelId") or "")
        if saved.get("channel_id") and channel_id and channel_id != saved["channel_id"]:
            log.warning("The upload landed on channel %s, not %s", channel_id, self.settings.title)
        return {"youtube_id": str(video_res.get("id") or ""), "privacy": privacy}


def make_uploaders(cfg, secrets_dir: Path) -> dict[str, DirectUploader]:
    """One uploader per config.yaml publish.channels entry (none when there are no extra channels)."""
    channels = channel_settings(cfg)
    if not channels:
        return {}
    try:
        oauth, problem = OAuthClient.from_file(secrets_dir / "youtube_client.json"), ""
    except NotConnected as exc:
        oauth, problem = None, str(exc)
    store = TokenStore(secrets_dir)
    return {key: DirectUploader(s, oauth, store, oauth_problem=problem) for key, s in channels.items()}
