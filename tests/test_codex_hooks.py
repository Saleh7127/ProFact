from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from profact import git_ops, store
from profact.adapters import codex_hooks, cursor_hooks
from profact.cli import app

runner = CliRunner()


def _event(name: str, *, session: str = "thread-1", turn: str | None = "turn-1", **extra) -> dict:
    event = {
        "hook_event_name": name,
        "session_id": session,
        "cwd": "",
        "model": "gpt-test",
    }
    if turn is not None:
        event["turn_id"] = turn
    event.update(extra)
    return event


def _version(repo: Path, session: str = "thread-1", turn: str = "turn-1"):
    with store.connect(repo) as conn:
        return store.find_prompt_by_turn(conn, f"codex-{session}", turn)


def test_codex_full_turn_captures_prompt_response_diff_and_completion(
    git_repo: Path,
) -> None:
    prompt = _event("UserPromptSubmit", prompt="add a Codex feature")
    assert codex_hooks.handle(git_repo, prompt) == {}
    (git_repo / "codex.py").write_text("enabled = True\n")

    stopped = _event(
        "Stop",
        last_assistant_message="Implemented the Codex feature.",
        stop_hook_active=False,
    )
    assert codex_hooks.handle(git_repo, stopped) == {}

    version = _version(git_repo)
    assert version is not None
    assert version.tree_after is not None and version.tree_after != version.tree_before
    assert store.read_object(git_repo, version.prompt_ref) == "add a Codex feature"
    assert store.read_object(git_repo, version.response_ref) == "Implemented the Codex feature."
    assert "codex.py" in (store.read_object(git_repo, version.diff_ref) or "")
    assert version.model == "gpt-test"
    assert version.thinking_source == store.THINKING_NONE
    assert version.completed_at is not None
    assert version.completion_source == "codex-stop"
    assert version.input_tokens is None and version.api_call_count is None


def test_codex_duplicate_stop_is_idempotent_and_does_not_duplicate_response(
    git_repo: Path,
) -> None:
    prompt = _event("UserPromptSubmit", prompt="do work")
    codex_hooks.handle(git_repo, prompt)
    codex_hooks.handle(git_repo, prompt)
    stop = _event("Stop", last_assistant_message="Done.", stop_hook_active=False)
    codex_hooks.handle(git_repo, stop)
    first = _version(git_repo)
    assert first is not None

    duplicate = dict(stop, stop_hook_active=True)
    codex_hooks.handle(git_repo, duplicate)
    second = _version(git_repo)

    assert second is not None
    assert second.completed_at == first.completed_at
    assert second.tree_after == first.tree_after
    assert store.read_object(git_repo, second.response_ref) == "Done."
    with store.connect(git_repo) as conn:
        prompts = [item for item in store.all_versions(conn) if item.type == "prompt"]
    assert len(prompts) == 1


def test_codex_accepts_zero_valued_session_and_turn_ids(git_repo: Path) -> None:
    codex_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", session=0, turn=0, prompt="zero ids"),
    )
    codex_hooks.handle(
        git_repo,
        _event("Stop", session=0, turn=0, last_assistant_message="Done."),
    )
    with store.connect(git_repo) as conn:
        version = store.find_prompt_by_turn(conn, "codex-0", "0")
    assert version is not None
    assert version.completion_source == "codex-stop"


def test_codex_new_prompt_supersedes_only_its_session_turn(git_repo: Path) -> None:
    codex_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", turn="turn-1", prompt="first"),
    )
    (git_repo / "first.py").write_text("first = True\n")
    codex_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", turn="turn-2", prompt="second"),
    )

    first = _version(git_repo, turn="turn-1")
    second = _version(git_repo, turn="turn-2")
    assert first is not None and second is not None
    assert first.tree_after is not None
    assert first.completion_source == "codex-superseded"
    assert second.tree_after is None
    assert second.parent_id == first.id


def test_codex_session_end_finalizes_and_reconciles_trailing_human_work(
    git_repo: Path,
) -> None:
    codex_hooks.handle(git_repo, _event("UserPromptSubmit", prompt="finish session"))
    (git_repo / "agent.py").write_text("agent = True\n")
    codex_hooks.handle(
        git_repo,
        _event("SessionEnd", turn=None, reason="other"),
    )
    version = _version(git_repo)
    assert version is not None
    assert version.tree_after is not None
    assert version.completion_source == "codex-session-end"

    (git_repo / "manual.py").write_text("human = True\n")
    codex_hooks.handle(
        git_repo,
        _event("SessionEnd", turn=None, reason="other"),
    )
    with store.connect(git_repo) as conn:
        humans = [item for item in store.all_versions(conn) if item.type == "human"]
    assert len(humans) == 1
    assert "manual.py" in (store.read_object(git_repo, humans[0].diff_ref) or "")


def test_codex_late_stop_routes_response_without_resnapshot(git_repo: Path) -> None:
    codex_hooks.handle(git_repo, _event("UserPromptSubmit", prompt="late response"))
    codex_hooks.handle(git_repo, _event("SessionEnd", turn=None, reason="other"))
    before = _version(git_repo)
    assert before is not None
    (git_repo / "later.py").write_text("later = True\n")

    codex_hooks.handle(
        git_repo,
        _event("Stop", last_assistant_message="Late final answer."),
    )
    after = _version(git_repo)

    assert after is not None
    assert after.tree_after == before.tree_after
    assert after.diff_ref == before.diff_ref
    assert after.completed_at == before.completed_at
    assert after.confidence == "uncertain"
    assert store.read_object(git_repo, after.response_ref) == "Late final answer."


def test_codex_and_cursor_overlap_route_separately(git_repo: Path) -> None:
    cursor_hooks.handle(
        git_repo,
        {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "cursor-session",
            "generation_id": "cursor-turn",
            "prompt": "Cursor prompt",
        },
    )
    codex_hooks.handle(git_repo, _event("UserPromptSubmit", prompt="Codex prompt"))
    codex_hooks.handle(git_repo, _event("Stop", last_assistant_message="Codex response"))
    cursor_hooks.handle(
        git_repo,
        {
            "hook_event_name": "afterAgentResponse",
            "conversation_id": "cursor-session",
            "generation_id": "cursor-turn",
            "text": "Cursor response",
        },
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
        cursor_version = store.find_prompt_by_turn(conn, "cursor-session", "cursor-turn")
        codex_version = store.find_prompt_by_turn(conn, "codex-thread-1", "turn-1")
    assert cursor_version is not None and codex_version is not None
    assert cursor_version.confidence == "uncertain"
    assert codex_version.confidence == "uncertain"
    assert store.read_object(git_repo, cursor_version.response_ref) == "Cursor response"
    assert store.read_object(git_repo, codex_version.response_ref) == "Codex response"


def test_codex_capture_uses_central_secret_redaction(git_repo: Path) -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    codex_hooks.handle(
        git_repo,
        _event("UserPromptSubmit", prompt=f"use {secret}"),
    )
    codex_hooks.handle(
        git_repo,
        _event("Stop", last_assistant_message=f"removed {secret}"),
    )
    version = _version(git_repo)
    assert version is not None
    prompt = store.read_object(git_repo, version.prompt_ref) or ""
    response = store.read_object(git_repo, version.response_ref) or ""
    assert secret not in prompt and secret not in response
    assert "[REDACTED:github-token]" in prompt
    assert "[REDACTED:github-token]" in response


def test_codex_debug_probe_redacts_nested_event_content(
    git_repo: Path, monkeypatch
) -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    monkeypatch.setenv("PROFACT_CODEX_HOOK_DEBUG", "1")
    codex_hooks.handle(
        git_repo,
        _event(
            "UserPromptSubmit",
            prompt=f"use {secret}",
            metadata={"nested": secret},
        ),
    )
    log = (git_repo / ".profact" / "codex-hook-events.log").read_text()
    assert secret not in log
    assert log.count("[REDACTED:github-token]") == 2


def test_codex_install_merges_idempotently_and_protects_infrastructure(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    codex_dir = git_repo / ".codex"
    codex_dir.mkdir()
    config_path = codex_dir / "hooks.json"
    config_path.write_text(
        json.dumps(
            {
                "description": "existing",
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo existing"}]}
                    ]
                },
            }
        )
    )

    first = runner.invoke(app, ["start", "--codex"])
    second = runner.invoke(app, ["start", "--codex"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "review and trust" in first.output

    config = json.loads(config_path.read_text())
    assert config["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "echo existing"
    for event_name in codex_hooks.HANDLED_EVENTS:
        groups = config["hooks"][event_name]
        profact_handlers = [
            handler
            for group in groups
            for handler in group.get("hooks", [])
            if "profact-hook" in handler.get("command", "")
        ]
        assert len(profact_handlers) == 1
        assert "commandWindows" in profact_handlers[0]
    assert (git_repo / ".codex" / "hooks" / "profact-hook.sh").exists()
    assert (git_repo / ".codex" / "hooks" / "profact-hook.cmd").exists()
    ignored = (git_repo / ".gitignore").read_text().splitlines()
    assert ".codex/hooks.json" in ignored
    assert ".codex/hooks/" in ignored

    tree = git_ops.write_tree(git_repo)
    files = set(git_ops.list_tree_files(git_repo, tree))
    assert not any(path.startswith(".codex/hooks") for path in files)
    with store.connect(git_repo) as conn:
        anchor = store.get_version(conn, "v1")
    assert anchor is not None and anchor.tree_after is not None
    current = git_ops.write_tree(git_repo)
    git_ops.checkout_tree(git_repo, anchor.tree_after, prev_tree=current)
    assert config_path.exists()
    assert (git_repo / ".codex" / "hooks" / "profact-hook.cmd").exists()


def test_codex_hook_cli_resolves_cwd_and_fails_open(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo.parent)
    event = _event(
        "UserPromptSubmit",
        prompt="via hidden CLI",
        cwd=str(git_repo),
    )
    invoked = runner.invoke(app, ["codex-hook"], input=json.dumps(event))
    assert invoked.exit_code == 0, invoked.output
    assert json.loads(invoked.output) == {}
    assert _version(git_repo) is not None

    garbage = runner.invoke(app, ["codex-hook"], input="not-json")
    assert garbage.exit_code == 0
    assert json.loads(garbage.output) == {}


def test_codex_install_preserves_malformed_existing_config(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    codex_dir = git_repo / ".codex"
    codex_dir.mkdir()
    config_path = codex_dir / "hooks.json"
    original = "{not valid JSON\n"
    config_path.write_text(original)

    result = runner.invoke(app, ["start", "--codex"])

    assert result.exit_code == 1
    assert "invalid Codex hooks JSON" in result.output
    assert config_path.read_text() == original


def test_codex_uninitialized_repo_is_noop(tmp_path: Path) -> None:
    assert codex_hooks.handle(tmp_path, _event("UserPromptSubmit", prompt="x")) == {}


def test_start_rejects_multiple_capture_modes(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    result = runner.invoke(app, ["start", "--claude", "--codex"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
