from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

from typer.testing import CliRunner

from profact import bisect as bisect_mod
from profact import git_ops, store
from profact.cli import app

runner = CliRunner()


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _prompt(repo: Path, *, text: str, mutate: Callable[[], None]) -> store.Version:
    """Mimic one finalized capture turn on the current Git branch."""
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


# A test command that passes (exit 0) only while app.py still contains "OK".
_CHECK_OK = [
    sys.executable,
    "-c",
    "import sys; sys.exit(0 if 'OK' in open('app.py').read() else 1)",
]


def _build_regression(repo: Path) -> dict[str, store.Version]:
    """Trunk where the 'OK' marker survives until v4 removes it (the culprit)."""
    f = repo / "app.py"
    v2 = _prompt(repo, text="good one", mutate=lambda: f.write_text("OK\n"))
    v3 = _prompt(repo, text="good two", mutate=lambda: f.write_text("OK\nmore\n"))
    v4 = _prompt(repo, text="break it", mutate=lambda: f.write_text("BROKEN\nmore\n"))
    v5 = _prompt(repo, text="carry on", mutate=lambda: f.write_text("BROKEN\nmore\nx\n"))
    v6 = _prompt(repo, text="carry more", mutate=lambda: f.write_text("BROKEN\nmore\nx\ny\n"))
    return {"v2": v2, "v3": v3, "v4": v4, "v5": v5, "v6": v6}


def test_bisect_finds_the_culprit_version(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    vs = _build_regression(git_repo)

    with store.connect(git_repo) as conn:
        result = bisect_mod.bisect(
            git_repo, conn, good_id="v2", bad_id=vs["v6"].id, cmd=_CHECK_OK
        )
    assert result.first_bad is not None
    assert result.first_bad.id == vs["v4"].id


def test_bisect_restores_working_tree_and_head(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    vs = _build_regression(git_repo)
    f = git_repo / "app.py"
    before = f.read_text()
    head_before = store.read_head(git_repo, "main")

    with store.connect(git_repo) as conn:
        bisect_mod.bisect(git_repo, conn, good_id="v2", bad_id=vs["v6"].id, cmd=_CHECK_OK)

    # Working tree and HEAD are exactly as they were pre-bisect (non-destructive).
    assert f.read_text() == before
    assert store.read_head(git_repo, "main") == head_before
    # No checkpoint/version was created during bisect.
    with store.connect(git_repo) as conn:
        assert store.get_version(conn, vs["v6"].id) is not None
        assert store.get_version(conn, "v7") is None


def test_bisect_rejects_good_that_is_not_ancestor(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    f = git_repo / "app.py"
    _prompt(git_repo, text="main", mutate=lambda: f.write_text("OK\n"))

    _git(["checkout", "-b", "experiment"], git_repo)
    v_exp = _prompt(git_repo, text="exp", mutate=lambda: f.write_text("BROKEN\n"))

    _git(["checkout", "main"], git_repo)
    v_main = _prompt(git_repo, text="main2", mutate=lambda: f.write_text("BROKEN2\n"))

    # v_exp is on another line, not an ancestor of v_main.
    with store.connect(git_repo) as conn:
        try:
            bisect_mod.bisect(
                git_repo, conn, good_id=v_exp.id, bad_id=v_main.id, cmd=_CHECK_OK
            )
            assert False, "expected BisectError"
        except bisect_mod.BisectError as exc:
            assert "not an ancestor" in str(exc)


def test_bisect_validates_endpoints(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    vs = _build_regression(git_repo)

    with store.connect(git_repo) as conn:
        # Marking a still-good version as bad should fail loudly.
        try:
            bisect_mod.bisect(
                git_repo, conn, good_id="v2", bad_id=vs["v3"].id, cmd=_CHECK_OK
            )
            assert False, "expected BisectError for good-as-bad"
        except bisect_mod.BisectError as exc:
            assert "passed the test" in str(exc)

    with store.connect(git_repo) as conn:
        # Marking an already-broken version as good should fail loudly.
        try:
            bisect_mod.bisect(
                git_repo, conn, good_id=vs["v4"].id, bad_id=vs["v6"].id, cmd=_CHECK_OK
            )
            assert False, "expected BisectError for bad-as-good"
        except bisect_mod.BisectError as exc:
            assert "failed the test" in str(exc)


def _build_long_regression(repo: Path) -> dict[str, store.Version]:
    """Eight prompts (v2..v9); the 'OK' marker survives until v6 (the culprit).

    Each snapshot has unique content so a test command can single one out to
    skip without ambiguity.
    """
    f = repo / "app.py"
    out: dict[str, store.Version] = {}
    plan = [
        ("g1", "OK\n1\n"),
        ("g2", "OK\n2\n"),
        ("g3", "OK\n3\n"),  # the snapshot we will force a SKIP on
        ("g4", "OK\n4\n"),
        ("bug", "BROKEN\n5\n"),  # culprit
        ("b2", "BROKEN\n6\n"),
        ("b3", "BROKEN\n7\n"),
        ("b4", "BROKEN\n8\n"),
    ]
    for text, content in plan:
        out[text] = _prompt(repo, text=text, mutate=lambda c=content: f.write_text(c))
    return out


def test_bisect_handles_skips(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    vs = _build_long_regression(git_repo)
    # Exit 125 (skip) for the 'OK\n3\n' snapshot; otherwise good iff 'OK' present.
    skip_cmd = [
        sys.executable,
        "-c",
        (
            "import sys\n"
            "t=open('app.py').read()\n"
            "sys.exit(125 if t=='OK\\n3\\n' else (0 if 'OK' in t else 1))"
        ),
    ]
    with store.connect(git_repo) as conn:
        result = bisect_mod.bisect(
            git_repo, conn, good_id=vs["g1"].id, bad_id=vs["b4"].id, cmd=skip_cmd
        )
    # The 'OK\n3\n' snapshot is skipped but is not on the good/bad boundary, so
    # bisect routes around it and still converges on the real culprit.
    assert any(s.outcome == "skip" for s in result.steps)
    assert result.first_bad is not None
    assert result.first_bad.id == vs["bug"].id


def test_bisect_cli_reports_culprit(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    vs = _build_regression(git_repo)

    result = runner.invoke(
        app, ["bisect", "--good", "v2", "--bad", vs["v6"].id, "--", *_CHECK_OK]
    )
    assert result.exit_code == 0, result.output
    assert f"first bad version: {vs['v4'].id}" in result.output
    assert "break it" in result.output  # the culprit prompt summary


def test_bisect_cli_defaults_bad_to_head(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    vs = _build_regression(git_repo)  # HEAD is now v6 (bad)

    result = runner.invoke(app, ["bisect", "--good", "v2", "--", *_CHECK_OK])
    assert result.exit_code == 0, result.output
    assert f"first bad version: {vs['v4'].id}" in result.output


def test_bisect_cli_errors_on_unknown_version(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    _build_regression(git_repo)

    result = runner.invoke(app, ["bisect", "--good", "v999", "--", *_CHECK_OK])
    assert result.exit_code == 1
    assert "no such version" in result.output
