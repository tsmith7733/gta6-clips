# gta6-clips

Turns GTA 6 streams from creators who've allowed clipping into YouTube Shorts.
**Clips Kitty** (a free desktop app) does the editing. This project decides
what gets clipped, credits the streamer, and makes sure nothing is published
without your approval.

| Phase | What it does | Status |
|---|---|---|
| 1 | One recorded stream → Clips Kitty → credited, titled clips waiting for review | ✅ built |
| 2 | Chat and audio spike scoring, spoiler filter | ✅ done, tested on a 19 h GTA 6 stream |
| 3 | Watch live streams and record several at once | ✅ done (YouTube), tested on a live GTA 6 stream |
| 4 | Review page | ✅ built |
| 5 | Publish approved clips, scheduled at peak times | ✅ built, waiting for YouTube to be connected in Clips Kitty |

## The rules it enforces

- **Only streamers marked `permission: true` in `streamers.yaml` are touched.** Anyone else is refused before anything is downloaded or sent to Clips Kitty.
- **Every description carries the streamer's credit line and channel link.**
- **Nothing is published without your approval.** It also refuses to run if Clips Kitty's own automation or auto-upload is switched on.

## Setup on Windows

You need four things on your PC:
- **Clips Kitty 2.1.0 or newer.** The Microsoft Store version is fine.
- **The GTA plugin installed in Clips Kitty** (see the next section).
- **Python 3.11 or newer.** Check with `py --version` in PowerShell. If it's missing, install it from python.org or with `winget install Python.Python.3.12`.
- *(Optional)* **A Claude API key** from https://console.anthropic.com, only if you turn on Claude-written titles (see step 3). It's pay-as-you-go, about 2-4 cents a clip.

### 1. Get the code

Either install Git (`winget install Git.Git`) and run:

```powershell
cd $env:USERPROFILE
git clone https://github.com/tsmith7733/gta6-clips
cd gta6-clips
```

or, on GitHub, click **Code → Download ZIP** and unzip it to `C:\Users\<you>\gta6-clips`.

### 2. Create the Python environment

Run this from the `gta6-clips` folder. It installs packages only inside this folder, in `.venv`:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The commands below call `.venv\Scripts\python.exe` directly, so you don't need to "activate" anything or change PowerShell's script policy.

### 3. (Optional) Claude-written titles

By default, clips keep the title Clips Kitty writes, which is free. To have Claude write 3 title options and an on-screen hook for each clip instead:

```powershell
copy .env.example .env
notepad .env
```

Put your key after `ANTHROPIC_API_KEY=` and save (`.env` is never committed to git). Then set `enabled: true` under `llm:` in `config.yaml`.

### 4. Check Clips Kitty's settings

In Clips Kitty:
- **Settings → AI:** your AI provider (for example, Claude with your key).
- **Watch page:** automation **off**.
- **Settings → uploads:** auto upload **off**. Set the default privacy to **Private** if you can.

Then, with Clips Kitty open:

```powershell
.venv\Scripts\python.exe -m gta6clips check
```

It lists your streamers and reports anything that would stop a run.

## Install the GTA plugin

Clips Kitty 2.1.0 doesn't list the official GTA plugin in its Marketplace, and it rejects one permission the plugin asks for (`twitch.context.read`, which only newer Clips Kitty versions know). The plugin only uses that permission for optional Twitch details, so we install a copy with that one line removed. It's pinned to the version that was reviewed (0.4.1, commit `f1176c6`).

With Clips Kitty open, paste this into PowerShell. It prepares the plugin and shows what would be installed:

```powershell
$commit = "f1176c632acec146be70b60568e7c7b88abe286d"
$dest = "$env:USERPROFILE\ClipsKittyPlugins"
New-Item -ItemType Directory -Force $dest | Out-Null
Invoke-WebRequest "https://github.com/ColinGPT9/Grand-Theft-Auto-6-Clips-Kitty-Plugin/archive/$commit.zip" -OutFile "$dest\gta-plugin.zip" -UseBasicParsing
Expand-Archive "$dest\gta-plugin.zip" -DestinationPath $dest -Force
$folder = "$dest\Grand-Theft-Auto-6-Clips-Kitty-Plugin-$commit"
$manifest = "$folder\clipskitty.yaml"
[IO.File]::WriteAllText($manifest, (Get-Content $manifest -Raw).Replace(", twitch.context.read]", "]"))
$secretFile = Get-ChildItem "$env:LOCALAPPDATA\Packages\ClipsStudio.ClipsStudio_*\LocalCache\Local\Clips Studio\data\plugins\session.secret", "$env:LOCALAPPDATA\Clips Studio\data\plugins\session.secret" -ErrorAction SilentlyContinue | Select-Object -First 1
$h = @{ "X-Clips-Kitty-Session" = (Get-Content $secretFile.FullName -Raw).Trim() }
$body = @{ source = @{ kind = "folder"; path = $folder } } | ConvertTo-Json -Depth 3
$plan = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8765/plugins/plan" -Headers $h -ContentType "application/json" -Body $body
"ok: $($plan.ok)"; $plan.errors; $plan.warnings
```

If it prints `ok: True`, install it:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8765/plugins/install" -Headers $h -ContentType "application/json" -Body (@{ plan_id = $plan.plan_id } | ConvertTo-Json)
```

## Clip a recorded stream (Phase 1)

With Clips Kitty open:

```powershell
.venv\Scripts\python.exe -m gta6clips vod "https://www.youtube.com/watch?v=VIDEO_ID"
```

It does five things:
1. reads the video's channel and checks it against `streamers.yaml`
2. checks Clips Kitty is ready
3. sends the video to Clips Kitty in Gaming mode with the GTA plugin
4. waits for Clips Kitty to finish (a 2-hour stream can take 30-60 minutes)
5. adds the credit line and hashtags to each clip (plus 3 Claude title options and a hook, if you turned that on), and saves each clip as **pending review**

If you'd rather not keep the window open, add `--no-wait`, then collect the clips later with the job number it prints:

```powershell
.venv\Scripts\python.exe -m gta6clips wait JOB_ID
```

To see clips waiting for review:

```powershell
.venv\Scripts\python.exe -m gta6clips clips
```

Watch and fine-tune the clips in Clips Kitty's studio. Nothing is uploaded.

## Clip only the best moments (Phase 2)

Instead of sending a whole stream, `scan` finds the moments where chat blew up (lots of messages, "CLIP IT", "KEKW", "?????") or the streamer got loud. It sends just those few minutes to Clips Kitty, which is much faster.

It needs **ffmpeg**. Install it once:

```powershell
winget install Gyan.FFmpeg
```

Then open a new PowerShell window. Or download a portable ffmpeg and put the path to its `ffmpeg.exe` under `paths: ffmpeg:` in `config.yaml`. `check` tells you whether it's found.

First, see what it would pick without sending anything:

```powershell
.venv\Scripts\python.exe -m gta6clips scan "https://www.youtube.com/watch?v=VIDEO_ID" --dry-run
```

It downloads the chat replay and the audio (the audio is deleted after it's measured) and lists the moments with their times. If that looks right, run it again without `--dry-run`. It downloads just those minutes of video, sends them to Clips Kitty, and saves the clips for review. Running the same command again never sends a moment twice: it picks up anything left over, such as a full queue or a failed download.

Too many or too few moments? Change `threshold` under `scoring:` in `config.yaml` (lower sends more). YouTube streams use chat and audio. Twitch and Kick streams use audio only for now.

**Spoilers:** a clip whose stream title, clip title or speech mentions things like "ending", "final mission" or "spoilers", or with several spoiler messages in chat around it, is held as a possible spoiler. It stays out of the main list:

```powershell
.venv\Scripts\python.exe -m gta6clips clips --status spoiler_hold
```

The words are under `spoilers:` in `config.yaml`. This applies to `vod` too.

## Clip streams while they're live (Phase 3)

`watch` keeps an eye on every permitted YouTube streamer. When one goes live with GTA 6 in the stream title, it records the stream in 1-minute pieces, reads the live chat, and scores it the same way `scan` does. A few minutes after a big moment it cuts that part out of the recording and sends it to Clips Kitty. The clips land in your review list like the others. Nothing is published.

```powershell
.venv\Scripts\python.exe -m gta6clips watch
```

Leave it running, with Clips Kitty open and the PC awake (set Windows sleep to Never while it runs). Ctrl+C stops it cleanly. Anything still in Clips Kitty is collected the next time you start `watch`. To try it for a short while, add `--minutes 30`.

**It's pickier than `scan`.** A VOD scan sees the whole stream before choosing; live it has to decide as it goes, so a moment is sent only when:

- its score is over `threshold` under `live: selection:` in `config.yaml` (4 by default),
- at least two signals agree (chat speed, hype words, loudness), so a raid saying hi or a loud game sound on its own isn't enough,
- it's the strongest moment in its 8 minutes, and
- no more than 3 windows went out that hour, unless it beats all of them.

After 45 minutes with nothing sent, the bar drops to 3.5, so a quieter stream still gets its best moments. His live chat is busy all the time, so a big laugh scores about 4–4.5 live; replayed minute by minute on the 19-hour GTA 6 stream, these settings sent about 30 windows, including 11 of its 12 strongest moments.

**Funny moments.** The GTA plugin only spots physical events (chases, crashes) in what's said, so it finds nothing in a moment that's funny because of a line he says. When that happens the window goes once more to Clips Kitty's own moment finder, which follows the "funny first" `focus` text in `config.yaml` and runs on Clips Kitty's AI setting (local Ollama here, so free). Turn it off with `fallback_to_clipskitty_finder: false`.

**GPU memory.** Clips Kitty's Ollama model, its Whisper transcription and its rendering share the graphics card. With gemma3:27b loaded (about 17 GB), transcription falls back to the CPU (weak captions) and after a few jobs renders fail with "CUDA error" (0 clips) until Clips Kitty is restarted. A smaller model (Settings → AI, for example gemma3:12b) avoids it. When a render does fail, `watch` sends that window again by itself after 5 minutes (up to 2 times, `windows.render_retries`), under a new file name so it's transcribed afresh; if it still fails you get a Discord message to restart Clips Kitty.

**Disk space:** recordings take about 4 GB an hour at 1080p. Pieces older than 2 hours are deleted (`retention_hours`). The windows sent to Clips Kitty are kept in `data\windows`.

**Other games:** a stream whose title doesn't mention GTA 6 isn't recorded. `--allow-non-gta` records it anyway (handy for a test). If he switches games mid-stream, the recording stops after two checks in a row (about 5 minutes).

## Review clips (Phase 4)

```powershell
.venv\Scripts\python.exe -m gta6clips review
```

Opens a page in your browser at `http://127.0.0.1:8770` (change the port with `review: port:` in `config.yaml`). It only answers on this PC.

- **To review**: watch each clip, pick one of its titles or write your own (up to 100 characters), then **Approve** or **Reject**. Captions and the description (with the credit line) are under each clip.
- **Spoilers**: clips held as possible spoilers. To approve one, tick **Not a spoiler** first. Undoing a decision on a spoiler puts it back here, never in the main list.
- **Approved** and **Rejected**: change a title, or undo the decision.

Approving writes the chosen title to Clips Kitty and checks that the clip's description there still has the streamer's credit; it refuses if the credit was removed. With `publish.auto_schedule: true` (the default), approving also uploads the clip at once into the next free time slot, private until then; if it can't be scheduled (YouTube off, quota, a failed check) the clip stays approved and the page says why. Otherwise approved clips are scheduled from the Approved tab (Phase 5, below).

Fine edits (framing, captions, trimming) happen in the Clips Kitty studio. When you re-render a clip there, Clips Kitty gives it a new number; the page notices and follows it, keeping your decision. The list refreshes itself every 30 seconds while `watch` adds clips.

## Publish on a schedule (Phase 5)

Approved clips go to YouTube through Clips Kitty, each **scheduled for a time slot**: it uploads right away as **private**, and YouTube makes it public at the slot's time. Nothing is ever uploaded public, and only approved clips are uploaded.

On the review page's **Approved** tab, press **Schedule approved clips**. You see the plan first (each clip, its title and its slot) and nothing uploads until you press **Upload**. Best-scoring clips get the earliest slots, one clip per slot. Or from PowerShell:

```powershell
.venv\Scripts\python.exe -m gta6clips publish          # show the plan
.venv\Scripts\python.exe -m gta6clips publish --yes    # upload it
```

**Slots** are in `config.yaml` under `publish:` (US Central by default): weekdays 3 PM and 8 PM, weekends noon, 3 PM and 8 PM. Gaming Shorts do best mid-afternoon (after school and work) and in the evening, weekends strongest. These are starting points: once the channel has views, YouTube Studio → Analytics → Audience → "When your viewers are on YouTube" shows the real peaks, so move the slots there.

Before each upload it re-checks that the streamer is permitted, the clip is approved and its description still has the credit line. Afterwards it compares what YouTube did with what was asked; the clip moves to **Published** with its link. It warns at 80 of YouTube's 100 uploads a day and stops at the limit.

### Connect YouTube in Clips Kitty (once)

Clips Kitty uploads with your own Google Cloud project, so nothing goes through anyone else's server.

1. Go to <https://console.cloud.google.com>, sign in with the Google account that owns your YouTube channel, and create a project (any name).
2. **APIs & Services → Library**: find **YouTube Data API v3** and press **Enable**.
3. **Google Auth Platform** (OAuth consent screen): choose **External**, give it a name and your email. Under **Audience**, add your own Google account as a **test user**.
4. **Clients → Create client → Desktop app**. Keep the client ID and secret (or download the JSON).
5. In Clips Kitty: **Settings → Publish to YouTube**, turn it on, add the client, press **Connect** and sign in with your channel's account. Then set its **default privacy to Private** (a safety net; this project always sends private anyway).

While the Google project's consent screen is in **Testing**, Google makes you reconnect about once a week; pressing **Publish app** on that screen stops that (Google shows an "unverified app" warning, which is fine for your own app).

**Important: YouTube's API audit.** YouTube has locked uploads from Google Cloud projects that haven't passed its API audit to private, permanently, so a scheduled clip would never go public. If that happens, the first upload shows **locked to private** and scheduling stops. The fix is YouTube's audit (free; Google Cloud console → YouTube API Services → audit and quota extension form), or uploading by hand from Clips Kitty. Start with one clip to find out.

## Phone notifications (Discord)

The program can ping you on Discord when a clip is ready for review (or held as a spoiler), when a stream starts or stops recording, when an upload goes through (with its YouTube link and time), and when something goes wrong (recording failing, a YouTube block, a failed render; the same problem at most every 30 minutes).

1. In Discord, pick a channel in your own server: **Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL**.
2. Add it to `.env`: `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...` (treat it like a password).
3. Test it: `.venv\Scripts\python.exe -m gta6clips notify-test`, then restart `watch` and `review`.

Turn each kind on or off under `notify:` in `config.yaml`. Only titles and short status lines are sent, and nobody is @-mentioned. Approving still happens on the review page on your PC.

## Streamers

Edit `streamers.yaml`. Each entry needs:

| Field | What it's for |
|---|---|
| `name` | Shown in logs, and used for the streamer's hashtag |
| `platform` | `youtube`, `twitch` or `kick` |
| `channel_url` | The channel's link, e.g. `https://www.youtube.com/@TheBurntPeanut` |
| `permission` | `true` only once the creator allows clipping. Write it without quotes. |
| `permission_source` | Where that permission came from, for your records |
| `credit` | The line added to every description |
| `priority` | 1 is highest. Decides which streams get recorded first when several are live. |

## Twitch API credentials (needed from Phase 3, for Twitch streamers)

1. Go to https://dev.twitch.tv/console and log in with your Twitch account. Twitch requires two-factor authentication to be on.
2. Click **Register Your Application**.
3. Fill in a name (e.g. `gta6-clips-yourname`), the OAuth redirect URL `http://localhost`, category **Application Integration**, and client type **Confidential**.
4. Open the app under **Manage**. Copy the **Client ID**, click **New Secret**, and copy the secret.
5. Put both in `.env` as `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`.

## Logs and data

- `logs\orchestrator.log` has the full detail of every run. Look here first when something goes wrong.
- `data\orchestrator.db` is this project's own record of what was sent and each clip's status. Clips Kitty keeps its own library separately.

## Development

```powershell
.venv\Scripts\python.exe -m pytest              # all tests
.venv\Scripts\python.exe -m pytest tests\test_streamers.py -k permission   # one group
```

The tests use a fake Clips Kitty and a fake Claude, so they need no network, no API key and no Clips Kitty.
