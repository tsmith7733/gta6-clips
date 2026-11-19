"""Checks Clips Kitty is safe and able to take work, before anything is sent to it."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import Config
from .client import ClipsKittyClient

log = logging.getLogger(__name__)


@dataclass
class Readiness:
    problems: list[str] = field(default_factory=list)   # blocking: nothing is sent
    warnings: list[str] = field(default_factory=list)   # logged, work continues
    app_version: str | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def check_readiness(client: ClipsKittyClient, cfg: Config) -> Readiness:
    """Raises ClipsKittyError only when Clips Kitty can't be reached at all."""
    result = Readiness()

    health = client.health()
    result.app_version = str(health.get("app_version") or "")
    tested = str(cfg.get("clipskitty.tested_version", ""))
    if tested and result.app_version != tested:
        result.warnings.append(
            f"Clips Kitty is version {result.app_version or 'unknown'}; this orchestrator was "
            f"tested with {tested}."
        )

    # Our orchestrator is the only thing allowed to queue or publish.
    if client.automation().get("enabled"):
        result.problems.append(
            "Clips Kitty's own automation (watched channels) is ON. Turn it off on its Watch "
            "page: this pipeline must be the only thing that decides what gets clipped."
        )
    settings = client.settings()
    if settings.get("auto_upload"):
        result.problems.append(
            "Clips Kitty's auto upload is ON. Turn it off in Settings: nothing may be "
            "published without your approval."
        )
    if str(settings.get("privacy", "")).lower() == "public":
        result.warnings.append(
            "Clips Kitty's default upload privacy is 'public'. This pipeline always sends "
            "'private' or a schedule, but consider changing the default to Private in Settings."
        )

    preflight = client.preflight()
    for check in preflight.get("checks", []):
        if check.get("blocking") and not check.get("ok"):
            fix = f" Fix: {check['fix']}" if check.get("fix") else ""
            result.problems.append(f"Clips Kitty check '{check.get('name')}' failed: {check.get('detail')}.{fix}")

    if client.queue().get("paused"):
        result.warnings.append(
            "Clips Kitty's queue is stopped. Jobs will wait until you press 'Start queue' on its Queue page."
        )

    plugin_id = cfg.require("clipskitty.gta_plugin_id")
    wanted_version = str(cfg.require("clipskitty.gta_plugin_version"))
    plugin = next((p for p in client.plugins() if p.get("id") == plugin_id), None)
    if plugin is None:
        result.problems.append(
            f"The GTA plugin ({plugin_id}) isn't installed in Clips Kitty. See README "
            "'Install the GTA plugin'."
        )
    else:
        if not plugin.get("enabled", True):
            result.problems.append(f"The GTA plugin ({plugin_id}) is installed but switched off.")
        if str(plugin.get("version")) != wanted_version:
            result.problems.append(
                f"The GTA plugin is version {plugin.get('version')}, but config.yaml pins "
                f"{wanted_version}. Review the new version before changing the pin."
            )
        for problem in plugin.get("problems_here") or []:
            text = problem.get("text") if isinstance(problem, dict) else str(problem)
            result.problems.append(f"The GTA plugin can't run here: {text}")

    # Callers decide how to show these (the check command prints them, vod logs them).
    return result
