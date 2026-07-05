from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from profact import git_ops, store


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    _git(["init", "-b", "main"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "test"], tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    (tmp_path / ".gitignore").write_text(f"{store.PROFACT_DIR}/\n")
    _git(["add", "a.txt", ".gitignore"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    store.init_db(tmp_path)
    with store.connect(tmp_path) as conn:
        tree = git_ops.write_tree(tmp_path)
        anchor = store.create_version(
            conn,
            type="anchor",
            git_branch=git_ops.current_branch(tmp_path),
            tree_before=tree,
            tree_after=tree,
            commit_hash=git_ops.head_commit(tmp_path),
        )
    store.write_head(tmp_path, anchor.id)
    return tmp_path
