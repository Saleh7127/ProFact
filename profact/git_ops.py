from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from . import privacy


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
    index_file = index_file or _temporary_index_path(repo_root)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    patterns = _snapshot_exclusion_patterns(repo_root)
    exclude_file = _temporary_exclude_file(repo_root, patterns)
    try:
        if has_commits(repo_root):
            _run(["read-tree", "HEAD"], repo_root, env=env)
        else:
            _run(["read-tree", "--empty"], repo_root, env=env)
        # Remove tracked private paths before `git add`: ignore rules do not
        # apply to tracked entries, so this prevents modified private bytes from
        # being hashed by ProFact even when an older revision exists in Git.
        _remove_index_paths(repo_root, env, patterns)
        # Exclude before staging so private file bytes are never hashed into the
        # repository's object database by ProFact's shadow-index snapshot.
        _run(
            ["-c", f"core.excludesFile={exclude_file.as_posix()}", "add", "-A"],
            repo_root,
            env=env,
        )
        _remove_index_paths(repo_root, env, patterns)
        return _run(["write-tree"], repo_root, env=env)
    finally:
        if index_file.exists():
            index_file.unlink()
        if exclude_file.exists():
            exclude_file.unlink()


def diff_trees(
    repo_root: Path,
    before: str,
    after: str,
    *,
    stat: bool = False,
    name_only: bool = False,
) -> str:
    args = ["diff"]
    if stat:
        args.append("--stat")
    if name_only:
        args.append("--name-only")
    args += [before, after]
    excluded = _snapshot_exclusion_patterns(repo_root)
    if excluded:
        args += ["--", ".", *_exclude_pathspecs(excluded)]
    return _run(args, repo_root)


def list_tree_files(repo_root: Path, tree: str) -> list[str]:
    out = _run(["ls-tree", "-r", "--name-only", tree], repo_root)
    return out.splitlines() if out else []


def path_in_tree(repo_root: Path, tree: str, rel_path: str) -> bool:
    """True if ``rel_path`` names a blob in ``tree``."""
    if privacy.is_private_path(repo_root, rel_path):
        return False
    out = _run(["ls-tree", tree, "--", rel_path], repo_root)
    return bool(out.strip())


def commit_tree(
    repo_root: Path,
    tree: str,
    *,
    message: str,
    parent: str | None = None,
    date: str | None = None,
) -> str:
    """Create a commit object wrapping an existing ``tree`` and return its SHA.

    Used by `profact blame` to replay the prompt lineage as a throwaway commit
    chain so Git's own blame engine can attribute lines. The commits are never
    referenced by any branch — they are unreachable objects that Git will garbage
    collect — so they never touch the user's real history.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "profact",
        "GIT_AUTHOR_EMAIL": "profact@localhost",
        "GIT_COMMITTER_NAME": "profact",
        "GIT_COMMITTER_EMAIL": "profact@localhost",
    }
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    args = ["commit-tree", tree, "-m", message]
    if parent:
        args += ["-p", parent]
    return _run(args, repo_root, env=env)


def blame_porcelain(repo_root: Path, commit: str, rel_path: str) -> str:
    """Run ``git blame --line-porcelain`` for ``rel_path`` at ``commit``."""
    return _run(["blame", "--line-porcelain", commit, "--", rel_path], repo_root)


# profact's own infrastructure: never deleted by checkout, and never captured
# into a snapshot, even if a checked-out version predates these paths or reverts
# the .gitignore that hides them.
_PROTECTED_PREFIXES = (".profact/", ".cursor/hooks/", ".codex/hooks/")
_PROTECTED_FILES = (
    ".cursor/hooks.json",
    ".codex/hooks.json",
    ".github/hooks/profact.json",
    ".github/hooks/profact-vscode-hook.sh",
    ".github/hooks/profact-vscode-hook.cmd",
    "profact.toml",
)

# The protected paths as plain repo-relative paths, removed from every snapshot
# index so they never enter a tree regardless of the current .gitignore state.
_INFRA_PATTERNS = (
    ".profact/**",
    ".cursor/hooks/**",
    ".cursor/hooks.json",
    ".codex/hooks/**",
    ".codex/hooks.json",
    ".github/hooks/profact.json",
    ".github/hooks/profact-vscode-hook.sh",
    ".github/hooks/profact-vscode-hook.cmd",
    "profact.toml",
)


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
    index_file = index_file or _temporary_index_path(repo_root)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    try:
        _run(["read-tree", tree], repo_root, env=env)
        _remove_index_paths(
            repo_root, env, _snapshot_exclusion_patterns(repo_root)
        )
        _run(["checkout-index", "-a", "-f"], repo_root, env=env)
    finally:
        if index_file.exists():
            index_file.unlink()

    if prev_tree is None:
        return
    target_files = set(list_tree_files(repo_root, tree))
    for rel in list_tree_files(repo_root, prev_tree):
        if (
            rel in target_files
            or _is_protected(rel)
            or privacy.is_private_path(repo_root, rel)
        ):
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


def _temporary_index_path(repo_root: Path) -> Path:
    """Return a unique, absent path suitable for ``GIT_INDEX_FILE``."""
    directory = repo_root / ".profact"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="tmp_index_", dir=directory)
    os.close(descriptor)
    path = Path(name)
    path.unlink()
    return path


def _temporary_exclude_file(repo_root: Path, patterns: tuple[str, ...]) -> Path:
    directory = repo_root / ".profact"
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix="privacy_exclude_", dir=directory, text=True
    )
    path = Path(name)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for pattern in patterns:
            clean = pattern.lstrip("/")
            handle.write(f"/{clean}\n")
            if clean.endswith("/**"):
                handle.write(f"/{clean[:-3]}\n")
    return path


def _snapshot_exclusion_patterns(repo_root: Path) -> tuple[str, ...]:
    return _INFRA_PATTERNS + privacy.private_paths(repo_root)


def _exclude_pathspecs(patterns: tuple[str, ...]) -> list[str]:
    return [f":(exclude,glob){pattern}" for pattern in patterns]


def _include_pathspecs(patterns: tuple[str, ...]) -> list[str]:
    return [f":(glob){pattern}" for pattern in patterns]


def _remove_index_paths(
    repo_root: Path, env: dict[str, str], patterns: tuple[str, ...]
) -> None:
    if not patterns:
        return
    _run(
        [
            "rm",
            "-r",
            "-f",
            "--cached",
            "--ignore-unmatch",
            "--",
            *_include_pathspecs(patterns),
        ],
        repo_root,
        env=env,
    )


def add_note(repo_root: Path, commit: str, payload: str) -> None:
    _run(["notes", "--ref=profact", "add", "-f", "-m", payload, commit], repo_root)
