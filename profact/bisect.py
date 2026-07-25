"""Regression hunt: binary-search the prompt lineage for the version that broke
a test.

Where `git bisect` searches commits, `profact bisect` searches the *prompt* DAG:
given a known-good version and a known-bad one, it checks out candidate
snapshots between them, runs a user-supplied test command at each, and converges
on the first version whose test fails — i.e. the prompt that introduced the
regression. Pairs naturally with `blame` (explain the culprit line) to answer
"which prompt broke this, and what did I ask for?".

Search space
------------
profact history is a DAG, but the lineage from a version back through its
parents is linear, so we bisect the ancestry chain between ``good`` and ``bad``.
``good`` must be an ancestor of ``bad`` (the normal "it worked at v3, it's broken
at v10" case). Cross-branch bisection is intentionally out of scope for now.

Safety
------
Bisect is non-destructive to history: it never creates versions and never moves
HEAD. It does drive the *working tree* through candidate snapshots, so it
records the starting tree up front and always restores it (even on error or
interrupt) via a ``finally`` block.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import git_ops, store

# Test-command exit-code convention (matches `git bisect run`):
#   0        -> good
#   125      -> skip (can't test this snapshot; exclude it)
#   anything -> bad
SKIP_EXIT = 125

GOOD = "good"
BAD = "bad"
SKIP = "skip"


class BisectError(RuntimeError):
    """User-facing bisect failure (bad args, invalid endpoints)."""


@dataclass
class StepResult:
    version: store.Version
    exit_code: int
    outcome: str  # GOOD | BAD | SKIP


@dataclass
class BisectResult:
    first_bad: store.Version | None
    steps: list[StepResult]
    # Set when skips left a gap we could not narrow further.
    inconclusive_between: tuple[store.Version, store.Version] | None = None


def classify(exit_code: int) -> str:
    if exit_code == 0:
        return GOOD
    if exit_code == SKIP_EXIT:
        return SKIP
    return BAD


def _choose_mid(lo: int, hi: int, skipped: set[int]) -> int | None:
    """Pick the middle still-testable candidate strictly between lo and hi."""
    candidates = [i for i in range(lo + 1, hi) if i not in skipped]
    if not candidates:
        return None
    return candidates[len(candidates) // 2]


def _run_test(repo_root: Path, cmd: list[str]) -> int:
    """Run the test command at the repo root, returning its exit code.

    Output is captured (not streamed) so bisect stays quiet; the exit code is
    the only signal that matters.
    """
    try:
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise BisectError(f"cannot run test command: {exc}") from exc
    return proc.returncode


def bisect(
    repo_root: Path,
    conn,
    *,
    good_id: str,
    bad_id: str,
    cmd: list[str],
    on_step: Callable[[StepResult], None] | None = None,
) -> BisectResult:
    """Find the first bad version between ``good_id`` and ``bad_id``.

    Restores the working tree to its pre-bisect state before returning.
    """
    if not cmd:
        raise BisectError("no test command given")

    good = store.get_version(conn, good_id)
    if good is None:
        raise BisectError(f"no such version '{good_id}'")
    bad = store.get_version(conn, bad_id)
    if bad is None:
        raise BisectError(f"no such version '{bad_id}'")
    if good.id == bad.id:
        raise BisectError("good and bad refer to the same version")

    chain = store.ancestry(conn, bad_id)  # oldest-first: root .. bad
    index = {v.id: i for i, v in enumerate(chain)}
    if good.id not in index:
        raise BisectError(
            f"'{good_id}' is not an ancestor of '{bad_id}'; bisect needs a "
            f"known-good ancestor of the known-bad version (same lineage)"
        )

    lo, hi = index[good.id], index[bad.id]

    steps: list[StepResult] = []
    start_tree = git_ops.write_tree(repo_root)
    current_tree = start_tree

    def test(version: store.Version) -> str:
        nonlocal current_tree
        target_tree = version.tree_after or version.tree_before
        git_ops.checkout_tree(repo_root, target_tree, prev_tree=current_tree)
        current_tree = target_tree
        code = _run_test(repo_root, cmd)
        step = StepResult(version, code, classify(code))
        steps.append(step)
        if on_step:
            on_step(step)
        return step.outcome

    try:
        # Validate the endpoints so a mislabeled/flaky setup fails loudly rather
        # than silently returning a wrong culprit.
        if test(bad) != BAD:
            raise BisectError(
                f"known-bad version '{bad.id}' passed the test; nothing to bisect"
            )
        if test(good) != GOOD:
            raise BisectError(
                f"known-good version '{good.id}' failed the test; choose an "
                f"earlier good version"
            )

        skipped: set[int] = set()
        while hi - lo > 1:
            mid = _choose_mid(lo, hi, skipped)
            if mid is None:
                return BisectResult(
                    first_bad=None,
                    steps=steps,
                    inconclusive_between=(chain[lo], chain[hi]),
                )
            outcome = test(chain[mid])
            if outcome == GOOD:
                lo = mid
            elif outcome == BAD:
                hi = mid
            else:  # SKIP
                skipped.add(mid)

        return BisectResult(first_bad=chain[hi], steps=steps)
    finally:
        # Always put the working tree back exactly as we found it.
        git_ops.checkout_tree(repo_root, start_tree, prev_tree=current_tree)
