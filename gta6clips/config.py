"""Loads config.yaml and .env. Every other module receives a Config, never reads files itself."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"


class ConfigError(Exception):
    """config.yaml is missing, unreadable or has an invalid value."""


@dataclass
class Config:
    data: dict[str, Any]
    base_dir: Path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted)
        if value is None:
            raise ConfigError(f"config.yaml is missing '{dotted}'")
        return value

    def path(self, dotted: str) -> Path:
        p = Path(str(self.require(dotted)))
        return p if p.is_absolute() else self.base_dir / p


def load_config(path: Path | str | None = None, *, load_env: bool = True) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.is_file():
        raise ConfigError(f"No config file at {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a mapping at the top level")
    if load_env:
        load_dotenv(path.parent / ".env")
    return Config(data=copy.deepcopy(data), base_dir=path.parent)


def env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None
