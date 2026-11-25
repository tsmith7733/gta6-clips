"""The music check: clips with a licensed song are held back before review (clip 107 was blocked)."""

from __future__ import annotations

import json

import pytest

from gta6clips import db
from gta6clips.llm.music import MUSIC_SCHEMA, MusicCheckError, MusicVerdict, evidence_text, parse_verdict
from gta6clips.music_check import MusicChecker, MusicSettings, music_of, note_for

LYRICS = [{"start": 4.8, "end": 7.1, "text": "glamorous. The glamorous,"},
          {"start": 7.4, "end": 8.6, "text": "glamorous, baby. The"},
          {"start": 16.3, "end": 18.9, "text": "that needs a new car"}]


class FakeClient:
    def __init__(self, captions):
        self.captions_by = captions

    def captions(self, clip_id):
        return self.captions_by.get(clip_id, [])


class FakeCheck:
    model = "claude-haiku-5-5"

    def __init__(self):
        self.seen = []

    def check(self, evidence):
        self.seen.append(evidence)
        text = " ".join(t for _, t in evidence.transcript)
        if "glamorous" in text:
            return MusicVerdict("likely", "Glamorous by Fergie", "He sings 'the glamorous, glamorous, baby'.",
                                self.model, 2000, 100)
        if any("song" in c for c in evidence.chat):
            return MusicVerdict("possible", "", "Chat asks 'song?'", self.model, 2000, 100)
        return MusicVerdict("none", "", "", self.model, 2000, 50)


def _clip(conn, clip_id, status="pending_review", window_id=None):
    db.upsert_clip(conn, ck_clip_id=clip_id, window_id=window_id, ck_video_id="v", streamer="The Burnt Peanut",
                   status=status, score=70, start_s=10.0, end_s=40.0, title=f"Clip {clip_id}", title_options=[],
                   hook="", description="", hashtags=[], notes=[])


def _checker(tmp_path, captions):
    conn = db.connect(tmp_path / "o.db")
    return MusicChecker(conn=conn, client=FakeClient(captions), check=FakeCheck(), settings=MusicSettings(),
                        clock=lambda: 1_800_000_000.0)


def test_a_song_holds_the_clip_back_and_a_possible_one_gets_a_note(tmp_path):
    m = _checker(tmp_path, {1: LYRICS, 2: [{"start": 1, "end": 2, "text": "let's go"}],
                            3: [{"start": 1, "end": 2, "text": "drive"}], 4: LYRICS})
    _clip(m.conn, 1)
    _clip(m.conn, 2)
    _clip(m.conn, 3, status="spoiler_hold")
    _clip(m.conn, 4, status="published")                      # already decided: not checked
    m.conn.execute("INSERT INTO streams (id, platform, video_id, streamer, url, created_at) VALUES "
                   "(1, 'youtube', 'x', 'The Burnt Peanut', 'u', 'now')")
    m.conn.execute("INSERT INTO windows (id, streamer, source_url, source_title, start_utc, end_utc, stream_id, "
                   "created_at, updated_at) VALUES (9, 'The Burnt Peanut', 'f', 'GTA 6 day 3', 1000, 1100, 1, 'n', 'n')")
    m.conn.execute("UPDATE clips SET window_id = 9 WHERE ck_clip_id = 3")
    m.conn.execute("INSERT INTO chat_messages (stream_id, offset_s, t_utc, text) VALUES (1, 15, 1015, 'song?')")
    m.conn.execute("INSERT INTO chat_messages (stream_id, offset_s, t_utc, text) VALUES (1, 99, 1099, 'late')")
    m.conn.commit()
    done = dict(m.run_once())
    assert set(done) == {1, 2, 3}
    row1 = db.get_clip(m.conn, 1)
    assert row1["status"] == "needs_fix" and row1["review_from"] == "pending_review"
    assert "Glamorous by Fergie" in json.loads(row1["notes"])[-1]
    assert db.get_clip(m.conn, 2)["status"] == "pending_review" and music_of(m.conn, 2) is None
    row3 = db.get_clip(m.conn, 3)
    assert row3["status"] == "spoiler_hold" and "Possibly copyrighted" in json.loads(row3["notes"])[-1]
    assert music_of(m.conn, 1) == {"verdict": "likely", "song": "Glamorous by Fergie",
                                   "evidence": "He sings 'the glamorous, glamorous, baby'."}
    assert m.check.seen[[e.title for e in m.check.seen].index("Clip 3")].chat == ["song?"]
    cost = m.conn.execute("SELECT SUM(cost_usd), COUNT(*) FROM llm_usage WHERE purpose = 'music check'").fetchone()
    assert cost[1] == 3 and cost[0] == pytest.approx(3 * 2000 * 0.10 / 1e6 + 250 * 0.50 / 1e6)
    assert m.run_once() == []                                 # each clip is checked once


def test_a_failed_check_is_tried_again_later(tmp_path):
    m = _checker(tmp_path, {1: LYRICS})
    _clip(m.conn, 1)

    def broken(evidence):
        raise MusicCheckError("Claude API error 500: overloaded")
    real = m.check.check
    m.check.check = broken
    assert m.run_once() == []
    m.check.check = real
    assert [c for c, _ in m.run_once()] == [1]


def test_verdicts_and_what_claude_reads():
    assert parse_verdict('{"verdict": "likely", "song": " Glamorous  by Fergie ", "evidence": "x"}').song == \
        "Glamorous by Fergie"
    with pytest.raises(MusicCheckError):
        parse_verdict('{"verdict": "maybe"}')
    assert MUSIC_SCHEMA["properties"]["verdict"]["enum"] == ["likely", "possible", "none"]
    from gta6clips.llm.music import MusicEvidence

    text = evidence_text(MusicEvidence("TheBurntPeanut", "A Song Demands a New Car", "GTA 6 day 3", 69.4,
                                       [(4.8, "glamorous. The glamorous,")], ["song?"]))
    assert "[4.8] glamorous. The glamorous," in text and "song?" in text and "GTA 6 day 3" in text
    assert note_for(MusicVerdict("likely", "", "")).startswith("Likely copyrighted music: a licensed song may")
