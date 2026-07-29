from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from profact import store
from profact.adapters import cursor_hooks
from profact.cli import app

runner = CliRunner()


def _event(name: str, gen: str = "gen-1", **extra) -> dict:
    base = {
        "hook_event_name": name,
        "conversation_id": "conv-1",
        "generation_id": gen,
        "workspace_roots": [],
    }
    base.update(extra)
    return base


def _prompts(repo: Path) -> list[store.Version]:
    with store.connect(repo) as conn:
        return [v for v in store.list_versions(conn, "main") if v.type == "prompt"]


def test_full_turn_creates_one_finalized_version(git_repo: Path) -> None:
    cursor_hooks.handle(
        git_repo,
        _event("beforeSubmitPrompt", prompt="add a feature", model="gpt-5"),
    )
    # Agent edits a file mid-turn.
    (git_repo / "feature.py").write_text("x = 1\n")
    cursor_hooks.handle(git_repo, _event("afterAgentResponse", text="Added the feature."))
    cursor_hooks.handle(git_repo, _event("stop", status="completed", loop_count=0))

    prompts = _prompts(git_repo)
    assert len(prompts) == 1
    version = prompts[0]
    assert version.tree_after is not None and version.tree_after != version.tree_before
    assert version.generation_id == "gen-1"
    assert store.read_object(git_repo, version.prompt_ref) == "add a feature"
    assert store.read_object(git_repo, version.response_ref) == "Added the feature."
    assert "feature.py" in store.read_object(git_repo, version.diff_ref)
    assert store.read_head(git_repo, "main") == version.id


def test_second_prompt_chains_and_closes_previous(git_repo: Path) -> None:
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", gen="g1", prompt="first"))
    (git_repo / "a.py").write_text("1\n")
    cursor_hooks.handle(git_repo, _event("stop", gen="g1"))

    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", gen="g2", prompt="second"))
    (git_repo / "b.py").write_text("2\n")
    cursor_hooks.handle(git_repo, _event("stop", gen="g2"))

    prompts = sorted(_prompts(git_repo), key=lambda v: v.timestamp)
    assert len(prompts) == 2
    assert prompts[1].parent_id == prompts[0].id  # chained
    assert all(v.tree_after is not None for v in prompts)  # both finalized


def test_prompt_closes_dangling_turn_when_stop_missing(git_repo: Path) -> None:
    # No `stop` between the two prompts; the next prompt must finalize the first.
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", gen="g1", prompt="first"))
    (git_repo / "a.py").write_text("1\n")
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", gen="g2", prompt="second"))

    prompts = sorted(_prompts(git_repo), key=lambda v: v.timestamp)
    assert len(prompts) == 2
    assert prompts[0].tree_after is not None  # auto-finalized on next prompt


def test_manual_edits_between_prompts_become_one_human_version(
    git_repo: Path,
) -> None:
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", gen="g1", prompt="first"))
    cursor_hooks.handle(git_repo, _event("stop", gen="g1"))
    first = _prompts(git_repo)[0]

    # Several saves and multiple files still represent one manual-edit interval.
    manual = git_repo / "manual.py"
    manual.write_text("draft 1\n")
    manual.write_text("draft 2\n")
    manual.write_text("final\n")
    (git_repo / "notes.py").write_text("also by hand\n")

    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", gen="g2", prompt="second"))

    with store.connect(git_repo) as conn:
        versions = store.list_versions(conn, "main")
    humans = [version for version in versions if version.type == "human"]
    prompts = [version for version in versions if version.type == "prompt"]

    assert len(humans) == 1
    assert len(prompts) == 2
    human = humans[0]
    second = prompts[1]
    assert human.parent_id == first.id
    assert second.parent_id == human.id
    assert second.tree_before == human.tree_after
    diff = store.read_object(git_repo, human.diff_ref)
    assert "manual.py" in diff and "notes.py" in diff
    assert "final" in diff
    assert "draft 1" not in diff and "draft 2" not in diff


def test_manual_delta_is_excluded_from_following_prompt_diff(git_repo: Path) -> None:
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", gen="g1", prompt="first"))
    cursor_hooks.handle(git_repo, _event("stop", gen="g1"))

    (git_repo / "manual.py").write_text("human\n")
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", gen="g2", prompt="second"))
    (git_repo / "agent.py").write_text("agent\n")
    cursor_hooks.handle(git_repo, _event("stop", gen="g2"))

    with store.connect(git_repo) as conn:
        versions = store.list_versions(conn, "main")
    human = next(version for version in versions if version.type == "human")
    prompt = [version for version in versions if version.type == "prompt"][-1]
    human_diff = store.read_object(git_repo, human.diff_ref)
    prompt_diff = store.read_object(git_repo, prompt.diff_ref)
    assert "manual.py" in human_diff and "agent.py" not in human_diff
    assert "agent.py" in prompt_diff and "manual.py" not in prompt_diff


def test_reverted_manual_edit_does_not_create_human_version(git_repo: Path) -> None:
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", gen="g1", prompt="first"))
    cursor_hooks.handle(git_repo, _event("stop", gen="g1"))

    original = (git_repo / "a.txt").read_text()
    (git_repo / "a.txt").write_text("temporary\n")
    (git_repo / "a.txt").write_text(original)
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", gen="g2", prompt="second"))

    with store.connect(git_repo) as conn:
        humans = [
            version
            for version in store.list_versions(conn, "main")
            if version.type == "human"
        ]
    assert humans == []


def test_session_end_captures_manual_edits_after_last_prompt(git_repo: Path) -> None:
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", prompt="first"))
    cursor_hooks.handle(git_repo, _event("stop"))
    (git_repo / "manual.py").write_text("after prompt\n")

    cursor_hooks.handle(git_repo, _event("sessionEnd"))

    with store.connect(git_repo) as conn:
        humans = [
            version
            for version in store.list_versions(conn, "main")
            if version.type == "human"
        ]
    assert len(humans) == 1
    assert "manual.py" in store.read_object(git_repo, humans[0].diff_ref)


def test_no_file_change_yields_empty_diff(git_repo: Path) -> None:
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", prompt="just a question"))
    cursor_hooks.handle(git_repo, _event("afterAgentResponse", text="Here's the answer."))
    cursor_hooks.handle(git_repo, _event("stop"))

    version = _prompts(git_repo)[0]
    assert version.tree_after == version.tree_before
    assert version.diff_ref is None  # no changes -> no diff object


def test_handle_is_noop_when_not_initialized(tmp_path: Path) -> None:
    # No .profact present; must not raise and must not create anything.
    assert cursor_hooks.handle(tmp_path, _event("beforeSubmitPrompt", prompt="x")) == {}


def test_hook_cli_reads_stdin_and_fails_open(git_repo: Path) -> None:
    payload = json.dumps(
        _event("beforeSubmitPrompt", prompt="via cli", workspace_roots=[str(git_repo)])
    )
    result = runner.invoke(app, ["hook"], input=payload)
    assert result.exit_code == 0
    assert _prompts(git_repo)[0].prompt_ref is not None

    # Garbage input must still exit 0 (fail open) and not crash.
    bad = runner.invoke(app, ["hook"], input="not json")
    assert bad.exit_code == 0


def test_start_installs_hooks_config(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    result = runner.invoke(app, ["start"])
    assert result.exit_code == 0, result.output

    script = git_repo / ".cursor" / "hooks" / "profact-hook.sh"
    assert script.exists()
    if os.name != "nt":
        assert script.stat().st_mode & 0o111  # POSIX executable bit

    config = json.loads((git_repo / ".cursor" / "hooks.json").read_text())
    assert config["version"] == 1
    for event in cursor_hooks.HANDLED_EVENTS:
        commands = [e["command"] for e in config["hooks"][event]]
        assert ".cursor/hooks/profact-hook.sh" in commands

    # Running start again must not duplicate entries.
    runner.invoke(app, ["start"])
    config2 = json.loads((git_repo / ".cursor" / "hooks.json").read_text())
    assert len(config2["hooks"]["beforeSubmitPrompt"]) == 1


def test_start_gitignores_hook_artifacts(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    runner.invoke(app, ["start"])
    ignored = (git_repo / ".gitignore").read_text().splitlines()
    assert ".cursor/hooks.json" in ignored
    assert ".cursor/hooks/" in ignored


def test_checkout_before_start_keeps_hooks_alive(git_repo: Path, monkeypatch) -> None:
    """Regression: checking out a version created before `start` must NOT delete
    the hook files (which would silently disable Cursor capture)."""
    monkeypatch.chdir(git_repo)
    runner.invoke(app, ["start"])  # installs + gitignores hooks (anchor v1 predates this)
    script = git_repo / ".cursor" / "hooks" / "profact-hook.sh"
    assert script.exists()

    # A captured turn after start -> v2.
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", prompt="edit"))
    (git_repo / "x.py").write_text("y = 1\n")
    cursor_hooks.handle(git_repo, _event("stop"))

    # Going back to the pre-start anchor must leave the hook machinery intact.
    back = runner.invoke(app, ["checkout", "v1"])
    assert back.exit_code == 0, back.output
    assert script.exists()  # hooks survived

    # Critical: checkout v1 reverted .gitignore (anchor predates the .cursor
    # entries), so a SECOND checkout would re-expose the hooks to deletion.
    # Hard protection must keep them alive regardless of gitignore state.
    fwd = runner.invoke(app, ["checkout", "v2"])
    assert fwd.exit_code == 0, fwd.output
    assert script.exists()  # hooks still alive -> capture keeps working


def test_checkout_after_start_makes_no_phantom_checkpoint(git_repo: Path, monkeypatch) -> None:
    """Regression: `start` gitignores the hook files, but checking out a pre-start
    version reverts .gitignore. The snapshot must still exclude the (undeletable)
    hook files, or checkout would round-trip to a different tree and invent a
    phantom checkpoint from work the user never did."""
    monkeypatch.chdir(git_repo)
    runner.invoke(app, ["start"])
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", prompt="edit"))
    (git_repo / "x.py").write_text("y = 1\n")
    cursor_hooks.handle(git_repo, _event("stop"))

    runner.invoke(app, ["checkout", "v1"])  # reverts .gitignore (predates .cursor)
    result = runner.invoke(app, ["checkout", "v2"])
    assert result.exit_code == 0, result.output
    assert "checkpoint" not in result.output.lower()  # no phantom work

    with store.connect(git_repo) as conn:
        checkpoints = [
            v for v in store.list_versions(conn, "main") if v.type == "checkpoint"
        ]
    assert checkpoints == []


def test_write_tree_excludes_hook_infra_regardless_of_gitignore(
    git_repo: Path, monkeypatch
) -> None:
    from profact import git_ops

    monkeypatch.chdir(git_repo)
    hooks_dir = git_repo / ".cursor" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "profact-hook.sh").write_text("#!/usr/bin/env bash\n")
    (git_repo / ".cursor" / "hooks.json").write_text("{}\n")

    # Even with NO .gitignore entry for them, the infra must never enter a tree.
    tree = git_ops.write_tree(git_repo)
    files = git_ops.list_tree_files(git_repo, tree)
    assert not any(f.startswith(".cursor/hooks") for f in files)
    assert not any(f.startswith(".profact") for f in files)


def test_thinking_is_captured_when_thought_events_fire(git_repo: Path) -> None:
    cursor_hooks.handle(
        git_repo, _event("beforeSubmitPrompt", prompt="refactor it", model="claude")
    )
    cursor_hooks.handle(
        git_repo, _event("afterAgentThought", text="First I'll read the file.", duration_ms=1200)
    )
    (git_repo / "r.py").write_text("z = 1\n")
    cursor_hooks.handle(git_repo, _event("afterAgentResponse", text="Refactored."))
    cursor_hooks.handle(git_repo, _event("stop"))

    version = _prompts(git_repo)[0]
    assert version.thinking_source == store.THINKING_CAPTURED
    assert version.thinking_ref is not None
    assert store.read_object(git_repo, version.thinking_ref) == "First I'll read the file."


def test_thinking_aggregates_multiple_blocks(git_repo: Path) -> None:
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", prompt="do it"))
    cursor_hooks.handle(git_repo, _event("afterAgentThought", text="Step one."))
    cursor_hooks.handle(git_repo, _event("afterAgentThought", text="Step two."))
    cursor_hooks.handle(git_repo, _event("stop"))

    version = _prompts(git_repo)[0]
    assert store.read_object(git_repo, version.thinking_ref) == "Step one.\nStep two."
    assert version.thinking_source == store.THINKING_CAPTURED


def test_no_thinking_marks_source_none(git_repo: Path) -> None:
    # A non-reasoning model never fires afterAgentThought.
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", prompt="quick one"))
    cursor_hooks.handle(git_repo, _event("afterAgentResponse", text="Done."))
    cursor_hooks.handle(git_repo, _event("stop"))

    version = _prompts(git_repo)[0]
    assert version.thinking_ref is None
    assert version.thinking_source == store.THINKING_NONE


def test_show_displays_thinking_then_placeholder(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    # v2: a reasoning turn.
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", prompt="think hard"))
    cursor_hooks.handle(git_repo, _event("afterAgentThought", text="Weighing options."))
    cursor_hooks.handle(git_repo, _event("stop"))
    # v3: a no-reasoning turn.
    cursor_hooks.handle(git_repo, _event("beforeSubmitPrompt", prompt="no think"))
    cursor_hooks.handle(git_repo, _event("stop"))

    shown = runner.invoke(app, ["show", "v2"])
    assert shown.exit_code == 0, shown.output
    assert "thinking:" in shown.output
    assert "Weighing options." in shown.output

    placeholder = runner.invoke(app, ["show", "v3"])
    assert placeholder.exit_code == 0, placeholder.output
    assert "thinking:" in placeholder.output
    assert "no reasoning captured" in placeholder.output


def test_hook_debug_probe_logs_raw_events(git_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROFACT_HOOK_DEBUG", "1")
    payload = json.dumps(
        _event("afterAgentThought", text="probe me", workspace_roots=[str(git_repo)])
    )
    result = runner.invoke(app, ["hook"], input=payload)
    assert result.exit_code == 0

    log = store.profact_dir(git_repo) / "hook-events.log"
    assert log.exists()
    logged = json.loads(log.read_text().splitlines()[-1])
    assert logged["hook_event_name"] == "afterAgentThought"
    assert logged["text"] == "probe me"


def test_start_preserves_existing_unrelated_hooks(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    cursor_dir = git_repo / ".cursor"
    cursor_dir.mkdir()
    (cursor_dir / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {"afterFileEdit": [{"command": "./hooks/format.sh"}]},
            }
        )
    )
    runner.invoke(app, ["start"])
    config = json.loads((cursor_dir / "hooks.json").read_text())
    assert {"command": "./hooks/format.sh"} in config["hooks"]["afterFileEdit"]
    assert "beforeSubmitPrompt" in config["hooks"]
