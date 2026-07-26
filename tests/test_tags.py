from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from typer.testing import CliRunner

from profact import git_ops, store
from profact.cli import app

runner = CliRunner()


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _prompt(repo: Path, text: str, mutate: Callable[[], None]) -> store.Version:
    branch = git_ops.current_branch(repo)
    tree_before = git_ops.write_tree(repo)
    parent_id = store.read_head(repo, branch)
    mutate()
    tree_after = git_ops.write_tree(repo)
    with store.connect(repo) as conn:
        version = store.create_version(
            conn,
            type="prompt",
            git_branch=branch,
            parent_id=parent_id,
            tree_before=tree_before,
            tree_after=tree_after,
            prompt_ref=store.write_object(repo, text),
        )
    store.write_head(repo, branch, version.id)
    return version


def test_tag_create_list_show_log_and_delete(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)

    created = runner.invoke(app, ["tag", "v1", "baseline"])
    assert created.exit_code == 0, created.output
    assert "tagged v1 as 'baseline'" in created.output
    repeated = runner.invoke(app, ["tag", "v1", "baseline"])
    assert repeated.exit_code == 0, repeated.output

    listed = runner.invoke(app, ["tag"])
    assert listed.exit_code == 0, listed.output
    assert "baseline" in listed.output and "v1" in listed.output

    shown = runner.invoke(app, ["show", "baseline"])
    assert shown.exit_code == 0, shown.output
    assert "version v1" in shown.output
    assert "tags:     baseline" in shown.output

    log = runner.invoke(app, ["log"])
    assert log.exit_code == 0, log.output
    assert "<baseline>" in log.output

    deleted = runner.invoke(app, ["tag", "--delete", "baseline"])
    assert deleted.exit_code == 0, deleted.output
    assert runner.invoke(app, ["tag"]).output == "No tags yet.\n"


def test_tag_ref_works_for_diff_and_checkout(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    file = git_repo / "app.py"
    version = _prompt(git_repo, "add app", lambda: file.write_text("x = 1\n"))
    assert runner.invoke(app, ["tag", version.id, "working"]).exit_code == 0

    diff = runner.invoke(app, ["diff", "v1", "working"])
    assert diff.exit_code == 0, diff.output
    assert "app.py" in diff.output

    assert runner.invoke(app, ["checkout", "v1"]).exit_code == 0
    restored = runner.invoke(app, ["checkout", "working"])
    assert restored.exit_code == 0, restored.output
    assert store.read_head(git_repo, "main") == version.id
    assert file.read_text() == "x = 1\n"


def test_tag_ref_keeps_cross_branch_checkout_guard(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    file = git_repo / "app.py"
    _git(["checkout", "-b", "experiment"], git_repo)
    version = _prompt(git_repo, "experiment", lambda: file.write_text("exp\n"))
    assert runner.invoke(app, ["tag", version.id, "experiment-tip"]).exit_code == 0

    _git(["checkout", "main"], git_repo)
    before = store.read_head(git_repo, "main")
    declined = runner.invoke(app, ["checkout", "experiment-tip"], input="n\n")
    assert declined.exit_code == 1
    assert "not part of branch 'main'" in declined.output
    assert store.read_head(git_repo, "main") == before


def test_tag_refuses_accidental_move_and_reserved_names(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    version = _prompt(
        git_repo, "second", lambda: (git_repo / "second.py").write_text("2\n")
    )
    assert runner.invoke(app, ["tag", "v1", "stable"]).exit_code == 0

    refused = runner.invoke(app, ["tag", version.id, "stable"])
    assert refused.exit_code == 1
    assert "use --force to move it" in refused.output

    moved = runner.invoke(app, ["tag", version.id, "stable", "--force"])
    assert moved.exit_code == 0, moved.output
    with store.connect(git_repo) as conn:
        assert store.get_tag(conn, "stable").version_id == version.id

    for reserved in ("HEAD", "head", "v7", "V42"):
        result = runner.invoke(app, ["tag", "v1", reserved])
        assert result.exit_code == 2
        assert "reserved" in result.output


def test_doctor_reports_and_removes_dangling_tags(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    with store.connect(git_repo) as conn:
        store.set_tag(conn, "lost", "v999")

    report = runner.invoke(app, ["doctor"])
    assert report.exit_code == 0, report.output
    assert "dangling tags:         1" in report.output
    assert "lost -> v999" in report.output

    fixed = runner.invoke(app, ["doctor", "--fix"])
    assert fixed.exit_code == 0, fixed.output
    assert "1 dangling tag(s)" in fixed.output
    with store.connect(git_repo) as conn:
        assert store.get_tag(conn, "lost") is None


def test_doctor_retargets_tag_from_phantom_to_parent(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    with store.connect(git_repo) as conn:
        anchor = store.get_version(conn, "v1")
        phantom = store.create_version(
            conn,
            type="checkpoint",
            git_branch="main",
            parent_id=anchor.id,
            tree_before=anchor.tree_after,
            tree_after=anchor.tree_after,
        )
        store.set_tag(conn, "saved", phantom.id)

    fixed = runner.invoke(app, ["doctor", "--fix"])
    assert fixed.exit_code == 0, fixed.output
    with store.connect(git_repo) as conn:
        assert store.get_version(conn, phantom.id) is None
        assert store.get_tag(conn, "saved").version_id == "v1"
