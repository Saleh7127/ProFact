"""Shared durable lifecycle for stateless hook-based capture adapters."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import git_ops, human_edits, store

OVERLAP_REASON = "overlapping sessions shared one working tree"
LATE_EVENT_REASON = "capture event arrived after turn finalization"
BRANCH_CHANGE_REASON = "git branch changed while turn was open"


def open_prompt(
    repo_root: Path,
    *,
    conversation_id: str | None,
    generation_id: str | None,
    prompt_text: str,
    model: str | None,
    superseded_source: str,
    allow_reused_turn_id: bool = False,
    legacy_fallback: bool = False,
) -> store.Version | None:
    """Open one idempotent prompt turn and atomically advance branch HEAD."""
    branch = git_ops.current_branch(repo_root)
    prompt_ref = store.write_object(repo_root, prompt_text) if prompt_text else None

    with store.connect(repo_root) as conn:
        existing = store.find_prompt_by_turn(conn, conversation_id, generation_id)
        if existing is not None:
            if existing.prompt_ref == prompt_ref or not allow_reused_turn_id:
                return existing
            # Some legacy Cursor payloads reuse a generation id. Preserve the
            # sequential turn without falsely assigning the duplicate key.
            generation_id = None
        previous = _open_for_adapter(
            repo_root,
            conn,
            conversation_id,
            legacy_fallback=legacy_fallback,
        )

    for version in previous:
        finalize_version(repo_root, version.id, superseded_source)

    with store.connect(repo_root) as conn:
        existing = store.find_prompt_by_turn(conn, conversation_id, generation_id)
        if existing is not None:
            return existing
        other_open = store.open_prompts(conn)

    tree_before = (
        git_ops.write_tree(repo_root)
        if other_open
        else human_edits.reconcile(repo_root, branch).tree
    )
    try:
        version, _created = store.create_prompt_at_head(
            repo_root,
            git_branch=branch,
            tree_before=tree_before,
            prompt_ref=prompt_ref,
            model=model,
            conversation_id=conversation_id,
            generation_id=generation_id,
        )
        return version
    except sqlite3.IntegrityError:
        # A duplicate hook process may race the first lookup. The unique turn
        # index makes the later delivery an idempotent success.
        with store.connect(repo_root) as conn:
            return store.find_prompt_by_turn(conn, conversation_id, generation_id)


def append_thinking(
    repo_root: Path,
    *,
    conversation_id: str | None,
    generation_id: str | None,
    text: str,
    legacy_fallback: bool = False,
    reused_key_fallback: bool = False,
) -> store.Version | None:
    if not text:
        return None
    return _append_content(
        repo_root,
        conversation_id=conversation_id,
        generation_id=generation_id,
        text=text,
        field="thinking_ref",
        model=None,
        deduplicate=False,
        legacy_fallback=legacy_fallback,
        reused_key_fallback=reused_key_fallback,
    )


def append_response(
    repo_root: Path,
    *,
    conversation_id: str | None,
    generation_id: str | None,
    text: str,
    model: str | None = None,
    deduplicate: bool = False,
    legacy_fallback: bool = False,
    reused_key_fallback: bool = False,
) -> store.Version | None:
    if not text:
        return None
    return _append_content(
        repo_root,
        conversation_id=conversation_id,
        generation_id=generation_id,
        text=text,
        field="response_ref",
        model=model,
        deduplicate=deduplicate,
        legacy_fallback=legacy_fallback,
        reused_key_fallback=reused_key_fallback,
    )


def finalize_turn(
    repo_root: Path,
    *,
    conversation_id: str | None,
    generation_id: str | None,
    completion_source: str,
    legacy_fallback: bool = False,
    reused_key_fallback: bool = False,
) -> bool:
    with store.connect(repo_root) as conn:
        version = resolve_turn(
            repo_root,
            conn,
            conversation_id,
            generation_id,
            legacy_fallback=legacy_fallback,
            reused_key_fallback=reused_key_fallback,
        )
    return bool(version) and finalize_version(
        repo_root, version.id, completion_source
    )


def finalize_version(
    repo_root: Path, version_id: str, completion_source: str
) -> bool:
    """Finalize one exact turn with compare-and-set idempotency."""
    with store.connect(repo_root) as conn:
        version = store.get_version(conn, version_id)
    if version is None or version.type != "prompt" or version.tree_after is not None:
        return False

    tree_after = git_ops.write_tree(repo_root)
    diff = git_ops.diff_trees(repo_root, version.tree_before, tree_after)
    diff_ref = store.write_object(repo_root, diff) if diff.strip() else None
    current_branch = git_ops.current_branch(repo_root)
    with store.connect(repo_root) as conn:
        current = store.get_version(conn, version.id)
        if current is None or current.tree_after is not None:
            return False
        open_now = store.open_prompts(conn)
        overlap = any(item.id != version.id for item in open_now)
        branch_changed = current_branch != version.git_branch
        reason = None
        if overlap:
            reason = OVERLAP_REASON
            store.mark_versions_uncertain(
                conn, [item.id for item in open_now], reason
            )
        elif branch_changed:
            reason = BRANCH_CHANGE_REASON
        confidence = (
            "uncertain" if overlap or branch_changed else current.confidence
        )
        cursor = conn.execute(
            "UPDATE versions SET tree_after = ?, diff_ref = ?, confidence = ?, "
            "confidence_reason = COALESCE(confidence_reason, ?), "
            "completed_at = ?, completion_source = ?, "
            "thinking_source = COALESCE(thinking_source, ?) "
            "WHERE id = ? AND tree_after IS NULL",
            (
                tree_after,
                diff_ref,
                confidence,
                reason,
                datetime.now(timezone.utc).isoformat(),
                completion_source,
                store.THINKING_NONE,
                version.id,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1


def refresh_finalized_turn(
    repo_root: Path,
    *,
    conversation_id: str,
    completion_source: str,
) -> bool:
    """Refresh a finalized turn after a provider-driven continuation."""
    with store.connect(repo_root) as conn:
        version = store.latest_prompt_for_conversation(conn, conversation_id)
    if version is None or version.tree_after is None:
        return False

    tree_after = git_ops.write_tree(repo_root)
    if tree_after == version.tree_after:
        return False
    diff = git_ops.diff_trees(repo_root, version.tree_before, tree_after)
    diff_ref = store.write_object(repo_root, diff) if diff.strip() else None
    current_branch = git_ops.current_branch(repo_root)

    with store.connect(repo_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = store.get_version(conn, version.id)
        if current is None or current.tree_after != version.tree_after:
            conn.rollback()
            return False
        open_now = store.open_prompts(conn)
        overlap = bool(open_now)
        branch_changed = current_branch != current.git_branch
        reason = None
        if overlap:
            reason = OVERLAP_REASON
            store.mark_versions_uncertain(
                conn, [current.id, *[item.id for item in open_now]], reason
            )
        elif branch_changed:
            reason = BRANCH_CHANGE_REASON
        confidence = (
            "uncertain" if overlap or branch_changed else current.confidence
        )
        cursor = conn.execute(
            "UPDATE versions SET tree_after = ?, diff_ref = ?, confidence = ?, "
            "confidence_reason = COALESCE(confidence_reason, ?), "
            "completed_at = ?, completion_source = ? "
            "WHERE id = ? AND tree_after = ?",
            (
                tree_after,
                diff_ref,
                confidence,
                reason,
                datetime.now(timezone.utc).isoformat(),
                completion_source,
                current.id,
                version.tree_after,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1


def reconcile_if_idle(repo_root: Path) -> None:
    with store.connect(repo_root) as conn:
        has_open_turns = bool(store.open_prompts(conn))
    if not has_open_turns:
        human_edits.reconcile(repo_root)


def resolve_turn(
    repo_root: Path,
    conn: sqlite3.Connection,
    conversation_id: str | None,
    generation_id: str | None,
    *,
    legacy_fallback: bool = False,
    reused_key_fallback: bool = False,
) -> store.Version | None:
    exact = store.find_prompt_by_turn(conn, conversation_id, generation_id)
    if exact is not None:
        if exact.tree_after is not None and conversation_id and reused_key_fallback:
            collision_fallbacks = [
                version
                for version in store.open_prompts(
                    conn, conversation_id=conversation_id
                )
                if version.generation_id is None
            ]
            if len(collision_fallbacks) == 1:
                return collision_fallbacks[0]
        return exact
    if conversation_id and generation_id:
        return None
    if conversation_id:
        open_for_session = store.open_prompts(
            conn, conversation_id=conversation_id
        )
        return open_for_session[-1] if open_for_session else None
    return _legacy_open_prompt(repo_root, conn) if legacy_fallback else None


def _append_content(
    repo_root: Path,
    *,
    conversation_id: str | None,
    generation_id: str | None,
    text: str,
    field: str,
    model: str | None,
    deduplicate: bool,
    legacy_fallback: bool,
    reused_key_fallback: bool,
) -> store.Version | None:
    with store.connect(repo_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        version = resolve_turn(
            repo_root,
            conn,
            conversation_id,
            generation_id,
            legacy_fallback=legacy_fallback,
            reused_key_fallback=reused_key_fallback,
        )
        if version is None:
            conn.rollback()
            return None
        reference = getattr(version, field)
        existing = store.read_object(repo_root, reference) if reference else None
        if deduplicate and existing == text:
            conn.commit()
            return version
        combined = f"{existing}\n{text}" if existing else text
        content_ref = store.write_object(repo_root, combined)
        late = version.tree_after is not None
        confidence = "uncertain" if late else version.confidence
        reason = LATE_EVENT_REASON if late else version.confidence_reason
        if field == "thinking_ref":
            conn.execute(
                "UPDATE versions SET thinking_ref = ?, thinking_source = ?, "
                "confidence = ?, confidence_reason = COALESCE(confidence_reason, ?) "
                "WHERE id = ?",
                (
                    content_ref,
                    store.THINKING_CAPTURED,
                    confidence,
                    reason,
                    version.id,
                ),
            )
        else:
            conn.execute(
                "UPDATE versions SET response_ref = ?, model = COALESCE(model, ?), "
                "confidence = ?, confidence_reason = COALESCE(confidence_reason, ?) "
                "WHERE id = ?",
                (content_ref, model, confidence, reason, version.id),
            )
        conn.commit()
        return store.get_version(conn, version.id)


def _open_for_adapter(
    repo_root: Path,
    conn: sqlite3.Connection,
    conversation_id: str | None,
    *,
    legacy_fallback: bool,
) -> list[store.Version]:
    if conversation_id:
        return store.open_prompts(conn, conversation_id=conversation_id)
    legacy = _legacy_open_prompt(repo_root, conn) if legacy_fallback else None
    return [legacy] if legacy else []


def _legacy_open_prompt(
    repo_root: Path, conn: sqlite3.Connection
) -> store.Version | None:
    branch = git_ops.current_branch(repo_root)
    head = store.read_head(repo_root, branch, seed=False)
    if not head:
        return None
    version = store.get_version(conn, head)
    if version and version.type == "prompt" and version.tree_after is None:
        return version
    return None
