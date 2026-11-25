"""The editor's sound library, sound moves in plans, where they land and the mixed render."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gta6clips.editing import PlanError, parse_plan, render
from gta6clips.editing.editor import CHECK_SCHEMA, EditBrief, brief_content, fixed_plan
from gta6clips.editing.plan import plan_schema
from gta6clips.editing.render import audio_inputs, build_graph, build_shots, sound_events
from gta6clips.editing.sounds import STARTER, Sound, SoundLibrary, make_starter

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg isn't installed")


def _plan(**kw):
    base = {"keep": [], "zooms": [], "replays": [], "freezes": [], "texts": [], "flashes": [], "notes": []}
    base.update(kw)
    return base


def _lib(tmp_path=Path(".")) -> SoundLibrary:
    return SoundLibrary([Sound("boom", "sfx", tmp_path / "boom.wav", 1.6, "a boom"),
                         Sound("whoosh", "sfx", tmp_path / "whoosh.wav", 0.6, "a whoosh"),
                         Sound("pop", "sfx", tmp_path / "pop.wav", 0.2, "a pop", gain=0.5),
                         Sound("beat", "music", tmp_path / "beat.mp3", 120.0, "chill beat", credit="Music: Beat")])


def test_plans_may_only_use_sounds_from_the_library():
    lib = _lib()
    plan = parse_plan(_plan(sounds=[{"at": 3, "sound": "boom", "volume": 5}],
                            texts=[{"start": 4, "end": 6, "text": "oof", "sound": "pop"}],
                            music={"track": "beat", "volume": 0.9, "start": 30}), 20, lib)
    assert plan.sounds[0].volume == 1.0 and plan.texts[0].sound == "pop"
    assert plan.music.track == "beat" and plan.music.volume == 0.35 and plan.music.start == 30
    with pytest.raises(PlanError, match="no sound effect 'vine-boom'"):
        parse_plan(_plan(sounds=[{"at": 3, "sound": "vine-boom"}]), 20, lib)
    with pytest.raises(PlanError, match="no music track 'boom'"):
        parse_plan(_plan(music={"track": "boom"}), 20, lib)         # an effect isn't music
    with pytest.raises(PlanError, match="no sound effect"):
        parse_plan(_plan(sounds=[{"at": 3, "sound": "boom"}]), 20)   # no library at all
    with pytest.raises(PlanError, match="too many"):
        parse_plan(_plan(sounds=[{"at": i, "sound": "boom"} for i in range(11)]), 20, lib)
    near_end = parse_plan(_plan(music={"track": "beat", "volume": 0.1, "start": 115}), 20, lib)
    assert near_end.music.start == 0.0


def test_sounds_land_at_output_times():
    lib = _lib()
    plan = parse_plan(_plan(keep=[[0, 5], [7, 12]], sounds=[{"at": 9, "sound": "boom", "volume": 0.8}],
                            replays=[{"start": 2, "end": 4, "speed": 0.5, "target": "game", "scale": 1.3,
                                      "label": "AGAIN", "sound": "whoosh"}],
                            freezes=[{"at": 8, "seconds": 0.5, "label": "BRUH", "sound": "pop"}]), 12, lib)
    shots = build_shots(plan, 12)
    events = [(round(t, 2), s.id, v) for t, s, v in sound_events(plan, shots, lib)]
    # kept 0-4 (4 s), the 4 s replay, 4-5, the cut 5-7, 7-8, the freeze (0.5 s), 8-12: 9 s lands at 11.5 s
    assert events == [(4.0, "whoosh", 0.7), (10.0, "pop", 0.7), (11.5, "boom", 0.8)]
    assert sound_events(plan, shots, None) == []


def test_the_mix_ducks_the_music_and_keeps_levels(tmp_path):
    lib = _lib(tmp_path)
    plan = parse_plan(_plan(sounds=[{"at": 2, "sound": "pop", "volume": 0.6}],
                            music={"track": "beat", "volume": 0.2, "start": 10}), 10, lib)
    shots = build_shots(plan, 10)
    graph, _ = build_graph(plan, shots, tmp_path, lib)
    assert "[1:a]" in graph and "[2:a]atrim=start=10.000:duration=10.000" in graph
    assert "sidechaincompress" in graph and "normalize=0" in graph
    # the pop, set against the clip's loudness: -16 (clip) + 4 (over it) + 20 log(0.6 / 0.7) + 20 (its own
    # -20 dB) - 6 (gain 0.5)
    assert "volume=0.6dB" in graph and "[acat]volume=-2.0dB[clip]" in graph
    loud, _ = build_graph(plan, shots, tmp_path, lib, clip_mean_db=-10.0)
    assert "volume=6.6dB" in loud                                          # a louder clip: a louder pop
    assert "adelay=delays=2000:all=1" in graph
    assert audio_inputs(plan, shots, lib) == [(tmp_path / "pop.wav", False), (tmp_path / "beat.mp3", True)]
    quiet, _ = build_graph(parse_plan(_plan(), 10, lib), shots, tmp_path, lib)
    assert "[acat]anull[aout]" in quiet


def test_claude_is_told_the_sounds_and_held_to_their_ids():
    lib = _lib()
    schema = plan_schema(lib.ids("sfx"), lib.ids("music"))
    assert schema["properties"]["sounds"]["items"]["properties"]["sound"]["enum"] == ["boom", "whoosh", "pop"]
    assert schema["properties"]["replays"]["items"]["properties"]["sound"]["enum"] == ["", "boom", "whoosh", "pop"]
    assert schema["properties"]["music"]["properties"]["track"]["enum"] == ["", "beat"]   # "": no music
    assert "sounds" not in plan_schema()["properties"]                      # no library: no sound moves
    # the check sends its fix back as JSON text (a nested plan schema was "too large" for the API)
    assert CHECK_SCHEMA["properties"]["fixed_plan_json"] == {"type": "string"}
    assert fixed_plan({"fixed_plan_json": '{"keep": []}'}) == {"keep": []}
    assert fixed_plan({"fixed_plan_json": ""}) is None and fixed_plan({"fixed_plan_json": "[1]"}) is None
    brief = EditBrief(1, "x", "t", 10.0, [], [], [], None, sounds=lib.describe(), sfx_ids=("boom",))
    text = brief_content(brief)[-1]["text"]
    assert "- boom, 1.6 s: a boom" in text and "- beat, 120.0 s: chill beat" in text
    assert "No sound library" in brief_content(EditBrief(1, "x", "t", 10.0, [], [], [], None))[-1]["text"]


@needs_ffmpeg
def test_starter_pack_and_a_mixed_render(tmp_path):
    ffmpeg = Path(shutil.which("ffmpeg"))
    folder = tmp_path / "sounds"
    assert len(make_starter(folder, ffmpeg)) == len(STARTER)
    assert make_starter(folder, ffmpeg) == []                              # already there
    subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=20",
                    "-ac", "2", str(folder / "music" / "Chill_Beat.wav")], check=True)
    (folder / "sounds.yaml").write_text("music/Chill_Beat.wav: {description: calm loop, credit: 'Music: Test'}\n",
                                        encoding="utf-8")
    lib = SoundLibrary.load(folder, ffmpeg)
    assert lib.get("starter-boom", "sfx").description.startswith("deep cinematic boom")
    beat = lib.get("chill-beat", "music")
    assert beat.description == "calm loop" and beat.credit == "Music: Test" and beat.seconds == pytest.approx(20)

    src = tmp_path / "clip.mp4"
    subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:d=8",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=8:sample_rate=48000", "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    plan = parse_plan(_plan(sounds=[{"at": 2, "sound": "starter-boom", "volume": 0.9}],
                            texts=[{"start": 4, "end": 6, "text": "oof", "sound": "starter-pop"}],
                            music={"track": "chill-beat", "volume": 0.15, "start": 1}), 8, lib)
    out = render(ffmpeg, src, plan, tmp_path / "edit.mp4", duration_s=8, sounds=lib)
    probe = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(out), "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
    hms = probe.split("Duration: ")[1].split(",")[0]
    assert float(hms.split(":")[2]) == pytest.approx(8, abs=0.2)           # the music doesn't make it longer
    peak = float(probe.split("max_volume: ")[1].split(" dB")[0])
    assert peak <= -0.3                                                    # the limiter caught the boom
