"""Reconcile manual working-tree changes into one human version."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import git_ops, store


class SnapshotError(RuntimeError):
    """Raised when pending human edits cannot be snapshotted safely."""


@dataclass(frozen=True)
class ReconciliationResult:
    """The current tree and any human version created for it."""

    tree: str
    version: store.Version | None


def reconcile(
    repo_root: Path,
    branch: str | None = None,
    *,
    current_tree: str | None = None,
) -> ReconciliationResult:
    """Capture the net manual delta since the finalized branch HEAD.

    Repeated saves and edits are deliberately collapsed into one immutable
    ``human`` version. An open prompt is never split because its concurrent
    changes cannot be attributed reliably from tree snapshots alone.
    """
    branch = branch or git_ops.current_branch(repo_root)
    tree_after = current_tree or git_ops.write_tree(repo_root)
    head_id = store.read_head(repo_root, branch)
    if head_id is None:
        return ReconciliationResult(tree=tree_after, version=None)

    with store.connect(repo_root) as conn:
        if store.open_prompts(conn):
            return ReconciliationResult(tree=tree_after, version=None)
        parent = store.get_version(conn, head_id)
        if parent is None or parent.tree_after is None:
            return ReconciliationResult(tree=tree_after, version=None)

        tree_before = parent.tree_after
        if tree_before == tree_after:
            return ReconciliationResult(tree=tree_after, version=None)

        diff = git_ops.diff_trees(repo_root, tree_before, tree_after)
        diff_ref = store.write_object(repo_root, diff) if diff.strip() else None
        version = store.create_version(
            conn,
            type="human",
            git_branch=branch,
            parent_id=head_id,
            tree_before=tree_before,
            tree_after=tree_after,
            diff_ref=diff_ref,
        )

    store.write_head(repo_root, branch, version.id)
    return ReconciliationResult(tree=tree_after, version=version)


def snapshot(repo_root: Path, branch: str | None = None) -> ReconciliationResult:
    """Manually capture pending human edits on top of a finalized HEAD."""
    branch = branch or git_ops.current_branch(repo_root)
    head_id = store.read_head(repo_root, branch)
    if head_id is None:
        raise SnapshotError("no finalized HEAD exists to use as a snapshot baseline")

    with store.connect(repo_root) as conn:
        open_turns = store.open_prompts(conn)
        parent = store.get_version(conn, head_id)
    if open_turns:
        identifiers = ", ".join(version.id for version in open_turns)
        raise SnapshotError(
            f"cannot snapshot while prompt turn(s) {identifiers} are still open; "
            "finish or recover them first"
        )
    if parent is None:
        raise SnapshotError(f"HEAD references missing version {head_id}")
    if parent.tree_after is None:
        raise SnapshotError(
            f"cannot snapshot while prompt {parent.id} is still open; "
            "finish or stop the active prompt first"
        )

    return reconcile(repo_root, branch)
