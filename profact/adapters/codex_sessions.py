"""Live capture from Codex's append-only local session rollouts."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from watchfiles import Change, watch

from .. import git_ops, prompt_text, store
from . import hook_capture

COMPLETION_SOURCE = "codex-session-monitor"
RESUMED_REASON = "Codex completion was processed after monitor restart"


class CodexMonitorError(RuntimeError):
    """Raised when the local Codex session directory cannot be monitored."""


@dataclass
class TurnState:
    turn_id: str
    baseline_usage: dict[str, int] | None
    model: str | None = None
    summed_usage: dict[str, int] = field(default_factory=dict)
    api_calls: int = 0


@dataclass
class FileState:
    offset: int = 0
    pending: bytes = b""
    session_id: str | None = None
    cwd: Path | None = None
    eligible: bool = False
    latest_total: dict[str, int] | None = None
    current_turn: str | None = None
    turns: dict[str, TurnState] = field(default_factory=dict)


def session_root() -> Path:
    """Return the active Codex session root for this user."""
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"


class CodexSessionMonitor:
    """Tail Codex rollouts and translate new local turns into ProFact versions."""

    def __init__(
        self,
        repo_root: Path,
        *,
        sessions: Path | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.sessions = (sessions or session_root()).expanduser().resolve()
        self.notify = notify or (lambda _message: None)
        self._files: dict[Path, FileState] = {}

    def run(self, stop_event: threading.Event | None = None) -> None:
        """Monitor until ``stop_event`` is set or the process is interrupted."""
        if not self.sessions.exists():
            raise CodexMonitorError(
                f"Codex session directory does not exist: {self.sessions}"
            )
        self._baseline_existing_files()
        self.notify(f"monitoring Codex sessions under {self.sessions}")
        for changes in watch(
            self.sessions,
            watch_filter=_jsonl_filter,
            stop_event=stop_event,
            debounce=50,
            step=10,
            rust_timeout=100,
            yield_on_timeout=True,
        ):
            if not changes:
                self._poll_known_files()
                continue
            for _change, raw_path in changes:
                self.process_path(Path(raw_path))

    def process_path(self, path: Path) -> None:
        """Consume complete records appended to one rollout file."""
        if path.suffix != ".jsonl" or not path.exists():
            return
        path = path.resolve()
        state = self._files.get(path)
        if state is None:
            state = FileState()
            self._files[path] = state
        size = path.stat().st_size
        if size < state.offset:
            self._prime(path, state, resumable=self._open_codex_turns())
            return
        if size == state.offset:
            return
        with path.open("rb") as handle:
            handle.seek(state.offset)
            chunk = handle.read()
        state.offset += len(chunk)
        data = state.pending + chunk
        complete, separator, trailing = data.rpartition(b"\n")
        if not separator:
            state.pending = data
            return
        state.pending = trailing
        for raw_line in complete.splitlines():
            self._process_line(state, raw_line)

    def process_record(self, state: FileState, record: dict) -> None:
        """Process one decoded record; exposed for deterministic adapter tests."""
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        record_type = record.get("type")
        timestamp = record.get("timestamp")

        if record_type == "session_meta":
            self._apply_session_meta(state, payload)
            return
        if not state.eligible or not state.session_id:
            return

        conversation_id = f"codex-{state.session_id}"
        payload_type = payload.get("type")
        if record_type == "event_msg" and payload_type == "task_started":
            turn_id = _text(payload.get("turn_id"))
            if not turn_id:
                return
            turn = TurnState(turn_id, _copy_usage(state.latest_total))
            state.turns[turn_id] = turn
            state.current_turn = turn_id
            hook_capture.open_prompt(
                self.repo_root,
                conversation_id=conversation_id,
                generation_id=turn_id,
                prompt_text="",
                model=None,
                superseded_source="codex-session-superseded",
            )
            return

        if record_type == "turn_context":
            turn_id = _text(payload.get("turn_id"))
            model = _text(payload.get("model"))
            self._set_model(state, conversation_id, turn_id, model)
            return

        if record_type == "event_msg" and payload_type == "thread_settings_applied":
            settings = payload.get("thread_settings")
            model = _text(settings.get("model")) if isinstance(settings, dict) else None
            self._set_model(state, conversation_id, state.current_turn, model)
            return

        if record_type == "response_item" and payload_type == "message":
            role = payload.get("role")
            if role == "user":
                turn_id = _message_turn_id(payload) or state.current_turn
                prompt = _message_content(payload)
                if turn_id and prompt:
                    hook_capture.attach_prompt(
                        self.repo_root,
                        conversation_id=conversation_id,
                        generation_id=turn_id,
                        text=prompt,
                    )
            elif role == "assistant" and payload.get("phase") == "commentary":
                turn_id = _message_turn_id(payload) or state.current_turn
                commentary = _message_content(payload)
                if turn_id and commentary:
                    hook_capture.append_thinking(
                        self.repo_root,
                        conversation_id=conversation_id,
                        generation_id=turn_id,
                        text=commentary,
                    )
            return

        if record_type == "event_msg" and payload_type == "user_message":
            turn_id = state.current_turn
            prompt = prompt_text.user_request(_text(payload.get("message")))
            if turn_id and prompt:
                hook_capture.attach_prompt(
                    self.repo_root,
                    conversation_id=conversation_id,
                    generation_id=turn_id,
                    text=prompt,
                )
            return

        if record_type == "response_item" and payload_type == "reasoning":
            turn_id = state.current_turn
            summary = _reasoning_summary(payload.get("summary"))
            if turn_id and summary:
                hook_capture.append_thinking(
                    self.repo_root,
                    conversation_id=conversation_id,
                    generation_id=turn_id,
                    text=summary,
                )
            return

        if record_type == "event_msg" and payload_type == "token_count":
            self._apply_usage(state, payload)
            return

        if record_type == "event_msg" and payload_type == "task_complete":
            turn_id = _text(payload.get("turn_id"))
            if not turn_id:
                return
            turn = state.turns.get(turn_id)
            response = _text(payload.get("last_agent_message"))
            if response:
                hook_capture.append_response(
                    self.repo_root,
                    conversation_id=conversation_id,
                    generation_id=turn_id,
                    text=response,
                    model=turn.model if turn else None,
                    deduplicate=True,
                )
            hook_capture.update_metadata(
                self.repo_root,
                conversation_id=conversation_id,
                generation_id=turn_id,
                model=turn.model if turn else None,
                usage=_turn_usage(turn, state.latest_total) if turn else None,
            )
            hook_capture.finalize_turn(
                self.repo_root,
                conversation_id=conversation_id,
                generation_id=turn_id,
                completion_source=COMPLETION_SOURCE,
                completed_at=_text(timestamp),
            )
            state.turns.pop(turn_id, None)
            if state.current_turn == turn_id:
                state.current_turn = None

    def _baseline_existing_files(self) -> None:
        resumable = self._open_codex_turns()
        for path in self.sessions.rglob("*.jsonl"):
            state = FileState()
            self._files[path.resolve()] = state
            self._prime(path, state, resumable=resumable)
        self._repair_invalid_prompts()

    def _prime(
        self,
        path: Path,
        state: FileState,
        *,
        resumable: set[tuple[str, str]] | None = None,
    ) -> None:
        """Learn file identity/usage while deliberately skipping old turns."""
        state.pending = b""
        state.session_id = None
        state.cwd = None
        state.eligible = False
        state.latest_total = None
        state.current_turn = None
        state.turns.clear()
        resumed_turn: str | None = None
        try:
            with path.open("rb") as handle:
                for raw_line in handle:
                    if not raw_line.endswith(b"\n"):
                        state.pending = raw_line
                        break
                    try:
                        record = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if record.get("type") == "session_meta":
                        self._apply_session_meta(state, payload)
                        if resumed_turn:
                            self.process_record(state, record)
                    elif (
                        record.get("type") == "event_msg"
                        and payload.get("type") == "task_started"
                        and state.session_id
                        and (
                            state.session_id,
                            _text(payload.get("turn_id")) or "",
                        )
                        in (resumable or set())
                    ):
                        resumed_turn = _text(payload.get("turn_id"))
                        self.process_record(state, record)
                    elif resumed_turn:
                        self.process_record(state, record)
                        if (
                            record.get("type") == "event_msg"
                            and payload.get("type") == "task_complete"
                            and _text(payload.get("turn_id")) == resumed_turn
                        ):
                            self._mark_resumed_uncertain(state.session_id, resumed_turn)
                            resumed_turn = None
                    elif (
                        record.get("type") == "event_msg"
                        and payload.get("type") == "token_count"
                    ):
                        total, _last = _usage_payload(payload)
                        if total is not None:
                            state.latest_total = total
            state.offset = path.stat().st_size
        except OSError:
            state.offset = 0

    def _poll_known_files(self) -> None:
        for path in tuple(self._files):
            self.process_path(path)

    def _open_codex_turns(self) -> set[tuple[str, str]]:
        with store.connect(self.repo_root) as conn:
            versions = store.open_prompts(conn)
        result: set[tuple[str, str]] = set()
        for version in versions:
            if (
                version.conversation_id
                and version.conversation_id.startswith("codex-")
                and version.generation_id
            ):
                result.add(
                    (
                        version.conversation_id.removeprefix("codex-"),
                        version.generation_id,
                    )
                )
        return result

    def _repair_invalid_prompts(self) -> None:
        """Replace previously captured provider context with the real request."""
        with store.connect(self.repo_root) as conn:
            versions = store.all_versions(conn)
        targets: set[tuple[str, str]] = set()
        for version in versions:
            if (
                not version.conversation_id
                or not version.conversation_id.startswith("codex-")
                or not version.generation_id
                or not version.prompt_ref
            ):
                continue
            captured = store.read_object(self.repo_root, version.prompt_ref)
            if captured and prompt_text.user_request(captured) is None:
                targets.add(
                    (
                        version.conversation_id.removeprefix("codex-"),
                        version.generation_id,
                    )
                )
        if not targets:
            return

        for path in self.sessions.rglob("*.jsonl"):
            session_id: str | None = None
            try:
                lines = path.open("rb")
            except OSError:
                continue
            with lines:
                for raw_line in lines:
                    try:
                        record = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if record.get("type") == "session_meta":
                        session_id = _text(
                            payload.get("session_id") or payload.get("id")
                        )
                        continue
                    if (
                        record.get("type") != "response_item"
                        or payload.get("type") != "message"
                        or payload.get("role") != "user"
                        or not session_id
                    ):
                        continue
                    turn_id = _message_turn_id(payload)
                    key = (session_id, turn_id or "")
                    prompt = _message_content(payload)
                    if key not in targets or not prompt:
                        continue
                    hook_capture.attach_prompt(
                        self.repo_root,
                        conversation_id=f"codex-{session_id}",
                        generation_id=turn_id or "",
                        text=prompt,
                    )
                    targets.discard(key)
                    if not targets:
                        return

    def _mark_resumed_uncertain(
        self, session_id: str | None, turn_id: str
    ) -> None:
        if not session_id:
            return
        with store.connect(self.repo_root) as conn:
            version = store.find_prompt_by_turn(
                conn, f"codex-{session_id}", turn_id
            )
            if version:
                store.mark_versions_uncertain(conn, [version.id], RESUMED_REASON)
                conn.commit()

    def _process_line(self, state: FileState, raw_line: bytes) -> None:
        if not raw_line.strip():
            return
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.notify("ignored one malformed Codex session record")
            return
        if isinstance(record, dict):
            self.process_record(state, record)

    def _apply_session_meta(self, state: FileState, payload: dict) -> None:
        state.session_id = _text(payload.get("session_id") or payload.get("id"))
        cwd = _text(payload.get("cwd"))
        state.cwd = Path(cwd).expanduser().resolve() if cwd else None
        state.eligible = bool(state.cwd and self._belongs_to_repo(state.cwd))

    def _belongs_to_repo(self, cwd: Path) -> bool:
        try:
            return git_ops.repo_root(cwd).resolve() == self.repo_root
        except git_ops.GitError:
            return False

    def _set_model(
        self,
        state: FileState,
        conversation_id: str,
        turn_id: str | None,
        model: str | None,
    ) -> None:
        if not turn_id or not model:
            return
        turn = state.turns.get(turn_id)
        if turn:
            turn.model = model
        hook_capture.update_metadata(
            self.repo_root,
            conversation_id=conversation_id,
            generation_id=turn_id,
            model=model,
        )

    def _apply_usage(self, state: FileState, payload: dict) -> None:
        total, last = _usage_payload(payload)
        if total is not None:
            state.latest_total = total
        if not state.current_turn:
            return
        turn = state.turns.get(state.current_turn)
        if turn is None:
            return
        if last:
            for key, value in last.items():
                turn.summed_usage[key] = turn.summed_usage.get(key, 0) + value
        turn.api_calls += 1


def _jsonl_filter(change: Change, path: str) -> bool:
    return change != Change.deleted and path.endswith(".jsonl")


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _message_turn_id(payload: dict) -> str | None:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    return _text(metadata.get("turn_id")) if isinstance(metadata, dict) else None


def _message_content(payload: dict) -> str | None:
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    texts = [
        item.get("text")
        for item in content
        if isinstance(item, dict)
        and item.get("type") in {"input_text", "output_text", "text"}
        and isinstance(item.get("text"), str)
    ]
    return prompt_text.user_request("\n".join(texts)) if texts else None


def _reasoning_summary(value) -> str | None:
    if not isinstance(value, list):
        return None
    texts: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            texts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return "\n".join(texts) if texts else None


def _usage_payload(payload: dict) -> tuple[dict[str, int] | None, dict[str, int] | None]:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None, None
    return _usage_values(info.get("total_token_usage")), _usage_values(
        info.get("last_token_usage")
    )


def _usage_values(value) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    mapping = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cached_input_tokens": "cache_read_tokens",
        "cache_write_input_tokens": "cache_creation_tokens",
        "reasoning_output_tokens": "reasoning_tokens",
    }
    result = {
        target: raw
        for source, target in mapping.items()
        if isinstance((raw := value.get(source)), int) and raw >= 0
    }
    return result or None


def _copy_usage(value: dict[str, int] | None) -> dict[str, int] | None:
    return dict(value) if value is not None else None


def _turn_usage(
    turn: TurnState, final_total: dict[str, int] | None
) -> dict[str, int]:
    usage: dict[str, int] = {}
    if final_total:
        for key, final in final_total.items():
            baseline = (turn.baseline_usage or {}).get(key, 0)
            if final >= baseline:
                usage[key] = final - baseline
    for key, value in turn.summed_usage.items():
        if key not in usage:
            usage[key] = value
    usage["api_call_count"] = turn.api_calls
    return usage
