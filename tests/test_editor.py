"""The Claude editor: budget, self-check points, the plan/render/check loop and the review page's part."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gta6clips import db
from gta6clips.editing import Editor, EditorSettings, parse_plan
from gta6clips.editing.editor import Answer, EditBrief, check_points, price, record_usage, spent_this_week

CAM = [0.0, 0.094, 0.38, 0.21]
NOW = 1_800_000_000.0


def _plan(**kw):
    base = {"keep": [], "zooms": [], "replays": [], "freezes": [], "texts": [], "flashes": [], "notes": []}
    base.update(kw)
    return base


def test_prices_and_the_weekly_budget(tmp_path):
    conn = db.connect(tmp_path / "o.db")
    assert price("claude-opus-5-5", 1_000_000, 100_000) == pytest.approx(6.0)
    assert price("claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.10)
    record_usage(conn, model="claude-opus-5-5", purpose="edit plan", clip_id=1, input_tokens=10_000,
                 output_tokens=5_000, now=NOW)                        # $0.14
    record_usage(conn, model="claude-opus-5-5", purpose="edit plan", clip_id=1, input_tokens=1_000_000,
                 output_tokens=0, now=NOW - 8 * 86400)               # more than a week ago
    record_usage(conn, model="claude-haiku-5-5", purpose="fail check", clip_id=1, input_tokens=1_000_000,
                 output_tokens=0, now=NOW)                            # not the editor's
    assert spent_this_week(conn, now=NOW) == pytest.approx(0.14)


def test_check_points_say_what_each_frame_should_show():
    plan = parse_plan(_plan(keep=[[0, 10], [14, 20]], hook={"text": "Watch this", "seconds": 2},
                            zooms=[{"start": 4, "end": 6, "target": "cam", "scale": 2.0}],
                            texts=[{"start": 15, "end": 17, "text": "no way", "position": "bottom", "size": "m"}],
                            replays=[{"start": 4, "end": 6, "speed": 0.5, "target": "game", "scale": 1.3,
                                      "label": "RUN IT BACK"}]), 20)
    points = check_points(plan, 20, CAM)
    whats = " | ".join(w for _, w in points)
    assert "hook text 'Watch this'" in whats and "punch-in on the cam" in whats and "'no way'" in whats
    assert "slow replay labelled 'RUN IT BACK'" in whats
    texts = [t for t, w in points if "no way" in w]
    assert texts == [pytest.approx(16.0)]      # 16 s of the clip: after the 4 s replay, minus the 4 s cut


class FakeClient:
    def __init__(self, source: Path, duration: float):
        self.source, self.duration = source, duration

    def video_clips(self, video_id):
        return [{"id": 7, "start_s": 100.0, "end_s": 100.0 + self.duration,
                 "render_opts": {"gaming": {"places": {"cam": CAM}}}}]

    def captions(self, clip_id):
        return [{"start": 2.0, "end": 3.0, "text": "oh no"}]

    def download_media(self, clip_id, dest):
        shutil.copy(self.source, dest)
        return dest


class FakeWriter:
    """First plan is invalid (refused and asked again), the check then fixes the zoom."""

    def __init__(self, ok=False):
        self.briefs: list[EditBrief] = []
        self.problems: list[str] = []
        self.ok = ok

    def plan(self, brief, problem=""):
        self.briefs.append(brief)
        self.problems.append(problem)
        if not problem:
            return Answer(_plan(zooms=[{"start": 1, "end": 3, "target": "cam", "scale": 9}]), "claude-opus-5-5",
                          10_000, 2_000)
        return Answer(_plan(zooms=[{"start": 1, "end": 3, "target": "cam", "scale": 2.0}],
                            hook={"text": "He did NOT see that", "seconds": 2}, notes=["punch-in on his reaction"]),
                      "claude-opus-5-5", 10_000, 2_000)

    def check(self, brief, plan, frames):
        assert frames and all(isinstance(j, bytes) for _, _, j in frames)
        if self.ok:
            return Answer({"ok": True, "problems": [], "fixed_plan": plan}, "claude-opus-5-5", 5_000, 1_000)
        fixed = dict(plan, zooms=[{"start": 1, "end": 3, "target": "cam", "scale": 1.6}],
                     notes=["punch-in on his reaction, a bit wider"])
        return Answer({"ok": False, "problems": ["the zoom cut his face in half"], "fixed_plan": fixed},
                      "claude-opus-5-5", 5_000, 1_000)


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")


def _source(tmp_path, seconds=6) -> Path:
    src = tmp_path / "src.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc=size=1080x1920:rate=30:d={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}:sample_rate=48000", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    return src


def _editor(tmp_path, writer, budget=10.0, src=None):
    conn = db.connect(tmp_path / "o.db", threads=True)
    db.upsert_clip(conn, ck_clip_id=7, window_id=None, ck_video_id="v1", streamer="TheBurntPeanut",
                   status="pending_review", score=70, start_s=100.0, end_s=106.0, title="He crashed",
                   title_options=[], hook="", description="", hashtags=[], notes=[])
    clock = iter(NOW + i for i in range(10_000))
    return Editor(conn=conn, client=FakeClient(src or tmp_path / "none.mp4", 6.0), ffmpeg=Path(shutil.which("ffmpeg") or "ffmpeg"),
                  writer=writer, edits_dir=tmp_path / "edits",
                  settings=EditorSettings(weekly_budget_usd=budget), clock=lambda: next(clock))


@needs_ffmpeg
def test_claude_plans_we_render_it_checks_and_fixes(tmp_path):
    writer = FakeWriter()
    editor = _editor(tmp_path, writer, src=_source(tmp_path))
    editor.conn.execute("INSERT INTO editor_feedback (ck_clip_id, text, created_at) VALUES (1, 'less text', 'x')")
    result = editor.edit(7, "zoom on him")
    assert result.state == "ready", result.error
    assert writer.problems[1].startswith("zoom scale")                 # the refused plan was sent back
    assert writer.briefs[0].owner_notes == ["less text"] and writer.briefs[0].request == "zoom on him"
    assert writer.briefs[0].cam_box == CAM and writer.briefs[0].transcript == [(2.0, 3.0, "oh no")]
    row = editor.conn.execute("SELECT * FROM edits WHERE id = ?", (result.edit_id,)).fetchone()
    assert row["state"] == "ready" and Path(row["path"]).is_file()
    assert json.loads(row["plan"])["zooms"][0]["scale"] == 1.6       # the fixed plan was rendered
    assert any("cut his face in half" in n for n in json.loads(row["notes"]))
    # two plans ($0.08 each) and a check ($0.04)
    assert row["cost_usd"] == pytest.approx(0.20) and editor.spent() == pytest.approx(0.20)
    again = editor.edit(7)
    states = [r[0] for r in editor.conn.execute("SELECT state FROM edits ORDER BY id")]
    assert again.state == "ready" and states == ["replaced", "ready"]


def test_no_edits_once_the_week_is_spent(tmp_path):
    editor = _editor(tmp_path, FakeWriter(), budget=0.30)
    record_usage(editor.conn, model="claude-opus-5-5", purpose="edit plan", clip_id=1, input_tokens=0,
                 output_tokens=10_000, now=NOW)           # $0.20 spent; one more edit would pass $0.30
    assert editor.waiting() == [7] and editor.run_once() == []


def test_a_failed_edit_says_why(tmp_path):
    class Broken(FakeWriter):
        def plan(self, brief, problem=""):
            return Answer(_plan(zooms=[{"start": 1, "end": 3, "target": "nowhere", "scale": 2}]), "claude-opus-5-5",
                          1000, 100)
    editor = _editor(tmp_path, Broken(), src=None)
    editor.client.download_media = lambda clip_id, dest: dest.write_bytes(b"x") and dest
    editor.brief = lambda clip_id, source, request="": EditBrief(7, "x", "t", 6.0, [], [], [], CAM)
    result = editor.edit(7)
    assert result.state == "failed" and "refused twice" in result.error
    assert editor.waiting() == []        # tried: not picked up again by itself


@needs_ffmpeg
def test_the_review_page_shows_and_drops_the_edit(tmp_path):
    from fastapi.testclient import TestClient

    from gta6clips.review.app import create_app
    from gta6clips.review.service import ReviewService

    editor = _editor(tmp_path, FakeWriter(ok=True), src=_source(tmp_path))
    result = editor.edit(7)
    service = ReviewService(conn=editor.conn, client=editor.client, streamers=[])
    card = service.card(7)
    assert card.edit["id"] == result.edit_id and card.edit["state"] == "ready"

    class Worker:
        asked = []

        def request(self, clip_id, note=""):
            self.asked.append((clip_id, note))

    worker = Worker()
    app = TestClient(create_app(service, editor.client, editor=worker), base_url="http://127.0.0.1")
    r = app.get(f"/edits/{result.edit_id}.mp4")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert app.post("/api/clips/7/edit", json={"note": "  more zoom  "}).json() == {"started": True}
    assert worker.asked == [(7, "more zoom")]
    assert [r[0] for r in editor.conn.execute("SELECT text FROM editor_feedback")] == ["more zoom"]
    assert app.post("/api/clips/7/edit/drop", json={}).json()["edit"]["state"] == "dropped"
