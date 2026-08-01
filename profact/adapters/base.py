from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class CaptureEvent:
    role: str
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    temperature: float | None = None
    timestamp: str = ""
    usage: dict[str, int] | None = None


class CaptureAdapter(Protocol):
    def start(self) -> None: ...

    def on_event(self, callback: Callable[[CaptureEvent], None]) -> None: ...

    def stop(self) -> None: ...
