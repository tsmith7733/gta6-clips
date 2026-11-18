# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A local pipeline that watches permitted streamers playing GTA 6 (Grand Theft Auto VI), spots viral moments while they're live, and hands those moments to **Clips Kitty** (`ColinGPT9/clips-studio`, a desktop app the owner installs separately) to be cut into vertical YouTube Shorts. A human approves every clip before it is published.

**This repo is the orchestrator around Clips Kitty, not a clip editor.** Clips Kitty does the transcription, choosing the exact clip boundaries, the gaming layout (webcam + game), captions, titles/descriptions/hashtags, rendering and the YouTube upload. This repo does what Clips Kitty can't: the permission gate, live GTA 6 category watching, rolling recording, per-channel chat and audio spike detection, the spoiler queue, mandatory credit lines, and the approve-before-publish rule.

**Status:** planning. No code yet. The plan below is waiting for the owner's OK. Update this section at the end of each phase.

Phases (each one runnable and tested, then committed, before the next starts):
1. One permitted GTA 6 VOD → Clips Kitty → our post-processing (credit, hashtags, 3 titles) → clips listed as `pending_review`
2. Chat and audio spike scoring that picks the windows to send, plus the spoiler filter
3. Live watcher and multi-stream rolling recording
4. Review page
5. Publishing through Clips Kitty (private or scheduled), with quota warnings

## Target machine

The owner runs this on **Windows with an NVIDIA GPU**, with Clips Kitty installed and set to use the **Claude API** (configured in Clips Kitty: Settings → AI). Development happens in a Linux cloud container where Clips Kitty isn't running, so:
- Use `pathlib`. Call `subprocess` with argument lists and `shell=False`. Don't use POSIX-only APIs (`fork`, `killpg`, sending SIGTERM to process groups). Stop children with `proc.terminate()`, and on Windows create them with `CREATE_NEW_PROCESS_GROUP`. Keep the default Proactor event loop.
- All Clips Kitty access goes through `gta6clips/clipskitty/client.py`. Tests run against `tests/fake_clipskitty.py`, a FastAPI stub that copies the documented responses. Real integration runs happen on the owner's PC.
- streamlink, yt-dlp and chat-downloader come from pip, inside the venv. ffmpeg for recording is a system dependency: ask before `winget install`, or point `config.yaml → paths.ffmpeg` at a portable build. Clips Kitty ships its own ffmpeg, but don't depend on its install path.

## Hard rules (owner requirements, do not relax)

- **Permission gate:** only channels with `permission: true` in `streamers.yaml` may be recorded, downloaded, sent to Clips Kitty or published. Enforce this in one function (`gta6clips/streamers.py`) that every entry point calls, and keep a test for it.
- **Nothing is published without approval.** Only clips with our status `approved` are published. Always send `privacy: "private"`, or a `publish_at` (which YouTube forces to private). Never rely on Clips Kitty's publish defaults, which can be `public`. After an upload, compare `privacy` against `actual_privacy`.
- **Clips Kitty's own automation stays off.** That means watched channels (`/automation`) and any auto-publish settings. Our orchestrator is the only thing that queues jobs and publishes. On startup, check `GET /automation` and `GET /settings` and refuse to run if either is on.
- **Credit is mandatory.** Every description includes the streamer's `credit` line and channel URL from `streamers.yaml`. We write it with `PATCH /clips/{id}`, and the publish step re-checks it and refuses if it's missing.
- **Spoilers** (major story missions, the ending) get our status `spoiler_hold` and show in a separate Spoilers tab. They are never in the main queue.
- Secrets live only in `.env`, which is gitignored. `streamers.yaml` is edited by the owner. Don't rewrite their entries.
- Ask the owner before installing anything system-wide. Installing into `.venv` is fine.
- Resolve the Twitch GTA 6 game ID at runtime (Helix `GET /games?name=Grand Theft Auto VI`) and cache it. Don't hardcode it.
- Clips Kitty is AGPL-3.0. Integrate with it over its HTTP API and plugin contract only. Don't copy its source into this repo.

## Architecture (planned)

```
watcher → recorders (+ chat) → spike detector → window cutter → Clips Kitty → post-processor → review → publish (via Clips Kitty)
```

- **The state lives in our own SQLite (`data/orchestrator.db`).** We never write to Clips Kitty's database; we only use its API. The tables are `streams`, `segments`, `chat_messages`, `signals`, `windows` (moments sent to Clips Kitty, with `ck_video_id`/`ck_job_id`) and `clips` (Clips Kitty clip ID plus our status: `pending_review | spoiler_hold → approved | rejected → published`), and `publish_log`.
- **A single clock.** Segments, chat and audio levels are all stored in wall-clock UTC. Segment files are named after their start time in UTC epoch seconds. VOD mode builds the same timeline from the VOD's start time.
- **Platform order: YouTube Live first** (the first streamer is `@TheBurntPeanut` on YouTube), then Twitch, then Kick. On YouTube, don't poll `search.list` for live status: it costs 100 units per call, which blows the 10,000/day quota at a 2–3 min interval. Instead, check `<channel>/live` with yt-dlp, which costs no quota. The Data API doesn't expose the game, so detect GTA 6 from the stream title and description keywords, with yt-dlp metadata where available. Make the keywords configurable.
- **watcher:** an asyncio loop that polls every `poll_interval_s` (120–180 s) through the platform adapter (Helix on Twitch). It starts a recorder when a permitted streamer is live in the GTA 6 category and stops it when they leave the category. Concurrency is capped by `max_concurrent_streams` (default 3, ordered by `priority`). A `platforms/` adapter leaves room for YouTube Live and Kick. (Clips Kitty's own watchers only pick up *finished* VODs, which is why this is ours.)
- **recorder:** `streamlink --stdout | ffmpeg -c copy -f segment -segment_time 60`, supervised and restarted with backoff. A janitor deletes segments older than `retention_hours`, except those that an unsent or in-progress window still references.
- **spike detector:** cheap, with no transcription or LLM. Chat velocity, keyword hits (`CLIP IT`, `KEKW`, `?????`…) and audio loudness are each z-scored against **that channel's rolling baseline**, then combined with the weights in `config.yaml → scoring.weights`. Spikes that overlap are merged before anything is sent.
- **window cutter → Clips Kitty:** concatenates the segments for `[spike − lead_in, spike + tail]` (about 2–4 min, so the clip gets 30–60 s of lead-up) into an mp4. It then calls `POST /videos/local` with the gaming options, `channel=<streamer>`, a small `max_clips`, a GTA-specific `focus` text (ragdolls, police chases, NPCs, glitches, rage, stunts, first reactions, one-liners), and a `webhook_url` pointing at our local listener. Clips Kitty runs **one job at a time**, which is why our windows are short and the queue is prioritized.
- **rater/understander plugin (Phase 2, optional):** a local Clips Kitty plugin (`plugins/gta6-signals/`, with `inputs: [moments, transcript]` and `outputs: [ratings, context]`) that feeds our chat and audio scores into Clips Kitty's ranking. Clips Kitty plugins are **not sandboxed**, so it must declare `network: []` and `sends: []` honestly.
- **post-processor:** on the webhook, reads `GET /videos/{id}/clips` and the captions, runs the spoiler check (Claude, through `gta6clips/llm/`), writes 3 title options, and sets the description (credit line + link) and hashtags (`#GTA6 #GTAVI #Shorts #<streamer>`) through `PATCH /clips/{id}`.
- **review:** our FastAPI page on `127.0.0.1` (not Clips Kitty's port, 8765). It previews clips through Clips Kitty's `/media/{clip_id}`, lets the owner approve, reject or pick and edit a title, and has a Spoilers tab. Fine edits happen in the Clips Kitty studio.
- **publish:** `POST /clips/{id}/publish` with an explicit private or `publish_at`. Clips Kitty's `quota` counts uploads (since June 2026, `videos.insert` has its own bucket of 100 per day per project). We warn at `youtube.quota_warn_at` and stop at the limit, but a 403 from YouTube is the real answer.

## Clips Kitty API notes

- `http://127.0.0.1:8765`, with **no authentication**. Address it only as `127.0.0.1`/`localhost` (other `Host` headers get a 400), and never expose it.
- `POST /jobs` and `/videos/local` return `{"job_id": null, "already_processed": true}` for repeats. Check for a null `job_id` instead of assuming a job was created. A 409 means the queue is full.
- Use only the endpoints that `docs/API.md` (in the clips-studio repo) marks as supported. Record the Clips Kitty version we tested against in `config.yaml → clipskitty.tested_version`, and log a warning if `GET /health` reports another version.

## Configuration files

- `config.yaml`: poll interval, concurrency, retention, scoring weights and thresholds, keywords, window lead-in and tail, Clips Kitty URL and job options, publish defaults and quota thresholds.
- `streamers.yaml`: owner-edited. Each entry has `name, platform, channel_url, permission, credit, priority`. The layout override is set per creator inside Clips Kitty's Gaming layout editor.
- `.env`: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `ANTHROPIC_API_KEY` (for our spoiler and title calls). Clips Kitty keeps its own keys.

## Conventions

- Python 3.11+, a `.venv` at the repo root, dependencies in `requirements.txt`. No Whisper, torch or MediaPipe on our side.
- Log to `logs/orchestrator.log` (rotating) and the console, using `logging.getLogger(__name__)`.
- Tests use pytest. Media fixtures are generated with ffmpeg (`testsrc` + `sine`). Helix, Claude and Clips Kitty are faked, so tests need no network.
- Commit at the end of each phase.

## Commands

To be filled in as Phase 1 lands.

## Reference repos reviewed (Nov 2026)

- `ColinGPT9/clips-studio` (Clips Kitty): AGPL-3.0, active. It's the clip engine (see above). Docs worth rereading: `docs/API.md`, `docs/developers/steps.md`, `docs/developers/security.md`, `docs/GAMING.md`.
- `mutonby/openshorts`: MIT, except `cloud/`, which is under a commercial license. Its `twitch_signals.py` (Helix clip heat) is worth learning from for the spike detector.
- `xenova/chat-downloader`: MIT. Its last commit is from Sep 2023 and it has no Kick support. Use it behind a chat adapter so it can be swapped out.
- `Anil-matcha/AI-Youtube-Shorts-Generator`: superseded by Clips Kitty. `maximehip/MXClip`: unreachable.
