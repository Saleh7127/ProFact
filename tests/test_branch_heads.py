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


def _prompt(repo: Path, *, text: str, mutate: Callable[[], None]) -> store.Version:
    """Mimic one finalized capture turn on the current Git branch."""
    branch = git_ops.current_branch(repo)
    tree_before = git_ops.write_tree(repo)
    parent = store.read_head(repo, branch)  # seed=True -> carry-over on new branch
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


def test_new_git_branch_carries_over_then_diverges(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"

    # main: v1(anchor) -> v2 -> v3 -> v4
    v2 = _prompt(git_repo, text="two", mutate=lambda: f.write_text("2\n"))
    v3 = _prompt(git_repo, text="three", mutate=lambda: f.write_text("3\n"))
    v4 = _prompt(git_repo, text="four", mutate=lambda: f.write_text("4\n"))
    assert store.read_head(git_repo, "main") == v4.id

    # Branch off main at v4 and prompt twice.
    _git(["checkout", "-b", "experiment"], git_repo)
    v5 = _prompt(git_repo, text="five", mutate=lambda: f.write_text("5\n"))
    v6 = _prompt(git_repo, text="six", mutate=lambda: f.write_text("6\n"))

    # Carry-over: experiment's first version chains onto main's tip (v4).
    assert v5.parent_id == v4.id
    assert store.read_head(git_repo, "experiment") == v6.id
    # main's HEAD is untouched by work on experiment.
    assert store.read_head(git_repo, "main") == v4.id

    # Back on main, prompting forks a new child off v4 (not off experiment).
    _git(["checkout", "main"], git_repo)
    v7 = _prompt(git_repo, text="seven", mutate=lambda: f.write_text("7\n"))
    assert v7.parent_id == v4.id
    assert store.read_head(git_repo, "main") == v7.id
    # experiment is unaffected.
    assert store.read_head(git_repo, "experiment") == v6.id


def test_log_is_branch_scoped_with_shared_trunk(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    v2 = _prompt(git_repo, text="two", mutate=lambda: f.write_text("2\n"))

    _git(["checkout", "-b", "experiment"], git_repo)
    v_exp = _prompt(git_repo, text="exp work", mutate=lambda: f.write_text("exp\n"))

    _git(["checkout", "main"], git_repo)
    v_main = _prompt(git_repo, text="main work", mutate=lambda: f.write_text("main\n"))

    # main log: shared trunk (v1, v2) + main's own child; experiment's excluded.
    main_log = runner.invoke(app, ["log"])
    assert main_log.exit_code == 0, main_log.output
    assert "v1" in main_log.output and v2.id in main_log.output
    assert v_main.id in main_log.output
    assert v_exp.id not in main_log.output

    # experiment log: shared trunk carried over + experiment's own child.
    _git(["checkout", "experiment"], git_repo)
    exp_log = runner.invoke(app, ["log"])
    assert exp_log.exit_code == 0, exp_log.output
    assert "v1" in exp_log.output and v2.id in exp_log.output  # carried-over trunk
    assert v_exp.id in exp_log.output
    assert v_main.id not in exp_log.output


def test_checkout_cross_branch_declined_aborts(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    _prompt(git_repo, text="two", mutate=lambda: f.write_text("2\n"))

    _git(["checkout", "-b", "experiment"], git_repo)
    v_exp = _prompt(git_repo, text="exp", mutate=lambda: f.write_text("exp\n"))

    # Back on main, v_exp is experiment-only -> prompt; declining aborts safely.
    _git(["checkout", "main"], git_repo)
    head_before = store.read_head(git_repo, "main")
    result = runner.invoke(app, ["checkout", v_exp.id], input="n\n")
    assert result.exit_code == 1
    assert "not part of branch 'main'" in result.output
    assert "experiment" in result.output
    assert store.read_head(git_repo, "main") == head_before  # HEAD unchanged

    # A shared-trunk ancestor (v1) is reachable from main -> no prompt, allowed.
    ok = runner.invoke(app, ["checkout", "v1"])
    assert ok.exit_code == 0, ok.output
    assert "not part of branch" not in ok.output


def test_checkout_cross_branch_confirmed_brings_over(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    _prompt(git_repo, text="two", mutate=lambda: f.write_text("2\n"))

    _git(["checkout", "-b", "experiment"], git_repo)
    v_exp = _prompt(git_repo, text="exp", mutate=lambda: f.write_text("exp\n"))

    _git(["checkout", "main"], git_repo)
    # Confirming (input "y") brings the version into the current branch.
    result = runner.invoke(app, ["checkout", v_exp.id], input="y\n")
    assert result.exit_code == 0, result.output
    assert f.read_text() == "exp\n"  # working tree restored
    assert store.read_head(git_repo, "main") == v_exp.id

    # It now shows in main's history so HEAD isn't a dangling reference.
    log = runner.invoke(app, ["log"])
    assert v_exp.id in log.output

    # --force skips the prompt entirely (no stdin needed).
    _prompt(git_repo, text="three", mutate=lambda: f.write_text("3\n"))
    _git(["checkout", "-b", "experiment2"], git_repo)
    v_e2 = _prompt(git_repo, text="e2", mutate=lambda: f.write_text("e2\n"))
    _git(["checkout", "main"], git_repo)
    forced = runner.invoke(app, ["checkout", v_e2.id, "--force"])
    assert forced.exit_code == 0, forced.output
    assert store.read_head(git_repo, "main") == v_e2.id


def test_log_shows_git_branch_column(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    _prompt(git_repo, text="two", mutate=lambda: f.write_text("2\n"))
    _git(["checkout", "-b", "experiment"], git_repo)
    _prompt(git_repo, text="exp", mutate=lambda: f.write_text("exp\n"))

    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0, result.output
    # Shared trunk is labeled with its origin branch; own work with experiment.
    assert "(main)" in result.output
    assert "(experiment)" in result.output


def test_diff_works_across_branches(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    _prompt(git_repo, text="two", mutate=lambda: f.write_text("2\n"))  # v2 on main

    _git(["checkout", "-b", "experiment"], git_repo)
    v_exp = _prompt(git_repo, text="exp", mutate=lambda: f.write_text("exp\n"))  # v3 on experiment

    # From main, diffing against an experiment-only version is allowed (read-only).
    _git(["checkout", "main"], git_repo)
    result = runner.invoke(app, ["diff", "v2", v_exp.id])
    assert result.exit_code == 0, result.output
    assert "app.py" in result.output


def test_untouched_branch_reports_no_head(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    _git(["checkout", "-b", "fresh"], git_repo)
    # Display commands don't borrow another branch's HEAD (seed=False).
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "(none yet)" in result.output


def _make_phantom_checkpoint(repo: Path) -> store.Version:
    """A checkpoint whose diff touches only profact infra (pre-fix bug artifact)."""
    with store.connect(repo) as conn:
        anchor = store.get_version(conn, "v1")
        diff = (
            "diff --git a/.cursor/hooks.json b/.cursor/hooks.json\n"
            "new file mode 100644\n"
        )
        diff_ref = store.write_object(repo, diff)
        return store.create_version(
            conn,
            type="checkpoint",
            git_branch="main",
            parent_id=anchor.id,
            tree_before=anchor.tree_after,
            tree_after=anchor.tree_after,
            diff_ref=diff_ref,
        )


def test_doctor_reports_then_fixes_phantom_checkpoint(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    phantom = _make_phantom_checkpoint(git_repo)

    report = runner.invoke(app, ["doctor"])
    assert report.exit_code == 0, report.output
    assert "phantom checkpoints:   1" in report.output
    assert phantom.id in report.output

    fixed = runner.invoke(app, ["doctor", "--fix"])
    assert fixed.exit_code == 0, fixed.output
    assert "removed 1 phantom checkpoint" in fixed.output

    with store.connect(git_repo) as conn:
        assert store.get_version(conn, phantom.id) is None  # pruned

    clean = runner.invoke(app, ["doctor"])
    assert "phantom checkpoints:   0" in clean.output


def test_doctor_keeps_real_checkpoints_and_orphans_only_unreferenced(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    # A real checkpoint (touches actual source) must never be pruned.
    with store.connect(git_repo) as conn:
        anchor = store.get_version(conn, "v1")
        real_diff = store.write_object(
            git_repo, "diff --git a/app.py b/app.py\n+real\n"
        )
        store.create_version(
            conn,
            type="checkpoint",
            git_branch="main",
            parent_id=anchor.id,
            tree_before=anchor.tree_after,
            tree_after=anchor.tree_after,
            diff_ref=real_diff,
        )
    # An unreferenced object should be reported and removed.
    orphan = store.objects_dir(git_repo) / ("f" * 64)
    orphan.write_text("dangling blob\n")

    report = runner.invoke(app, ["doctor"])
    assert "phantom checkpoints:   0" in report.output
    assert "orphaned objects:      1" in report.output

    fixed = runner.invoke(app, ["doctor", "--fix"])
    assert fixed.exit_code == 0, fixed.output
    assert not orphan.exists()  # orphan pruned
    # The real checkpoint survived (still referenced object present).
    assert (store.objects_dir(git_repo) / real_diff).exists()
