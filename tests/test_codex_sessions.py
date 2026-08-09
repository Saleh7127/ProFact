from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from profact import store
from profact.adapters import codex_sessions, hook_capture
from profact.cli import app

runner = CliRunner()


def _record(record_type: str, payload: dict, timestamp: str) -> dict:
    return {"timestamp": timestamp, "type": record_type, "payload": payload}


def _meta(repo: Path, session: str = "session-1") -> dict:
    return _record(
        "session_meta",
        {
            "session_id": session,
            "cwd": str(repo),
            "source": "vscode",
            "originator": "codex_vscode",
        },
        "2026-08-09T00:00:00+00:00",
    )


def _usage(total: dict, last: dict | None = None) -> dict:
    return _record(
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "total_token_usage": total,
                "last_token_usage": last or total,
            },
        },
        "2026-08-09T00:00:03+00:00",
    )


def _version(repo: Path, turn: str):
    with store.connect(repo) as conn:
        return store.find_prompt_by_turn(conn, "codex-session-1", turn)


def test_codex_session_turn_captures_full_version_details(git_repo: Path) -> None:
    monitor = codex_sessions.CodexSessionMonitor(git_repo, sessions=git_repo)
    state = codex_sessions.FileState()
    monitor.process_record(state, _meta(git_repo))
    monitor.process_record(
        state,
        _record(
            "event_msg",
            {"type": "task_started", "turn_id": "turn-1"},
            "2026-08-09T00:00:01+00:00",
        ),
    )
    monitor.process_record(
        state,
        _record(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "# Context from my IDE setup:\n\n"
                            "## Active file: monitored.py\n\n"
                            "## Open tabs:\n- monitored.py\n\n"
                            "## My request:\nadd a monitored file\n"
                        ),
                    }
                ],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "turn-1"
                },
            },
            "2026-08-09T00:00:01+00:00",
        ),
    )
    monitor.process_record(
        state,
        _record(
            "turn_context",
            {"turn_id": "turn-1", "model": "gpt-5.4-mini"},
            "2026-08-09T00:00:01+00:00",
        ),
    )
    monitor.process_record(
        state,
        _record(
            "response_item",
            {
                "type": "reasoning",
                "summary": [{"text": "I will add one focused file."}],
                "encrypted_content": "opaque-private-content",
            },
            "2026-08-09T00:00:02+00:00",
        ),
    )
    (git_repo / "monitored.py").write_text("enabled = True\n")
    monitor.process_record(
        state,
        _usage(
            {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "cache_write_input_tokens": 5,
                "output_tokens": 20,
                "reasoning_output_tokens": 7,
            }
        ),
    )
    monitor.process_record(
        state,
        _record(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "Added monitored.py.",
            },
            "2026-08-09T00:00:04+00:00",
        ),
    )

    version = _version(git_repo, "turn-1")
    assert version is not None
    assert store.read_object(git_repo, version.prompt_ref) == "add a monitored file"
    assert store.read_object(git_repo, version.response_ref) == "Added monitored.py."
    assert store.read_object(git_repo, version.thinking_ref) == (
        "I will add one focused file."
    )
    assert "opaque-private-content" not in (
        store.read_object(git_repo, version.thinking_ref) or ""
    )
    assert "monitored.py" in (store.read_object(git_repo, version.diff_ref) or "")
    assert version.model == "gpt-5.4-mini"
    assert version.input_tokens == 100
    assert version.output_tokens == 20
    assert version.cache_read_tokens == 40
    assert version.cache_creation_tokens == 5
    assert version.reasoning_tokens == 7
    assert version.api_call_count == 1
    assert version.completed_at == "2026-08-09T00:00:04+00:00"
    assert version.completion_source == codex_sessions.COMPLETION_SOURCE


def test_codex_session_usage_is_delta_per_turn(git_repo: Path) -> None:
    monitor = codex_sessions.CodexSessionMonitor(git_repo, sessions=git_repo)
    state = codex_sessions.FileState()
    monitor.process_record(state, _meta(git_repo))

    totals = [
        {
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "output_tokens": 10,
            "reasoning_output_tokens": 2,
        },
        {
            "input_tokens": 175,
            "cached_input_tokens": 50,
            "output_tokens": 25,
            "reasoning_output_tokens": 6,
        },
    ]
    for index, total in enumerate(totals, start=1):
        turn_id = f"turn-{index}"
        monitor.process_record(
            state,
            _record(
                "event_msg",
                {"type": "task_started", "turn_id": turn_id},
                f"2026-08-09T00:00:0{index}+00:00",
            ),
        )
        monitor.process_record(state, _usage(total))
        monitor.process_record(
            state,
            _record(
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": turn_id,
                    "last_agent_message": "done",
                },
                f"2026-08-09T00:00:1{index}+00:00",
            ),
        )

    second = _version(git_repo, "turn-2")
    assert second is not None
    assert second.input_tokens == 75
    assert second.output_tokens == 15
    assert second.cache_read_tokens == 30
    assert second.reasoning_tokens == 4


def test_codex_public_commentary_is_captured_as_thinking(git_repo: Path) -> None:
    monitor = codex_sessions.CodexSessionMonitor(git_repo, sessions=git_repo)
    state = codex_sessions.FileState()
    monitor.process_record(state, _meta(git_repo))
    monitor.process_record(
        state,
        _record(
            "event_msg",
            {"type": "task_started", "turn_id": "commentary-turn"},
            "2026-08-09T00:00:01+00:00",
        ),
    )
    for text in ("I am checking the file.", "I am making the change now."):
        monitor.process_record(
            state,
            _record(
                "response_item",
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": text}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "commentary-turn"
                    },
                },
                "2026-08-09T00:00:02+00:00",
            ),
        )
    monitor.process_record(
        state,
        _record(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Final answer."}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "commentary-turn"
                },
            },
            "2026-08-09T00:00:03+00:00",
        ),
    )
    monitor.process_record(
        state,
        _record(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "commentary-turn",
                "last_agent_message": "Final answer.",
            },
            "2026-08-09T00:00:04+00:00",
        ),
    )

    version = _version(git_repo, "commentary-turn")
    assert version is not None
    assert version.thinking_source == store.THINKING_CAPTURED
    assert store.read_object(git_repo, version.thinking_ref) == (
        "I am checking the file.\nI am making the change now."
    )
    assert store.read_object(git_repo, version.response_ref) == "Final answer."


def test_codex_ignores_plugin_context_before_real_prompt(git_repo: Path) -> None:
    monitor = codex_sessions.CodexSessionMonitor(git_repo, sessions=git_repo)
    state = codex_sessions.FileState()
    monitor.process_record(state, _meta(git_repo))
    monitor.process_record(
        state,
        _record(
            "event_msg",
            {"type": "task_started", "turn_id": "desktop-turn"},
            "2026-08-09T00:00:01+00:00",
        ),
    )
    for text in (
        "<recommended_plugins>\n- Example\n</recommended_plugins>\n"
        "<environment_context><cwd>/repo</cwd></environment_context>",
        "# Context from my IDE setup:\n\n"
        "## Active file: calculator.c\n\n"
        "## My request:\nadd calculator.js",
    ):
        monitor.process_record(
            state,
            _record(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "desktop-turn"
                    },
                },
                "2026-08-09T00:00:02+00:00",
            ),
        )

    version = _version(git_repo, "desktop-turn")
    assert version is not None
    assert store.read_object(git_repo, version.prompt_ref) == "add calculator.js"


def test_monitor_repairs_previously_captured_plugin_context(
    git_repo: Path, tmp_path: Path
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    plugin_context = "<recommended_plugins>\n- Example\n</recommended_plugins>"
    with store.connect(git_repo) as conn:
        anchor = store.get_version(conn, "v1")
        assert anchor is not None
        version = store.create_version(
            conn,
            git_branch="main",
            parent_id=anchor.id,
            tree_before=anchor.tree_after or anchor.tree_before,
            tree_after=anchor.tree_after,
            prompt_ref=store.write_object(git_repo, plugin_context),
            conversation_id="codex-session-1",
            generation_id="repair-turn",
        )
    rollout = sessions / "rollout.jsonl"
    rollout.write_text(
        json.dumps(_meta(git_repo))
        + "\n"
        + json.dumps(
            _record(
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "# Context from my IDE setup:\n\n"
                                "## My request:\nreal repaired prompt"
                            ),
                        }
                    ],
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "repair-turn"
                    },
                },
                "2026-08-09T00:00:02+00:00",
            )
        )
        + "\n"
    )

    monitor = codex_sessions.CodexSessionMonitor(git_repo, sessions=sessions)
    monitor._baseline_existing_files()

    with store.connect(git_repo) as conn:
        repaired = store.get_version(conn, version.id)
    assert repaired is not None
    assert store.read_object(git_repo, repaired.prompt_ref) == "real repaired prompt"


def test_monitor_baselines_existing_history_and_handles_partial_lines(
    git_repo: Path, tmp_path: Path
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / "rollout.jsonl"
    old = [
        _meta(git_repo),
        _record(
            "event_msg",
            {"type": "task_started", "turn_id": "old-turn"},
            "2026-08-09T00:00:00+00:00",
        ),
        _record(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "old-turn",
                "last_agent_message": "old",
            },
            "2026-08-09T00:00:01+00:00",
        ),
    ]
    rollout.write_text("".join(json.dumps(item) + "\n" for item in old))
    monitor = codex_sessions.CodexSessionMonitor(git_repo, sessions=sessions)
    monitor._baseline_existing_files()
    assert _version(git_repo, "old-turn") is None

    started = _record(
        "event_msg",
        {"type": "task_started", "turn_id": "new-turn"},
        "2026-08-09T00:00:02+00:00",
    )
    encoded = json.dumps(started)
    with rollout.open("a") as handle:
        handle.write(encoded[: len(encoded) // 2])
    monitor.process_path(rollout)
    assert _version(git_repo, "new-turn") is None

    with rollout.open("a") as handle:
        handle.write(encoded[len(encoded) // 2 :] + "\n")
    monitor._poll_known_files()
    assert _version(git_repo, "new-turn") is not None


def test_monitor_resumes_an_existing_open_turn_from_rollout(
    git_repo: Path, tmp_path: Path
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / "rollout.jsonl"
    hook_capture.open_prompt(
        git_repo,
        conversation_id="codex-session-1",
        generation_id="resumed-turn",
        prompt_text="",
        model=None,
        superseded_source="codex-session-superseded",
    )
    (git_repo / "resumed.py").write_text("recovered = True\n")
    records = [
        _meta(git_repo),
        _record(
            "event_msg",
            {"type": "task_started", "turn_id": "resumed-turn"},
            "2026-08-09T00:00:01+00:00",
        ),
        _record(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "resume this turn"}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "resumed-turn"
                },
            },
            "2026-08-09T00:00:01+00:00",
        ),
        _usage({"input_tokens": 20, "output_tokens": 4}),
        _record(
            "event_msg",
            {
                "type": "task_complete",
                "turn_id": "resumed-turn",
                "last_agent_message": "Recovered response.",
            },
            "2026-08-09T00:00:04+00:00",
        ),
    ]
    rollout.write_text("".join(json.dumps(item) + "\n" for item in records))

    monitor = codex_sessions.CodexSessionMonitor(git_repo, sessions=sessions)
    monitor._baseline_existing_files()

    version = _version(git_repo, "resumed-turn")
    assert version is not None and version.tree_after is not None
    assert "resumed.py" in (store.read_object(git_repo, version.diff_ref) or "")
    assert store.read_object(git_repo, version.prompt_ref) == "resume this turn"
    assert store.read_object(git_repo, version.response_ref) == "Recovered response."
    assert version.input_tokens == 20
    assert version.confidence == "uncertain"
    assert version.confidence_reason == codex_sessions.RESUMED_REASON


def test_monitor_ignores_sessions_for_another_repository(
    git_repo: Path, tmp_path: Path
) -> None:
    monitor = codex_sessions.CodexSessionMonitor(git_repo, sessions=tmp_path)
    state = codex_sessions.FileState()
    foreign = git_repo.parent / f"{git_repo.name}-foreign"
    foreign.mkdir()
    monitor.process_record(state, _meta(foreign))
    monitor.process_record(
        state,
        _record(
            "event_msg",
            {"type": "task_started", "turn_id": "foreign"},
            "2026-08-09T00:00:00+00:00",
        ),
    )
    assert _version(git_repo, "foreign") is None


def test_start_codex_runs_foreground_monitor(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    called: list[Path] = []

    def stop_immediately(self) -> None:
        called.append(self.repo_root)
        raise KeyboardInterrupt

    monkeypatch.setattr(codex_sessions.CodexSessionMonitor, "run", stop_immediately)
    result = runner.invoke(app, ["start", "--codex"])

    assert result.exit_code == 0, result.output
    assert called == [git_repo.resolve()]
    assert "Keep this command running" in result.output
    assert "monitoring stopped" in result.output
