from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

from typer.testing import CliRunner

from profact import git_ops, store, testrunner
from profact.cli import app

runner = CliRunner()


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _prompt(repo: Path, *, text: str, mutate: Callable[[], None]) -> store.Version:
    branch = git_ops.current_branch(repo)
    tree_before = git_ops.write_tree(repo)
    parent = store.read_head(repo, branch)
    prompt_ref = store.write_object(repo, text)
    mutate()
    tree_after = git_ops.write_tree(repo)
    diff = git_ops.diff_trees(repo, tree_before, tree_after)
    diff_ref = store.write_object(repo, diff) if diff.strip() else None
    with store.connect(repo) as conn:
        version = store.create_version(
            conn,
            type="prompt",
            git_branch=branch,
            parent_id=parent,
            tree_before=tree_before,
            tree_after=tree_after,
            prompt_ref=prompt_ref,
            diff_ref=diff_ref,
        )
    store.write_head(repo, branch, version.id)
    return version


_PASS = [sys.executable, "-c", "import sys; sys.exit(0)"]
_FAIL = [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"]


def _result(repo: Path, version_id: str) -> store.TestResult | None:
    with store.connect(repo) as conn:
        return store.get_test_result(conn, version_id)


def test_test_records_pass_on_head(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    res = runner.invoke(app, ["test", "--", *_PASS])
    assert res.exit_code == 0, res.output
    assert "v1: [pass] pass" in res.output

    stored = _result(git_repo, "v1")
    assert stored is not None
    assert stored.status == "pass"
    assert stored.exit_code == 0
    assert "sys.exit(0)" in (stored.command or "")


def test_test_records_fail_and_exits_nonzero(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    res = runner.invoke(app, ["test", "--", *_FAIL])
    # A failing test => the command exits non-zero (CI-friendly), but the result
    # is still recorded.
    assert res.exit_code == 1, res.output
    stored = _result(git_repo, "v1")
    assert stored is not None
    assert stored.status == "fail"
    assert stored.exit_code == 3
    # Output was captured to a content-addressed blob.
    assert stored.output_ref is not None
    assert "boom" in (store.read_object(git_repo, stored.output_ref) or "")


def test_test_records_error_when_command_missing(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    res = runner.invoke(app, ["test", "--", "profact-no-such-binary-xyz"])
    assert res.exit_code == 1, res.output
    stored = _result(git_repo, "v1")
    assert stored is not None
    assert stored.status == "error"
    assert stored.exit_code is None


def test_rerun_overwrites_previous_result(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    runner.invoke(app, ["test", "--", *_FAIL])
    assert _result(git_repo, "v1").status == "fail"
    runner.invoke(app, ["test", "--", *_PASS])
    stored = _result(git_repo, "v1")
    assert stored.status == "pass"  # latest run wins (upsert)


def test_test_targets_explicit_version(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    v2 = _prompt(git_repo, text="two", mutate=lambda: f.write_text("2\n"))

    # Attribute a result to v1 while HEAD is v2 (note: warns about tree mismatch).
    res = runner.invoke(app, ["test", "--version", "v1", "--", *_PASS])
    assert res.exit_code == 0, res.output
    assert _result(git_repo, "v1").status == "pass"
    assert _result(git_repo, v2.id) is None  # HEAD untouched


def test_show_and_status_display_test_result(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    runner.invoke(app, ["test", "--", *_PASS])

    shown = runner.invoke(app, ["show", "v1"])
    assert shown.exit_code == 0, shown.output
    assert "test:" in shown.output and "pass" in shown.output

    stat = runner.invoke(app, ["status"])
    assert stat.exit_code == 0, stat.output
    assert "tests:" in stat.output and "pass" in stat.output


def test_log_tests_flag_shows_markers_plain_log_does_not(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    _prompt(git_repo, text="two", mutate=lambda: f.write_text("2\n"))
    runner.invoke(app, ["test", "--", *_FAIL])  # records [FAIL] on HEAD (v2)

    with_tests = runner.invoke(app, ["log", "--tests"])
    assert with_tests.exit_code == 0, with_tests.output
    assert "[FAIL]" in with_tests.output

    plain = runner.invoke(app, ["log"])
    assert plain.exit_code == 0, plain.output
    assert "[FAIL]" not in plain.output  # markers are opt-in


def test_doctor_keeps_referenced_test_output_blob(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    runner.invoke(app, ["test", "--", *_FAIL])
    out_ref = _result(git_repo, "v1").output_ref
    assert out_ref is not None
    blob = store.objects_dir(git_repo) / out_ref
    assert blob.exists()

    # Add a genuine orphan; doctor --fix should delete it but keep the test blob.
    orphan = store.objects_dir(git_repo) / ("a" * 64)
    orphan.write_text("dangling\n")

    fixed = runner.invoke(app, ["doctor", "--fix"])
    assert fixed.exit_code == 0, fixed.output
    assert not orphan.exists()
    assert blob.exists()  # test output is referenced, so it survives


def test_run_and_record_module_classifies(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    with store.connect(git_repo) as conn:
        ok = testrunner.run_and_record(git_repo, conn, version_id="v1", cmd=_PASS)
        assert ok.result.status == "pass"
        bad = testrunner.run_and_record(git_repo, conn, version_id="v1", cmd=_FAIL)
        assert bad.result.status == "fail"
    assert testrunner.classify(0) == "pass"
    assert testrunner.classify(2) == "fail"
