from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from profact import recovery, store
from profact.adapters.base import CaptureEvent
from profact.aggregator import Turn
from profact.adapters import cursor_hooks
from profact.cli import app
from profact.tui import presenter

runner = CliRunner()


def _event(name: str, **extra) -> dict:
    event = {
        "hook_event_name": name,
        "conversation_id": "conversation-telemetry",
        "generation_id": "generation-1",
    }
    event.update(extra)
    return event


def _open_cursor_turn(repo: Path) -> store.Version:
    cursor_hooks.handle(repo, _event("beforeSubmitPrompt", prompt="measure this turn"))
    with store.connect(repo) as conn:
        version = store.find_prompt_by_turn(
            conn, "conversation-telemetry", "generation-1"
        )
    assert version is not None
    return version


def test_turn_usage_preserves_unreported_categories_as_unknown() -> None:
    prompt = CaptureEvent("user", [{"type": "text", "text": "prompt"}])
    turn = Turn(index=1, prompt=prompt)
    turn.responses.extend(
        [
            CaptureEvent("assistant", usage={"output_tokens": 3}),
            CaptureEvent("assistant", usage={"output_tokens": 4}),
        ]
    )

    assert turn.usage_totals == {"output_tokens": 7, "api_call_count": 2}


def test_cursor_completion_is_idempotent_and_usage_is_unavailable(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    version = _open_cursor_turn(git_repo)
    stop = _event("stop")
    cursor_hooks.handle(git_repo, stop)
    with store.connect(git_repo) as conn:
        completed = store.get_version(conn, version.id)
    assert completed is not None
    assert completed.completed_at is not None
    assert completed.completion_source == "cursor-stop"
    assert completed.input_tokens is None
    assert completed.output_tokens is None
    assert completed.cache_read_tokens is None
    assert completed.cache_creation_tokens is None
    assert completed.api_call_count is None

    first_completed_at = completed.completed_at
    cursor_hooks.handle(git_repo, stop)
    with store.connect(git_repo) as conn:
        duplicate = store.get_version(conn, version.id)
    assert duplicate is not None and duplicate.completed_at == first_completed_at

    shown = runner.invoke(app, ["show", version.id])
    assert shown.exit_code == 0, shown.output
    assert "cursor-stop" in shown.output
    assert "tokens:   not reported by capture source" in shown.output

    detail = presenter.load_detail(git_repo, version.id, branch="main")
    overview = presenter.overview_text(detail)
    assert "Completed:" in overview and "cursor-stop" in overview
    assert "Tokens: not reported by capture source" in overview


def test_recovery_records_closure_source_without_inventing_usage(
    git_repo: Path,
) -> None:
    version = _open_cursor_turn(git_repo)

    result = recovery.preserve(git_repo, version.id)

    assert result.version.completed_at is not None
    assert result.version.completion_source == "recovery-preserved"
    assert result.version.input_tokens is None
    assert result.version.api_call_count is None


def test_legacy_migration_adds_nullable_telemetry_columns(tmp_path: Path) -> None:
    directory = tmp_path / store.PROFACT_DIR
    directory.mkdir()
    import sqlite3

    raw = sqlite3.connect(directory / store.DB_NAME)
    raw.executescript(
        """
        CREATE TABLE versions (
            id TEXT PRIMARY KEY, parent_id TEXT, type TEXT NOT NULL,
            git_branch TEXT NOT NULL, timestamp TEXT NOT NULL,
            prompt_ref TEXT, response_ref TEXT, model TEXT, temperature REAL,
            diff_ref TEXT, tree_before TEXT NOT NULL, tree_after TEXT,
            commit_hash TEXT, confidence TEXT
        );
        INSERT INTO versions (
            id, type, git_branch, timestamp, tree_before, confidence
        ) VALUES ('v1', 'anchor', 'main', '2026-01-01T00:00:00+00:00', 'tree', 'high');
        """
    )
    raw.commit()
    raw.close()

    with store.connect(tmp_path) as conn:
        version = store.get_version(conn, "v1")
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(versions)")
        }

    assert version is not None
    assert {
        "completed_at",
        "completion_source",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "api_call_count",
    }.issubset(columns)
    assert version.completed_at is None
    assert version.input_tokens is None