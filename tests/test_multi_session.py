from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess

from profact import store
from profact.adapters import cursor_hooks
from profact.adapters.base import CaptureEvent
from profact.cli import app
from profact.session import CaptureSession
from typer.testing import CliRunner

runner = CliRunner()


def _event(
    name: str,
    *,
    conversation: str,
    generation: str,
    **extra,
) -> dict:
    event = {
        "hook_event_name": name,
        "conversation_id": conversation,
        "generation_id": generation,
    }
    event.update(extra)
    return event


def _capture_event(role: str, text: str) -> CaptureEvent:
    return CaptureEvent(
        role,
        [{"type": "text", "text": text}],
        "claude-test",
        None,
        datetime.now(timezone.utc).isoformat(),
    )


def test_turn_indexes_and_lookup_are_available(git_repo: Path) -> None:
    with store.connect(git_repo) as conn:
        anchor = store.get_version(conn, "v1")
        assert anchor is not None
        prompt = store.create_version(
            conn,
            git_branch="main",
            parent_id=anchor.id,
            tree_before=anchor.tree_after or anchor.tree_before,
            conversation_id="conversation-a",
            generation_id="generation-1",
        )
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(versions)")
        }
        resolved = store.find_prompt_by_turn(
            conn, "conversation-a", "generation-1"
        )
        open_for_session = store.open_prompts(
            conn, conversation_id="conversation-a"
        )

    assert {
        "idx_versions_conv_gen",
        "idx_versions_parent",
        "idx_versions_open",
        "uq_versions_turn",
    }.issubset(indexes)
    assert resolved is not None and resolved.id == prompt.id
    assert [version.id for version in open_for_session] == [prompt.id]


def test_concurrent_version_creation_allocates_unique_ids(git_repo: Path) -> None:
    with store.connect(git_repo) as conn:
        anchor = store.get_version(conn, "v1")
    assert anchor is not None
    tree = anchor.tree_after or anchor.tree_before

    def create(index: int) -> str:
        conn = store.connect(git_repo)
        try:
            return store.create_version(
                conn,
                type="human",
                git_branch="main",
                parent_id="v1",
                tree_before=tree,
                tree_after=tree,
                conversation_id=f"session-{index}",
            ).id
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=6) as executor:
        identifiers = list(executor.map(create, range(12)))

    assert len(set(identifiers)) == 12
    with store.connect(git_repo) as conn:
        assert len(store.all_versions(conn)) == 13


def test_concurrent_prompt_opens_serialize_branch_head_and_parentage(
    git_repo: Path,
) -> None:
    with store.connect(git_repo) as conn:
        anchor = store.get_version(conn, "v1")
    assert anchor is not None
    tree = anchor.tree_after or anchor.tree_before

    def create(index: int) -> store.Version:
        version, created = store.create_prompt_at_head(
            git_repo,
            git_branch="main",
            tree_before=tree,
            prompt_ref=store.write_object(git_repo, f"prompt {index}"),
            model="test",
            conversation_id=f"conversation-{index}",
            generation_id="1",
        )
        assert created
        return version

    with ThreadPoolExecutor(max_workers=2) as executor:
        versions = list(executor.map(create, range(2)))

    by_id = {version.id: version for version in versions}
    head_id = store.read_head(git_repo, "main", seed=False)
    assert head_id in by_id
    head = by_id[head_id]
    other = next(version for version in versions if version.id != head.id)
    assert head.parent_id == other.id
    assert other.parent_id == "v1"
    assert head.confidence == "uncertain"
    assert head.confidence_reason == "overlapping sessions shared one working tree"
    with store.connect(git_repo) as conn:
        refreshed_other = store.get_version(conn, other.id)
    assert refreshed_other is not None
    assert refreshed_other.confidence == "uncertain"
    assert (
        refreshed_other.confidence_reason
        == "overlapping sessions shared one working tree"
    )
    # SQLite is authoritative for new-branch carry-over even if the legacy file
    # was left stale by an older or concurrently exiting process.
    store.head_path(git_repo).write_text("v1")
    assert store.read_head(git_repo, "new-branch") == head.id


def test_cursor_routes_interleaved_conversations_and_marks_overlap(
    git_repo: Path,
) -> None:
    cursor_hooks.handle(
        git_repo,
        _event(
            "beforeSubmitPrompt",
            conversation="conversation-a",
            generation="a1",
            prompt="prompt A",
        ),
    )
    (git_repo / "a.py").write_text("a = 1\n")
    cursor_hooks.handle(
        git_repo,
        _event(
            "beforeSubmitPrompt",
            conversation="conversation-b",
            generation="b1",
            prompt="prompt B",
        ),
    )
    (git_repo / "b.py").write_text("b = 1\n")
    cursor_hooks.handle(
        git_repo,
        _event(
            "afterAgentResponse",
            conversation="conversation-a",
            generation="a1",
            text="response A",
        ),
    )
    cursor_hooks.handle(
        git_repo,
        _event("stop", conversation="conversation-a", generation="a1"),
    )
    cursor_hooks.handle(
        git_repo,
        _event(
            "afterAgentResponse",
            conversation="conversation-b",
            generation="b1",
            text="response B",
        ),
    )
    cursor_hooks.handle(
        git_repo,
        _event("stop", conversation="conversation-b", generation="b1"),
    )

    with store.connect(git_repo) as conn:
        first = store.find_prompt_by_turn(conn, "conversation-a", "a1")
        second = store.find_prompt_by_turn(conn, "conversation-b", "b1")
        assert store.open_prompts(conn) == []
    assert first is not None and second is not None
    assert first.tree_after is not None and second.tree_after is not None
    assert first.confidence == "uncertain" and second.confidence == "uncertain"
    assert first.confidence_reason == "overlapping sessions shared one working tree"
    assert second.confidence_reason == "overlapping sessions shared one working tree"
    assert store.read_object(git_repo, first.response_ref) == "response A"
    assert store.read_object(git_repo, second.response_ref) == "response B"
    assert second.parent_id == first.id


def test_cursor_prompt_and_stop_delivery_are_idempotent(git_repo: Path) -> None:
    prompt = _event(
        "beforeSubmitPrompt",
        conversation="conversation-a",
        generation="a1",
        prompt="only once",
    )
    cursor_hooks.handle(git_repo, prompt)
    cursor_hooks.handle(git_repo, prompt)
    stop = _event("stop", conversation="conversation-a", generation="a1")
    cursor_hooks.handle(git_repo, stop)
    cursor_hooks.handle(git_repo, stop)

    with store.connect(git_repo) as conn:
        prompts = [
            version for version in store.all_versions(conn) if version.type == "prompt"
        ]
    assert len(prompts) == 1
    assert prompts[0].tree_after is not None


def test_concurrent_response_hooks_append_without_lost_updates(
    git_repo: Path,
) -> None:
    cursor_hooks.handle(
        git_repo,
        _event(
            "beforeSubmitPrompt",
            conversation="conversation-a",
            generation="a1",
            prompt="collect responses",
        ),
    )

    def respond(text: str) -> None:
        cursor_hooks.handle(
            git_repo,
            _event(
                "afterAgentResponse",
                conversation="conversation-a",
                generation="a1",
                text=text,
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(respond, ["block one", "block two"]))

    with store.connect(git_repo) as conn:
        version = store.find_prompt_by_turn(conn, "conversation-a", "a1")
    assert version is not None and version.response_ref is not None
    response = store.read_object(git_repo, version.response_ref) or ""
    assert response.count("block one") == 1
    assert response.count("block two") == 1


def test_late_response_routes_to_finalized_turn_without_resnapshot(
    git_repo: Path,
) -> None:
    cursor_hooks.handle(
        git_repo,
        _event(
            "beforeSubmitPrompt",
            conversation="conversation-a",
            generation="a1",
            prompt="finish",
        ),
    )
    cursor_hooks.handle(
        git_repo,
        _event("stop", conversation="conversation-a", generation="a1"),
    )
    with store.connect(git_repo) as conn:
        before = store.find_prompt_by_turn(conn, "conversation-a", "a1")
    assert before is not None

    (git_repo / "later.py").write_text("later = True\n")
    cursor_hooks.handle(
        git_repo,
        _event(
            "afterAgentResponse",
            conversation="conversation-a",
            generation="a1",
            text="late response",
        ),
    )
    with store.connect(git_repo) as conn:
        after = store.find_prompt_by_turn(conn, "conversation-a", "a1")
    assert after is not None
    assert after.tree_after == before.tree_after
    assert after.diff_ref == before.diff_ref
    assert after.confidence == "uncertain"
    assert after.confidence_reason == "capture event arrived after turn finalization"
    assert store.read_object(git_repo, after.response_ref) == "late response"


def test_branch_switch_finalizes_original_turn_without_moving_new_branch_head(
    git_repo: Path,
) -> None:
    cursor_hooks.handle(
        git_repo,
        _event(
            "beforeSubmitPrompt",
            conversation="conversation-a",
            generation="a1",
            prompt="switch branches",
        ),
    )
    subprocess.run(
        ["git", "checkout", "-b", "experiment"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    cursor_hooks.handle(
        git_repo,
        _event("stop", conversation="conversation-a", generation="a1"),
    )

    with store.connect(git_repo) as conn:
        version = store.find_prompt_by_turn(conn, "conversation-a", "a1")
    assert version is not None and version.tree_after is not None
    assert version.git_branch == "main"
    assert version.confidence == "uncertain"
    assert version.confidence_reason == "git branch changed while turn was open"
    assert store.read_head(git_repo, "experiment", seed=False) is None


def test_claude_sessions_route_interleaved_turns_and_mark_overlap(
    git_repo: Path,
) -> None:
    first_session = CaptureSession(git_repo, session_id="claude-session-a")
    second_session = CaptureSession(git_repo, session_id="claude-session-b")

    first_session.handle_event(_capture_event("user", "prompt A"))
    (git_repo / "a.py").write_text("a = 1\n")
    second_session.handle_event(_capture_event("user", "prompt B"))
    (git_repo / "b.py").write_text("b = 1\n")
    first_session.handle_event(_capture_event("assistant", "response A"))
    second_session.handle_event(_capture_event("assistant", "response B"))
    first_session.flush()
    second_session.flush()

    with store.connect(git_repo) as conn:
        first = store.find_prompt_by_turn(conn, "claude-session-a", "1")
        second = store.find_prompt_by_turn(conn, "claude-session-b", "1")
        assert store.open_prompts(conn) == []
    assert first is not None and second is not None
    assert first.confidence == "uncertain" and second.confidence == "uncertain"
    assert first.confidence_reason == "overlapping sessions shared one working tree"
    assert second.confidence_reason == "overlapping sessions shared one working tree"
    assert first.tree_after is not None and second.tree_after is not None
    assert store.read_object(git_repo, first.response_ref) == "response A"
    assert store.read_object(git_repo, second.response_ref) == "response B"
    assert first.conversation_id != second.conversation_id


def test_status_and_recover_identify_multiple_open_sessions(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    cursor_hooks.handle(
        git_repo,
        _event(
            "beforeSubmitPrompt",
            conversation="cursor-session-a",
            generation="a1",
            prompt="prompt A",
        ),
    )
    cursor_hooks.handle(
        git_repo,
        _event(
            "beforeSubmitPrompt",
            conversation="cursor-session-b",
            generation="b1",
            prompt="prompt B",
        ),
    )

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0, status.output
    assert "2 open turns" in status.output
    assert "cursor-session-a" in status.output
    assert "cursor-session-b" in status.output

    recover = runner.invoke(app, ["recover"])
    assert recover.exit_code == 0, recover.output
    assert "session=cursor-session-a" in recover.output
    assert "session=cursor-session-b" in recover.output
    assert recover.output.count("finalize=yes") == 1

    with store.connect(git_repo) as conn:
        first = store.find_prompt_by_turn(conn, "cursor-session-a", "a1")
    assert first is not None
    shown = runner.invoke(app, ["show", first.id])
    assert shown.exit_code == 0, shown.output
    assert "overlapping sessions shared one working tree" in shown.output


def test_mutating_navigation_and_human_snapshot_block_on_hidden_open_turn(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    cursor_hooks.handle(
        git_repo,
        _event(
            "beforeSubmitPrompt",
            conversation="cursor-session-a",
            generation="a1",
            prompt="prompt A",
        ),
    )
    cursor_hooks.handle(
        git_repo,
        _event(
            "beforeSubmitPrompt",
            conversation="cursor-session-b",
            generation="b1",
            prompt="prompt B",
        ),
    )
    cursor_hooks.handle(
        git_repo,
        _event("stop", conversation="cursor-session-b", generation="b1"),
    )
    with store.connect(git_repo) as conn:
        open_turns = store.open_prompts(conn, git_branch="main")
    assert len(open_turns) == 1
    assert store.read_head(git_repo, "main", seed=False) != open_turns[0].id

    checkout = runner.invoke(app, ["checkout", "v1"])
    assert checkout.exit_code == 1
    assert open_turns[0].id in checkout.output
    assert "profact recover" in checkout.output

    (git_repo / "manual.py").write_text("manual\n")
    snapshot = runner.invoke(app, ["snapshot"])
    assert snapshot.exit_code == 1
    assert open_turns[0].id in snapshot.output

    bisect = runner.invoke(
        app,
        ["bisect", "--good", "v1", "--", "python", "-c", "raise SystemExit(1)"],
    )
    assert bisect.exit_code == 1
    assert "cannot bisect while prompt turn" in bisect.output


def test_legacy_duplicate_turn_keys_migrate_without_rewriting_history(
    tmp_path: Path,
) -> None:
    database_dir = tmp_path / store.PROFACT_DIR
    database_dir.mkdir()
    raw = sqlite3.connect(database_dir / store.DB_NAME)
    raw.executescript(
        """
        CREATE TABLE versions (
            id TEXT PRIMARY KEY, parent_id TEXT, type TEXT NOT NULL,
            git_branch TEXT NOT NULL, timestamp TEXT NOT NULL,
            prompt_ref TEXT, response_ref TEXT, model TEXT, temperature REAL,
            diff_ref TEXT, tree_before TEXT NOT NULL, tree_after TEXT,
            commit_hash TEXT, confidence TEXT, generation_id TEXT,
            conversation_id TEXT, thinking_ref TEXT, thinking_source TEXT
        );
        """
    )
    for identifier in ("v1", "v2"):
        raw.execute(
            "INSERT INTO versions (id, type, git_branch, timestamp, tree_before, "
            "confidence, conversation_id, generation_id) "
            "VALUES (?, 'prompt', 'main', ?, 'tree', 'high', 'duplicate', '1')",
            (identifier, f"2026-01-01T00:00:0{identifier[1:]}+00:00"),
        )
    raw.commit()
    raw.close()

    conn = store.connect(tmp_path)
    try:
        versions = store.all_versions(conn)
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(versions)")
        }
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(versions)")
        }
    finally:
        conn.close()

    assert [version.id for version in versions] == ["v1", "v2"]
    assert "confidence_reason" in columns
    assert "idx_versions_conv_gen" in indexes
    assert "uq_versions_turn" not in indexes