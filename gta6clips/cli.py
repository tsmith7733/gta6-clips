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
from .llm import ClaudeTitleWriter, ClipsKittyTitles, TitleWriter, TitleWriterError
from .logging_setup import setup_logging
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
        return ClipsKittyTitles()
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

        writer = make_title_writer(cfg)
        if writer is None:
            print("Claude titles are on (llm.enabled: true in config.yaml) but ANTHROPIC_API_KEY isn't set.\n"
                  "Add your key to .env, or set llm.enabled: false to keep Clips Kitty's titles.",
                  file=sys.stderr)
            return 2
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
    except (StreamerFileError, NotReady, NotGta6, JobFailed, SourceError, ClipsKittyError, TitleWriterError) as exc:
        log.error("%s", exc)
        return 1


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
