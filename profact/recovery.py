"""Explicit recovery for prompt turns left open after an interrupted capture."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import git_ops, store

INCOMPLETE_CONFIDENCE = "incomplete"


class RecoveryError(RuntimeError):
    """A user-facing recovery validation failure."""


@dataclass(frozen=True)
class RecoveryCandidate:
    version: store.Version
    is_current_branch: bool
    is_branch_head: bool

    @property
    def can_finalize(self) -> bool:
        return self.is_current_branch and self.is_branch_head


@dataclass(frozen=True)
class RecoveryResult:
    version: store.Version
    action: str  # "finalized" | "preserved"


def audit(repo_root: Path) -> list[RecoveryCandidate]:
    """Describe every dangling turn without mutating history or files."""
    current_branch = git_ops.current_branch(repo_root)
    conn = store.connect(repo_root)
    try:
        turns = store.dangling_open_turns(conn)
        heads = store.all_heads(conn)
    finally:
        conn.close()
    return [
        RecoveryCandidate(
            version=version,
            is_current_branch=version.git_branch == current_branch,
            is_branch_head=heads.get(version.git_branch) == version.id,
        )
        for version in turns
    ]


def finalize(repo_root: Path, version_id: str) -> RecoveryResult:
    """Finalize the current branch's dangling HEAD from the working tree.

    The result is marked uncertain because a crash removes the evidence needed
    to distinguish agent writes from edits made afterward.
    """
    branch = git_ops.current_branch(repo_root)
    conn = store.connect(repo_root)
    try:
        version = _require_dangling(conn, version_id)
        if version.git_branch != branch:
            raise RecoveryError(
                f"cannot finalize {version.id} from branch '{branch}'; "
                f"switch to '{version.git_branch}' first"
            )
        head_id = store.read_head(repo_root, branch, seed=False)
        if head_id != version.id:
            raise RecoveryError(
                f"cannot finalize {version.id} from the working tree because "
                f"it is not branch '{branch}' HEAD"
            )

        tree_after = git_ops.write_tree(repo_root)
        diff = git_ops.diff_trees(repo_root, version.tree_before, tree_after)
        diff_ref = store.write_object(repo_root, diff) if diff.strip() else None
        cursor = conn.execute(
            "UPDATE versions SET tree_after = ?, diff_ref = ?, confidence = ?, "
            "confidence_reason = COALESCE(confidence_reason, ?), "
            "thinking_source = COALESCE(thinking_source, ?) "
            "WHERE id = ? AND type = 'prompt' AND tree_after IS NULL",
            (
                tree_after,
                diff_ref,
                "uncertain",
                "turn finalized after interrupted capture",
                store.THINKING_NONE,
                version.id,
            ),
        )
        if cursor.rowcount != 1:
            raise RecoveryError(f"{version.id} is no longer a dangling prompt turn")
        conn.commit()
        recovered = store.get_version(conn, version.id)
        assert recovered is not None
        return RecoveryResult(recovered, "finalized")
    finally:
        conn.close()


def preserve(repo_root: Path, version_id: str) -> RecoveryResult:
    """Close a dangling turn without attributing current files to the prompt.

    The prompt and any captured response/reasoning remain immutable history.
    ``tree_after`` is set to the turn's start tree, so current working-tree
    changes remain untouched and can be recorded separately as human work.
    """
    conn = store.connect(repo_root)
    try:
        version = _require_dangling(conn, version_id)
        cursor = conn.execute(
            "UPDATE versions SET tree_after = tree_before, diff_ref = NULL, "
            "confidence = ?, thinking_source = COALESCE(thinking_source, ?) "
            "WHERE id = ? AND type = 'prompt' AND tree_after IS NULL",
            (INCOMPLETE_CONFIDENCE, store.THINKING_NONE, version.id),
        )
        if cursor.rowcount != 1:
            raise RecoveryError(f"{version.id} is no longer a dangling prompt turn")
        conn.commit()
        recovered = store.get_version(conn, version.id)
        assert recovered is not None
        return RecoveryResult(recovered, "preserved")
    finally:
        conn.close()


def _require_dangling(conn, version_id: str) -> store.Version:
    version = store.get_version(conn, version_id)
    if version is None:
        raise RecoveryError(f"no such version '{version_id}'")
    if version.type != "prompt" or version.tree_after is not None:
        raise RecoveryError(f"{version.id} is not a dangling prompt turn")
    return version