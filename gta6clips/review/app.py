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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from ..clipskitty import ClipsKittyClient, ClipsKittyError
from ..publish.service import PublishService
from .service import ReviewError, ReviewService

log = logging.getLogger(__name__)

LOOPBACK = ("127.0.0.1", "localhost")


def create_app(service: ReviewService, client: ClipsKittyClient, publisher: PublishService | None = None,
               *, auto_schedule: bool = False, editor=None) -> FastAPI:
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

    @app.get("/api/channels")
    def channels():
        """The channels clips go to, for the page's channel switch: [{key, title}]."""
        def go():
            return {"channels": [{"key": k, "title": publisher.channel_title(k) if publisher is not None
                                  else (k or "Clips Kitty's channel")} for k in service.channels()]}
        return run(go)

    @app.get("/api/clips")
    def clips(status: str = "pending_review", channel: str | None = None):
        def go():
            moved = service.reconcile()
            if publisher is not None:
                publisher.refresh()
            cards = []
            for c in service.cards(status, channel):
                d = c.as_dict()
                d["upload"] = publisher.upload_for(c.id) if publisher is not None else None
                d["channel"] = publisher.channel_for_streamer(c.streamer) if publisher is not None else ""
                cards.append(d)
            return {"counts": service.counts(channel), "clips": cards, "followed": moved}
        return run(go)

    def need_publisher() -> PublishService:
        if publisher is None:
            raise HTTPException(404, "Publishing isn't set up on this page.")
        return publisher

    @app.get("/api/publish/status")
    def publish_status():
        pub = need_publisher()
        return run(lambda: pub.status().as_dict())

    @app.post("/api/publish/plan")
    def publish_plan(body: dict = Body(default={})):
        pub = need_publisher()
        ids = body.get("clip_ids")
        return run(lambda: pub.plan([int(i) for i in ids] if isinstance(ids, list) else None).as_dict())

    @app.post("/api/publish/execute")
    def publish_execute(body: dict = Body(...)):
        pub = need_publisher()
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise HTTPException(400, "Nothing to upload.")
        return run(lambda: pub.execute(items))

    @app.get("/api/clips/{clip_id}/captions")
    def captions(clip_id: int):
        return run(lambda: {"lines": service.captions(clip_id)})

    @app.post("/api/clips/{clip_id}/title")
    def title(clip_id: int, body: dict = Body(...)):
        return run(lambda: service.set_title(clip_id, str(body.get("title", ""))).as_dict())

    @app.post("/api/clips/{clip_id}/approve")
    def approve(clip_id: int, body: dict = Body(...)):
        def go():
            card = service.approve(clip_id, str(body.get("title", "")),
                                   confirm_spoiler=body.get("confirm_spoiler") is True).as_dict()
            if publisher is not None and auto_schedule:
                try:
                    card.update(publisher.schedule_one(clip_id))
                except (ReviewError, ClipsKittyError) as exc:   # the approval stands either way
                    card["not_scheduled"] = str(exc)
            return card
        return run(go)

    @app.post("/api/clips/{clip_id}/reject")
    def reject(clip_id: int, body: dict = Body(default={})):
        return run(lambda: service.reject(clip_id).as_dict())

    @app.post("/api/clips/{clip_id}/to-review")
    def to_review(clip_id: int, body: dict = Body(default={})):
        return run(lambda: service.send_to_review(clip_id).as_dict())

    @app.post("/api/clips/{clip_id}/rerender")
    def rerender(clip_id: int, body: dict = Body(default={})):
        return run(lambda: service.rerender(clip_id).as_dict())

    @app.post("/api/clips/{clip_id}/undo")
    def undo(clip_id: int, body: dict = Body(default={})):
        return run(lambda: service.undo(clip_id).as_dict())

    @app.get("/edits/{edit_id}.mp4")
    def edited(edit_id: int):
        return FileResponse(run(lambda: service.edit_file(edit_id)), media_type="video/mp4")

    @app.post("/api/clips/{clip_id}/edit")
    def edit(clip_id: int, body: dict = Body(default={})):
        """Claude edits the clip (again), with the owner's note; it runs in the background."""
        if editor is None:
            raise HTTPException(404, "The Claude editor isn't set up (it needs ANTHROPIC_API_KEY in .env).")
        def go():
            if service.card(clip_id).status not in ("pending_review", "spoiler_hold", "approved", "needs_fix"):
                raise ReviewError("Only clips that aren't published or rejected can be edited.")
            note = service.note_for_editor(clip_id, str(body.get("note", "")))
            return note
        note = run(go)
        editor.request(clip_id, note)
        return {"started": True}

    @app.post("/api/clips/{clip_id}/note")
    def note(clip_id: int, body: dict = Body(...)):
        """A suggestion for the editor's later edits, without editing this clip again."""
        return {"saved": run(lambda: service.note_for_editor(clip_id, str(body.get("note", ""))))}

    @app.post("/api/clips/{clip_id}/edit/drop")
    def drop_edit(clip_id: int, body: dict = Body(default={})):
        return run(lambda: service.drop_edit(clip_id).as_dict())

    @app.get("/media/{clip_id}")
    def media(clip_id: int):
        """Only clips we track; the browser then reads the file straight from Clips Kitty, which
        serves range requests (seeking). Proxying it here leaked a connection whenever the
        browser dropped a half-read video."""
        run(lambda: service.card(clip_id))
        return RedirectResponse(client.media_url(clip_id), status_code=307)

    return app


def serve(service: ReviewService, client: ClipsKittyClient, *, port: int,
          publisher: PublishService | None = None, auto_schedule: bool = False, editor=None) -> None:
    import uvicorn

    uvicorn.run(create_app(service, client, publisher, auto_schedule=auto_schedule, editor=editor), host="127.0.0.1", port=port, log_level="warning")
