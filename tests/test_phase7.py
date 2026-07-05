from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from profact import git_ops, session, store
from profact.cli import app

runner = CliRunner()


def test_note_links_version_to_commit(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    commit = git_ops.head_commit(git_repo)

    result = runner.invoke(app, ["note", commit])
    assert result.exit_code == 0, result.output
    assert "linked v1" in result.output

    notes = subprocess.run(
        ["git", "notes", "--ref=profact", "show", commit],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert notes.returncode == 0
    assert json.loads(notes.stdout)["version_id"] == "v1"


def test_note_rejects_unknown_version(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    commit = git_ops.head_commit(git_repo)
    result = runner.invoke(app, ["note", commit, "v999"])
    assert result.exit_code == 1
    assert "no such version" in result.output


def test_start_claude_errors_without_claude_binary(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    result = runner.invoke(app, ["start", "--claude"])
    assert result.exit_code != 0
    assert "claude" in (result.output + str(result.exception)).lower()


def test_start_claude_runs_proxy_then_claude(git_repo: Path, monkeypatch) -> None:
    """run_claude wires proxy+watcher around the claude subprocess and tears down."""
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")

    captured: dict = {}

    def fake_run(cmd, env=None, cwd=None):
        captured["cmd"] = cmd
        captured["base_url"] = env.get("ANTHROPIC_BASE_URL")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("subprocess.run", fake_run)
    code = session.run_claude(git_repo, port=7999)
    assert code == 0
    assert captured["cmd"] == ["/usr/bin/claude"]
    assert captured["base_url"] == "http://127.0.0.1:7999"
