from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from typer.testing import CliRunner

from profact import git_ops, store
from profact.cli import app

runner = CliRunner()


def _row_ids(output: str) -> set[str]:
    ids: set[str] = set()
    for line in output.splitlines():
        match = re.match(r"^(?:\[[^]]+\]\s+)?(v\d+)\s", line)
        if match:
            ids.add(match.group(1))
    return ids


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _prompt(
    repo: Path,
    *,
    text: str,
    model: str,
    mutate: Callable[[], None],
) -> store.Version:
    branch = git_ops.current_branch(repo)
    before = git_ops.write_tree(repo)
    parent = store.read_head(repo, branch)
    mutate()
    after = git_ops.write_tree(repo)
    diff = git_ops.diff_trees(repo, before, after)
    with store.connect(repo) as conn:
        version = store.create_version(
            conn,
            type="prompt",
            git_branch=branch,
            parent_id=parent,
            tree_before=before,
            tree_after=after,
            prompt_ref=store.write_object(repo, text),
            diff_ref=store.write_object(repo, diff),
            model=model,
        )
    store.write_head(repo, branch, version.id)
    return version


def test_log_filters_by_tag_type_and_model(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    v2 = _prompt(
        git_repo,
        text="calculator",
        model="claude-4.6-opus",
        mutate=lambda: (git_repo / "calculator.py").write_text("x = 1\n"),
    )
    v3 = _prompt(
        git_repo,
        text="readme",
        model="gpt-5",
        mutate=lambda: (git_repo / "README.md").write_text("docs\n"),
    )
    assert runner.invoke(app, ["tag", v2.id, "working"]).exit_code == 0

    tagged = runner.invoke(app, ["log", "--tag", "working"])
    assert tagged.exit_code == 0, tagged.output
    assert _row_ids(tagged.output) == {v2.id}
    assert "filters: tag=working" in tagged.output

    prompts = runner.invoke(app, ["log", "--type", "prompt"])
    assert prompts.exit_code == 0, prompts.output
    assert _row_ids(prompts.output) == {v2.id, v3.id}

    model = runner.invoke(app, ["log", "--model", "CLAUDE"])
    assert model.exit_code == 0, model.output
    assert _row_ids(model.output) == {v2.id}


def test_log_filters_by_changed_file_without_breaking_ancestry_path(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    v2 = _prompt(
        git_repo,
        text="calculator",
        model="claude",
        mutate=lambda: (git_repo / "src" / "calculator.py").parent.mkdir()
        or (git_repo / "src" / "calculator.py").write_text("x = 1\n"),
    )
    v3 = _prompt(
        git_repo,
        text="readme",
        model="claude",
        mutate=lambda: (git_repo / "README.md").write_text("docs\n"),
    )

    changed = runner.invoke(app, ["log", "--file", "calculator.py"])
    assert changed.exit_code == 0, changed.output
    assert _row_ids(changed.output) == {v2.id}
    assert "file=calculator.py" in changed.output

    ancestry = runner.invoke(app, ["log", "--path"])
    assert ancestry.exit_code == 0, ancestry.output
    assert "HEAD ancestry for branch 'main'" in ancestry.output


def test_log_filters_other_branch_and_combines_with_tag_and_test(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    _prompt(
        git_repo,
        text="main work",
        model="claude",
        mutate=lambda: (git_repo / "main.py").write_text("main\n"),
    )
    _git(["checkout", "-b", "exp1"], git_repo)
    exp = _prompt(
        git_repo,
        text="buggy experiment",
        model="claude",
        mutate=lambda: (git_repo / "exp.py").write_text("broken\n"),
    )
    assert runner.invoke(app, ["tag", exp.id, "buggy"]).exit_code == 0
    with store.connect(git_repo) as conn:
        store.set_test_result(
            conn,
            exp.id,
            status="fail",
            exit_code=1,
            command="pytest -q",
            output_ref=None,
        )

    result = runner.invoke(
        app,
        [
            "log",
            "--branch",
            "exp1",
            "--tag",
            "buggy",
            "--test-status",
            "fail",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _row_ids(result.output) == {exp.id}
    assert "[FAIL]" in result.output
    assert "branch=exp1 · tag=buggy · test=fail" in result.output


def test_log_filters_by_test_status(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    failed = _prompt(
        git_repo,
        text="failed",
        model="claude",
        mutate=lambda: (git_repo / "a.py").write_text("bad\n"),
    )
    passed = _prompt(
        git_repo,
        text="passed",
        model="claude",
        mutate=lambda: (git_repo / "b.py").write_text("good\n"),
    )
    with store.connect(git_repo) as conn:
        store.set_test_result(
            conn,
            failed.id,
            status="fail",
            exit_code=1,
            command="pytest",
            output_ref=None,
        )
        store.set_test_result(
            conn,
            passed.id,
            status="pass",
            exit_code=0,
            command="pytest",
            output_ref=None,
        )

    result = runner.invoke(app, ["log", "--test-status", "fail"])
    assert result.exit_code == 0, result.output
    assert _row_ids(result.output) == {failed.id}
    assert "[FAIL]" in result.output
    assert "tests: 0 passed, 1 failed, 0 errors, 0 untested" in result.output


def test_log_filters_since_and_rejects_invalid_values(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    old = _prompt(
        git_repo,
        text="old",
        model="claude",
        mutate=lambda: (git_repo / "old.py").write_text("old\n"),
    )
    recent = _prompt(
        git_repo,
        text="recent",
        model="claude",
        mutate=lambda: (git_repo / "recent.py").write_text("recent\n"),
    )
    with store.connect(git_repo) as conn:
        conn.execute(
            "UPDATE versions SET timestamp = ? WHERE id IN (?, ?)",
            ("2020-01-01T00:00:00+00:00", "v1", old.id),
        )
        conn.execute(
            "UPDATE versions SET timestamp = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), recent.id),
        )
        conn.commit()

    result = runner.invoke(app, ["log", "--since", "7d"])
    assert result.exit_code == 0, result.output
    assert _row_ids(result.output) == {recent.id}

    bad_since = runner.invoke(app, ["log", "--since", "yesterday"])
    assert bad_since.exit_code == 2
    assert "invalid --since" in bad_since.output

    bad_status = runner.invoke(app, ["log", "--test-status", "maybe"])
    assert bad_status.exit_code == 2
    assert "unknown test status" in bad_status.output
