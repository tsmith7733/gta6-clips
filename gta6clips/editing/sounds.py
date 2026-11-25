"""The editor's sound library: sound effects and music the editor may put into an edit.

The library is a folder (config.yaml -> editor.sounds_dir, default `sounds/`) with two subfolders:
`sfx/` (short effects: booms, whooshes, pops) and `music/` (background tracks). Any audio file
dropped there is used; `sounds.yaml` in the folder can describe a file for Claude and give its
credit line, which a track's licence may ask for (YouTube Audio Library, Pixabay):

    sfx/whoosh-6316.mp3: {description: "fast airy whoosh"}
    music/late-night-drive.mp3: {description: "chill synthwave, 90 bpm", credit: "Music: Late Night Drive by ..."}
    sfx/too-loud.wav: {volume: 0.5}                   # scales this file's loudness

Without a description Claude sees the file name. Only use sounds and music you're allowed to put
in a YouTube video: otherwise Content ID claims the Short.

`make_starter` writes a small starter pack of effects synthesised by ffmpeg (ours, no licence
needed), so the editor has sounds before anything is downloaded.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..media import _popen_kwargs

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
KINDS = ("sfx", "music")


@dataclass(frozen=True)
class Sound:
    id: str
    kind: str                 # sfx | music
    path: Path
    seconds: float
    description: str
    credit: str = ""
    gain: float = 1.0
    mean_db: float | None = None   # its loudness (ffmpeg volumedetect), so the mix can match it to the clip


# The starter pack: (file name, description, aevalsrc expression, seconds, extra filters).
# Expressions are mono; t is seconds. random(0) is noise in 0-1.
STARTER = [
    ("starter-boom.wav", "deep cinematic boom with a hard hit, for a crash or big impact",
     "0.85*exp(-3.2*t)*sin(2*PI*(120*t-32*t*t))+0.5*exp(-35*t)*(2*random(0)-1)", 1.6, "lowpass=f=1200"),
    ("starter-hit.wav", "short punchy hit, for a smaller bump or a hard cut",
     "0.9*exp(-16*t)*sin(2*PI*(95*t-30*t*t))+0.55*exp(-55*t)*(2*random(0)-1)", 0.4, "lowpass=f=3000"),
    ("starter-whoosh.wav", "airy whoosh, into a replay, a punch-in or a transition",
     "0.9*pow(sin(PI*t/0.65),2)*(2*random(0)-1)", 0.65, "highpass=f=350,lowpass=f=3800"),
    ("starter-riser.wav", "rising tension sweep (2 s) that ends on the moment, before a payoff",
     "0.45*pow(t/2,2)*sin(2*PI*(180*t+260*t*t))+0.25*pow(t/2,3)*(2*random(0)-1)", 2.0, "lowpass=f=6000"),
    ("starter-pop.wav", "small bubbly pop, when a text appears",
     "0.7*exp(-45*t)*sin(2*PI*(950*t-1700*t*t))", 0.16, ""),
    ("starter-ding.wav", "bright bell ding, for a count (FAIL #2) or a win",
     "0.45*exp(-3*t)*(sin(2*PI*1568*t)+0.4*sin(2*PI*3136*t)+0.15*sin(2*PI*4704*t))", 1.3, ""),
    ("starter-sting.wav", "dark dramatic 'dun' chord, for a shock or 'uh oh' moment",
     "0.28*exp(-1.1*t)*(sin(2*PI*110*t)+sin(2*PI*130.81*t)+sin(2*PI*164.81*t)+0.5*sin(2*PI*55*t))", 2.6,
     "lowpass=f=2500"),
    # Sad trombone: four falling notes, the last one wobbling; st(0) is the note's pitch.
    ("starter-fail.wav", "sad 'wah wah wah waaah' fail tune (2.6 s), for a fail at the end",
     "st(0,if(lt(t,0.45),293.66,if(lt(t,0.9),277.18,if(lt(t,1.35),261.63,246.94*(1+0.025*sin(2*PI*5.5*t))))));"
     "st(1,if(lt(t,1.35),sin(PI*mod(t,0.45)/0.45),exp(-1.4*(t-1.35))*min(1,(t-1.35)*20)));"
     "0.3*ld(1)*(sin(2*PI*ld(0)*t)+0.55*sin(4*PI*ld(0)*t)+0.3*sin(6*PI*ld(0)*t))", 2.6, "lowpass=f=2200"),
]
STARTER_DESCRIPTIONS = {name: desc for name, desc, *_ in STARTER}


def make_starter(folder: Path, ffmpeg: Path) -> list[Path]:
    """Writes the starter effects that are missing into folder/sfx. Returns the files written."""
    sfx = folder / "sfx"
    sfx.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        (folder / kind).mkdir(exist_ok=True)
    written = []
    for name, _, expr, seconds, extra in STARTER:
        dest = sfx / name
        if dest.is_file():
            continue
        chain = ",".join(x for x in (extra, "afade=t=out:st={:.3f}:d=0.05".format(seconds - 0.05),
                                     "aformat=channel_layouts=stereo") if x)
        src = f"aevalsrc='{expr}':s=48000:d={seconds}"   # quoted: the commas and ; stay in the expression
        proc = subprocess.run([str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", src,
                               "-af", chain, "-c:a", "pcm_s16le", str(dest)], capture_output=True, text=True,
                              **_popen_kwargs())
        if proc.returncode != 0:
            log.warning("Couldn't make the starter sound %s: %s", name, proc.stderr.strip()[-300:])
            continue
        written.append(dest)
    return written


_DURATION = re.compile(r"Duration: (\d+):(\d+):([\d.]+)")
_MEAN = re.compile(r"mean_volume: (-?[\d.]+) dB")
LEVELS_FILE = ".levels.json"      # cache of each file's length and loudness, by name, size and mtime


def audio_info(ffmpeg: Path, path: Path) -> tuple[float, float | None] | None:
    """(seconds, mean volume in dB) of an audio file, or None if ffmpeg can't read its sound."""
    proc = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null",
                           "-"], capture_output=True, text=True, errors="replace", **_popen_kwargs())
    m = _DURATION.search(proc.stderr or "")
    if not m or "Audio" not in proc.stderr:
        return None
    mean = _MEAN.search(proc.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]), float(mean[1]) if mean else None


def mean_volume(ffmpeg: Path, path: Path) -> float | None:
    info = audio_info(ffmpeg, path)
    return info[1] if info else None


def _slug(stem: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:40] or "sound"


class SoundLibrary:
    def __init__(self, sounds: list[Sound] | None = None):
        self.sounds: dict[str, Sound] = {s.id: s for s in sounds or []}

    @classmethod
    def load(cls, folder: Path, ffmpeg: Path) -> "SoundLibrary":
        meta: dict = {}
        info = folder / "sounds.yaml"
        if info.is_file():
            try:
                meta = yaml.safe_load(info.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                log.warning("%s isn't valid YAML (%s); using the sounds without their descriptions.", info, exc)
        sounds: list[Sound] = []
        seen: set[str] = set()
        cache_file = folder / LEVELS_FILE
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.is_file() else {}
        except (OSError, ValueError):
            cache = {}
        fresh: dict = {}
        for kind in KINDS:
            for path in sorted((folder / kind).glob("*")) if (folder / kind).is_dir() else []:
                if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
                    continue
                st = path.stat()
                key = f"{kind}/{path.name}"
                hit = cache.get(key)
                if hit and hit.get("size") == st.st_size and hit.get("mtime") == st.st_mtime:
                    seconds, mean = hit["seconds"], hit.get("mean_db")
                else:
                    info = audio_info(ffmpeg, path)
                    if not info or not info[0]:
                        log.warning("Skipping %s: ffmpeg can't read its sound.", path)
                        continue
                    seconds, mean = info
                fresh[key] = {"size": st.st_size, "mtime": st.st_mtime, "seconds": seconds, "mean_db": mean}
                m = meta.get(f"{kind}/{path.name}") or meta.get(path.name) or {}
                m = m if isinstance(m, dict) else {"description": str(m)}
                sid = base = _slug(path.stem)
                n = 2
                while sid in seen:
                    sid, n = f"{base}-{n}", n + 1
                seen.add(sid)
                desc = str(m.get("description") or STARTER_DESCRIPTIONS.get(path.name)
                           or path.stem.replace("_", " ").replace("-", " "))
                try:
                    gain = max(0.05, min(4.0, float(m.get("volume", 1.0))))
                except (TypeError, ValueError):
                    gain = 1.0
                sounds.append(Sound(sid, kind, path, round(seconds, 2), desc[:200], str(m.get("credit") or "")[:300],
                                    gain, mean))
        if fresh != cache:
            try:
                cache_file.write_text(json.dumps(fresh, indent=1), encoding="utf-8")
            except OSError:
                pass
        return cls(sounds)

    def ids(self, kind: str) -> list[str]:
        return [s.id for s in self.sounds.values() if s.kind == kind]

    def get(self, sid: str, kind: str | None = None) -> Sound | None:
        s = self.sounds.get(sid)
        return s if s is not None and (kind is None or s.kind == kind) else None

    def describe(self) -> str:
        """The list Claude gets with each brief."""
        out = []
        for kind, title in (("sfx", "Sound effects"), ("music", "Music tracks")):
            items = [s for s in self.sounds.values() if s.kind == kind]
            if items:
                out.append(f"{title} (id, length: what it is):")
                out += [f"- {s.id}, {s.seconds:.1f} s: {s.description}" for s in items]
        return "\n".join(out)
