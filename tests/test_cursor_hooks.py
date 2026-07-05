from __future__ import annotations

import json
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
    assert store.read_head(git_repo) == version.id


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
    assert script.stat().st_mode & 0o111  # executable

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
