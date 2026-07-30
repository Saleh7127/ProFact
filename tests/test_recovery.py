from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from profact import git_ops, human_edits, recovery, store
from profact.adapters import cursor_hooks
from profact.cli import app

runner = CliRunner()


def _open_prompt(repo: Path, prompt: str = "unfinished") -> store.Version:
    cursor_hooks.handle(
        repo,
        {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "conversation",
            "generation_id": "generation",
            "prompt": prompt,
        },
    )
    with store.connect(repo) as conn:
        turns = store.dangling_open_turns(conn)
    assert len(turns) == 1
    return turns[0]


def test_preserve_closes_turn_and_keeps_work_for_human_snapshot(
    git_repo: Path,
) -> None:
    prompt = _open_prompt(git_repo)
    (git_repo / "recovered.py").write_text("manual or unknown\n")

    result = recovery.preserve(git_repo, prompt.id)

    assert result.action == "preserved"
    assert result.version.tree_after == result.version.tree_before
    assert result.version.diff_ref is None
    assert result.version.confidence == recovery.INCOMPLETE_CONFIDENCE
    assert (git_repo / "recovered.py").read_text() == "manual or unknown\n"

    snapshot = human_edits.snapshot(git_repo)
    assert snapshot.version is not None
    assert snapshot.version.type == "human"
    assert snapshot.version.parent_id == prompt.id
    assert "recovered.py" in (
        store.read_object(git_repo, snapshot.version.diff_ref) or ""
    )


def test_finalize_adopts_current_tree_with_uncertain_attribution(
    git_repo: Path,
) -> None:
    prompt = _open_prompt(git_repo)
    (git_repo / "agent.py").write_text("created before the crash\n")

    result = recovery.finalize(git_repo, prompt.id)

    assert result.action == "finalized"
    assert result.version.tree_after == git_ops.write_tree(git_repo)
    assert result.version.confidence == "uncertain"
    assert result.version.thinking_source == store.THINKING_NONE
    assert "agent.py" in (store.read_object(git_repo, result.version.diff_ref) or "")
    assert recovery.audit(git_repo) == []


def test_finalize_rejects_cross_branch_or_non_head_turn(
    git_repo: Path,
) -> None:
    prompt = _open_prompt(git_repo)
    store.write_head(git_repo, "main", "v1")

    candidates = recovery.audit(git_repo)
    assert len(candidates) == 1
    assert not candidates[0].can_finalize
    with pytest.raises(recovery.RecoveryError, match="not branch 'main' HEAD"):
        recovery.finalize(git_repo, prompt.id)

    preserved = recovery.preserve(git_repo, prompt.id)
    assert preserved.version.prompt_ref == prompt.prompt_ref


def test_recovery_rejects_finalized_or_unknown_versions(git_repo: Path) -> None:
    with pytest.raises(recovery.RecoveryError, match="not a dangling prompt turn"):
        recovery.preserve(git_repo, "v1")
    with pytest.raises(recovery.RecoveryError, match="no such version"):
        recovery.finalize(git_repo, "v999")


def test_recover_cli_reports_preserves_and_updates_doctor(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    prompt = _open_prompt(git_repo, "recover this prompt")

    listed = runner.invoke(app, ["recover"])
    assert listed.exit_code == 0, listed.output
    assert prompt.id in listed.output
    assert "finalize=yes" in listed.output
    assert "--preserve" in listed.output

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0, status.output
    assert "run `profact recover`" in status.output

    preserved = runner.invoke(app, ["recover", prompt.id, "--preserve"])
    assert preserved.exit_code == 0, preserved.output
    assert "working files were unchanged" in preserved.output

    shown = runner.invoke(app, ["show", prompt.id])
    assert shown.exit_code == 0, shown.output
    assert "capture ended before its artifact was recovered" in shown.output
    assert "working-tree changes were not attributed" in shown.output

    clean = runner.invoke(app, ["recover"])
    assert clean.exit_code == 0
    assert "No dangling prompt turns" in clean.output
    doctor = runner.invoke(app, ["doctor"])
    assert "dangling open turns:   0" in doctor.output


def test_checkout_refuses_to_orphan_an_open_head(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    prompt = _open_prompt(git_repo)

    result = runner.invoke(app, ["checkout", "v1"])

    assert result.exit_code == 1
    assert "unfinished prompt turn" in result.output
    assert "profact recover" in result.output
    assert store.read_head(git_repo, "main") == prompt.id


def test_recover_cli_validates_action_flags(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    prompt = _open_prompt(git_repo)

    missing = runner.invoke(app, ["recover", prompt.id])
    assert missing.exit_code == 2
    assert "choose --finalize or --preserve" in missing.output

    conflicting = runner.invoke(
        app, ["recover", prompt.id, "--finalize", "--preserve"]
    )
    assert conflicting.exit_code == 2
    assert "mutually exclusive" in conflicting.output


def test_finalize_rejects_dangling_turn_from_another_git_branch(
    git_repo: Path,
) -> None:
    prompt = _open_prompt(git_repo)
    subprocess.run(
        ["git", "checkout", "-b", "experiment"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    with pytest.raises(recovery.RecoveryError, match="switch to 'main' first"):
        recovery.finalize(git_repo, prompt.id)

    preserved = recovery.preserve(git_repo, prompt.id)
    assert preserved.version.confidence == recovery.INCOMPLETE_CONFIDENCE


def test_recover_cli_finalizes_current_head_and_is_discoverable(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    prompt = _open_prompt(git_repo)
    (git_repo / "result.py").write_text("recovered result\n")

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "recover" in help_result.output

    started = runner.invoke(app, ["start"])
    assert started.exit_code == 0, started.output
    assert "unfinished prompt turn" in started.output
    assert "profact recover" in started.output

    finalized = runner.invoke(app, ["recover", "HEAD", "--finalize"])
    assert finalized.exit_code == 0, finalized.output
    assert "uncertain attribution" in finalized.output
    with store.connect(git_repo) as conn:
        version = store.get_version(conn, prompt.id)
    assert version is not None and version.tree_after is not None
    assert "result.py" in (store.read_object(git_repo, version.diff_ref) or "")