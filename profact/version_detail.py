"""Structured version content shared by CLI and interactive frontends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import store

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
        prompt=_read(repo_root, version.prompt_ref),
        thinking=_read(repo_root, version.thinking_ref),
        response=_read(repo_root, version.response_ref),
        diff=_read(repo_root, version.diff_ref),
    )


def _read(repo_root: Path, reference: str | None) -> str | None:
    return store.read_object(repo_root, reference) if reference else None