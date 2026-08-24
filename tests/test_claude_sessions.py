from __future__ import annotations

import json
from pathlib import Path

from profact import store
from profact.adapters import claude_sessions


def _human(
    repo: Path,
    *,
    session: str = "session-1",
    prompt_id: str = "prompt-1",
    text: str = "add a power function in calculator.js file",
) -> dict:
    return {
        "type": "user",
        "promptId": prompt_id,
        "message": {"role": "user", "content": text},
        "origin": {"kind": "human"},
        "promptSource": "typed",
        "uuid": f"user-{prompt_id}",
        "timestamp": "2026-08-09T20:31:43.514Z",
        "cwd": str(repo),
        "sessionId": session,
    }


def _assistant(
    *,
    uuid: str,
    message_id: str,
    content: list[dict],
    stop_reason: str,
    usage: dict,
) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": "2026-08-09T20:31:50.000Z",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-5",
            "id": message_id,
            "content": content,
            "stop_reason": stop_reason,
            "usage": usage,
        },
    }


def _complete(repo: Path, *, session: str = "session-1") -> dict:
    return {
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 8264,
        "timestamp": "2026-08-09T20:31:53.795Z",
        "cwd": str(repo),
        "sessionId": session,
    }


def _version(repo: Path, prompt_id: str = "prompt-1"):
    with store.connect(repo) as conn:
        return store.find_prompt_by_turn(
            conn, "claude-session-session-1", prompt_id
        )


def test_claude_transcript_captures_one_complete_version_per_typed_prompt(
    git_repo: Path,
) -> None:
    monitor = claude_sessions.ClaudeSessionMonitor(git_repo, sessions=git_repo)
    state = claude_sessions.FileState()
    monitor.process_record(state, _human(git_repo))

    # Claude metadata and internal features are not user turns.
    monitor.process_record(
        state,
        {
            "type": "ai-title",
            "aiTitle": "Add power function",
            "sessionId": "session-1",
        },
    )

    first_usage = {
        "input_tokens": 2,
        "output_tokens": 96,
        "cache_read_input_tokens": 42_458,
        "cache_creation_input_tokens": 12_668,
    }
    monitor.process_record(
        state,
        _assistant(
            uuid="assistant-thinking",
            message_id="message-1",
            content=[{"type": "thinking", "thinking": ""}],
            stop_reason="tool_use",
            usage=first_usage,
        ),
    )
    # Claude writes another record for a tool block from the same provider call.
    monitor.process_record(
        state,
        _assistant(
            uuid="assistant-read",
            message_id="message-1",
            content=[{"type": "tool_use", "id": "tool-1", "name": "Read"}],
            stop_reason="tool_use",
            usage=first_usage,
        ),
    )
    monitor.process_record(
        state,
        {
            "type": "user",
            "promptId": "prompt-1",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1"}],
            },
            "uuid": "tool-result-1",
            "sessionId": "session-1",
            "cwd": str(git_repo),
        },
    )
    monitor.process_record(
        state,
        _assistant(
            uuid="assistant-commentary",
            message_id="message-2",
            content=[
                {
                    "type": "text",
                    "text": "I'll add a power function following the existing pattern.",
                }
            ],
            stop_reason="tool_use",
            usage={
                "input_tokens": 2,
                "output_tokens": 308,
                "cache_read_input_tokens": 55_126,
                "cache_creation_input_tokens": 286,
            },
        ),
    )

    (git_repo / "calculator.js").write_text(
        "function power(base, exponent) {\n  return Math.pow(base, exponent);\n}\n"
    )
    monitor.process_record(
        state,
        _assistant(
            uuid="assistant-final",
            message_id="message-3",
            content=[
                {
                    "type": "text",
                    "text": "Added power(base, exponent) and exported it.",
                }
            ],
            stop_reason="end_turn",
            usage={
                "input_tokens": 2,
                "output_tokens": 36,
                "cache_read_input_tokens": 55_412,
                "cache_creation_input_tokens": 396,
            },
        ),
    )
    monitor.process_record(state, _complete(git_repo))
    monitor.process_record(
        state,
        {
            "type": "last-prompt",
            "lastPrompt": "add a power function in calculator.js file",
            "sessionId": "session-1",
        },
    )

    version = _version(git_repo)
    assert version is not None
    assert store.read_object(git_repo, version.prompt_ref) == (
        "add a power function in calculator.js file"
    )
    assert store.read_object(git_repo, version.thinking_ref) == (
        "I'll add a power function following the existing pattern."
    )
    assert store.read_object(git_repo, version.response_ref) == (
        "Added power(base, exponent) and exported it."
    )
    assert "calculator.js" in (store.read_object(git_repo, version.diff_ref) or "")
    assert version.model == "claude-sonnet-5"
    assert version.input_tokens == 6
    assert version.output_tokens == 440
    assert version.cache_read_tokens == 152_996
    assert version.cache_creation_tokens == 13_350
    assert version.api_call_count == 3
    assert version.completed_at == "2026-08-09T20:31:53.795Z"
    assert version.completion_source == claude_sessions.COMPLETION_SOURCE

    with store.connect(git_repo) as conn:
        prompts = [item for item in store.all_versions(conn) if item.type == "prompt"]
    assert [item.id for item in prompts] == [version.id]


def test_claude_monitor_baselines_history_and_handles_partial_appends(
    git_repo: Path, tmp_path: Path
) -> None:
    sessions = tmp_path / "projects"
    sessions.mkdir()
    transcript = sessions / "session.jsonl"
    old = [_human(git_repo, prompt_id="old"), _complete(git_repo)]
    transcript.write_text("".join(json.dumps(item) + "\n" for item in old))

    monitor = claude_sessions.ClaudeSessionMonitor(git_repo, sessions=sessions)
    monitor._baseline_existing_files()
    assert _version(git_repo, "old") is None

    new_prompt = json.dumps(_human(git_repo, prompt_id="new"))
    with transcript.open("a") as handle:
        handle.write(new_prompt[: len(new_prompt) // 2])
    monitor.process_path(transcript)
    assert _version(git_repo, "new") is None

    with transcript.open("a") as handle:
        handle.write(new_prompt[len(new_prompt) // 2 :] + "\n")
    monitor.poll()
    assert _version(git_repo, "new") is not None


def test_claude_monitor_ignores_another_repository(
    git_repo: Path, tmp_path: Path
) -> None:
    foreign = git_repo.parent / f"{git_repo.name}-foreign"
    foreign.mkdir()
    monitor = claude_sessions.ClaudeSessionMonitor(git_repo, sessions=tmp_path)
    state = claude_sessions.FileState()

    monitor.process_record(state, _human(foreign))

    assert _version(git_repo) is None


def test_claude_monitor_resumes_an_existing_open_turn(
    git_repo: Path, tmp_path: Path
) -> None:
    initial = claude_sessions.ClaudeSessionMonitor(git_repo, sessions=tmp_path)
    state = claude_sessions.FileState()
    prompt = _human(git_repo, prompt_id="resumed")
    initial.process_record(state, prompt)
    assert _version(git_repo, "resumed").tree_after is None

    final = _assistant(
        uuid="resumed-final",
        message_id="resumed-message",
        content=[{"type": "text", "text": "Finished after restart."}],
        stop_reason="end_turn",
        usage={"input_tokens": 3, "output_tokens": 4},
    )
    transcript = tmp_path / "resumed.jsonl"
    transcript.write_text(
        json.dumps(prompt)
        + "\n"
        + json.dumps(final)
        + "\n"
        + json.dumps(_complete(git_repo))
        + "\n"
    )

    restarted = claude_sessions.ClaudeSessionMonitor(git_repo, sessions=tmp_path)
    restarted._baseline_existing_files()

    version = _version(git_repo, "resumed")
    assert version is not None
    assert version.tree_after is not None
    assert version.confidence == "uncertain"
    assert version.confidence_reason == claude_sessions.RESUMED_REASON
    assert store.read_object(git_repo, version.response_ref) == (
        "Finished after restart."
    )


def test_claude_monitor_poll_discovers_a_new_transcript(
    git_repo: Path, tmp_path: Path
) -> None:
    monitor = claude_sessions.ClaudeSessionMonitor(git_repo, sessions=tmp_path)
    monitor._baseline_existing_files()
    transcript = tmp_path / "new-session.jsonl"
    transcript.write_text(json.dumps(_human(git_repo, prompt_id="discovered")) + "\n")

    monitor.poll()

    assert _version(git_repo, "discovered") is not None
