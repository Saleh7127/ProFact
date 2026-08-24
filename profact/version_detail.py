"""Structured version content shared by CLI and interactive frontends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import prompt_text, store

NO_THINKING_PLACEHOLDER = (
    "(no reasoning captured — the model has no thinking capability, "
    "or none was emitted for this turn)"
)


@dataclass(frozen=True)
class VersionDetail:
    version: store.Version
    is_head: bool
    tags: tuple[str, ...]
    test_result: store.TestResult | None
    test_output: str | None
    prompt: str | None
    thinking: str | None
    response: str | None
    diff: str | None

    def section(self, name: str) -> str:
        """Return one display-ready content section without a heading."""
        if name == "prompt":
            return self.prompt or "(no prompt recorded)"
        if name == "thinking":
            if self.thinking:
                return self.thinking
            if self.version.thinking_source == store.THINKING_NONE:
                return NO_THINKING_PLACEHOLDER
            return "(no reasoning recorded)"
        if name == "response":
            return self.response or "(no response recorded)"
        if name == "diff":
            return self.diff or "(no file changes)"
        raise ValueError(f"unknown version detail section: {name}")


def load(
    repo_root: Path,
    version: store.Version,
    *,
    head_id: str | None,
    tags: tuple[str, ...] = (),
    test_result: store.TestResult | None = None,
) -> VersionDetail:
    """Load immutable content objects associated with ``version``."""
    return VersionDetail(
        version=version,
        is_head=version.id == head_id,
        tags=tags,
        test_result=test_result,
        test_output=_read(repo_root, test_result.output_ref) if test_result else None,
        prompt=prompt_text.user_request(_read(repo_root, version.prompt_ref)),
        thinking=_read(repo_root, version.thinking_ref),
        response=_read(repo_root, version.response_ref),
        diff=_read(repo_root, version.diff_ref),
    )


def _read(repo_root: Path, reference: str | None) -> str | None:
    return store.read_object(repo_root, reference) if reference else None


def completion_text(version: store.Version) -> str | None:
    if not version.completed_at:
        return None
    source = version.completion_source or "unknown source"
    duration = _duration_text(version.timestamp, version.completed_at)
    suffix = f", {duration}" if duration else ""
    return f"{version.completed_at} ({source}{suffix})"


def usage_text(version: store.Version) -> str | None:
    if version.type != "prompt":
        return None
    values = (
        version.input_tokens,
        version.output_tokens,
        version.cache_read_tokens,
        version.cache_creation_tokens,
        version.reasoning_tokens,
        version.api_call_count,
    )
    if all(value is None for value in values):
        return "not reported by capture source"
    parts: list[str] = []
    for label, value in (
        ("input", version.input_tokens),
        ("output", version.output_tokens),
        ("cache-read", version.cache_read_tokens),
        ("cache-write", version.cache_creation_tokens),
        ("reasoning", version.reasoning_tokens),
        ("api-calls", version.api_call_count),
    ):
        if value is not None:
            parts.append(f"{label}={value:,}")
    return "  ".join(parts)


def _duration_text(start: str, end: str) -> str | None:
    try:
        seconds = max(
            0,
            int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()),
        )
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"