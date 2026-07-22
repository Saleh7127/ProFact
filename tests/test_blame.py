from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from typer.testing import CliRunner

from profact import blame as blame_mod
from profact import git_ops, store
from profact.cli import app

runner = CliRunner()


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _prompt(
    repo: Path, *, text: str, mutate: Callable[[], None], response: str | None = None
) -> store.Version:
    """Mimic one finalized capture turn on the current Git branch."""
    branch = git_ops.current_branch(repo)
    tree_before = git_ops.write_tree(repo)
    parent = store.read_head(repo, branch)
    prompt_ref = store.write_object(repo, text)
    response_ref = store.write_object(repo, response) if response else None
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
            response_ref=response_ref,
            diff_ref=diff_ref,
        )
    store.write_head(repo, branch, version.id)
    return version


def _blame_map(repo: Path, file: str, version_id: str) -> dict[int, str]:
    """lineno -> version id for the file at version_id."""
    with store.connect(repo) as conn:
        result = blame_mod.blame_file(repo, conn, file=file, version_id=version_id)
    return {line.lineno: (line.version.id if line.version else "?") for line in result.lines}


def test_blame_attributes_each_line_to_the_prompt_that_wrote_it(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"

    v2 = _prompt(git_repo, text="add greet", mutate=lambda: f.write_text("a\nb\n"))
    v3 = _prompt(git_repo, text="add third", mutate=lambda: f.write_text("a\nb\nc\n"))

    mapping = _blame_map(git_repo, "app.py", v3.id)
    # Lines 1-2 came from v2; line 3 from v3.
    assert mapping == {1: v2.id, 2: v2.id, 3: v3.id}


def test_blame_marks_preexisting_lines_as_anchor(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    # a.txt ("hello\n") existed at the anchor (before any prompt).
    a = git_repo / "a.txt"
    v2 = _prompt(git_repo, text="append", mutate=lambda: a.write_text("hello\nworld\n"))

    with store.connect(git_repo) as conn:
        result = blame_mod.blame_file(git_repo, conn, file="a.txt", version_id=v2.id)
    by_line = {ln.lineno: ln.version for ln in result.lines}
    assert by_line[1].type == "anchor"  # pre-existing
    assert by_line[2].id == v2.id  # added by the prompt


def test_blame_revert_and_readd_attributes_to_last_writer(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    _prompt(git_repo, text="add x", mutate=lambda: f.write_text("x\n"))
    _prompt(git_repo, text="remove x", mutate=lambda: f.write_text("y\n"))
    v4 = _prompt(git_repo, text="re-add x", mutate=lambda: f.write_text("y\nx\n"))

    mapping = _blame_map(git_repo, "app.py", v4.id)
    # The re-added "x" belongs to v4 (last writer), not the original v2.
    assert mapping[2] == v4.id


def test_blame_at_historical_version_ignores_later_edits(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    v2 = _prompt(git_repo, text="one line", mutate=lambda: f.write_text("one\n"))
    _prompt(git_repo, text="two lines", mutate=lambda: f.write_text("one\ntwo\n"))

    # Blaming at v2 sees only the single line as of v2.
    mapping = _blame_map(git_repo, "app.py", v2.id)
    assert mapping == {1: v2.id}


def test_blame_follows_branch_ancestry(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    v2 = _prompt(git_repo, text="base", mutate=lambda: f.write_text("base\n"))

    _git(["checkout", "-b", "experiment"], git_repo)
    v3 = _prompt(git_repo, text="exp line", mutate=lambda: f.write_text("base\nexp\n"))

    mapping = _blame_map(git_repo, "app.py", v3.id)
    # Carried-over trunk line -> v2 (main); experiment line -> v3.
    assert mapping == {1: v2.id, 2: v3.id}


def test_blame_cli_tier1_and_show(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    _prompt(
        git_repo,
        text="make it say hi",
        response="Sure, added a greeting.",
        mutate=lambda: f.write_text("print('hi')\n"),
    )

    res = runner.invoke(app, ["blame", "app.py"])
    assert res.exit_code == 0, res.output
    assert "make it say hi" in res.output
    assert "print('hi')" in res.output

    shown = runner.invoke(app, ["blame", "app.py", "--show"])
    assert shown.exit_code == 0, shown.output
    assert "make it say hi" in shown.output
    assert "Sure, added a greeting." in shown.output  # response excerpt (Tier 2)
    assert "change to app.py:" in shown.output


def test_blame_cli_line_range(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    v2 = _prompt(git_repo, text="two", mutate=lambda: f.write_text("a\nb\n"))
    v3 = _prompt(git_repo, text="three", mutate=lambda: f.write_text("a\nb\nc\n"))

    res = runner.invoke(app, ["blame", "app.py", "-L", "3:3"])
    assert res.exit_code == 0, res.output
    assert v3.id in res.output
    # Only line 3 shown; line 1/2's content should be absent.
    assert "3| c" in res.output
    assert "1| a" not in res.output


def test_why_expands_single_line(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    _prompt(git_repo, text="one", mutate=lambda: f.write_text("a\n"))
    v3 = _prompt(
        git_repo, text="add special line", response="done", mutate=lambda: f.write_text("a\nspecial\n")
    )

    res = runner.invoke(app, ["why", "app.py:2"])
    assert res.exit_code == 0, res.output
    assert v3.id in res.output
    assert "add special line" in res.output


def test_blame_errors_on_missing_file_and_version(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    _prompt(git_repo, text="one", mutate=lambda: f.write_text("a\n"))

    missing = runner.invoke(app, ["blame", "nope.py"])
    assert missing.exit_code == 1
    assert "does not exist" in missing.output

    bad_version = runner.invoke(app, ["blame", "app.py", "v999"])
    assert bad_version.exit_code == 1
    assert "no such version" in bad_version.output
