from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from profact import git_ops, store
from profact.adapters import cursor_hooks, vscode_copilot_hooks
from profact.cli import app

runner = CliRunner()


def _event(
    name: str,
    *,
    session: str | int = "session-1",
    timestamp: str | int = "2026-08-05T10:00:00.000Z",
    **extra,
) -> dict:
    return {
        "hook_event_name": name,
        "session_id": session,
        "timestamp": timestamp,
        **extra,
    }


def _version(
    repo: Path,
    session: str | int = "session-1",
    timestamp: str | int = "2026-08-05T10:00:00.000Z",
):
    with store.connect(repo) as conn:
        return store.find_prompt_by_turn(
            conn,
            f"vscode-copilot-{session}",
            str(timestamp),
        )


def test_vscode_copilot_full_turn_captures_prompt_diff_and_completion(
    git_repo: Path,
) -> None:
    prompt = _event("UserPromptSubmit", prompt="add Copilot hook support")
    assert vscode_copilot_hooks.handle(git_repo, prompt) == {}
    (git_repo / "copilot.py").write_text("enabled = True\n")

    stop = _event(
        "Stop",
        timestamp="2026-08-05T10:01:00.000Z",
        stop_hook_active=False,
    )
    assert vscode_copilot_hooks.handle(git_repo, stop) == {}

    version = _version(git_repo)
    assert version is not None
    assert version.tree_after is not None and version.tree_after != version.tree_before
    assert store.read_object(git_repo, version.prompt_ref) == "add Copilot hook support"
    assert "copilot.py" in (store.read_object(git_repo, version.diff_ref) or "")
    assert version.response_ref is None
    assert version.thinking_source == store.THINKING_NONE
    assert version.model is None
    assert version.completed_at is not None
    assert version.completion_source == "vscode-copilot-stop"
    assert version.input_tokens is None
    assert version.output_tokens is None
    assert version.api_call_count is None


def test_vscode_copilot_duplicate_prompt_and_stop_are_idempotent(
    git_repo: Path,
) -> None:
    prompt = _event("UserPromptSubmit", prompt="do work")
    vscode_copilot_hooks.handle(git_repo, prompt)
    vscode_copilot_hooks.handle(git_repo, prompt)
    stop = _event("Stop", stop_hook_active=False)
    vscode_copilot_hooks.handle(git_repo, stop)
    first = _version(git_repo)
    assert first is not None

    vscode_copilot_hooks.handle(git_repo, dict(stop, stop_hook_active=True))
    second = _version(git_repo)

    assert second is not None
    assert second.completed_at == first.completed_at
    assert second.tree_after == first.tree_after
    with store.connect(git_repo) as conn:
        prompts = [item for item in store.all_versions(conn) if item.type == "prompt"]
    assert len(prompts) == 1


def test_vscode_copilot_refreshes_turn_after_stop_hook_continuation(
    git_repo: Path,
) -> None:
    vscode_copilot_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", prompt="finish all requested work"),
    )
    (git_repo / "first.py").write_text("first = True\n")
    vscode_copilot_hooks.handle(
        git_repo,
        _event("Stop", timestamp="first-stop", stop_hook_active=False),
    )
    first = _version(git_repo)
    assert first is not None and first.tree_after is not None

    (git_repo / "continued.py").write_text("continued = True\n")
    vscode_copilot_hooks.handle(
        git_repo,
        _event("Stop", timestamp="final-stop", stop_hook_active=True),
    )
    refreshed = _version(git_repo)

    assert refreshed is not None
    assert refreshed.tree_after != first.tree_after
    diff = store.read_object(git_repo, refreshed.diff_ref) or ""
    assert "first.py" in diff
    assert "continued.py" in diff
    assert refreshed.completion_source == "vscode-copilot-stop"


def test_vscode_copilot_repeated_prompt_text_uses_timestamp_turn_identity(
    git_repo: Path,
) -> None:
    vscode_copilot_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", timestamp="turn-1", prompt="try again"),
    )
    (git_repo / "first.py").write_text("first = True\n")
    vscode_copilot_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", timestamp="turn-2", prompt="try again"),
    )

    first = _version(git_repo, timestamp="turn-1")
    second = _version(git_repo, timestamp="turn-2")
    assert first is not None and second is not None
    assert first.completion_source == "vscode-copilot-superseded"
    assert first.tree_after is not None
    assert second.tree_after is None
    assert second.parent_id == first.id


def test_vscode_copilot_routes_overlapping_sessions_honestly(
    git_repo: Path,
) -> None:
    vscode_copilot_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", session="one", timestamp="one-1", prompt="one"),
    )
    vscode_copilot_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", session="two", timestamp="two-1", prompt="two"),
    )
    (git_repo / "shared.py").write_text("shared = True\n")
    vscode_copilot_hooks.handle(
        git_repo,
        _event("Stop", session="one", timestamp="one-stop", stop_hook_active=False),
    )
    vscode_copilot_hooks.handle(
        git_repo,
        _event("Stop", session="two", timestamp="two-stop", stop_hook_active=False),
    )

    first = _version(git_repo, session="one", timestamp="one-1")
    second = _version(git_repo, session="two", timestamp="two-1")
    assert first is not None and second is not None
    assert first.confidence == "uncertain"
    assert second.confidence == "uncertain"
    assert first.confidence_reason == "overlapping sessions shared one working tree"
    assert second.confidence_reason == "overlapping sessions shared one working tree"


def test_vscode_copilot_overlap_with_cursor_keeps_turns_separate(
    git_repo: Path,
) -> None:
    cursor_hooks.handle(
        git_repo,
        {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "cursor-session",
            "generation_id": "cursor-turn",
            "prompt": "Cursor prompt",
        },
    )
    vscode_copilot_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", prompt="Copilot prompt"),
    )
    vscode_copilot_hooks.handle(
        git_repo,
        _event("Stop", timestamp="stop", stop_hook_active=False),
    )
    cursor_hooks.handle(
        git_repo,
        {
            "hook_event_name": "stop",
            "conversation_id": "cursor-session",
            "generation_id": "cursor-turn",
        },
    )

    with store.connect(git_repo) as conn:
        cursor_version = store.find_prompt_by_turn(
            conn, "cursor-session", "cursor-turn"
        )
    copilot_version = _version(git_repo)
    assert cursor_version is not None and copilot_version is not None
    assert cursor_version.confidence == "uncertain"
    assert copilot_version.confidence == "uncertain"


def test_vscode_copilot_capture_uses_central_secret_redaction(
    git_repo: Path,
) -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    vscode_copilot_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", prompt=f"remove {secret}"),
    )
    version = _version(git_repo)
    assert version is not None
    prompt = store.read_object(git_repo, version.prompt_ref) or ""
    assert secret not in prompt
    assert "[REDACTED:github-token]" in prompt


def test_vscode_copilot_accepts_zero_session_and_timestamp(git_repo: Path) -> None:
    vscode_copilot_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", session=0, timestamp=0, prompt="zero ids"),
    )
    vscode_copilot_hooks.handle(
        git_repo,
        _event("Stop", session=0, timestamp=1, stop_hook_active=False),
    )
    version = _version(git_repo, session=0, timestamp=0)
    assert version is not None
    assert version.completion_source == "vscode-copilot-stop"


def test_vscode_copilot_install_merges_and_protects_only_generated_hooks(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    hooks_dir = git_repo / ".github" / "hooks"
    hooks_dir.mkdir(parents=True)
    config_path = hooks_dir / "profact.json"
    config_path.write_text(
        json.dumps(
            {
                "$schema": "https://example.invalid/hooks.schema.json",
                "hooks": {
                    "PostToolUse": [
                        {"type": "command", "command": "echo existing"}
                    ]
                },
            }
        )
    )
    team_hook = hooks_dir / "team.json"
    team_hook.write_text(
        json.dumps(
            {"hooks": {"PostToolUse": [{"type": "command", "command": "echo team"}]}}
        )
    )

    first = runner.invoke(app, ["start", "--copilot"])
    second = runner.invoke(app, ["start", "--copilot"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "VS Code GitHub Copilot" in first.output

    config = json.loads(config_path.read_text())
    assert config["$schema"] == "https://example.invalid/hooks.schema.json"
    assert config["hooks"]["PostToolUse"] == [
        {"type": "command", "command": "echo existing"}
    ]
    for event_name in vscode_copilot_hooks.HANDLED_EVENTS:
        handlers = config["hooks"][event_name]
        profact_handlers = [
            item
            for item in handlers
            if "profact-vscode-hook" in item.get("command", "")
        ]
        assert len(profact_handlers) == 1
        assert "windows" in profact_handlers[0]
        assert profact_handlers[0]["command"] == (
            '"./.github/hooks/profact-vscode-hook.sh"'
        )
        assert profact_handlers[0]["windows"] == (
            '".\\.github\\hooks\\profact-vscode-hook.cmd"'
        )
        assert profact_handlers[0]["cwd"] == "."
        assert "hooks" not in profact_handlers[0]

    shell_runner = git_repo / vscode_copilot_hooks.SHELL_RUNNER_REL
    windows_runner = git_repo / vscode_copilot_hooks.WINDOWS_RUNNER_REL
    assert shell_runner.exists()
    assert windows_runner.exists()
    ignored = (git_repo / ".gitignore").read_text().splitlines()
    assert vscode_copilot_hooks.CONFIG_REL in ignored
    assert vscode_copilot_hooks.SHELL_RUNNER_REL in ignored
    assert vscode_copilot_hooks.WINDOWS_RUNNER_REL in ignored

    tree = git_ops.write_tree(git_repo)
    files = set(git_ops.list_tree_files(git_repo, tree))
    assert vscode_copilot_hooks.CONFIG_REL not in files
    assert vscode_copilot_hooks.SHELL_RUNNER_REL not in files
    assert vscode_copilot_hooks.WINDOWS_RUNNER_REL not in files
    assert ".github/hooks/team.json" in files

    with store.connect(git_repo) as conn:
        anchor = store.get_version(conn, "v1")
    assert anchor is not None and anchor.tree_after is not None
    git_ops.checkout_tree(git_repo, anchor.tree_after, prev_tree=tree)
    assert config_path.exists()
    assert shell_runner.exists()
    assert windows_runner.exists()
    assert not team_hook.exists()


def test_vscode_copilot_hidden_cli_uses_hook_working_directory(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    event = _event("UserPromptSubmit", prompt="via hidden CLI")
    invoked = runner.invoke(app, ["copilot-hook"], input=json.dumps(event))
    assert invoked.exit_code == 0, invoked.output
    assert json.loads(invoked.output) == {}
    assert _version(git_repo) is not None

    garbage = runner.invoke(app, ["copilot-hook"], input="not-json")
    assert garbage.exit_code == 0
    assert json.loads(garbage.output) == {}


def test_vscode_copilot_generated_platform_runner_executes(
    git_repo: Path,
) -> None:
    _config_rel, runner_rel = vscode_copilot_hooks.install(git_repo)
    runner_path = git_repo / runner_rel
    event = _event(
        "UserPromptSubmit",
        timestamp="runner-turn",
        prompt="through generated runner",
        cwd=str(git_repo),
    )
    command = (
        ["cmd.exe", "/d", "/c", str(runner_path)]
        if os.name == "nt"
        else [str(runner_path)]
    )

    completed = subprocess.run(
        command,
        cwd=git_repo,
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {}
    assert _version(git_repo, timestamp="runner-turn") is not None


def test_vscode_copilot_install_preserves_malformed_config(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    hooks_dir = git_repo / ".github" / "hooks"
    hooks_dir.mkdir(parents=True)
    config_path = hooks_dir / "profact.json"
    original = "{not valid JSON\n"
    config_path.write_text(original)

    result = runner.invoke(app, ["start", "--copilot"])

    assert result.exit_code == 1
    assert "invalid VS Code hooks JSON" in result.output
    assert config_path.read_text() == original
    assert not (git_repo / vscode_copilot_hooks.SHELL_RUNNER_REL).exists()
    assert not (git_repo / vscode_copilot_hooks.WINDOWS_RUNNER_REL).exists()


def test_vscode_copilot_ignores_events_without_durable_identity(
    git_repo: Path,
) -> None:
    missing_session = _event("UserPromptSubmit", prompt="x")
    missing_session.pop("session_id")
    missing_timestamp = _event("UserPromptSubmit", prompt="y")
    missing_timestamp.pop("timestamp")

    assert vscode_copilot_hooks.handle(git_repo, missing_session) == {}
    assert vscode_copilot_hooks.handle(git_repo, missing_timestamp) == {}
    with store.connect(git_repo) as conn:
        assert not [item for item in store.all_versions(conn) if item.type == "prompt"]


def test_vscode_copilot_uninitialized_repo_is_noop(tmp_path: Path) -> None:
    event = _event("UserPromptSubmit", prompt="x")
    assert vscode_copilot_hooks.handle(tmp_path, event) == {}


def test_start_rejects_copilot_with_another_capture_mode(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    result = runner.invoke(app, ["start", "--codex", "--copilot"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
