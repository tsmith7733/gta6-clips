from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gta6clips import db
from gta6clips.clipskitty import ClipsKittyClient
from gta6clips.config import load_config
from gta6clips.llm import ClipBrief, TitleSuggestions
from gta6clips.sources import SourceInfo
from gta6clips.streamers import load_streamers

from .fake_clipskitty import FakeState, create_app

REPO = Path(__file__).resolve().parent.parent

STREAMERS_YAML = """
streamers:
  - name: The Burnt Peanut
    platform: youtube
    channel_url: https://www.youtube.com/@TheBurntPeanut
    permission: true
    permission_source: test
    credit: "Clip from The Burnt Peanut"
    priority: 1
  - name: No Permission Pete
    platform: youtube
    channel_url: https://www.youtube.com/@NoPermissionPete
    permission: false
    credit: "Clip from Pete"
    priority: 2
"""


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    shutil.copy(REPO / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "streamers.yaml").write_text(STREAMERS_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def cfg(workdir: Path):
    config = load_config(workdir / "config.yaml", load_env=False)
    config.data["clipskitty"]["poll_seconds"] = 0
    config.data["twitch_clips"]["min_score"] = 0    # tests pick their own view counts
    return config


@pytest.fixture
def streamers(workdir: Path):
    return load_streamers(workdir / "streamers.yaml")


@pytest.fixture
def conn(workdir: Path):
    connection = db.connect(workdir / "data" / "orchestrator.db")
    yield connection
    connection.close()


@pytest.fixture
def ck_state() -> FakeState:
    return FakeState()


@pytest.fixture
def ck(ck_state: FakeState) -> ClipsKittyClient:
    http = TestClient(create_app(ck_state), base_url="http://127.0.0.1:8765")
    return ClipsKittyClient("http://127.0.0.1:8765", http=http)


class FakeWriter:
    def __init__(self, fail: Exception | None = None):
        self.briefs: list[ClipBrief] = []
        self.fail = fail

    def write(self, brief: ClipBrief) -> TitleSuggestions:
        self.briefs.append(brief)
        if self.fail:
            raise self.fail
        return TitleSuggestions(titles=[f"Title A {len(self.briefs)}", "Title B", "Title C"],
                                hook="He did NOT expect that")


@pytest.fixture
def writer() -> FakeWriter:
    return FakeWriter()


def source(handle: str = "@TheBurntPeanut", title: str = "GTA 6 first day!! | stream") -> SourceInfo:
    return SourceInfo(
        url="https://www.youtube.com/watch?v=CW_rULxbDs0", platform="youtube", video_id="CW_rULxbDs0",
        title=title, description="", channel_name=handle.lstrip("@"),
        channel_values=(f"https://www.youtube.com/{handle}", "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
                        handle),
        duration=7200.0,
    )
