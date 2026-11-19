"""Chat replay parsing, ffmpeg loudness, and the database migration."""

import json
import shutil
import sqlite3
import subprocess
import sys

import pytest

from gta6clips import db
from gta6clips.chat import parse_youtube_live_chat
from gta6clips.media import (
    SILENCE_LUFS,
    MediaToolMissing,
    bin_loudness,
    find_ffmpeg,
    measure_loudness,
    parse_ebur128_line,
)


def _yt_line(offset_ms, runs, renderer="liveChatTextMessageRenderer"):
    return json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(offset_ms), "actions": [
        {"addChatItemAction": {"item": {renderer: {"message": {"runs": runs}, "authorName": {"simpleText": "x"}}}}}
    ]}})


def test_youtube_chat_replay_parsing():
    lines = [
        _yt_line(65_500, [{"text": "CLIP IT "}, {"emoji": {"emojiId": "😂", "shortcuts": [":joy:"]}}]),
        _yt_line(1_000, [{"emoji": {"emojiId": "UCxyz/abcdefghijklmnop", "shortcuts": [":peanut-wave:"]}}]),
        _yt_line(2_000, [{"text": "thanks!"}], renderer="liveChatPaidMessageRenderer"),
        _yt_line(3_000, [], renderer="liveChatViewerEngagementMessageRenderer"),  # not a chat message
        "not json",
        "",
        json.dumps({"replayChatItemAction": {"actions": []}}),  # no offset
    ]
    assert parse_youtube_live_chat(lines) == [(1.0, ":peanut-wave:"), (2.0, "thanks!"), (65.5, "CLIP IT 😂")]


def test_ebur128_line_parsing():
    line = "[Parsed_ebur128_0 @ 000001] t: 12.3      TARGET:-23 LUFS    M: -18.2 S: -20.1     I: -21.0 LUFS"
    assert parse_ebur128_line(line) == (12.3, -18.2)
    assert parse_ebur128_line("[Parsed_ebur128_0 @ 1] t: 0.1  TARGET:-23 LUFS  M:-inf S:-inf") == (0.1, SILENCE_LUFS)
    assert parse_ebur128_line("Stream #0:0: Audio: aac") is None


def test_bin_loudness_takes_the_loudest_reading():
    assert bin_loudness([(0.1, -30), (0.5, -20), (5.2, -40), (12.0, -10)], 5.0) == [-20, -40, -10]


def test_ffmpeg_from_config_must_exist(cfg, tmp_path):
    cfg.data["paths"]["ffmpeg"] = str(tmp_path / "nope" / "ffmpeg.exe")
    with pytest.raises(MediaToolMissing, match="paths.ffmpeg"):
        find_ffmpeg(cfg)
    folder = tmp_path / "ffmpeg-bin"
    folder.mkdir()
    exe = folder / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    exe.write_bytes(b"")
    cfg.data["paths"]["ffmpeg"] = str(folder)
    assert find_ffmpeg(cfg) == exe


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")
def test_real_ffmpeg_finds_the_loud_part(tmp_path):
    """10 s of quiet tone, 2 s loud, 8 s quiet: the loud bins stand out."""
    media = tmp_path / "tone.wav"
    expr = "if(between(t,10,12),0.9,0.01)*sin(2*PI*440*t)"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"aevalsrc='{expr}':s=16000:d=20", str(media)], check=True)  # quotes: the commas
    bins = measure_loudness(__import__("pathlib").Path(shutil.which("ffmpeg")), media, 2.0)
    assert len(bins) >= 9
    assert bins[5] > bins[2] + 20


def test_old_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE windows (id INTEGER PRIMARY KEY, streamer TEXT NOT NULL,
        source_url TEXT NOT NULL, source_title TEXT NOT NULL DEFAULT '', start_utc REAL, end_utc REAL,
        ck_job_id INTEGER, ck_video_id TEXT, state TEXT NOT NULL DEFAULT 'submitted',
        error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO windows (streamer, source_url, created_at, updated_at) VALUES ('p', 'u', 'x', 'x');""")
    old.commit()
    old.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(windows)")}
    assert {"stream_id", "offset_start_s", "offset_end_s", "score", "reasons", "local_path"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 1
    db.connect(path).close()  # running the migration twice is fine
    conn.close()
