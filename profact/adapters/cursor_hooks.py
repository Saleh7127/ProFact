"""Cursor Hooks capture adapter (primary).

Cursor invokes a hook as a short-lived process per event, so this adapter holds
no in-memory state: each call is a direct, idempotent write to the profact store.
The only state carried between events is *which version is currently open* — the
HEAD prompt version whose ``tree_after`` is still NULL (LOCKED decision (b)).

Event flow for one user turn:
    beforeSubmitPrompt  -> open a `prompt` version, snapshot tree_before, move HEAD
    afterAgentResponse  -> append the assistant text to the open version
    stop / sessionEnd   -> snapshot tree_after, compute the diff, finalize

Diffs come from Git-tree snapshots (same mechanism as the proxy path), so
`afterFileEdit` is not required for capture and is intentionally not wired.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import git_ops, store

EVENT_PROMPT = "beforeSubmitPrompt"
EVENT_RESPONSE = "afterAgentResponse"
EVENT_STOP = "stop"
EVENT_SESSION_END = "sessionEnd"

# Events profact registers in .cursor/hooks.json (see cli.start).
HANDLED_EVENTS = (EVENT_PROMPT, EVENT_RESPONSE, EVENT_STOP, EVENT_SESSION_END)


def handle(repo_root: Path, event: dict) -> dict:
    """Apply one Cursor hook event to the store. Returns a hook response dict.

    All profact hooks are informational/fire-and-forget, so the response is
    always an empty object; callers must still fail open on any error.
    """
    if not store.is_initialized(repo_root):
        return {}

    name = event.get("hook_event_name")
    if name == EVENT_PROMPT:
        _on_prompt(repo_root, event)
    elif name == EVENT_RESPONSE:
        _on_response(repo_root, event)
    elif name in (EVENT_STOP, EVENT_SESSION_END):
        with store.connect(repo_root) as conn:
            _finalize_open(repo_root, conn)
    return {}


def _on_prompt(repo_root: Path, event: dict) -> None:
    branch = git_ops.current_branch(repo_root)
    prompt_text = event.get("prompt") or ""
    prompt_ref = store.write_object(repo_root, prompt_text) if prompt_text else None
    with store.connect(repo_root) as conn:
        # A new user message closes any turn left open (e.g. `stop` never fired).
        _finalize_open(repo_root, conn)
        tree_before = git_ops.write_tree(repo_root)
        parent_id = store.read_head(repo_root) or None
        version = store.create_version(
            conn,
            type="prompt",
            git_branch=branch,
            parent_id=parent_id,
            tree_before=tree_before,
            prompt_ref=prompt_ref,
            model=event.get("model"),
            generation_id=event.get("generation_id"),
            conversation_id=event.get("conversation_id"),
        )
    store.write_head(repo_root, version.id)


def _on_response(repo_root: Path, event: dict) -> None:
    text = event.get("text") or ""
    if not text:
        return
    with store.connect(repo_root) as conn:
        version = _open_prompt(repo_root, conn)
        if version is None:
            return
        existing = (
            store.read_object(repo_root, version.response_ref)
            if version.response_ref
            else None
        )
        combined = f"{existing}\n{text}" if existing else text
        response_ref = store.write_object(repo_root, combined)
        conn.execute(
            "UPDATE versions SET response_ref = ?, model = COALESCE(model, ?) WHERE id = ?",
            (response_ref, event.get("model"), version.id),
        )
        conn.commit()


def _finalize_open(repo_root: Path, conn: sqlite3.Connection) -> None:
    """Snapshot tree_after + diff for the currently open turn, if any. Idempotent."""
    version = _open_prompt(repo_root, conn)
    if version is None:
        return
    tree_after = git_ops.write_tree(repo_root)
    diff = git_ops.diff_trees(repo_root, version.tree_before, tree_after)
    diff_ref = store.write_object(repo_root, diff) if diff.strip() else None
    conn.execute(
        "UPDATE versions SET tree_after = ?, diff_ref = ? WHERE id = ?",
        (tree_after, diff_ref, version.id),
    )
    conn.commit()


def _open_prompt(repo_root: Path, conn: sqlite3.Connection) -> store.Version | None:
    """The open turn = the HEAD `prompt` version not yet finalized (tree_after NULL)."""
    head = store.read_head(repo_root)
    if not head:
        return None
    version = store.get_version(conn, head)
    if version and version.type == "prompt" and version.tree_after is None:
        return version
    return None
