"""Exact clip ranges (our exact-moments plugin) and the quality check before the review page."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from gta6clips import db
from gta6clips.live.dispatch import QualitySettings, quality_problems
from gta6clips.live.session import LiveSession
from gta6clips.review.service import ReviewError, ReviewService

from .test_live import LIVE, PEANUT, SCORING, START, WINDOWS, Clock, FakeChat, FakeRecorder, World, _check, \
    _fake_cut, _loudness_for, _run, _watcher

PLUGIN_SRC = Path(__file__).resolve().parent.parent / "plugins" / "exact-moments" / "src" / "ranges.py"


def _plugin():
    spec = importlib.util.spec_from_file_location("exact_ranges", PLUGIN_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -- the plugin's own logic -------------------------------------------------------------------

def test_plugin_reads_ranges_and_refuses_bad_ones():
    p = _plugin()
    assert p.parse_ranges("33.0-87.5; 120-180") == [(33.0, 87.5), (120.0, 180.0)]
    assert p.parse_ranges("") == []
    for bad in ("33", "90-80", "x-y"):
        with pytest.raises(ValueError):
            p.parse_ranges(bad)


def test_plugin_reads_notes_for_the_title():
    p = _plugin()
    assert p.parse_notes('Twitch viewers called it "13 YEARS" || chat: "new car" ||  || chat: "new car"') == [
        'Twitch viewers called it "13 YEARS"', 'chat: "new car"']
    assert len(p.parse_notes(" || ".join(f"note {i}" for i in range(9)))) == 5
    assert len(p.parse_notes("x" * 300)[0]) == 160


def test_live_chat_notes_keep_what_viewers_said_not_laughs():
    from gta6clips.exact import chat_notes

    notes = chat_notes(["LMAOOOO", "lmao", "KEKW", "?????", "hahaha", "new car", "New car!", "gas station",
                        ":face-turquoise-music-note:", "omg", "pay and spray"])
    assert notes == ['Live chat during this moment: "new car"; "gas station"; "pay and spray"']
    assert chat_notes(["LOL", "W", "xD"]) == []


def test_plugin_moves_ends_to_sentence_breaks_within_the_limit():
    p = _plugin()
    segs = [{"start": 30.0, "end": 34.0}, {"start": 34.5, "end": 88.2}, {"start": 95.0, "end": 99.0}]
    assert p.snap(33.0, 87.0, segs, 4) == (30.0, 88.2)       # back to where the sentence starts, on to its end
    assert p.snap(33.0, 87.0, segs, 0) == (33.0, 87.0)       # snapping off
    assert p.snap(60.0, 92.0, segs, 1) == (60.0, 92.0)       # no break close enough: stays


# -- the check ------------------------------------------------------------------------------------

Q = QualitySettings()


def _ck_clip(start, end, layout="split"):
    return {"start_s": start, "end_s": end, "render_opts": {"gaming": {"layout": layout, "by": "video"}}}


def test_quality_rules():
    assert quality_problems(_ck_clip(40, 100), 180, Q) == ([], False)
    problems, widen = quality_problems(_ck_clip(40, 50), 180, Q)
    assert widen and "too short" in problems[0]
    problems, widen = quality_problems(_ck_clip(120, 179.5), 180, Q)
    assert widen and "cut off at the end" in problems[0]
    problems, widen = quality_problems(_ck_clip(40, 100, layout="fill"), 180, Q)
    assert not widen and "no split screen" in problems[0]


class ExactWorld(World):
    def factory(self, conn):
        def make(streamer, check):
            self.made.append(streamer.name)
            rec = FakeRecorder(self.clock, self.workdir / "rec" / check.video_id)
            self.recorders.append(rec)
            return LiveSession(conn=conn, streamer=streamer, check=check, recorder=rec,
                               chat=FakeChat(self.clock, self.chat), measure=_loudness_for(self.moment),
                               cut=_fake_cut, windows_dir=self.workdir / "windows", scoring=SCORING,
                               windows=WINDOWS, live=LIVE, exact=(50.0, 18.0))
        return make


def _live(cfg, ck, conn, streamers, writer, workdir, seconds=1800):
    clock = Clock()
    world = ExactWorld(workdir, clock)
    world.live[PEANUT] = _check()
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    _run(w, clock, seconds)
    return w


def test_live_windows_ask_for_the_moment_around_the_spike(cfg, ck, ck_state, conn, streamers, writer, workdir):
    _live(cfg, ck, conn, streamers, writer, workdir)
    body = ck_state.submissions[0]
    assert body["pipeline"]["id"] == "gta6clips/exact-moments" and body["max_clips"] == 1
    start, end = (float(x) for x in body["pipeline"]["settings"]["ranges"].split("-"))
    row = db.live_windows(conn, ("collected",))[0]
    peak = START + 1500 - row["start_utc"]           # where the spike's peak sits in the cut
    assert end - start == pytest.approx(68, abs=0.2) and start < peak < end
    assert body["pipeline"]["settings"]["notes"].startswith("Live chat during this moment:")
    clip = db.clips_by_status(conn, "pending_review")[0]
    assert clip["score"] == max(1, min(99, round((row["score"] - 3) * 20)))   # ours, not the plugin's flat 90


def test_exact_clips_are_scored_from_our_window():
    from gta6clips.live.dispatch import window_clip_score

    assert window_clip_score({"finder": "exact", "score": 5.0}) == 40          # a Twitch clip with 100 views
    assert window_clip_score({"finder": "exact", "score": 6.0}) == 60          # 1,000 views
    assert window_clip_score({"finder": "exact", "score": 12.0}) == 99
    assert window_clip_score({"finder": "clipskitty", "score": 6.0}) is None   # a real finder scored it


def test_a_short_clip_is_sent_back_wider_once_then_held(cfg, ck, ck_state, conn, streamers, writer, workdir):
    cfg.data["qa"]["min_seconds"] = 200               # every clip "too short", even after widening
    _live(cfg, ck, conn, streamers, writer, workdir, seconds=2400)
    first, second = ck_state.submissions[:2]
    s1, e1 = (float(x) for x in first["pipeline"]["settings"]["ranges"].split("-"))
    s2, e2 = (float(x) for x in second["pipeline"]["settings"]["ranges"].split("-"))
    assert (s2, e2) == (pytest.approx(max(0.0, s1 - 8)), pytest.approx(e1 + 8))
    assert "_wide1" in second["path"]
    rejected = db.clips_by_status(conn, "rejected")
    assert len(rejected) == 1 and "wider range" in rejected[0]["notes"]
    held = db.clips_by_status(conn, "needs_fix")
    assert len(held) == 1 and "too short" in held[0]["notes"]
    assert len(ck_state.submissions) == 2              # only one widening


def test_no_split_screen_goes_to_needs_a_fix(cfg, ck, ck_state, conn, streamers, writer, workdir):
    ck_state.layout = "fill"
    _live(cfg, ck, conn, streamers, writer, workdir)
    held = db.clips_by_status(conn, "needs_fix")
    assert len(held) == 1 and "no split screen" in held[0]["notes"]
    assert not db.clips_by_status(conn, "pending_review")


# -- the Needs a fix tab ------------------------------------------------------------------------

def test_rerender_and_send_to_review(cfg, ck, ck_state, conn, streamers, writer, workdir):
    ck_state.layout = "fill"
    _live(cfg, ck, conn, streamers, writer, workdir)
    review = ReviewService(conn=conn, client=ck, streamers=streamers)
    clip = db.clips_by_status(conn, "needs_fix")[0]
    assert review.counts()["needs_fix"] == 1
    card = review.rerender(clip["ck_clip_id"])
    assert card.status == "rejected"
    window = db.live_windows(conn, ("planned",))[0]
    assert "_fix1" in window["local_path"] and Path(window["local_path"]).is_file() and window["finder"] == "exact"
    # The owner drew the webcam box in Clips Kitty: the re-render comes back split and goes to review.
    ck_state.layout = "split"
    w = _watcher(cfg, ck, conn, streamers, writer, ExactWorld(workdir, Clock()), Clock())
    for _ in range(4):
        w.dispatcher.tick()
    assert db.clips_by_status(conn, "pending_review")
    with pytest.raises(ReviewError):
        review.send_to_review(clip["ck_clip_id"])     # it's rejected now, not waiting for a fix


def test_send_to_review_as_is(cfg, ck, ck_state, conn, streamers, writer, workdir):
    ck_state.layout = "fill"
    _live(cfg, ck, conn, streamers, writer, workdir)
    review = ReviewService(conn=conn, client=ck, streamers=streamers)
    clip = db.clips_by_status(conn, "needs_fix")[0]
    assert review.send_to_review(clip["ck_clip_id"]).status == "pending_review"
    notes = json.loads(db.get_clip(conn, clip["ck_clip_id"])["notes"])
    assert any("quality check" in n for n in notes) and any("sent to review as it is" in n for n in notes)


def test_a_whole_twitch_clip_is_not_too_short_or_cut_off():
    assert quality_problems(_ck_clip(0, 25), 25, Q, check_edges=False) == ([], False)
    problems, _ = quality_problems(_ck_clip(0, 25, layout="fill"), 25, Q, check_edges=False)
    assert len(problems) == 1 and "no split screen" in problems[0]


# -- sound ----------------------------------------------------------------------------------------

import shutil
import subprocess

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")


@needs_ffmpeg
def test_silent_stretch_finds_where_the_sound_stops(tmp_path):
    from gta6clips.media import silent_stretch

    ffmpeg = shutil.which("ffmpeg")
    media = tmp_path / "half_muted.mp4"
    # 20 s of tone, then 20 s of silence (what Twitch's muting leaves).
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=20", "-filter_complex",
                    "[0:a][1:a]concat=n=2:v=0:a=1", "-c:a", "aac", str(media)], check=True)
    assert silent_stretch(Path(ffmpeg), media, 0, 18) is None
    assert silent_stretch(Path(ffmpeg), media, 10, 38) == pytest.approx(26.0)   # chunks of 8 s from 10 s


def test_a_silent_clip_goes_to_needs_a_fix(cfg, ck, ck_state, conn, streamers, writer, workdir):
    clock = Clock()
    world = ExactWorld(workdir, clock)
    world.live[PEANUT] = _check()
    w = _watcher(cfg, ck, conn, streamers, writer, world, clock)
    w.dispatcher.silent = lambda path, s, e: s + 5          # no sound 5 s in
    _run(w, clock, 1800)
    held = db.clips_by_status(conn, "needs_fix")
    assert len(held) == 1 and "no sound from 5 s into the clip" in held[0]["notes"]
