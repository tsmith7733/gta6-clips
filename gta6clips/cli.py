"""Command line: python -m gta6clips <command>."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import db
from .clipskitty import ClipsKittyClient, ClipsKittyError
from .clipskitty.readiness import check_readiness
from .config import ConfigError, env, load_config
from .downloads import DownloadError, YtDlpFetcher
from .llm import ClaudeTitleWriter, ClipsKittyTitles, TitleWriter, TitleWriterError
from .logging_setup import setup_logging
from .media import MediaToolMissing, find_ffmpeg
from .scan import ScanError, ScanResult, run_scan
from .scoring import clock
from .sources import SourceError
from .streamers import PermissionDenied, StreamerFileError, load_streamers
from .vod import JobFailed, NotGta6, NotReady, VodResult, resume_job, run_vod

log = logging.getLogger("gta6clips")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m gta6clips",
                                description="Turn permitted GTA 6 streams into Shorts through Clips Kitty.")
    p.add_argument("--config", help="path to config.yaml (default: the one in the repo)")
    p.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="check config, streamers.yaml and that Clips Kitty is ready")

    vod = sub.add_parser("vod", help="clip one recorded stream (Phase 1)")
    vod.add_argument("url", help="YouTube, Twitch or Kick video link")
    vod.add_argument("--no-wait", action="store_true",
                     help="queue it in Clips Kitty and return; collect later with 'wait JOB_ID'")
    vod.add_argument("--force", action="store_true", help="process again even if Clips Kitty already did")
    vod.add_argument("--allow-non-gta", action="store_true",
                     help="send it even though the title doesn't mention GTA 6")

    scan = sub.add_parser("scan", help="score a recorded stream on chat and audio, clip only its best "
                                       "moments (Phase 2)")
    scan.add_argument("url", help="YouTube video link (Twitch/Kick: audio only for now)")
    scan.add_argument("--dry-run", action="store_true",
                      help="score it and list the windows, but don't cut or send anything")
    scan.add_argument("--no-wait", action="store_true",
                      help="send the windows and return; run the same command again to collect them")
    scan.add_argument("--max-windows", type=int, help="override windows.max_per_stream")
    scan.add_argument("--allow-non-gta", action="store_true",
                      help="scan it even though the title doesn't mention GTA 6")

    watch = sub.add_parser("watch", help="watch permitted streamers and clip their GTA 6 streams while they're "
                                         "live (Phase 3)")
    watch.add_argument("--minutes", type=float, help="stop after this many minutes (default: run until Ctrl+C)")
    watch.add_argument("--allow-non-gta", action="store_true",
                       help="record a permitted streamer even when the stream title doesn't mention GTA 6")

    tc = sub.add_parser("twitch-clips", help="queue the most-viewed GTA 6 Twitch clips of permitted streamers "
                                             "(the running watcher sends them to Clips Kitty)")
    tc.add_argument("--dry-run", action="store_true", help="list what would be queued; download nothing")

    comp = sub.add_parser("compile", help="check new clips for driving fails (Claude) and queue a compilation "
                                          "when there are enough (the running watcher sends it to Clips Kitty)")
    comp.add_argument("--dry-run", action="store_true",
                      help="show the fails found so far and what a compilation would hold; no Claude calls")
    comp.add_argument("--top", type=int, metavar="N",
                      help="build 'top N driving fails' Shorts from the best unused fails (clips and hunted streams)")
    comp.add_argument("--shorts", type=int, default=3, help="with --top: how many Shorts (default 3)")
    comp.add_argument("--refine", action="store_true",
                      help="look again at fails found before Claude named the crash's second, to cut around it")
    comp.add_argument("--unbuild", type=int, action="append", metavar="ID",
                      help="take compilation ID back (its clip is rejected, its fails are free again)")
    comp.add_argument("--drop", metavar="CLIP",
                      help="leave this fail out (a clip id, or v<number> for one found by hunt): its compilation "
                           "is rejected and built again without it")

    hunt = sub.add_parser("hunt", help="find driving fails in finished GTA 6 streams (chat + Claude) and build a "
                                       "best-of compilation from them and the clips' fails")
    hunt.add_argument("urls", nargs="*", help="stream links (default: the GTA 6 streams of compilations.streamers)")
    hunt.add_argument("--no-build", action="store_true", help="only find the fails; don't build a compilation")

    ed = sub.add_parser("edit", help="edit a clip Clips Kitty rendered: punch-in zooms, replays, text (the editor's "
                                     "engine; nothing is uploaded)")
    ed.add_argument("clip", type=int, help="the Clips Kitty clip id")
    ed.add_argument("--plan", help="render this edit plan (JSON) instead of asking Claude")
    ed.add_argument("--sample", action="store_true", help="a free sample plan from the loud moments, no Claude")
    ed.add_argument("--note", default="", help="a note for Claude about this clip (also kept for later edits)")
    ed.add_argument("--out", help="where to write the edited video (default: data/edits/clip<id>_<n>.mp4)")

    review = sub.add_parser("review", help="open the review page: watch clips, pick titles, approve or reject "
                                           "(Phase 4)")
    review.add_argument("--port", type=int, help="override review.port in config.yaml")
    review.add_argument("--no-browser", action="store_true", help="don't open the page in the browser")

    pub = sub.add_parser("publish", help="schedule approved clips on YouTube through Clips Kitty (Phase 5)")
    pub.add_argument("--yes", action="store_true", help="upload the plan (without it, only show the plan)")
    pub.add_argument("--clip", type=int, action="append", help="only this clip (repeatable)")

    sub.add_parser("install-plugin", help="install or update our exact-moments plugin (plugins/exact-moments) "
                                          "in the Clips Kitty running on this PC")

    cy = sub.add_parser("connect-youtube", help="sign in once to a YouTube channel listed in config.yaml "
                                                "publish.channels (a second channel, besides Clips Kitty's)")
    cy.add_argument("channel", help="its key in publish.channels, e.g. cutscene_skipped")

    sub.add_parser("notify-test", help="send a test notification to Discord (DISCORD_WEBHOOK_URL in .env)")

    wait = sub.add_parser("wait", help="wait for a queued Clips Kitty job, then title and credit its clips")
    wait.add_argument("job_id", type=int)

    clips = sub.add_parser("clips", help="list clips waiting for review")
    clips.add_argument("--status", default="pending_review",
                       help="pending_review, spoiler_hold, approved, rejected, published, or all")
    return p


def claude_titles_on(cfg) -> bool:
    return cfg.get("llm.enabled", False) is True


def make_title_writer(cfg) -> TitleWriter | None:
    """Clips Kitty's own titles unless llm.enabled is true. None means: on, but no API key."""
    if not claude_titles_on(cfg):
        return ClipsKittyTitles(suffix=str(cfg.get("metadata.title_suffix", "") or ""))
    if not env("ANTHROPIC_API_KEY"):
        return None
    return ClaudeTitleWriter(model=cfg.get("llm.model", "claude-opus-5-5"),
                             effort=cfg.get("llm.effort", "medium"))


def _print_result(result: VodResult) -> None:
    if not result.waited:
        print(f"\nQueued in Clips Kitty as job {result.job_id}. When it's done, run:\n"
              f"  python -m gta6clips wait {result.job_id}")
        return
    made = [c for c in result.clips if not c.skipped]
    print(f"\n{result.streamer}: {len(made)} new clip(s) ready for review "
          f"({len(result.clips) - len(made)} already tracked).")
    for c in made:
        flag = f"   ! {'; '.join(c.notes)}" if c.notes else ""
        print(f"  [{c.ck_clip_id}] {c.title}{flag}")
    held = sum(c.status == "spoiler_hold" for c in made)
    if held:
        print(f"{held} of them held as possible spoilers: python -m gta6clips clips --status spoiler_hold")
    print("\nNothing is published. Review them with: python -m gta6clips clips")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config problem: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.path("paths.logs"), verbose=args.verbose)
    from . import notify
    notifications = notify.setup(cfg)
    if args.command == "notify-test":
        if not notifications.on:
            print("No Discord webhook: put DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... in .env",
                  file=sys.stderr)
            return 2
        ok = notifications.notifier.send("✅ GTA 6 clips: notifications are working. You'll hear about new clips "
                                         "here.")
        print("Sent. Check Discord." if ok else "Discord didn't accept it: check the webhook link in .env.")
        return 0 if ok else 1

    try:
        streamers = load_streamers(cfg.path("paths.streamers"))
        conn = db.connect(cfg.path("paths.database"))
        client = ClipsKittyClient(cfg.require("clipskitty.url"))

        if args.command == "install-plugin":
            return _install_plugin(cfg, client)
        if args.command == "connect-youtube":
            return _connect_youtube(cfg, args)
        if args.command == "check":
            return _check(cfg, client, streamers)
        if args.command == "clips":
            return _list_clips(conn, args.status)
        if args.command == "publish":
            return _publish(cfg, client, conn, streamers, args)
        if args.command == "compile":
            return _compile(cfg, client, conn, streamers, args)
        if args.command == "edit":
            return _edit(cfg, client, conn, args)
        if args.command == "hunt":
            return _hunt(cfg, client, conn, streamers, args)
        if args.command == "review":
            conn.close()
            return _review(cfg, client, streamers, args)

        writer = make_title_writer(cfg)
        if writer is None:
            print("Claude titles are on (llm.enabled: true in config.yaml) but ANTHROPIC_API_KEY isn't set.\n"
                  "Add your key to .env, or set llm.enabled: false to keep Clips Kitty's titles.",
                  file=sys.stderr)
            return 2
        if args.command == "scan":
            fetcher = YtDlpFetcher(ffmpeg=find_ffmpeg(cfg), cache_dir=cfg.path("paths.cache"),
                                   video_format=str(cfg.get("windows.video_format") or "b"))
            scanned = run_scan(args.url, cfg=cfg, client=client, conn=conn, streamers=streamers, writer=writer,
                               fetcher=fetcher, windows_dir=cfg.path("paths.windows"), dry_run=args.dry_run,
                               wait=not args.no_wait, allow_non_gta=args.allow_non_gta,
                               max_windows=args.max_windows)
            _print_scan(scanned)
            return 0
        if args.command == "watch":
            return _watch(cfg, client, conn, streamers, writer, args)
        if args.command == "twitch-clips":
            return _twitch_clips(cfg, conn, streamers, args)
        if args.command == "vod":
            result = run_vod(args.url, cfg=cfg, client=client, conn=conn, streamers=streamers,
                             writer=writer, wait=not args.no_wait, force=args.force,
                             allow_non_gta=args.allow_non_gta)
        else:
            result = resume_job(args.job_id, cfg=cfg, client=client, conn=conn, streamers=streamers,
                                writer=writer)
        _print_result(result)
        return 0
    except PermissionDenied as exc:
        log.error("%s", exc)
        return 3
    except MediaToolMissing as exc:
        log.error("%s", exc)
        return 2
    except (StreamerFileError, NotReady, NotGta6, JobFailed, SourceError, ClipsKittyError, TitleWriterError,
            ScanError, DownloadError) as exc:
        log.error("%s", exc)
        return 1


def _print_scan(result: ScanResult) -> None:
    chat = "no chat replay" if result.chat_messages is None else f"{result.chat_messages} chat messages"
    print(f"\n{result.streamer}: {result.title}\n  scored on {chat}, audio {'yes' if result.audio else 'no'}")
    if not result.windows:
        print("  No moment stood out enough to send. Lower scoring.threshold in config.yaml to send more.")
        return
    verb = "Would send" if result.planned_only else "Windows"
    print(f"  {verb}:")
    for w in result.windows:
        print(f"    {clock(w.start_s)}-{clock(w.end_s)}  [{w.state}]  {w.reasons}")
        if w.error:
            print(f"      ! {w.error.splitlines()[0]}")
        for c in w.clips:
            if c.skipped:
                continue
            flag = f"   ! {'; '.join(c.notes)}" if c.notes else ""
            print(f"      [{c.ck_clip_id}] ({c.status}) {c.title}{flag}")
    if result.queue_full:
        print("\nClips Kitty's queue filled up. Run the same command again later to send the rest.")
    if not result.waited:
        print("\nSent. Run the same command again to collect the clips once Clips Kitty is done.")
    clips = [c for w in result.windows for c in w.clips if not c.skipped]
    held = sum(c.status == "spoiler_hold" for c in clips)
    if clips:
        print(f"\n{len(clips) - held} clip(s) ready for review, {held} held as possible spoilers. "
              "Nothing is published.\nReview: python -m gta6clips clips   "
              "Spoilers: python -m gta6clips clips --status spoiler_hold")


def _check(cfg, client: ClipsKittyClient, streamers) -> int:
    print("Streamers:")
    for s in streamers:
        print(f"  {'OK  ' if s.permission else 'NO  '} {s.name} ({s.platform}) priority {s.priority}"
              f"{'' if s.permission else '  <- not permitted, will be skipped'}")
    if claude_titles_on(cfg):
        print(f"Claude titles: on, API key {'set' if env('ANTHROPIC_API_KEY') else 'MISSING (add it to .env)'}")
    else:
        print("Claude titles: off (clips keep Clips Kitty's titles; set llm.enabled: true to turn on)")
    try:
        print(f"ffmpeg: {find_ffmpeg(cfg)}")
    except MediaToolMissing as exc:
        print(f"ffmpeg: MISSING (needed by 'scan'). {exc}")
    try:
        readiness = check_readiness(client, cfg)
    except ClipsKittyError as exc:
        print(f"Clips Kitty: {exc}")
        return 1
    print(f"Clips Kitty {readiness.app_version}: {'ready' if readiness.ok else 'NOT ready'}")
    for w in readiness.warnings:
        print(f"  warning: {w}")
    for p in readiness.problems:
        print(f"  problem: {p}")
    return 0 if readiness.ok else 1


def _list_clips(conn, status: str) -> int:
    rows = db.clips_by_status(conn, None if status == "all" else status)
    if not rows:
        print(f"No clips with status {status}.")
        return 0
    for r in rows:
        options = json.loads(r["title_options"])
        notes = json.loads(r["notes"])
        print(f"[{r['ck_clip_id']}] {r['streamer']}  score {r['score']}  "
              f"{(r['end_s'] or 0) - (r['start_s'] or 0):.0f}s  ({r['status']})")
        for i, t in enumerate(options, 1):
            print(f"    {i}. {t}")
        if r["hook"]:
            print(f"    hook: {r['hook']}")
        for n in notes:
            print(f"    ! {n}")
    return 0


def _publish(cfg, client, conn, streamers, args) -> int:
    from .publish.service import PublishService

    pub = PublishService(conn=conn, client=client, streamers=streamers, cfg=cfg, is_live=_live_check(streamers),
                         uploaders=_uploaders(cfg))
    for change in pub.refresh(force=True):
        print(f"  clip {change['clip_id']}: upload {change['state']}")
    plan = pub.plan(args.clip)
    st = plan.status
    if st.enabled:
        print(f"YouTube: {st.channel or 'connected'}, {st.used}/{st.limit} uploads used today")
    for w in st.warnings:
        print(f"  warning: {w}")
    for p in st.problems:
        print(f"  problem: {p}")
    if not st.ok:
        return 1
    for s in plan.skipped:
        print(f"  skipped clip {s['clip_id']}: {s['reason']}")
    if not plan.items:
        print("No approved clips to schedule.")
        return 0
    print("Plan (each uploads private now; YouTube publishes it at its slot):")
    for i in plan.items:
        print(f"  {i.when}  [{i.clip_id}] {i.title}  → {i.channel}")
    if not args.yes:
        print("\nNothing uploaded. Run again with --yes to upload this plan.")
        return 0
    result = pub.execute([i.as_dict() for i in plan.items])
    for s in result["started"]:
        print(f"  uploading clip {s['clip_id']}, scheduled for {s['when']}")
    for s in result["skipped"]:
        print(f"  skipped clip {s['clip_id']}: {s['reason']}")
    return 0


def _review(cfg, client, streamers, args) -> int:
    import threading
    import webbrowser

    from .review.app import serve
    from .review.service import ReviewService

    port = args.port or int(cfg.get("review.port", 8770))
    if port == 8765:
        print("Port 8765 is Clips Kitty's. Pick another review.port.", file=sys.stderr)
        return 2
    from .publish.service import PublishService

    shared = db.connect(cfg.path("paths.database"), threads=True)
    service = ReviewService(conn=shared, client=client, streamers=streamers)
    publisher = PublishService(conn=shared, client=client, streamers=streamers, cfg=cfg,
                               is_live=_live_check(streamers), uploaders=_uploaders(cfg))
    from .editing import EditorWorker

    make = _editor_factory(cfg, client)
    worker = EditorWorker(make) if make is not None else None
    url = f"http://127.0.0.1:{port}/"
    print(f"Review page: {url}  (Ctrl+C stops it; nothing is published from it)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    serve(service, client, port=port, publisher=publisher, editor=worker,
          auto_schedule=cfg.get("publish.auto_schedule", False) is True)
    return 0


def _clip_source(cfg, conn, streamers, *, required: bool):
    from .twitch import Helix, TwitchSetupError
    from .twitch_clips import TwitchClipSource, twitch_streamers

    if not required and (cfg.get("twitch_clips.enabled", True) is False or not twitch_streamers(streamers)):
        return None
    try:
        helix = Helix.from_env()
    except TwitchSetupError as exc:
        if required:
            raise
        log.warning("Twitch clips are off: %s", exc)
        return None
    windows = cfg.get("windows", {}) or {}
    fetcher = YtDlpFetcher(ffmpeg=find_ffmpeg(cfg), cache_dir=cfg.path("paths.cache"),
                           video_format=str(windows.get("video_format", "b")))
    from .media import silent_stretch
    ffmpeg = find_ffmpeg(cfg)
    return TwitchClipSource(cfg=cfg, conn=conn, helix=helix, streamers=streamers, fetcher=fetcher,
                            windows_dir=cfg.path("paths.windows"),
                            silent=lambda path, s, e: silent_stretch(ffmpeg, path, s, e))


def _secrets_dir(cfg):
    from pathlib import Path
    p = Path(str(cfg.get("paths.secrets") or "secrets"))
    return p if p.is_absolute() else cfg.base_dir / p


def _uploaders(cfg):
    from .publish.youtube_direct import make_uploaders
    return make_uploaders(cfg, _secrets_dir(cfg))


def _install_plugin(cfg, client) -> int:
    import glob
    import os
    from pathlib import Path

    folder = (cfg.base_dir / "plugins" / "exact-moments").resolve()
    found = glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Packages") +
                      r"\ClipsStudio.ClipsStudio_*\LocalCache\Local\Clips Studio\data\plugins\session.secret")
    found += [os.path.expandvars(r"%LOCALAPPDATA%\Clips Studio\data\plugins\session.secret")]
    secret_file = next((Path(f) for f in found if Path(f).is_file()), None)
    if secret_file is None:
        print("Couldn't find Clips Kitty's session.secret. Is Clips Kitty open?", file=sys.stderr)
        return 2
    plan = client.plan_plugin(str(folder), secret_file.read_text(encoding="utf-8").strip())
    details = plan.get("details") or {}
    print("Clips Kitty's install screen:")
    for p in details.get("permissions") or []:
        print(f"  permission: {p.get('label')}")
    print(f"  network: {details.get('network') or 'none'}; steps: {', '.join(details.get('steps') or [])}")
    for w in (plan.get("warnings") or []) + (plan.get("problems") or []):
        print(f"  ! {w}")
    if plan.get("problems"):
        return 1
    installed = client.install_plugin(plan["plan_id"], secret_file.read_text(encoding="utf-8").strip())
    print(f"Installed {installed.get('id')} {installed.get('version')}.")
    return 0


def _connect_youtube(cfg, args) -> int:
    from .publish.youtube_direct import (OAuthClient, TokenStore, YouTubeError, channel_settings,
                                         connect)

    channels = channel_settings(cfg)
    if args.channel not in channels:
        print(f"No channel {args.channel!r} in config.yaml publish.channels (have: {', '.join(channels) or 'none'})",
              file=sys.stderr)
        return 2
    secrets_dir = _secrets_dir(cfg)
    try:
        oauth = OAuthClient.from_file(secrets_dir / "youtube_client.json")
        channel = connect(channels[args.channel], oauth, TokenStore(secrets_dir))
    except YouTubeError as exc:
        print(exc, file=sys.stderr)
        return 2 if "client file" in str(exc) else 1
    print(f"Connected to {channel['title']} ({channel['handle']}). Its sign-in is saved encrypted in "
          f"{secrets_dir}.")
    return 0


def _live_check(streamers):
    """Twitch live status for streamers marked hold_uploads_while_live (None without Twitch keys)."""
    if not any(s.hold_uploads_while_live for s in streamers):
        return None
    from .twitch import Helix, TwitchLiveCheck, TwitchSetupError
    try:
        return TwitchLiveCheck(Helix.from_env())
    except TwitchSetupError as exc:
        log.warning("Clips of streamers marked hold_uploads_while_live wait: %s", exc)
        return None


def _twitch_clips(cfg, conn, streamers, args) -> int:
    from .twitch import HelixError, TwitchSetupError

    try:
        source = _clip_source(cfg, conn, streamers, required=True)
        run = source.run_once(dry_run=args.dry_run)
    except TwitchSetupError as exc:
        print(exc, file=sys.stderr)
        return 2
    except HelixError as exc:
        print(f"Twitch: {exc}", file=sys.stderr)
        return 1
    for p in run.picks:
        how = "from the VOD" if p.from_vod else "clip only"
        print(f"  {p.views:>6} views  {p.streamer.name}: {p.clip.get('title')!r} ({how}, "
              f"{p.end_s - p.start_s:.0f} s)")
    for line in run.skipped:
        print(f"  skipped: {line}")
    if args.dry_run:
        print("\nDry run: nothing downloaded or queued.")
    elif run.queued:
        print(f"\nQueued {len(run.queued)} clip(s). The running watcher sends them to Clips Kitty "
              "(start it with 'watch' if it isn't running).")
    else:
        print("\nNothing new to queue.")
    return 0


def _held_publisher(cfg, client, conn, streamers):
    """With publish.auto_schedule, the watcher uploads clips held while their streamer was live."""
    if cfg.get("publish.auto_schedule", False) is not True or not any(s.hold_uploads_while_live for s in streamers):
        return None
    from .publish.service import PublishService
    return PublishService(conn=conn, client=client, streamers=streamers, cfg=cfg, is_live=_live_check(streamers),
                          uploaders=_uploaders(cfg))


def _watch(cfg, client, conn, streamers, writer, args) -> int:
    from .live.factory import find_ytdlp, session_factory
    from .live.watcher import Watcher

    from .media import silent_stretch
    ffmpeg = find_ffmpeg(cfg)
    make = session_factory(cfg, conn, ffmpeg=ffmpeg, ytdlp=find_ytdlp())
    compiler = _compiler(cfg, client, conn, streamers)
    watcher = Watcher(cfg=cfg, client=client, conn=conn, streamers=streamers, writer=writer, make_session=make,
                      allow_non_gta=args.allow_non_gta,
                      clip_source=_clip_source(cfg, conn, streamers, required=False),
                      publisher=_held_publisher(cfg, client, conn, streamers),
                      silent=lambda path, s, e: silent_stretch(ffmpeg, path, s, e),
                      compiler=compiler, compilation_writer=_compilation_writer(cfg) if compiler else None,
                      editor=_watch_editor(cfg, client))
    watcher.run(stop_after_s=args.minutes * 60 if args.minutes else None)
    print("\nStopped watching. Nothing is published. Review the clips with: python -m gta6clips clips")
    return 0


def _compiler(cfg, client, conn, streamers, *, required: bool = False):
    """The driving-fail compiler, or None when compilations are off or there's no Claude key."""
    from .compilation import CompilationSettings, Compiler, grab_frames, stitch
    from .llm.fails import ClaudeFailJudge

    settings = CompilationSettings.from_config(cfg)
    if not settings.enabled and not required:
        return None
    if not env("ANTHROPIC_API_KEY"):
        log.warning("Compilations are off: they need ANTHROPIC_API_KEY in .env.")
        return None
    ffmpeg = find_ffmpeg(cfg)
    return Compiler(cfg=cfg, conn=conn, client=client, streamers=streamers,
                    judge=ClaudeFailJudge(model=settings.judge_model, effort=settings.judge_effort),
                    frames=lambda path, s, e, n: grab_frames(ffmpeg, path, s, e, n),
                    cut=lambda parts, dest: stitch(ffmpeg, parts, dest), windows_dir=cfg.path("paths.windows"),
                    fetch=_hd_fetch(cfg, conn, streamers))


def _hd_fetch(cfg, conn, streamers):
    """Downloads a finished stream's fail in HD, a few seconds each side (one yt-dlp download each, spaced
    out so YouTube doesn't take the PC for a bot)."""
    import time

    from .compilation import HD_PAD_S
    from .crash_hunt import stream_source

    fetcher = YtDlpFetcher(ffmpeg=find_ffmpeg(cfg), cache_dir=cfg.path("paths.cache"),
                           video_format=str(cfg.get("windows.video_format") or "b"))

    def fetch(part, dest):
        info = stream_source(conn, part.source)
        streamer = next(s for s in streamers if s.name == conn.execute(
            "SELECT streamer FROM streams WHERE video_id = ?", (part.source,)).fetchone()[0])
        time.sleep(10)
        return fetcher.cut(info, streamer, max(0.0, part.start_s - HD_PAD_S), part.end_s + HD_PAD_S, dest)
    return fetch


def _compilation_writer(cfg):
    from .compilation import CompilationSettings
    from .llm import compilation_title_writer

    s = CompilationSettings.from_config(cfg)
    return compilation_title_writer(model=s.title_model, effort=s.title_effort)


def _compile(cfg, client, conn, streamers, args) -> int:
    from .compilation import CompilationSettings, Compiler, plan_parts

    settings = CompilationSettings.from_config(cfg)
    if args.dry_run:
        # No Claude calls: what's been found so far, and what would go in.
        dry = Compiler(cfg=cfg, conn=conn, client=client, streamers=streamers, judge=None, frames=None, cut=None,
                       windows_dir=cfg.path("paths.windows"))
        for streamer in dry._streamers():
            waiting = len(dry.unchecked(streamer))
            parts = plan_parts(dry.available(streamer), settings)
            print(f"{streamer.name}: {waiting} clip(s) not checked yet; {len(parts)} usable fail(s) "
                  f"(a compilation needs {settings.min_fails})")
            for p in parts:
                print(f"  clip {p.clip_id}  strength {p.strength}  {p.seconds:4.1f} s  {p.kind}: {p.note}")
        return 0
    compiler = _compiler(cfg, client, conn, streamers, required=True)
    if compiler is None:
        print("Compilations need ANTHROPIC_API_KEY in .env.", file=sys.stderr)
        return 2
    if args.unbuild:
        for comp_id in args.unbuild:
            compiler.unbuild(comp_id, "taken back to be rebuilt")
            print(f"Compilation {comp_id} taken back; its fails are free again.")
    if args.refine:
        from .crash_hunt import HuntSettings, frames_from_url, refine_vod_fails, resolve_low_url
        if _recording_now(conn):
            print("A live stream is being recorded right now; run --refine once it's over.", file=sys.stderr)
            return 1
        ffmpeg = find_ffmpeg(cfg)
        clips_done = compiler.refine_clip_fails()
        vods_done = refine_vod_fails(conn, compiler.judge,
                                     lambda url, h, s, e, n: frames_from_url(ffmpeg, url, h, s, e, n),
                                     resolve_low_url, HuntSettings.from_config(cfg).low_format)
        print(f"Looked again at {clips_done} clip fail(s) and {vods_done} stream fail(s).")
    if (args.refine or args.unbuild) and not args.top:
        return 0
    if args.top:
        made = []
        for streamer in compiler._streamers():
            made += compiler.build_top(streamer, shorts=args.shorts, per=args.top, include_vods=True)
        for comp in made:
            print(f"Top {args.top} compilation {comp} queued: the running watcher sends it to Clips Kitty, then it's "
                  "on the review page.")
        if not made:
            print(f"Not enough unused fails for a top {args.top}.")
        return 0
    if args.drop:
        try:
            comp = compiler.drop(args.drop)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"Clip {args.drop} is left out." + (f" Compilation {comp} queued without it." if comp else
              " Not enough fails left for a compilation yet."))
        return 0
    run = compiler.run_once()
    print(f"Checked {run.judged} clip(s): {run.fails} driving fail(s).")
    for comp in run.queued:
        print(f"Compilation {comp} queued: the running watcher sends it to Clips Kitty, then it's on the "
              "review page.")
    if not run.queued:
        print("No compilation yet (not enough unused fails). Run with --dry-run to see what's there.")
    return 0


def _recording_now(conn) -> bool:
    """A live stream is being recorded (a piece written in the last 5 minutes)."""
    import time
    row = conn.execute("SELECT MAX(end_utc) FROM segments").fetchone()
    return bool(row and row[0] and time.time() - float(row[0]) < 300)


def _gta_streams(cfg, streamers) -> list[str]:
    """The finished GTA 6 streams of compilations.streamers' YouTube channels (one flat listing each)."""
    import yt_dlp

    from .compilation import CompilationSettings
    from .sources import looks_like_gta6

    urls = []
    for name in CompilationSettings.from_config(cfg).streamers:
        s = next((x for x in streamers if x.name == name and x.permission and x.platform == "youtube"), None)
        if s is None:
            continue
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                               "playlistend": 60}) as ydl:
            listing = ydl.extract_info(s.channel_url.rstrip("/") + "/streams", download=False)
        for e in listing.get("entries") or []:
            if e.get("live_status") == "was_live" and looks_like_gta6(e.get("title") or "",
                                                                      cfg.get("gta6.keywords", [])):
                urls.append(f"https://www.youtube.com/watch?v={e['id']}")
    return urls


def _hunt(cfg, client, conn, streamers, args) -> int:
    import time

    from .compilation import CompilationSettings
    from .crash_hunt import FailHunter, HuntError, frames_from_url
    from .llm.fails import ClaudeFailJudge

    if _recording_now(conn):
        print("A live stream is being recorded right now. Hunting reads a lot from YouTube, which could get the "
              "recording blocked as a bot; run it once the stream is over.", file=sys.stderr)
        return 1
    if not env("ANTHROPIC_API_KEY"):
        print("hunt needs ANTHROPIC_API_KEY in .env.", file=sys.stderr)
        return 2
    settings = CompilationSettings.from_config(cfg)
    ffmpeg = find_ffmpeg(cfg)
    fetcher = YtDlpFetcher(ffmpeg=ffmpeg, cache_dir=cfg.path("paths.cache"),
                           video_format=str(cfg.get("windows.video_format") or "b"))
    hunter = FailHunter(cfg=cfg, conn=conn, streamers=streamers,
                        judge=ClaudeFailJudge(model=settings.judge_model, effort=settings.judge_effort),
                        fetcher=fetcher,
                        frames=lambda url, headers, s, e, n: frames_from_url(ffmpeg, url, headers, s, e, n))
    urls = args.urls or _gta_streams(cfg, streamers)
    print(f"Looking for driving fails in {len(urls)} stream(s).")
    for i, url in enumerate(urls):
        if i:
            time.sleep(30)   # spread the YouTube lookups out
        try:
            result = hunter.hunt(url)
        except (HuntError, NotGta6, SourceError, DownloadError, ScanError) as exc:
            print(f"  {url}: {exc}")
            continue
        print(f"  {result.title[:70]}: {result.candidates} moment(s) checked, {len(result.fails)} fail(s)")
        for start, strength, note in sorted(result.fails, key=lambda f: -f[1]):
            print(f"    {clock(start)}  strength {strength}  {note}")
    if args.no_build:
        return 0
    compiler = _compiler(cfg, client, conn, streamers, required=True)
    built = []
    for streamer in compiler._streamers():
        comp = compiler.build(streamer, include_vods=True)
        if comp is not None:
            built.append(comp)
    for comp in built:
        print(f"Compilation {comp} queued: the running watcher sends it to Clips Kitty, then it's on the review page.")
    if not built:
        print("Not enough fails for a compilation.")
    return 0


def sample_plan(moments, duration_s: float, title: str) -> dict:
    """A plan made without Claude, to try the engine: the title as the hook, a punch-in on the webcam at
    the loudest moment (shaking when it's very loud) with a flash, and a slow replay of what led up to it."""
    import re

    plan: dict = {"keep": [], "zooms": [], "replays": [], "freezes": [], "texts": [], "flashes": [], "notes": []}
    hook = re.sub(r"\s*[|#].*$", "", title).strip()
    if hook:
        plan["hook"] = {"text": hook[:40], "seconds": 2.0}
    if moments:
        m = moments[0]
        a, b = max(0.0, m.peak - 0.8), min(duration_s, m.peak + 1.7)
        plan["zooms"].append({"start": a, "end": b, "target": "cam", "scale": 2.0, "shake": m.over_db >= 5.5})
        plan["flashes"].append(m.peak)
        ra, rb = max(0.0, m.peak - 3.0), min(duration_s, m.peak + 0.5)
        if rb - ra >= 1.0:
            plan["replays"].append({"start": ra, "end": rb, "speed": 0.5, "target": "game", "scale": 1.3,
                                    "label": "RUN IT BACK", "after": b})
        plan["notes"].append(f"punch-in on the loudest moment ({m.peak:.1f} s, {m.over_db:.0f} dB over normal)")
    return plan


def _edit(cfg, client, conn, args) -> int:
    from pathlib import Path

    from .editing import PlanError, RenderError, measure_moments, parse_plan, render

    row = db.get_clip(conn, args.clip)
    if row is None:
        print(f"We don't track clip {args.clip}.", file=sys.stderr)
        return 1
    theirs = next((c for c in client.video_clips(row["ck_video_id"]) if int(c["id"]) == args.clip), None)
    if theirs is None:
        print(f"Clips Kitty doesn't have clip {args.clip} any more.", file=sys.stderr)
        return 1
    places = ((theirs.get("render_opts") or {}).get("gaming") or {}).get("places") or {}
    cam_box = places.get("cam")
    duration = float(theirs["end_s"]) - float(theirs["start_s"])
    edits = cfg.path("paths.edits") if cfg.get("paths.edits") else cfg.base_dir / "data" / "edits"
    source = edits / f"clip{args.clip}_source.mp4"
    if not source.is_file():
        client.download_media(args.clip, source)
    ffmpeg = find_ffmpeg(cfg)
    if not args.plan and not args.sample:
        if args.out:
            print("--out only works with --plan or --sample.", file=sys.stderr)
            return 2
        make = _editor_factory(cfg, client)
        if make is None:
            print("Claude edits need ANTHROPIC_API_KEY in .env (or use --sample).", file=sys.stderr)
            return 2
        editor = make()
        if args.note:
            _save_feedback(editor.conn, args.clip, args.note)
        if not editor.can_afford():
            print(f"The editor has spent ${editor.spent():.2f} of its ${editor.settings.weekly_budget_usd:.2f} "
                  "this week.", file=sys.stderr)
            return 1
        result = editor.edit(args.clip, args.note)
        for n in result.notes:
            print(f"  {n}")
        if result.state != "ready":
            print(f"No edit: {result.error}", file=sys.stderr)
            return 1
        print(f"Edited video: {result.path}  (cost ${result.cost_usd:.2f}; ${editor.spent():.2f} of "
              f"${editor.settings.weekly_budget_usd:.2f} spent this week)\nNothing was uploaded.")
        return 0
    if args.plan:
        data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    else:
        moments = measure_moments(ffmpeg, source)
        for m in moments:
            print(f"  loud at {m.peak:5.1f} s ({m.start:.1f}-{m.end:.1f} s), {m.over_db:.0f} dB over normal")
        data = sample_plan(moments, duration, row["title"])
    try:
        plan = parse_plan(data, duration)
    except PlanError as exc:
        print(f"The plan isn't valid: {exc}", file=sys.stderr)
        return 1
    n = 1
    while (edits / f"clip{args.clip}_{n}.mp4").exists():
        n += 1
    dest = Path(args.out) if args.out else edits / f"clip{args.clip}_{n}.mp4"
    (dest.with_suffix(".plan.json")).parent.mkdir(parents=True, exist_ok=True)
    dest.with_suffix(".plan.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        render(ffmpeg, source, plan, dest, duration_s=duration, cam_box=cam_box)
    except RenderError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Edited video: {dest}\nIts plan: {dest.with_suffix('.plan.json')}\nNothing was uploaded.")
    return 0


def _save_feedback(conn, clip_id: int | None, text: str, edit_id: int | None = None) -> None:
    text = " ".join(text.split())[:500]
    if text:
        conn.execute("INSERT INTO editor_feedback (ck_clip_id, edit_id, text, created_at) VALUES (?, ?, ?, ?)",
                     (clip_id, edit_id, text, db._now()))
        conn.commit()


def _editor_factory(cfg, client):
    """Makes Editors (each with its own database connection), or None without a Claude key."""
    from .editing import Editor, EditorSettings
    from .editing.editor import ClaudePlanWriter

    if not env("ANTHROPIC_API_KEY"):
        return None
    settings = EditorSettings.from_config(cfg)
    ffmpeg = find_ffmpeg(cfg)
    edits = cfg.path("paths.edits") if cfg.get("paths.edits") else cfg.base_dir / "data" / "edits"
    writer = ClaudePlanWriter(model=settings.model, effort=settings.effort)

    def make():
        return Editor(conn=db.connect(cfg.path("paths.database"), threads=True), client=client, ffmpeg=ffmpeg,
                      writer=writer, edits_dir=edits, settings=settings)
    return make


def _watch_editor(cfg, client):
    """The watcher's background editor (edits new clips while it records), or None when it's off."""
    from .editing import EditorSettings, EditorWorker

    if not EditorSettings.from_config(cfg).enabled or cfg.get("editor.auto", True) is not True:
        return None
    make = _editor_factory(cfg, client)
    return EditorWorker(make) if make is not None else None
