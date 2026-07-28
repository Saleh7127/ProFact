"""Safe working-tree navigation between profact versions.

This module owns the checkout workflow: target resolution, cross-branch
protection, automatic checkpoints, tree restoration, and branch-local HEAD
movement.  The CLI supplies presentation callbacks so this behavior remains
usable and testable without depending on Typer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import git_ops, store


class CheckoutError(RuntimeError):
    """A user-facing checkout validation failure."""


@dataclass(frozen=True)
class CheckoutResult:
    target: store.Version
    checkpoint: store.Version | None = None
    aborted: bool = False


def checkout(
    repo_root: Path,
    version_token: str,
    *,
    force: bool = False,
    confirm: Callable[[str], bool] | None = None,
    notify: Callable[[str], None] | None = None,
) -> CheckoutResult:
    """Restore ``version_token`` while preserving uncommitted work.

    ``confirm`` is called only for a target outside the current branch's
    reachable history. ``notify`` receives the same progress messages the CLI
    has historically displayed, in the same order.
    """
    branch = git_ops.current_branch(repo_root)
    notify = notify or (lambda _message: None)

    with store.connect(repo_root) as conn:
        resolved_id = _resolve_version_token(repo_root, conn, branch, version_token)
        target = store.get_version(conn, resolved_id) if resolved_id else None
        if target is None:
            raise CheckoutError(f"no such version or tag '{version_token}'")
        if target.tree_after is None:
            raise CheckoutError(
                f"'{version_token}' has no finalized snapshot to restore"
            )

        head_id = store.read_head(repo_root, branch)

        # A carried-over ancestor is part of this branch's history. A target on
        # another line requires explicit confirmation before it is brought over.
        reachable = {
            version.id
            for version in store.list_branch_versions(conn, branch, head=head_id)
        }
        if target.id not in reachable:
            notify(
                f"note: '{version_token}' is not part of branch '{branch}' history "
                f"(it was created on '{target.git_branch}' and isn't an ancestor "
                f"of this branch)."
            )
            if not force:
                prompt = f"Check it out into '{branch}' anyway?"
                if confirm is None or not confirm(prompt):
                    return CheckoutResult(target=target, aborted=True)

        head_version = store.get_version(conn, head_id) if head_id else None
        current_tree = git_ops.write_tree(repo_root)
        checkpoint = None

        if head_version is not None:
            head_tree = head_version.tree_after or head_version.tree_before
            if current_tree != head_tree:
                diff = git_ops.diff_trees(repo_root, head_tree, current_tree)
                diff_ref = store.write_object(repo_root, diff) if diff.strip() else None
                checkpoint = store.create_version(
                    conn,
                    type="checkpoint",
                    git_branch=branch,
                    parent_id=head_id,
                    tree_before=head_tree,
                    tree_after=current_tree,
                    diff_ref=diff_ref,
                )
                notify(
                    f"saved uncommitted work as checkpoint {checkpoint.id} "
                    f"(restore it with `profact checkout {checkpoint.id}`)"
                )

    git_ops.checkout_tree(repo_root, target.tree_after, prev_tree=current_tree)
    store.write_head(repo_root, branch, target.id)
    return CheckoutResult(target=target, checkpoint=checkpoint)


def _resolve_version_token(
    repo_root: Path, conn, branch: str, token: str
) -> str | None:
    if token.upper() == "HEAD":
        return store.read_head(repo_root, branch, seed=False)
    return store.resolve_version_id(conn, token)
