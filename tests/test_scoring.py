from gta6clips.scoring import (
    ScoringSettings,
    Spike,
    WindowSettings,
    find_spikes,
    plan_windows,
    rolling_z,
    score_stream,
)

KEYWORDS = ("kekw", "clip it", "re:\\?{3,}")
SETTINGS = ScoringSettings(keywords=KEYWORDS)
HOUR = 3600.0


def steady_chat(every_s: float = 5.0, duration: float = HOUR, text: str = "nice") -> list[tuple[float, str]]:
    return [(t * every_s, text) for t in range(int(duration / every_s))]


def burst(at: float, n: int = 40, text: str = "KEKW", over_s: float = 10.0) -> list[tuple[float, str]]:
    return [(at + i * over_s / n, text) for i in range(n)]


def chat_with_burst(at: float = 1800.0, **kwargs):
    return sorted(steady_chat() + burst(at, **kwargs))


def test_chat_burst_is_one_spike_near_the_burst():
    scores = score_stream(SETTINGS, duration_s=HOUR, chat=chat_with_burst(), loudness=None)
    spikes = find_spikes(scores, SETTINGS.threshold)
    assert len(spikes) == 1
    assert 1795 <= spikes[0].peak_s <= 1815
    assert spikes[0].signals["keywords"] > 2


def test_steady_chat_has_no_spikes():
    scores = score_stream(SETTINGS, duration_s=HOUR, chat=steady_chat(every_s=1.0), loudness=None)
    assert find_spikes(scores, SETTINGS.threshold) == []


def test_baseline_is_per_channel():
    """A burst that's huge for a small chat is normal for a big one."""
    small = sorted(steady_chat(every_s=5.0) + burst(1800, n=30, text="wow"))
    big = sorted(steady_chat(every_s=0.05) + burst(1800, n=30, text="wow"))
    assert find_spikes(score_stream(SETTINGS, duration_s=HOUR, chat=small, loudness=None), 2.0)
    assert not find_spikes(score_stream(SETTINGS, duration_s=HOUR, chat=big, loudness=None), 2.0)


def test_no_spike_during_warm_up():
    scores = score_stream(SETTINGS, duration_s=HOUR, chat=chat_with_burst(at=30.0), loudness=None)
    assert find_spikes(scores, SETTINGS.threshold) == []


def test_audio_alone_can_spike():
    bins = int(HOUR / SETTINGS.bin_s)
    loud = [-30.0 + (i % 3) for i in range(bins)]
    for i in range(400, 404):
        loud[i] = -8.0
    scores = score_stream(SETTINGS, duration_s=HOUR, chat=None, loudness=loud)
    assert set(scores.z) == {"audio"}
    spikes = find_spikes(scores, SETTINGS.threshold)
    assert len(spikes) == 1 and 2000 <= spikes[0].peak_s <= 2020


def test_rolling_z_caps_and_floors_spread():
    z = rolling_z([0.0] * 50 + [100.0], baseline_bins=20, min_bins=10, min_spread=1.0, z_cap=6.0)
    assert z[:10] == [0.0] * 10
    assert z[-1] == 6.0


def _spike(t: float, score: float = 3.0) -> Spike:
    return Spike(t, score, {"chat": score})


def test_windows_have_lead_in_and_tail_and_stay_inside_the_video():
    w = plan_windows([_spike(1000)], WindowSettings(lead_in_s=120, tail_s=60), duration_s=HOUR)
    assert (w[0].start_s, w[0].end_s) == (880, 1060)
    early = plan_windows([_spike(30)], WindowSettings(), duration_s=HOUR)[0]
    late = plan_windows([_spike(HOUR - 10)], WindowSettings(), duration_s=HOUR)[0]
    assert early.start_s == 0 and late.end_s == HOUR


def test_overlapping_spikes_merge_up_to_max_length():
    settings = WindowSettings(lead_in_s=120, tail_s=60, max_s=240)
    merged = plan_windows([_spike(1000, 5), _spike(1040, 3)], settings, HOUR)
    assert len(merged) == 1 and (merged[0].start_s, merged[0].end_s) == (880, 1100)
    # Too far apart to fit in one window, but overlapping: the weaker spike is dropped.
    dropped = plan_windows([_spike(1000, 5), _spike(1150, 3)], settings, HOUR)
    assert len(dropped) == 1 and dropped[0].peak_s == 1000


def test_keeps_only_the_strongest_windows_in_stream_order():
    spikes = [_spike(600 * i, score=i) for i in range(1, 6)]
    kept = plan_windows(spikes, WindowSettings(max_per_stream=2), duration_s=4 * HOUR)
    assert [w.peak_s for w in kept] == [2400, 3000]
