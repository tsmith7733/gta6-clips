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

    review = sub.add_parser("review", help="open the review page: watch clips, pick titles, approve or reject "
                                           "(Phase 4)")
    review.add_argument("--port", type=int, help="override review.port in config.yaml")
    review.add_argument("--no-browser", action="store_true", help="don't open the page in the browser")

    pub = sub.add_parser("publish", help="schedule approved clips on YouTube through Clips Kitty (Phase 5)")
    pub.add_argument("--yes", action="store_true", help="upload the plan (without it, only show the plan)")
    pub.add_argument("--clip", type=int, action="append", help="only this clip (repeatable)")

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

    try:
        streamers = load_streamers(cfg.path("paths.streamers"))
        conn = db.connect(cfg.path("paths.database"))
        client = ClipsKittyClient(cfg.require("clipskitty.url"))

        if args.command == "check":
            return _check(cfg, client, streamers)
        if args.command == "clips":
            return _list_clips(conn, args.status)
        if args.command == "publish":
            return _publish(cfg, client, conn, streamers, args)
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

    pub = PublishService(conn=conn, client=client, streamers=streamers, cfg=cfg)
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
        print(f"  {i.when}  [{i.clip_id}] {i.title}")
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
    publisher = PublishService(conn=shared, client=client, streamers=streamers, cfg=cfg)
    url = f"http://127.0.0.1:{port}/"
    print(f"Review page: {url}  (Ctrl+C stops it; nothing is published from it)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    serve(service, client, port=port, publisher=publisher)
    return 0


def _watch(cfg, client, conn, streamers, writer, args) -> int:
    from .live.factory import find_ytdlp, session_factory
    from .live.watcher import Watcher

    make = session_factory(cfg, conn, ffmpeg=find_ffmpeg(cfg), ytdlp=find_ytdlp())
    watcher = Watcher(cfg=cfg, client=client, conn=conn, streamers=streamers, writer=writer, make_session=make,
                      allow_non_gta=args.allow_non_gta)
    watcher.run(stop_after_s=args.minutes * 60 if args.minutes else None)
    print("\nStopped watching. Nothing is published. Review the clips with: python -m gta6clips clips")
    return 0
