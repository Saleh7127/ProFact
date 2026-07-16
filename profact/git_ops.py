from __future__ import annotations

import os
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(args: list[str], repo_root: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def repo_root(start: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    if result.returncode != 0:
        raise GitError("not inside a git repository")
    return Path(result.stdout.strip())


def current_branch(repo_root: Path) -> str:
    return _run(["branch", "--show-current"], repo_root)


def has_commits(repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def head_commit(repo_root: Path) -> str | None:
    return _run(["rev-parse", "HEAD"], repo_root) if has_commits(repo_root) else None


def write_tree(repo_root: Path, index_file: Path | None = None) -> str:
    index_file = index_file or repo_root / ".profact" / "tmp_index"
    index_file.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    try:
        _run(["add", "-A"], repo_root, env=env)
        # Drop profact's own capture machinery from the snapshot unconditionally.
        # .gitignore is itself versioned, so checking out a version created before
        # `profact start` reverts the ignore rules; `git add -A` would then
        # capture the (hard-protected, undeletable) hook files, making every
        # snapshot drift — phantom checkpoints and machine-specific hook scripts
        # leaking into history. --ignore-unmatch keeps this a no-op otherwise.
        _run(
            ["rm", "-r", "--cached", "--ignore-unmatch", "--", *_INFRA_PATHS],
            repo_root,
            env=env,
        )
        return _run(["write-tree"], repo_root, env=env)
    finally:
        if index_file.exists():
            index_file.unlink()


def diff_trees(repo_root: Path, before: str, after: str) -> str:
    return _run(["diff", before, after], repo_root)


def list_tree_files(repo_root: Path, tree: str) -> list[str]:
    out = _run(["ls-tree", "-r", "--name-only", tree], repo_root)
    return out.splitlines() if out else []


# profact's own infrastructure: never deleted by checkout, and never captured
# into a snapshot, even if a checked-out version predates these paths or reverts
# the .gitignore that hides them.
_PROTECTED_PREFIXES = (".profact/", ".cursor/hooks/")
_PROTECTED_FILES = (".cursor/hooks.json",)

# The protected paths as plain repo-relative paths, removed from every snapshot
# index so they never enter a tree regardless of the current .gitignore state.
_INFRA_PATHS = (".profact", ".cursor/hooks", ".cursor/hooks.json")


def _is_protected(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return rel in _PROTECTED_FILES or any(rel.startswith(p) for p in _PROTECTED_PREFIXES)


def is_infra_path(rel: str) -> bool:
    """True if ``rel`` is one of profact's own capture-machinery paths."""
    return _is_protected(rel)


def checkout_tree(
    repo_root: Path,
    tree: str,
    prev_tree: str | None = None,
    index_file: Path | None = None,
) -> None:
    """Restore ``tree`` into the working directory via a shadow index.

    If ``prev_tree`` is given, files present in ``prev_tree`` but absent in
    ``tree`` are deleted (spec §8). Only files inside the tracked-domain trees
    are touched; ``.gitignore``-d files (e.g. ``.env``) are left alone.
    """
    index_file = index_file or repo_root / ".profact" / "tmp_index"
    index_file.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    try:
        _run(["read-tree", tree], repo_root, env=env)
        _run(["checkout-index", "-a", "-f"], repo_root, env=env)
    finally:
        if index_file.exists():
            index_file.unlink()

    if prev_tree is None:
        return
    target_files = set(list_tree_files(repo_root, tree))
    for rel in list_tree_files(repo_root, prev_tree):
        if rel in target_files or _is_protected(rel):
            continue
        path = repo_root / rel
        if path.is_file() or path.is_symlink():
            path.unlink()
        _prune_empty_dirs(path.parent, repo_root)


def _prune_empty_dirs(start: Path, repo_root: Path) -> None:
    current = start
    while current != repo_root and current.is_dir():
        try:
            next(current.iterdir())
            return
        except StopIteration:
            current.rmdir()
            current = current.parent


def add_note(repo_root: Path, commit: str, payload: str) -> None:
    _run(["notes", "--ref=profact", "add", "-f", "-m", payload, commit], repo_root)
