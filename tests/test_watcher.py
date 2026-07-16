from __future__ import annotations

import time
from pathlib import Path

from profact import store
from profact.adapters.base import CaptureEvent
from profact.session import CaptureSession
from profact.watcher import Watcher


def _user_text(text: str) -> CaptureEvent:
    return CaptureEvent("user", [{"type": "text", "text": text}], "claude-x", 1.0, _iso())


def _iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _prompts(repo: Path) -> list:
    with store.connect(repo) as conn:
        return [v for v in store.list_versions(conn, "main") if v.type == "prompt"]


def _humans(repo: Path) -> list:
    with store.connect(repo) as conn:
        return [v for v in store.list_versions(conn, "main") if v.type == "human"]


def test_ai_turn_finalizes_tree_after_and_diff(git_repo: Path):
    session = CaptureSession(git_repo)
    session.handle_event(_user_text("add three files"))

    (git_repo / "x.py").write_text("x\n")
    (git_repo / "y.py").write_text("y\n")
    (git_repo / "z.py").write_text("z\n")
    session.settle(["x.py", "y.py", "z.py"])

    prompts = _prompts(git_repo)
    assert len(prompts) == 1
    version = prompts[0]
    assert version.tree_after and version.tree_after != version.tree_before
    diff = store.read_object(git_repo, version.diff_ref)
    assert diff.count("diff --git") == 3


def test_prompt_with_no_file_change_finalizes_empty_diff(git_repo: Path):
    session = CaptureSession(git_repo)
    session.handle_event(_user_text("just a question"))
    session.flush()  # closes the turn -> finalize with no writes

    version = _prompts(git_repo)[0]
    assert version.tree_after == version.tree_before
    assert version.diff_ref is None


def test_human_edit_with_no_open_turn_creates_human_version(git_repo: Path):
    session = CaptureSession(git_repo)
    (git_repo / "manual.py").write_text("by hand\n")
    session.settle(["manual.py"])

    humans = _humans(git_repo)
    assert len(humans) == 1
    human = humans[0]
    assert human.parent_id is not None
    assert store.read_head(git_repo, "main") == human.id
    assert "manual.py" in store.read_object(git_repo, human.diff_ref)


def test_watcher_thread_finalizes_after_quiet_period(git_repo: Path):
    session = CaptureSession(git_repo)
    session.handle_event(_user_text("edit via watcher"))
    watcher = Watcher(git_repo, session, quiet_period=0.2, max_wait=2.0)
    watcher.start()
    try:
        (git_repo / "w.py").write_text("watched\n")
        deadline = time.time() + 5.0
        version = None
        while time.time() < deadline:
            prompts = _prompts(git_repo)
            if prompts and prompts[0].tree_after:
                version = prompts[0]
                break
            time.sleep(0.1)
    finally:
        watcher.stop()

    assert version is not None
    assert "w.py" in store.read_object(git_repo, version.diff_ref)
