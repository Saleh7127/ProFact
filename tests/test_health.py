from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from profact import git_ops, health, store
from profact.cli import app

runner = CliRunner()


def _version(
    repo: Path,
    *,
    parent_id: str,
    text: str,
    content: str,
    branch: str = "main",
    confidence: str = "high",
) -> store.Version:
    with store.connect(repo) as conn:
        parent = store.get_version(conn, parent_id)
    assert parent is not None and parent.tree_after is not None
    tree_before = parent.tree_after
    (repo / "app.py").write_text(content)
    tree_after = git_ops.write_tree(repo)
    diff = git_ops.diff_trees(repo, tree_before, tree_after)
    with store.connect(repo) as conn:
        return store.create_version(
            conn,
            type="prompt",
            git_branch=branch,
            parent_id=parent_id,
            tree_before=tree_before,
            tree_after=tree_after,
            prompt_ref=store.write_object(repo, text),
            diff_ref=store.write_object(repo, diff),
            confidence=confidence,
        )


def _history_with_findings(repo: Path) -> tuple[store.Version, ...]:
    v2 = _version(
        repo,
        parent_id="v1",
        text="add app",
        content="value = 1\n",
    )
    v3 = _version(
        repo,
        parent_id=v2.id,
        text="change app",
        content="value = 2\n",
        confidence="uncertain",
    )
    v4 = _version(
        repo,
        parent_id=v2.id,
        text="try another app",
        content="value = 3\n",
        branch="experiment",
    )
    with store.connect(repo) as conn:
        v5 = store.create_version(
            conn,
            type="prompt",
            git_branch="main",
            parent_id=v3.id,
            tree_before=v3.tree_after,
            prompt_ref=store.write_object(repo, "unfinished"),
        )
        store.set_test_result(
            conn,
            v2.id,
            status="pass",
            exit_code=0,
            command="pytest -q",
            output_ref=None,
        )
        store.set_test_result(
            conn,
            v3.id,
            status="fail",
            exit_code=1,
            command="pytest -q",
            output_ref=None,
        )
    store.write_head(repo, "main", v3.id)
    return v2, v3, v4, v5


def test_health_analyzes_activity_tests_churn_and_topology(git_repo: Path) -> None:
    v2, v3, v4, v5 = _history_with_findings(git_repo)

    report = health.analyze(git_repo)

    assert report.total_versions == 5
    assert report.version_counts == {
        "prompt": 4,
        "anchor": 1,
        "checkpoint": 0,
        "human": 0,
    }
    assert report.branch_names == ("experiment", "main")
    assert report.branch_points == (v2.id,)
    assert report.tips == (v4.id, v5.id)
    assert report.side_path_tips == (v4.id,)
    assert report.uncertain_versions == (v3.id,)
    assert report.dangling_turns == (v5.id,)
    assert report.tests.passed == (v2.id,)
    assert report.tests.failed == (v3.id,)
    assert report.tests.errored == ()
    assert report.tests.untested == ("v1", v4.id)
    assert report.churn[0] == health.FileChurn(
        path="app.py",
        version_ids=(v2.id, v3.id, v4.id),
        additions=3,
        deletions=2,
    )
    assert report.bisect_candidates == (
        health.BisectCandidate(v2.id, v3.id, "pytest -q"),
    )


def test_health_command_renders_findings_and_is_listed_in_help(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    v2, v3, v4, v5 = _history_with_findings(git_repo)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0, result.output
    assert "ProFact history health" in result.output
    assert "prompts: 4" in result.output
    assert f"branch points: 1 ({v2.id})" in result.output
    assert f"app.py: 3 versions ({v2.id}, {v3.id}, {v4.id}), +3/-2" in result.output
    assert f"{v3.id}: tests fail: pytest -q" in result.output
    assert f"{v5.id}: dangling prompt turn" in result.output
    assert f"{v4.id}: unreferenced side-path tip" in result.output
    assert f"profact bisect --good {v2.id} --bad {v3.id}" in result.output
    assert "profact blame app.py" in result.output

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "health" in help_result.output


def test_health_handles_initialized_history_with_no_versions(
    tmp_path: Path, monkeypatch
) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    store.init_db(tmp_path)
    monkeypatch.chdir(tmp_path)

    report = health.analyze(tmp_path)
    assert report.total_versions == 0
    assert report.branch_names == ()
    assert report.churn == ()
    assert report.tests.untested == ()

    result = runner.invoke(app, ["health"])
    assert result.exit_code == 0, result.output
    assert "Current: main @ (none)" in result.output
    assert "versions: 0" in result.output
    assert "High-churn files" in result.output
    assert "(none yet)" in result.output
    assert "(nothing currently needs attention)" in result.output