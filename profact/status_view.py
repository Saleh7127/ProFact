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
    open_turn: bool


def inspect(repo_root: Path, *, include_dirty: bool = True) -> StatusReport:
    """Read branch-local status, optionally snapshotting the working tree."""
    branch = git_ops.current_branch(repo_root)
    head_id = store.read_head(repo_root, branch, seed=False)
    head = None
    visible_versions = 0
    test_result = None

    if head_id is not None:
        conn = store.connect(repo_root)
        try:
            head = store.get_version(conn, head_id)
            visible_versions = len(
                store.list_branch_versions(conn, branch, head=head_id)
            )
            test_result = store.get_test_result(conn, head_id)
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
        open_turn=bool(
            head is not None and head.type == "prompt" and head.tree_after is None
        ),
    )