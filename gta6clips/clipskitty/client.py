"""The only module that talks to Clips Kitty's local API.

Built against the API as documented for Clips Kitty 2.1.0 (docs/API.md in
ColinGPT9/clips-studio). Only "stable" endpoints are used, plus GET /plugins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class ClipsKittyError(Exception):
    """Clips Kitty isn't reachable, or answered with an error."""


class QueueFull(ClipsKittyError):
    """409: Clips Kitty's queue is at its limit. Try again later."""


class JobRefused(ClipsKittyError):
    """400/422: Clips Kitty refused the job (for example, the GTA plugin isn't installed)."""


@dataclass(frozen=True)
class SubmitResult:
    job_id: int | None
    video_id: str | None
    already_processed: bool = False
    already_queued: bool = False
    queued_job_id: int | None = None

    @property
    def created(self) -> bool:
        return self.job_id is not None


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(f"{'.'.join(map(str, e.get('loc', [])))}: {e.get('msg')}" for e in detail)
    return str(detail)


class ClipsKittyClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", *, http: httpx.Client | None = None,
                 timeout: float = 30.0):
        host = urlparse(base_url).hostname or ""
        if host not in LOOPBACK_HOSTS:
            # The API has no authentication; it must only ever be reached on this PC.
            raise ValueError(f"Clips Kitty must be addressed as 127.0.0.1 or localhost, not {host!r}")
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(base_url=self.base_url, timeout=timeout)

    # -- plumbing -------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise ClipsKittyError(
                f"Can't reach Clips Kitty at {self.base_url}. Is the app open?"
            ) from exc
        except httpx.HTTPError as exc:
            raise ClipsKittyError(f"Clips Kitty request {method} {path} failed: {exc}") from exc
        log.debug("Clips Kitty %s %s -> %s", method, path, response.status_code)
        if response.status_code == 409:
            raise QueueFull(f"Clips Kitty's queue is full: {_detail(response)}")
        if response.status_code in (400, 422):
            raise JobRefused(_detail(response))
        if response.status_code >= 400:
            raise ClipsKittyError(f"{method} {path} -> {response.status_code}: {_detail(response)}")
        return response

    def _get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs).json()

    # -- readiness ------------------------------------------------------------------
    def health(self) -> dict:
        return self._get("/health")

    def preflight(self) -> dict:
        return self._get("/health/preflight")

    def automation(self) -> dict:
        return self._get("/automation")

    def settings(self) -> dict:
        return self._get("/settings")

    def plugins(self) -> list[dict]:
        data = self._get("/plugins")
        if isinstance(data, dict):  # tolerate {"plugins": [...]}
            data = data.get("plugins", [])
        return list(data or [])

    # -- submitting -----------------------------------------------------------------
    @staticmethod
    def _submit_result(data: dict) -> SubmitResult:
        return SubmitResult(
            job_id=data.get("job_id"),
            video_id=data.get("video_id"),
            already_processed=bool(data.get("already_processed")),
            already_queued=bool(data.get("already_queued")),
            queued_job_id=data.get("queued_job_id"),
        )

    def submit_url(self, url: str, options: dict) -> SubmitResult:
        """POST /jobs. A null job_id means "already processed/queued", not failure."""
        body = {"url": url, **options}
        return self._submit_result(self._request("POST", "/jobs", json=body).json())

    def submit_local(self, path: str, *, title: str, channel: str, options: dict) -> SubmitResult:
        body = {"path": path, "title": title, "channel": channel, **options}
        return self._submit_result(self._request("POST", "/videos/local", json=body).json())

    # -- progress -------------------------------------------------------------------
    def job(self, job_id: int) -> dict:
        return self._get(f"/jobs/{int(job_id)}")

    def job_log(self, job_id: int, tail: int = 300) -> str:
        return str(self._get(f"/jobs/{int(job_id)}/log", params={"tail": tail}).get("log", ""))

    # -- results --------------------------------------------------------------------
    def videos(self) -> list[dict]:
        return list(self._get("/videos"))

    def video(self, video_id: str) -> dict | None:
        """GET /videos has no single-video route; an unknown id gives None."""
        return next((v for v in self.videos() if v.get("video_id") == video_id), None)

    def video_clips(self, video_id: str) -> list[dict]:
        # Unknown ids return 200 [] here, so callers check video() first.
        return list(self._get(f"/videos/{video_id}/clips"))

    def captions(self, clip_id: int) -> list[dict]:
        return list(self._get(f"/clips/{int(clip_id)}/captions").get("lines", []))

    def patch_clip(self, clip_id: int, *, title: str | None = None, description: str | None = None,
                   hashtags: list[str] | None = None) -> dict:
        body = {k: v for k, v in (("title", title), ("description", description), ("hashtags", hashtags))
                if v is not None}
        return self._request("PATCH", f"/clips/{int(clip_id)}", json=body).json()

    def media_url(self, clip_id: int) -> str:
        return f"{self.base_url}/media/{int(clip_id)}"
