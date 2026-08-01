from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .adapters.base import CaptureEvent

NEW_TURN = "new_turn"
CONTINUATION = "continuation"
RESPONSE = "response"
IGNORE = "ignore"

DEFAULT_IDLE_TIMEOUT = 60.0
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def classify(event: CaptureEvent) -> str:
    if event.role == "assistant":
        return RESPONSE
    if event.role == "user":
        if _has_text_block(event.content_blocks):
            return NEW_TURN
        if _is_tool_result_only(event.content_blocks):
            return CONTINUATION
    return IGNORE


def _has_text_block(blocks: list[dict]) -> bool:
    return any(block.get("type") == "text" for block in blocks)


def _is_tool_result_only(blocks: list[dict]) -> bool:
    return bool(blocks) and all(block.get("type") == "tool_result" for block in blocks)


@dataclass
class Turn:
    index: int
    prompt: CaptureEvent
    responses: list[CaptureEvent] = field(default_factory=list)
    continuations: list[CaptureEvent] = field(default_factory=list)
    opened_at: str = ""
    closed_at: str | None = None
    close_reason: str | None = None

    @property
    def prompt_text(self) -> str:
        return "".join(
            block.get("text", "")
            for block in self.prompt.content_blocks
            if block.get("type") == "text"
        )

    @property
    def model(self) -> str | None:
        if self.prompt.model:
            return self.prompt.model
        return self.responses[0].model if self.responses else None

    @property
    def temperature(self) -> float | None:
        if self.prompt.temperature is not None:
            return self.prompt.temperature
        return self.responses[0].temperature if self.responses else None

    @property
    def usage_totals(self) -> dict[str, int] | None:
        """Sum provider-reported usage across API calls in this prompt turn."""
        reported = [event.usage for event in self.responses if event.usage is not None]
        if not reported:
            return None
        totals = {}
        for key in USAGE_KEYS:
            values = [usage[key] for usage in reported if key in usage]
            if values:
                totals[key] = sum(values)
        totals["api_call_count"] = len(reported)
        return totals


class TurnAggregator:
    """Groups a stream of CaptureEvents into one Turn per user-typed prompt.

    A user message with a text block opens a new turn; a user message with only
    tool_result blocks is a tool-loop continuation; assistant messages are
    responses. Turns close on the next new turn, on idle, or on flush.
    """

    def __init__(
        self,
        on_open: Callable[[Turn], None] | None = None,
        on_close: Callable[[Turn], None] | None = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self._on_open = on_open
        self._on_close = on_close
        self.idle_timeout = idle_timeout
        self._current: Turn | None = None
        self._count = 0
        self._last_activity: str | None = None

    @property
    def current(self) -> Turn | None:
        return self._current

    @property
    def turn_count(self) -> int:
        return self._count

    def handle(self, event: CaptureEvent) -> None:
        kind = classify(event)
        if kind == NEW_TURN:
            self._close_current("new_turn")
            self._open(event)
        elif kind == CONTINUATION and self._current is not None:
            self._current.continuations.append(event)
        elif kind == RESPONSE and self._current is not None:
            self._current.responses.append(event)
        if event.timestamp:
            self._last_activity = event.timestamp

    def check_idle(self, now: str) -> bool:
        if self._current is None or self._last_activity is None:
            return False
        if _seconds_between(self._last_activity, now) >= self.idle_timeout:
            self._close_current("idle")
            return True
        return False

    def flush(self, reason: str = "session_end") -> None:
        self._close_current(reason)

    def _open(self, event: CaptureEvent) -> None:
        self._count += 1
        self._current = Turn(index=self._count, prompt=event, opened_at=event.timestamp)
        self._last_activity = event.timestamp
        if self._on_open is not None:
            self._on_open(self._current)

    def _close_current(self, reason: str) -> None:
        if self._current is None:
            return
        closed = self._current
        closed.closed_at = self._last_activity
        closed.close_reason = reason
        self._current = None
        if self._on_close is not None:
            self._on_close(closed)


def _seconds_between(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
