from __future__ import annotations

from pathlib import Path

from profact import git_ops, recovery, store, version_detail
from profact.tui import presenter


def _prompt(repo: Path, prompt_text: str, file_text: str) -> store.Version:
    branch = git_ops.current_branch(repo)
    tree_before = git_ops.write_tree(repo)
    parent_id = store.read_head(repo, branch)
    (repo / "app.py").write_text(file_text)
    tree_after = git_ops.write_tree(repo)
    diff = git_ops.diff_trees(repo, tree_before, tree_after)
    with store.connect(repo) as conn:
        version = store.create_version(
            conn,
            git_branch=branch,
            parent_id=parent_id,
            tree_before=tree_before,
            tree_after=tree_after,
            prompt_ref=store.write_object(repo, prompt_text),
            response_ref=store.write_object(repo, "done"),
            diff_ref=store.write_object(repo, diff),
            model="claude-test",
            thinking_source=store.THINKING_NONE,
        )
    store.write_head(repo, branch, version.id)
    return version


def test_presenter_builds_history_search_and_detail(git_repo: Path) -> None:
    first = _prompt(git_repo, "add login validation", "value = 1\n")
    second = _prompt(git_repo, "add dashboard", "value = 2\n")
    with store.connect(git_repo) as conn:
        store.set_tag(conn, "working", second.id)
        store.set_test_result(
            conn,
            second.id,
            status="pass",
            exit_code=0,
            command="pytest -q",
            output_ref=store.write_object(git_repo, "2 passed\n"),
        )

    model = presenter.load_history(git_repo, presenter.HistoryQuery())
    assert model.branch == "main"
    assert model.head_id == second.id
    assert [row.version.id for row in model.rows] == [second.id, first.id, "v1"]
    assert model.rows[0].tags == ("working",)
    assert model.rows[0].test_result is not None
    assert model.rows[0].is_head

    searched = presenter.load_history(
        git_repo, presenter.HistoryQuery(search="login")
    )
    assert [row.version.id for row in searched.rows] == [first.id]
    assert searched.filtered_out == 2

    detail = presenter.load_detail(git_repo, second.id, branch="main")
    assert detail.prompt == "add dashboard"
    assert detail.test_output == "2 passed\n"
    assert "Version: v3 (HEAD)" in presenter.overview_text(detail)
    assert "one recorded run" in presenter.tests_text(detail)


def test_presenter_preserves_no_reasoning_placeholder(git_repo: Path) -> None:
    version = _prompt(git_repo, "do work", "value = 1\n")

    detail = presenter.load_detail(git_repo, version.id, branch="main")

    assert detail.section("thinking") == version_detail.NO_THINKING_PLACEHOLDER


def test_presenter_explains_preserved_incomplete_capture(git_repo: Path) -> None:
    version = _prompt(git_repo, "unfinished", "value = 1\n")
    with store.connect(git_repo) as conn:
        conn.execute(
            "UPDATE versions SET confidence = ? WHERE id = ?",
            (recovery.INCOMPLETE_CONFIDENCE, version.id),
        )
        conn.commit()

    detail = presenter.load_detail(git_repo, version.id, branch="main")
    overview = presenter.overview_text(detail)

    assert "capture ended before its artifact was recovered" in overview
    assert "working-tree changes were not attributed" in overview