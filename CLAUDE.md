# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A local pipeline that watches permitted streamers playing GTA 6 (Grand Theft Auto VI), spots viral moments while they're live, and hands those moments to **Clips Kitty** (`ColinGPT9/clips-studio`, a desktop app the owner installs separately) to be cut into vertical YouTube Shorts. A human approves every clip before it is published.

**This repo is the orchestrator around Clips Kitty, not a clip editor.** Clips Kitty does the transcription, choosing the exact clip boundaries, the gaming layout (webcam + game), captions, titles/descriptions/hashtags, rendering and the YouTube upload. This repo does what Clips Kitty can't: the permission gate, live GTA 6 category watching, rolling recording, per-channel chat and audio spike detection, the spoiler queue, mandatory credit lines, and the approve-before-publish rule.

**Status:** Phase 1 built and tested against a fake Clips Kitty (`python -m gta6clips vod <url>`). Waiting on the owner's first real run on their PC with the test VOD `https://www.youtube.com/watch?v=CW_rULxbDs0` (The Burnt Peanut). Next: Phase 2. Update this section at the end of each phase.

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
- **Moment finding uses the official GTA plugin** `colingpt9/gta-vi-highlights` (MIT, local, `network: []`, `sends: []`), which the owner installs from Clips Kitty's Marketplace. Every job sends `pipeline: {"id": "colingpt9/gta-vi-highlights", "version": <config.yaml → clipskitty.gta_plugin_version>}`, pinned at `0.4.1` (commit `f1176c6`), and it needs Clips Kitty 2.1.0 or later. Before raising the pin, re-review the plugin's diff, because plugins aren't sandboxed. If the plugin isn't installed, Clips Kitty refuses the job with a message naming the plugin. Show that to the owner rather than silently dropping `pipeline`. Its author says it is **not validated on GTA VI gameplay** and only labels physical events (chases, crashes) from transcript cues, so our chat and audio spikes stay the main reason a window is sent.
- **Our own rater plugin (Phase 2, optional):** a local plugin (`plugins/gta6-signals/`, with `inputs: [moments, transcript]` and `outputs: [ratings, context]`) that sends our chat and audio scores through `rate`, on top of the GTA plugin's finds. It must declare `network: []` and `sends: []` honestly.
- **post-processor:** on the webhook, reads `GET /videos/{id}/clips` and the captions, runs the spoiler check (Claude, through `gta6clips/llm/`), writes 3 title options, and sets the description (credit line + link) and hashtags (`#GTA6 #GTAVI #Shorts #<streamer>`) through `PATCH /clips/{id}`.
- **review:** our FastAPI page on `127.0.0.1` (not Clips Kitty's port, 8765). It previews clips through Clips Kitty's `/media/{clip_id}`, lets the owner approve, reject or pick and edit a title, and has a Spoilers tab. Fine edits happen in the Clips Kitty studio.
- **publish:** `POST /clips/{id}/publish` with an explicit private or `publish_at`. Clips Kitty's `quota` counts uploads (since June 2026, `videos.insert` has its own bucket of 100 per day per project). We warn at `youtube.quota_warn_at` and stop at the limit, but a 403 from YouTube is the real answer.

## Clips Kitty API notes

- The owner has the **Microsoft Store** build. Its data folder is `%LOCALAPPDATA%\Packages\ClipsStudio.ClipsStudio_<id>\LocalCache\Local\Clips Studio\data`, not the winget path `%LOCALAPPDATA%\Clips Studio\data`. Find it with a glob on `ClipsStudio.ClipsStudio_*`. Plugin routes need the `X-Clips-Kitty-Session` header, read from `<data>\plugins\session.secret`.
- Clips Kitty 2.1.0 doesn't bundle the official GTA plugin in its catalog (it was listed after the release), and the online list never adds the project's own listings. Until a newer release bundles it, install it by Git address through `POST /plugins/plan` + `/plugins/install`.
- **2.1.0 also rejects the plugin's `twitch.context.read` permission** ("unknown permission"). Every plugin version asks for it, but only unreleased Clips Kitty knows it. Workaround in use: a local copy of plugin commit `f1176c6` in `%USERPROFILE%\ClipsKittyPlugins\`, with only `twitch.context.read` removed from `clipskitty.yaml`, installed with `{"kind": "folder"}`. The plugin's Twitch use is optional (report metadata, read through `getattr`), and 2.1.0 ships SDK 1.3.0, which is what the plugin needs. Its ID and version are unchanged, so the job's `pipeline` ref still matches. Once a Clips Kitty release knows the permission, reinstall the unmodified plugin from Git.
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

Run from the repo root. On the owner's Windows PC, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest                                    # all tests (no network, no Clips Kitty needed)
.venv/bin/python -m pytest tests/test_vod.py -k permission    # a single test
.venv/bin/python -m gta6clips check                           # streamers, API key, Clips Kitty readiness
.venv/bin/python -m gta6clips vod <url> [--no-wait] [--force] # Phase 1: one VOD end to end
.venv/bin/python -m gta6clips wait <job_id>                   # collect a --no-wait job
.venv/bin/python -m gta6clips clips [--status all]            # list clips for review
```

The cloud dev container can't reach YouTube (the proxy blocks it) or Clips Kitty, so real runs only happen on the owner's PC. Exit codes: 0 ok, 1 runtime problem, 2 setup problem (config or missing key), 3 permission refused.

## Code map

- `gta6clips/streamers.py`: streamers.yaml loading and **the permission gate** (`require_permission`, `permitted_streamer_for`). `permission` must be a real YAML boolean.
- `gta6clips/clipskitty/client.py`: the only Clips Kitty HTTP code. `readiness.py` refuses to start when automation or auto-upload is on, a blocking preflight check fails, or the GTA plugin is missing, off, or not at the pinned version.
- `gta6clips/vod.py`: Phase 1 flow (probe → gate → readiness → `POST /jobs` with `gaming`, the pinned `pipeline`, `focus` and required `hashtags` → poll → post-process). `resume_job` collects `--no-wait` jobs, but only ones in our `windows` table.
- `gta6clips/postprocess.py`: credit block (idempotent, re-checked with `has_credit`), hashtag merge, Claude titles, `PATCH /clips/{id}`, and the `clips` row as `pending_review`. Clips already tracked are skipped (no repeat Claude calls). Approved, rejected and published clips are never touched.
- `gta6clips/llm/titles.py`: Claude (`claude-opus-5-5`, effort from config) with JSON-schema structured output and `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). All prompts live in `gta6clips/llm/`.
- `tests/fake_clipskitty.py`: the 2.1.0 API stand-in. Extend it whenever the client uses a new endpoint, copying shapes from the 2.1.0 `docs/API.md`, not from `main`.

## Reference repos reviewed (Nov 2026)

- `ColinGPT9/clips-studio` (Clips Kitty): AGPL-3.0, active. It's the clip engine (see above). Docs worth rereading: `docs/API.md`, `docs/developers/steps.md`, `docs/developers/security.md`, `docs/GAMING.md`.
- `ColinGPT9/Grand-Theft-Auto-6-Clips-Kitty-Plugin` (`colingpt9/gta-vi-highlights`): MIT. Reviewed at 0.4.1: its permissions are `video.read`, `transcript.read`, `ffmpeg`, `ollama` and `twitch.context.read`; it has no network, subprocess or eval calls in `src/`; and its runtime imports are the standard library plus the Clips Kitty SDK. Its dialogue fallback uses a local Ollama model only, and without one it falls back to its rules.
- `mutonby/openshorts`: MIT, except `cloud/`, which is under a commercial license. Its `twitch_signals.py` (Helix clip heat) is worth learning from for the spike detector.
- `xenova/chat-downloader`: MIT. Its last commit is from Sep 2023 and it has no Kick support. Use it behind a chat adapter so it can be swapped out.
- `Anil-matcha/AI-Youtube-Shorts-Generator`: superseded by Clips Kitty. `maximehip/MXClip`: unreachable.
