from __future__ import annotations

import subprocess
from pathlib import Path

from profact import git_ops, human_edits, store


def _git(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _configure(repo: Path) -> None:
    (repo / "profact.toml").write_text(
        "[privacy]\n"
        'private_paths = [".env", "secret.env", "private/**"]\n',
        encoding="utf-8",
    )


def test_private_files_never_enter_snapshot_or_git_object_database(
    git_repo: Path,
) -> None:
    _configure(git_repo)
    secret = "unique-private-value-that-must-not-be-hashed\n"
    (git_repo / "secret.env").write_text(secret)
    private_dir = git_repo / "private"
    private_dir.mkdir()
    (private_dir / "credentials.txt").write_text("nested-secret\n")
    (git_repo / "public.txt").write_text("safe\n")
    blob = _git(git_repo, "hash-object", "--stdin", input_text=secret)
    loose_object = git_repo / ".git" / "objects" / blob[:2] / blob[2:]
    assert not loose_object.exists()

    tree = git_ops.write_tree(git_repo)
    files = set(git_ops.list_tree_files(git_repo, tree))

    assert "public.txt" in files
    assert "secret.env" not in files
    assert "private/credentials.txt" not in files
    assert "profact.toml" not in files
    assert not loose_object.exists()


def test_private_transition_is_absent_from_diffs_and_human_history(
    git_repo: Path,
) -> None:
    (git_repo / "secret.env").write_text("old-secret\n")
    (git_repo / "public.txt").write_text("old-public\n")
    before = git_ops.write_tree(git_repo)

    _configure(git_repo)
    (git_repo / "secret.env").write_text("new-secret\n")
    (git_repo / "public.txt").write_text("new-public\n")
    after = git_ops.write_tree(git_repo)
    diff = git_ops.diff_trees(git_repo, before, after)

    assert "public.txt" in diff and "new-public" in diff
    assert "secret.env" not in diff
    assert "old-secret" not in diff and "new-secret" not in diff

    with store.connect(git_repo) as conn:
        parent = store.create_version(
            conn,
            type="human",
            git_branch="main",
            parent_id="v1",
            tree_before=after,
            tree_after=after,
        )
    store.write_head(git_repo, "main", parent.id)
    (git_repo / "secret.env").write_text("private-only-change\n")

    reconciled = human_edits.reconcile(git_repo)
    assert reconciled.version is None


def test_checkout_preserves_live_private_files_and_privacy_config(
    git_repo: Path,
) -> None:
    (git_repo / "secret.env").write_text("historical-secret\n")
    historical = git_ops.write_tree(git_repo)

    _configure(git_repo)
    config_text = (git_repo / "profact.toml").read_text()
    (git_repo / "secret.env").write_text("live-secret\n")
    current = git_ops.write_tree(git_repo)

    git_ops.checkout_tree(git_repo, historical, prev_tree=current)

    assert (git_repo / "secret.env").read_text() == "live-secret\n"
    assert (git_repo / "profact.toml").read_text() == config_text
    assert not git_ops.path_in_tree(git_repo, historical, "secret.env")


def test_modified_tracked_private_file_is_not_hashed_by_snapshot(
    git_repo: Path,
) -> None:
    private_file = git_repo / ".env"
    private_file.write_text("historical-value\n")
    _git(git_repo, "add", ".env")
    _git(git_repo, "commit", "-m", "track historical env")

    _configure(git_repo)
    new_secret = "new-private-value-never-hashed-by-profact\n"
    private_file.write_text(new_secret)
    new_blob = _git(git_repo, "hash-object", "--stdin", input_text=new_secret)
    loose_object = git_repo / ".git" / "objects" / new_blob[:2] / new_blob[2:]
    assert not loose_object.exists()

    tree = git_ops.write_tree(git_repo)

    assert ".env" not in git_ops.list_tree_files(git_repo, tree)
    assert not loose_object.exists()
    assert private_file.read_text() == new_secret