# gta6-clips

Turns GTA 6 streams from creators who've allowed clipping into YouTube Shorts.
**Clips Kitty** (a free desktop app) does the editing. This project decides
what gets clipped, credits the streamer, and makes sure nothing is published
without your approval.

| Phase | What it does | Status |
|---|---|---|
| 1 | One recorded stream → Clips Kitty → credited, titled clips waiting for review | ✅ built |
| 2 | Chat and audio spike scoring, spoiler filter | next |
| 3 | Watch live streams and record several at once | |
| 4 | Review page | |
| 5 | Publish approved clips (private or scheduled) | |

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
