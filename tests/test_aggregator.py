from __future__ import annotations

import json
from pathlib import Path

from profact.adapters.base import CaptureEvent
from profact.aggregator import (
    CONTINUATION,
    NEW_TURN,
    RESPONSE,
    Turn,
    TurnAggregator,
    classify,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _user_text(text: str = "hi", ts: str = "2026-06-20T00:00:00+00:00") -> CaptureEvent:
    return CaptureEvent("user", [{"type": "text", "text": text}], "claude-x", 1.0, ts)


def _assistant(ts: str = "2026-06-20T00:00:01+00:00") -> CaptureEvent:
    return CaptureEvent("assistant", [{"type": "text", "text": "ok"}], "claude-x", None, ts)


def _tool_result(ts: str = "2026-06-20T00:00:02+00:00") -> CaptureEvent:
    return CaptureEvent(
        "user", [{"type": "tool_result", "tool_use_id": "t", "content": "done"}], "claude-x", 1.0, ts
    )


def test_classify_distinguishes_message_kinds():
    assert classify(_user_text()) == NEW_TURN
    assert classify(_tool_result()) == CONTINUATION
    assert classify(_assistant()) == RESPONSE


def test_one_prompt_with_tool_loop_is_one_version():
    opens: list[Turn] = []
    agg = TurnAggregator(on_open=opens.append)
    sequence = [
        _user_text("create login"),
        _assistant(),
        _tool_result(),
        _assistant(),
        _tool_result(),
        _assistant(),
        _user_text("make it blue"),
    ]
    for event in sequence:
        agg.handle(event)
    assert len(opens) == 2


def test_continuations_and_responses_are_absorbed():
    closed: list[Turn] = []
    agg = TurnAggregator(on_close=closed.append)
    for event in [_user_text(), _assistant(), _tool_result(), _assistant(), _tool_result(), _assistant()]:
        agg.handle(event)
    agg.flush()
    assert len(closed) == 1
    turn = closed[0]
    assert len(turn.continuations) == 2
    assert len(turn.responses) == 3
    assert turn.prompt_text == "hi"
    assert turn.model == "claude-x"
    assert turn.temperature == 1.0


def test_idle_finalizes_open_turn():
    closed: list[Turn] = []
    agg = TurnAggregator(on_close=closed.append, idle_timeout=60.0)
    agg.handle(_user_text(ts="2026-06-20T00:00:00+00:00"))
    assert agg.check_idle("2026-06-20T00:00:30+00:00") is False
    assert agg.check_idle("2026-06-20T00:02:00+00:00") is True
    assert len(closed) == 1
    assert closed[0].close_reason == "idle"


def test_fixture_sequence_produces_two_versions():
    data = json.loads((FIXTURES / "claude_turn_sequence.json").read_text())
    opens: list[Turn] = []
    agg = TurnAggregator(on_open=opens.append)
    for raw in data["events"]:
        agg.handle(
            CaptureEvent(
                role=raw["role"],
                content_blocks=raw["content_blocks"],
                model=raw.get("model"),
                temperature=raw.get("temperature"),
                timestamp=raw.get("timestamp", ""),
            )
        )
    agg.flush()
    assert len(opens) == 2
