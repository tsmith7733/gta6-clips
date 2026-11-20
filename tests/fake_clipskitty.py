"""A stand-in for Clips Kitty 2.1.0's local API, shaped like its documented responses.

Only what the orchestrator uses. Tests drive it through FakeState.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

PLUGIN_ID = "colingpt9/gta-vi-highlights"


@dataclass
class FakeState:
    app_version: str = "2.1.0"
    automation_enabled: bool = False
    auto_upload: bool = False
    privacy: str = "private"
    preflight_ok: bool = True
    plugins: list[dict] = field(default_factory=lambda: [
        {"id": PLUGIN_ID, "version": "0.4.1", "enabled": True, "problems_here": []}
    ])
    queue_full: bool = False
    paused: bool = False
    processed: set[str] = field(default_factory=set)        # video ids already done
    jobs: dict[int, dict] = field(default_factory=dict)
    # Each job is "running" for this many GETs before it finishes.
    polls_until_done: int = 1
    fail_jobs: bool = False
    clips_per_video: int = 2
    plugin_finds_nothing: bool = False   # the GTA plugin's answer on a window that's only funny
    clips: dict[int, dict] = field(default_factory=dict)
    submissions: list[dict] = field(default_factory=list)
    patches: list[tuple[int, dict]] = field(default_factory=list)
    youtube_enabled: bool = True
    youtube_connected: bool = True
    uploads_used: int = 0
    uploads_limit: int = 100
    lock_uploads: bool = False            # YouTube locks uploads from an unaudited project to private
    publish_fails: bool = False
    publishes: list[tuple[int, dict]] = field(default_factory=list)
    uploads: dict[int, dict] = field(default_factory=dict)   # clip id -> upload
    _next_publish: int = 1
    _next_job: int = 100
    _next_clip: int = 800


def _video_id_from(url: str) -> str:
    return url.rsplit("v=", 1)[-1] if "v=" in url else url.rsplit("/", 1)[-1]


def create_app(state: FakeState) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def loopback_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return JSONResponse({"detail": "host not allowed"}, status_code=400)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"ok": True, "app_version": state.app_version, "api_version": 1}

    @app.get("/health/preflight")
    def preflight():
        return {"ready": state.preflight_ok, "checks": [
            {"name": "ffmpeg", "ok": True, "detail": "8.1", "fix": "", "blocking": True},
            {"name": "model", "ok": state.preflight_ok, "detail": "no model" if not state.preflight_ok else "ok",
             "fix": "Pick a model in Settings" if not state.preflight_ok else "", "blocking": True},
        ]}

    @app.get("/automation")
    def automation():
        return {"enabled": state.automation_enabled, "delete_sources": False, "interval_minutes": 15,
                "watches": 0, "watching": 0, "presets": []}

    @app.get("/settings")
    def settings():
        return {"model": "claude", "channel": "", "auto_upload": state.auto_upload, "privacy": state.privacy}

    @app.get("/plugins")
    def plugins():
        return {"plugins": state.plugins}

    def _queue(body: dict, video_id: str) -> dict:
        state.submissions.append(body)
        if state.queue_full:
            raise HTTPException(409, "the queue is full")
        pipeline = body.get("pipeline")
        if pipeline:
            pid = pipeline["id"] if isinstance(pipeline, dict) else pipeline
            if not any(p["id"] == pid for p in state.plugins):
                raise HTTPException(400, f"pipeline: the pipeline {pid} isn't installed")
        if video_id in state.processed and not body.get("force"):
            return {"job_id": None, "already_processed": True, "video_id": video_id}
        for job in state.jobs.values():
            if job["video_id"] == video_id and job["status"] in ("queued", "running"):
                return {"job_id": None, "already_queued": True, "video_id": video_id,
                        "queued_job_id": job["id"]}
        state._next_job += 1
        state.jobs[state._next_job] = {"id": state._next_job, "type": "process", "status": "queued",
                                       "error": "", "video_id": video_id, "polls": 0,
                                       "pipeline": bool(pipeline)}
        return {"job_id": state._next_job}

    @app.post("/jobs")
    async def post_job(request: Request):
        body = await request.json()
        if "url" not in body:
            raise HTTPException(422, "url required")
        return _queue(body, _video_id_from(body["url"]))

    @app.post("/videos/local")
    async def post_local(request: Request):
        body = await request.json()
        if not body.get("path") or not body.get("title"):
            raise HTTPException(422, "path and title required")
        return _queue(body, "local_" + re.split(r"[\\/]", body["path"])[-1])

    def _finish(job: dict) -> None:
        if state.fail_jobs:
            job["status"], job["error"] = "failed", "transcription crashed"
            return
        job["status"] = "done"
        state.processed.add(job["video_id"])
        found = 0 if state.plugin_finds_nothing and job.get("pipeline") else state.clips_per_video
        for i in range(found):
            state._next_clip += 1
            start = 100.0 + 60 * i
            state.clips[state._next_clip] = {
                "id": state._next_clip, "video_id": job["video_id"],
                "start_s": start, "end_s": start + (30 if i == 0 else 70), "score": 90 - i,
                "title": f"Draft title {i}", "description": "A short summary written by the model.",
                "hashtags": ["#clip", "#gta6"], "hook": f"no way {i}", "status": "queued",
                "lines": [{"start": 0.0, "end": 1.0, "text": f"he crashed the car {i}"}],
            }

    @app.get("/queue")
    def queue():
        return {"processing": [], "queued": [], "completed": [], "failed": [], "paused": state.paused,
                "capacity": 5, "max_active": 5}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: int):
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        if job["status"] == "queued" and state.paused:
            return {k: v for k, v in job.items() if k != "polls"}
        if job["status"] in ("queued", "running"):
            job["polls"] += 1
            if job["polls"] > state.polls_until_done:
                _finish(job)
            else:
                job["status"] = "running"
        return {k: v for k, v in job.items() if k != "polls"}

    @app.get("/jobs/{job_id}/log")
    def job_log(job_id: int, tail: int = 300):
        return {"log": "stage transcribe\nERROR: transcription crashed", "missing": False}

    @app.get("/videos")
    def videos():
        return [{"video_id": v, "title": "", "status": "done"} for v in sorted(state.processed)]

    @app.get("/videos/{video_id}/clips")
    def video_clips(video_id: str):
        return [{k: v for k, v in c.items() if k != "lines"} for c in state.clips.values()
                if c["video_id"] == video_id]

    @app.get("/clips/{clip_id}/captions")
    def captions(clip_id: int):
        clip = state.clips.get(clip_id)
        if clip is None:
            raise HTTPException(404, "no such clip")
        return {"lines": clip["lines"]}

    @app.get("/media/{clip_id}")
    def media(clip_id: int, request: Request):
        clip = state.clips.get(clip_id)
        if clip is None:
            raise HTTPException(404, "no such clip")
        data = f"fake mp4 for clip {clip_id} ".encode() * 40
        rng = request.headers.get("range", "")
        m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
        if not m:
            return Response(data, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})
        start = int(m.group(1))
        end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
        return Response(data[start:end + 1], status_code=206, media_type="video/mp4",
                        headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{len(data)}"})

    @app.get("/youtube/status")
    def youtube_status():
        if not state.youtube_enabled:
            return {"enabled": False}
        return {"enabled": True, "backend": "windows-dpapi", "has_client": True,
                "connected": state.youtube_connected,
                "channel": {"id": "UCtest", "title": "GTA Clips", "handle": "@gtaclips"},
                "settings": {"enabled": True, "privacy": state.privacy, "category_id": "22",
                             "made_for_kids": False, "notify_subscribers": True, "region": "US"},
                "quota": {"uploads_used": state.uploads_used, "uploads_limit": state.uploads_limit,
                          "remaining": state.uploads_limit - state.uploads_used, "resets_at": ""}}

    @app.post("/clips/{clip_id}/publish")
    async def publish(clip_id: int, request: Request):
        if not state.youtube_enabled:
            raise HTTPException(404, "Not Found")
        clip = state.clips.get(clip_id)
        if clip is None:
            raise HTTPException(404, "no such clip")
        body = await request.json()
        if not body.get("title"):
            raise HTTPException(422, "title required")
        if body.get("publish_at") and not re.search(r"(Z|[+-]\d\d:\d\d)$", body["publish_at"]):
            raise HTTPException(400, "publish_at needs an offset")
        state.publishes.append((clip_id, body))
        job = state._next_publish
        state._next_publish += 1
        state.uploads_used += 1
        privacy = "private" if body.get("publish_at") else body.get("privacy", state.privacy)
        if state.publish_fails:
            state.uploads[clip_id] = {"clip_id": clip_id, "youtube_id": "", "privacy": privacy,
                                      "actual_privacy": "", "state": "failed", "publish_at": "",
                                      "error": "The upload failed"}
        else:
            state.uploads[clip_id] = {"clip_id": clip_id, "youtube_id": f"yt{clip_id:08d}"[:11],
                                      "privacy": privacy, "actual_privacy": "private",
                                      "state": "locked_private" if state.lock_uploads else "uploaded",
                                      "publish_at": body.get("publish_at", ""), "channel_title": "GTA Clips"}
        return {"publish_job_id": job, "render_job_id": None}

    @app.get("/clips/{clip_id}/publish")
    def publish_state(clip_id: int):
        if not state.youtube_enabled:
            raise HTTPException(404, "Not Found")
        return {"upload": state.uploads.get(clip_id), "job": None}

    @app.patch("/clips/{clip_id}")
    async def patch_clip(clip_id: int, request: Request):
        clip = state.clips.get(clip_id)
        if clip is None:
            raise HTTPException(404, "no such clip")
        body = await request.json()
        state.patches.append((clip_id, body))
        clip.update({k: v for k, v in body.items() if k in ("title", "description", "hashtags")})
        return {k: v for k, v in clip.items() if k != "lines"}

    return app
