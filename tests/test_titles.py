import json
from types import SimpleNamespace

import pytest

from gta6clips.llm import ClaudeTitleWriter, ClipBrief, TitleWriterError
from gta6clips.llm.titles import parse_suggestions

BRIEF = ClipBrief(streamer="The Burnt Peanut", source_title="GTA 6 day 1", clipskitty_title="Car flip",
                  clipskitty_hook="no way", transcript="he flipped the car", duration_s=28)


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _client(text="", stop_reason="end_turn", stop_details=None):
    response = SimpleNamespace(stop_reason=stop_reason, stop_details=stop_details,
                               content=[SimpleNamespace(type="text", text=text)])
    messages = FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_writer_returns_three_titles_and_sends_schema():
    client, messages = _client(json.dumps({"titles": ["A", "B", "C"], "hook": "He did NOT expect that 💀"}))
    result = ClaudeTitleWriter(client=client).write(BRIEF)
    assert result.titles == ["A", "B", "C"] and result.hook.startswith("He did NOT")
    sent = messages.kwargs
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["fallbacks"] == "default"
    assert "he flipped the car" in sent["messages"][0]["content"]


def test_refusal_raises():
    client, _ = _client(stop_reason="refusal", stop_details=SimpleNamespace(category="other"))
    with pytest.raises(TitleWriterError, match="declined"):
        ClaudeTitleWriter(client=client).write(BRIEF)


def test_short_answer_is_padded_with_clipskitty_title():
    result = parse_suggestions(json.dumps({"titles": ["Only one"], "hook": ""}), "Car flip")
    assert result.titles == ["Only one", "Car flip"]


def test_titles_are_trimmed_and_deduplicated():
    raw = json.dumps({"titles": ['  "Same"  ', "same", "x" * 150, "Other"], "hook": "h"})
    result = parse_suggestions(raw, "")
    assert result.titles[0] == "Same" and len(result.titles[1]) == 100 and len(result.titles) == 3


def test_invalid_json_raises():
    with pytest.raises(TitleWriterError):
        parse_suggestions("not json", "fallback")
