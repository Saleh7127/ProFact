from __future__ import annotations

from pathlib import Path
from typing import Callable

from typer.testing import CliRunner

from profact import git_ops, store
from profact.cli import app

runner = CliRunner()


def _make_prompt(repo: Path, *, prompt: str, mutate: Callable[[], None]) -> store.Version:
    """Mimic a finalized capture turn: snapshot before, mutate, snapshot after."""
    branch = git_ops.current_branch(repo)
    tree_before = git_ops.write_tree(repo)
    parent = store.read_head(repo, branch)
    prompt_ref = store.write_object(repo, prompt)
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
            response_ref=store.write_object(repo, "done"),
            diff_ref=diff_ref,
            model="claude-test",
        )
    store.write_head(repo, branch, version.id)
    return version


def test_log_lists_branch_versions(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    _make_prompt(
        git_repo,
        prompt="create button",
        mutate=lambda: (git_repo / "button.py").write_text("color = 'blue'\n"),
    )

    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0, result.output
    assert "create button" in result.output
    assert "anchor" in result.output
    assert "HEAD" in result.output


def test_show_renders_prompt_response_and_diff(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    version = _make_prompt(
        git_repo,
        prompt="add a greeting",
        mutate=lambda: (git_repo / "greet.py").write_text("print('hi')\n"),
    )

    result = runner.invoke(app, ["show", version.id])
    assert result.exit_code == 0, result.output
    assert "add a greeting" in result.output
    assert "done" in result.output
    assert "greet.py" in result.output  # diff mentions the new file


def test_show_unknown_version_errors(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    result = runner.invoke(app, ["show", "v999"])
    assert result.exit_code == 1
    assert "no such version" in result.output


def test_status_reports_clean_then_dirty(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    clean = runner.invoke(app, ["status"])
    assert clean.exit_code == 0, clean.output
    assert "clean" in clean.output

    (git_repo / "scratch.txt").write_text("uncommitted\n")
    dirty = runner.invoke(app, ["status"])
    assert dirty.exit_code == 0, dirty.output
    assert "dirty" in dirty.output


def test_checkout_branching_never_loses_work(git_repo: Path, monkeypatch) -> None:
    """The core DoD: branch backward, both lines survive, checkout loses nothing."""
    monkeypatch.chdir(git_repo)
    button = git_repo / "button.py"

    v2 = _make_prompt(
        git_repo,
        prompt="make the button blue",
        mutate=lambda: button.write_text("color = 'blue'\n"),
    )
    assert button.read_text() == "color = 'blue'\n"

    # Go back to the anchor (v1). button.py should disappear from the working dir.
    back = runner.invoke(app, ["checkout", "v1"])
    assert back.exit_code == 0, back.output
    assert store.read_head(git_repo, "main") == "v1"
    assert not button.exists()

    # New prompt from v1 -> v3, a separate branch (parent is v1, not v2).
    v3 = _make_prompt(
        git_repo,
        prompt="make the button green",
        mutate=lambda: button.write_text("color = 'green'\n"),
    )
    assert v3.parent_id == "v1"
    assert button.read_text() == "color = 'green'\n"

    # --graph must reveal both branches off v1.
    graph = runner.invoke(app, ["log", "--graph"])
    assert graph.exit_code == 0, graph.output
    assert v2.id in graph.output and v3.id in graph.output
    assert "branch points: v1" in graph.output

    # Checkout v2 -> blue restored; v3's green work is NOT lost, just not HEAD.
    to_blue = runner.invoke(app, ["checkout", v2.id])
    assert to_blue.exit_code == 0, to_blue.output
    assert button.read_text() == "color = 'blue'\n"
    assert store.read_head(git_repo, "main") == v2.id

    # Prove v3 survived: checking it back out restores green.
    to_green = runner.invoke(app, ["checkout", v3.id])
    assert to_green.exit_code == 0, to_green.output
    assert button.read_text() == "color = 'green'\n"


def test_checkout_autocheckpoints_uncommitted_work(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    v2 = _make_prompt(
        git_repo,
        prompt="add config",
        mutate=lambda: (git_repo / "config.py").write_text("DEBUG = True\n"),
    )

    # Manual, uncaptured edit on top of HEAD.
    (git_repo / "config.py").write_text("DEBUG = False\n")

    result = runner.invoke(app, ["checkout", "v1"])
    assert result.exit_code == 0, result.output
    assert "checkpoint" in result.output.lower()

    # The checkpoint preserved the manual edit; restoring it brings DEBUG=False back.
    with store.connect(git_repo) as conn:
        checkpoints = [
            v for v in store.list_versions(conn, "main") if v.type == "checkpoint"
        ]
    assert len(checkpoints) == 1
    runner.invoke(app, ["checkout", checkpoints[0].id])
    assert (git_repo / "config.py").read_text() == "DEBUG = False\n"


def test_graph_renders_ascii_dag_plain_log_is_flat(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    button = git_repo / "button.py"
    _make_prompt(git_repo, prompt="blue", mutate=lambda: button.write_text("blue\n"))
    runner.invoke(app, ["checkout", "v1"])
    _make_prompt(git_repo, prompt="green", mutate=lambda: button.write_text("green\n"))

    # --graph: real DAG with node markers, a convergence connector, and a footer.
    graph = runner.invoke(app, ["log", "--graph"])
    assert graph.exit_code == 0, graph.output
    assert "DAG for branch 'main'" in graph.output
    assert "*" in graph.output  # node marker
    assert "/" in graph.output  # lanes converging on the shared parent (v1)
    assert "branch points: v1" in graph.output

    # plain log: flat list — no header, no graph glyphs, rows don't start with '*'.
    plain = runner.invoke(app, ["log"])
    assert plain.exit_code == 0, plain.output
    assert "DAG for branch" not in plain.output
    assert "/" not in plain.output
    for line in plain.output.splitlines():
        if line.strip():
            assert not line.lstrip().startswith("*")


def test_log_path_shows_only_head_ancestry(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    button = git_repo / "button.py"
    v2 = _make_prompt(
        git_repo, prompt="blue", mutate=lambda: button.write_text("blue\n")
    )
    runner.invoke(app, ["checkout", "v1"])
    v3 = _make_prompt(
        git_repo, prompt="green", mutate=lambda: button.write_text("green\n")
    )

    result = runner.invoke(app, ["log", "--path"])
    assert result.exit_code == 0, result.output
    # HEAD is v3; its ancestry is v3 -> v1. v2 is on the other branch and excluded.
    assert v3.id in result.output
    assert "v1" in result.output
    assert v2.id not in result.output
