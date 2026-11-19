"""The review page: python -m gta6clips review, then open http://127.0.0.1:<port>.

Listens on 127.0.0.1 only. Requests with another Host header are refused (no DNS rebinding), and
every change is a JSON POST from this page's own origin, so another website can't approve or
reject clips through the browser.
"""

from __future__ import annotations

import logging
import threading
from importlib import resources
from typing import Callable

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..clipskitty import ClipsKittyClient, ClipsKittyError
from .service import ReviewError, ReviewService

log = logging.getLogger(__name__)

LOOPBACK = ("127.0.0.1", "localhost")


def create_app(service: ReviewService, client: ClipsKittyClient) -> FastAPI:
    app = FastAPI(title="GTA 6 clips review", docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()          # one sqlite connection, shared by the worker threads
    page = resources.files(__package__).joinpath("page.html").read_text(encoding="utf-8")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in LOOPBACK:
            return JSONResponse({"detail": "This page only answers on 127.0.0.1."}, status_code=400)
        if request.method == "POST":
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "JSON only"}, status_code=415)
            origin = request.headers.get("origin")
            if origin and origin.split("//", 1)[-1].split(":")[0] not in LOOPBACK:
                return JSONResponse({"detail": "Changes are only accepted from this page."}, status_code=403)
        return await call_next(request)

    def run(fn: Callable):
        with lock:
            try:
                return fn()
            except ReviewError as exc:
                raise HTTPException(400, str(exc)) from exc
            except ClipsKittyError as exc:
                raise HTTPException(502, f"Clips Kitty: {exc}") from exc

    @app.get("/", response_class=HTMLResponse)
    def index():
        return page

    @app.get("/api/clips")
    def clips(status: str = "pending_review"):
        def go():
            moved = service.reconcile()
            return {"counts": service.counts(), "clips": [c.as_dict() for c in service.cards(status)],
                    "followed": moved}
        return run(go)

    @app.get("/api/clips/{clip_id}/captions")
    def captions(clip_id: int):
        return run(lambda: {"lines": service.captions(clip_id)})

    @app.post("/api/clips/{clip_id}/title")
    def title(clip_id: int, body: dict = Body(...)):
        return run(lambda: service.set_title(clip_id, str(body.get("title", ""))).as_dict())

    @app.post("/api/clips/{clip_id}/approve")
    def approve(clip_id: int, body: dict = Body(...)):
        return run(lambda: service.approve(clip_id, str(body.get("title", "")),
                                           confirm_spoiler=body.get("confirm_spoiler") is True).as_dict())

    @app.post("/api/clips/{clip_id}/reject")
    def reject(clip_id: int, body: dict = Body(default={})):
        return run(lambda: service.reject(clip_id).as_dict())

    @app.post("/api/clips/{clip_id}/undo")
    def undo(clip_id: int, body: dict = Body(default={})):
        return run(lambda: service.undo(clip_id).as_dict())

    @app.get("/media/{clip_id}")
    def media(clip_id: int):
        """Only clips we track; the browser then reads the file straight from Clips Kitty, which
        serves range requests (seeking). Proxying it here leaked a connection whenever the
        browser dropped a half-read video."""
        run(lambda: service.card(clip_id))
        return RedirectResponse(client.media_url(clip_id), status_code=307)

    return app


def serve(service: ReviewService, client: ClipsKittyClient, *, port: int) -> None:
    import uvicorn

    uvicorn.run(create_app(service, client), host="127.0.0.1", port=port, log_level="warning")
