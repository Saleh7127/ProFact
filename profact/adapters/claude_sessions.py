"""Live capture from Claude Code's append-only local session transcripts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from watchfiles import Change, watch

from .. import git_ops, human_edits, store
from . import hook_capture

COMPLETION_SOURCE = "claude-session-monitor"
SUPERSEDED_SOURCE = "claude-session-superseded"
RESUMED_REASON = "Claude completion was processed after monitor restart"


class ClaudeMonitorError(RuntimeError):
    """Raised when Claude's local session directory cannot be monitored."""


@dataclass
class TurnState:
    prompt_id: str
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    seen_message_ids: set[str] = field(default_factory=set)
    seen_event_ids: set[str] = field(default_factory=set)
    api_calls: int = 0


@dataclass
class FileState:
    offset: int = 0
    pending: bytes = b""
    session_id: str | None = None
    cwd: Path | None = None
    eligible: bool = False
    current_prompt: str | None = None
    turns: dict[str, TurnState] = field(default_factory=dict)


def session_root() -> Path:
    """Return the directory containing Claude Code project transcripts."""
    return Path(
        os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")
    ).expanduser() / "projects"


class ClaudeSessionMonitor:
    """Tail Claude transcripts and translate typed turns into ProFact versions."""

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
        self._ready = threading.Event()

    def run(self, stop_event: threading.Event | None = None) -> None:
        if not self.sessions.exists():
            self._ready.set()
            raise ClaudeMonitorError(
                f"Claude session directory does not exist: {self.sessions}"
            )
        self._baseline_existing_files()
        self._ready.set()
        self.notify(f"monitoring Claude sessions under {self.sessions}")
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
                self.poll()
                continue
            for _change, raw_path in changes:
                self.process_path(Path(raw_path))

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def poll(self) -> None:
        """Process appends to known files and discover newly created transcripts."""
        for path in tuple(self._files):
            self.process_path(path)
        if self.sessions.exists():
            for path in self.sessions.rglob("*.jsonl"):
                if "/subagents/" not in path.as_posix() and path.resolve() not in self._files:
                    self.process_path(path)

    def process_path(self, path: Path) -> None:
        if (
            path.suffix != ".jsonl"
            or not path.exists()
            or "/subagents/" in path.as_posix()
        ):
            return
        path = path.resolve()
        state = self._files.setdefault(path, FileState())
        size = path.stat().st_size
        if size < state.offset:
            self._prime(path, state, resumable=self._open_claude_turns())
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
        self._apply_identity(state, record)
        if not state.eligible or not state.session_id:
            return
        if record.get("isSidechain") is True:
            return

        if _is_typed_human_prompt(record):
            prompt_id = _text(record.get("promptId"))
            prompt = _message_text(record.get("message"))
            if not prompt_id or not prompt:
                return
            state.current_prompt = prompt_id
            state.turns[prompt_id] = TurnState(prompt_id)
            hook_capture.open_prompt(
                self.repo_root,
                conversation_id=_conversation_id(state.session_id),
                generation_id=prompt_id,
                prompt_text=prompt,
                model=None,
                superseded_source=SUPERSEDED_SOURCE,
            )
            return

        prompt_id = state.current_prompt
        if not prompt_id:
            return
        turn = state.turns.get(prompt_id)
        if turn is None:
            return

        if record.get("type") == "assistant":
            self._process_assistant(state.session_id, turn, record)
            return

        if record.get("type") == "system" and record.get("subtype") == "turn_duration":
            conversation_id = _conversation_id(state.session_id)
            hook_capture.update_metadata(
                self.repo_root,
                conversation_id=conversation_id,
                generation_id=prompt_id,
                model=turn.model,
                usage={**turn.usage, "api_call_count": turn.api_calls},
            )
            hook_capture.finalize_turn(
                self.repo_root,
                conversation_id=conversation_id,
                generation_id=prompt_id,
                completion_source=COMPLETION_SOURCE,
                completed_at=_text(record.get("timestamp")),
            )
            state.turns.pop(prompt_id, None)
            state.current_prompt = None

    def _process_assistant(
        self, session_id: str, turn: TurnState, record: dict
    ) -> None:
        event_id = _text(record.get("uuid"))
        if event_id and event_id in turn.seen_event_ids:
            return
        if event_id:
            turn.seen_event_ids.add(event_id)

        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        model = _text(message.get("model"))
        if model:
            turn.model = model
        message_id = _text(message.get("id"))
        usage = _normalize_usage(message.get("usage"))
        if usage and (not message_id or message_id not in turn.seen_message_ids):
            for key, value in usage.items():
                turn.usage[key] = turn.usage.get(key, 0) + value
            turn.api_calls += 1
            if message_id:
                turn.seen_message_ids.add(message_id)

        thinking: list[str] = []
        response: list[str] = []
        stop_reason = _text(message.get("stop_reason"))
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = _text(block.get("thinking") or block.get("text"))
                if not text:
                    continue
                if block.get("type") == "thinking" or stop_reason != "end_turn":
                    thinking.append(text)
                elif block.get("type") == "text":
                    response.append(text)

        conversation_id = _conversation_id(session_id)
        for text in thinking:
            hook_capture.append_thinking(
                self.repo_root,
                conversation_id=conversation_id,
                generation_id=turn.prompt_id,
                text=text,
            )
        for text in response:
            hook_capture.append_response(
                self.repo_root,
                conversation_id=conversation_id,
                generation_id=turn.prompt_id,
                text=text,
                model=turn.model,
                deduplicate=True,
            )

    def _baseline_existing_files(self) -> None:
        resumable = self._open_claude_turns()
        for path in self.sessions.rglob("*.jsonl"):
            if "/subagents/" in path.as_posix():
                continue
            state = FileState()
            self._files[path.resolve()] = state
            self._prime(path, state, resumable=resumable)

    def _prime(
        self,
        path: Path,
        state: FileState,
        *,
        resumable: set[tuple[str, str]] | None = None,
    ) -> None:
        state.pending = b""
        state.session_id = None
        state.cwd = None
        state.eligible = False
        state.current_prompt = None
        state.turns.clear()
        replaying: str | None = None
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
                    if not isinstance(record, dict):
                        continue
                    self._apply_identity(state, record)
                    prompt_id = _text(record.get("promptId"))
                    key = (
                        state.session_id or "",
                        prompt_id or "",
                    )
                    if _is_typed_human_prompt(record) and key in (resumable or set()):
                        replaying = prompt_id
                        self.process_record(state, record)
                    elif replaying:
                        self.process_record(state, record)
                        if (
                            record.get("type") == "system"
                            and record.get("subtype") == "turn_duration"
                        ):
                            self._mark_resumed_uncertain(state.session_id, replaying)
                            replaying = None
            state.offset = path.stat().st_size
        except OSError:
            state.offset = 0

    def _apply_identity(self, state: FileState, record: dict) -> None:
        session_id = _text(record.get("sessionId") or record.get("session_id"))
        if session_id:
            state.session_id = session_id
        cwd = _text(record.get("cwd"))
        if cwd:
            state.cwd = Path(cwd).expanduser().resolve()
            state.eligible = self._belongs_to_repo(state.cwd)

    def _belongs_to_repo(self, cwd: Path) -> bool:
        try:
            return git_ops.repo_root(cwd).resolve() == self.repo_root
        except git_ops.GitError:
            return False

    def _open_claude_turns(self) -> set[tuple[str, str]]:
        with store.connect(self.repo_root) as conn:
            versions = store.open_prompts(conn)
        return {
            (
                version.conversation_id.removeprefix("claude-session-"),
                version.generation_id,
            )
            for version in versions
            if version.conversation_id
            and version.conversation_id.startswith("claude-session-")
            and version.generation_id
        }

    def _mark_resumed_uncertain(
        self, session_id: str | None, prompt_id: str
    ) -> None:
        if not session_id:
            return
        with store.connect(self.repo_root) as conn:
            version = store.find_prompt_by_turn(
                conn, _conversation_id(session_id), prompt_id
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
            self.notify("ignored one malformed Claude session record")
            return
        if isinstance(record, dict):
            self.process_record(state, record)


def run_claude(
    repo_root: Path,
    *,
    sessions: Path | None = None,
    notify: Callable[[str], None] | None = None,
) -> int:
    """Launch Claude Code while monitoring its structured local transcript."""
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        raise SystemExit(
            "`claude` CLI not found on PATH. Install Claude Code, or use the "
            "default `profact start` (Cursor hooks) instead."
        )

    monitor = ClaudeSessionMonitor(repo_root, sessions=sessions, notify=notify)
    stop_event = threading.Event()
    errors: list[BaseException] = []

    def monitor_loop() -> None:
        try:
            monitor.run(stop_event)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()
    if not monitor.wait_until_ready():
        stop_event.set()
        raise ClaudeMonitorError("Claude session monitor did not become ready")
    if errors:
        raise errors[0]

    print("profact: capturing Claude Code from its local session transcript")
    exit_code = 130
    try:
        result = subprocess.run([claude_bin], cwd=repo_root)
        exit_code = result.returncode
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        thread.join(timeout=5)
        monitor.poll()
        human_edits.reconcile(repo_root)
    if errors:
        error = errors[0]
        if isinstance(error, (OSError, ClaudeMonitorError)):
            raise error
        raise ClaudeMonitorError(str(error)) from error
    return exit_code


def _conversation_id(session_id: str) -> str:
    return f"claude-session-{session_id}"


def _is_typed_human_prompt(record: dict) -> bool:
    origin = record.get("origin")
    message = record.get("message")
    return (
        record.get("type") == "user"
        and record.get("promptSource") == "typed"
        and isinstance(origin, dict)
        and origin.get("kind") == "human"
        and isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
    )


def _message_text(message) -> str | None:
    if not isinstance(message, dict):
        return None
    return _text(message.get("content"))


def _normalize_usage(value) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    mapping = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cache_read_input_tokens": "cache_read_tokens",
        "cache_creation_input_tokens": "cache_creation_tokens",
    }
    return {
        target: amount
        for source, target in mapping.items()
        if isinstance((amount := value.get(source)), int)
        and not isinstance(amount, bool)
        and amount >= 0
    }


def _jsonl_filter(change: Change, path: str) -> bool:
    return (
        change != Change.deleted
        and path.endswith(".jsonl")
        and "/subagents/" not in path.replace("\\", "/")
    )


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None
