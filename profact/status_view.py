"""Structured repository and branch status for CLI and interactive views."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import git_ops, store


@dataclass(frozen=True)
class StatusReport:
    branch: str
    head_id: str | None
    head: store.Version | None
    visible_versions: int
    test_result: store.TestResult | None
    dirty: bool | None
    open_turns: tuple[store.Version, ...]

    @property
    def open_turn(self) -> bool:
        """Compatibility shorthand for callers that only need a boolean."""
        return bool(self.open_turns)


def inspect(repo_root: Path, *, include_dirty: bool = True) -> StatusReport:
    """Read branch-local status, optionally snapshotting the working tree."""
    branch = git_ops.current_branch(repo_root)
    head_id = store.read_head(repo_root, branch, seed=False)
    head = None
    visible_versions = 0
    test_result = None
    conn = store.connect(repo_root)
    try:
        if head_id is not None:
            head = store.get_version(conn, head_id)
            visible_versions = len(
                store.list_branch_versions(conn, branch, head=head_id)
            )
            test_result = store.get_test_result(conn, head_id)
        open_turns = tuple(store.open_prompts(conn, git_branch=branch))
    finally:
        conn.close()

    dirty = None
    if include_dirty and head is not None:
        head_tree = head.tree_after or head.tree_before
        dirty = git_ops.write_tree(repo_root) != head_tree

    return StatusReport(
        branch=branch,
        head_id=head_id,
        head=head,
        visible_versions=visible_versions,
        test_result=test_result,
        dirty=dirty,
        open_turns=open_turns,
    )


def session_label(version: store.Version, *, max_length: int = 22) -> str:
    """Compact, non-secret session label for status and recovery views."""
    label = version.conversation_id or "legacy"
    if len(label) <= max_length:
        return label
    return label[: max_length - 1] + "…"