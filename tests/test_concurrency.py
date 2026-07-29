from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from profact import git_ops, store


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_parallel_snapshots_use_independent_indexes_and_preserve_real_index(
    git_repo: Path,
) -> None:
    (git_repo / "a.txt").write_text("working tree\n")
    (git_repo / "staged.txt").write_text("staged\n")
    _git(git_repo, "add", "staged.txt")
    staged_before = _git(git_repo, "diff", "--cached", "--name-only")

    with ThreadPoolExecutor(max_workers=2) as executor:
        trees = list(executor.map(lambda _index: git_ops.write_tree(git_repo), range(2)))

    assert trees[0] == trees[1]
    assert _git(git_repo, "diff", "--cached", "--name-only") == staged_before
    assert not list(store.profact_dir(git_repo).glob("tmp_index_*"))


def test_sqlite_wal_allows_writer_while_reader_transaction_is_open(
    git_repo: Path,
) -> None:
    reader = store.connect(git_repo)
    try:
        assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert (
            reader.execute("PRAGMA busy_timeout").fetchone()[0]
            == store.SQLITE_BUSY_TIMEOUT_MS
        )
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM versions").fetchone()

        def create_tag() -> None:
            with store.connect(git_repo) as writer:
                store.set_tag(writer, "concurrent-read", "v1")

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(create_tag).result(timeout=3)
    finally:
        reader.rollback()
        reader.close()

    with store.connect(git_repo) as conn:
        tag = store.get_tag(conn, "concurrent-read")
    assert tag is not None and tag.version_id == "v1"