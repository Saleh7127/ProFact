from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import git_ops, store
from .adapters.base import CaptureEvent
from .aggregator import Turn, TurnAggregator

CORRELATION_WINDOW = 10.0


class CaptureSession:
    """Wires CaptureEvents and filesystem settles to the version store.

    Turn open  -> snapshot tree_before, create an open `prompt` version, move HEAD.
    Settle     -> snapshot tree_after + diff for the owning version; edits with no
                  open turn become a `human` version (timeline stays continuous).
    Turn close -> persist response text and finalize tree_after.

    All public methods are thread-safe: the proxy event loop drives open/close
    while the watcher thread drives settle.
    """

    def __init__(self, repo_root: Path, correlation_window: float = CORRELATION_WINDOW) -> None:
        self.repo_root = repo_root
        self.correlation_window = correlation_window
        self.aggregator = TurnAggregator(on_open=self._on_open, on_close=self._on_close)
        self._lock = threading.RLock()
        self._version_by_turn: dict[int, str] = {}
        self._open_version_id: str | None = None
        self._turn_open = False
        self._last_ai_version: str | None = None
        self._last_close_monotonic: float | None = None

    def handle_event(self, event: CaptureEvent) -> None:
        with self._lock:
            self.aggregator.handle(event)

    def check_idle(self) -> None:
        with self._lock:
            self.aggregator.check_idle(_now_iso())

    def flush(self) -> None:
        with self._lock:
            self.aggregator.flush()

    def settle(self, changed_paths: list[str] | None = None) -> None:
        with self._lock:
            if self._turn_open and self._open_version_id is not None:
                self._finalize(self._open_version_id)
            elif self._last_ai_version is not None and self._within_window():
                self._finalize(self._last_ai_version, confidence="uncertain")
            else:
                self._create_human_version()

    def _on_open(self, turn: Turn) -> None:
        branch = git_ops.current_branch(self.repo_root)
        tree_before = git_ops.write_tree(self.repo_root)
        parent_id = store.read_head(self.repo_root, branch)
        prompt_ref = store.write_object(self.repo_root, turn.prompt_text)
        with store.connect(self.repo_root) as conn:
            version = store.create_version(
                conn,
                type="prompt",
                git_branch=branch,
                parent_id=parent_id,
                tree_before=tree_before,
                prompt_ref=prompt_ref,
                model=turn.model,
                temperature=turn.temperature,
            )
        self._version_by_turn[turn.index] = version.id
        self._open_version_id = version.id
        self._last_ai_version = version.id
        self._turn_open = True
        store.write_head(self.repo_root, branch, version.id)

    def _on_close(self, turn: Turn) -> None:
        version_id = self._version_by_turn.pop(turn.index, None)
        self._turn_open = False
        self._open_version_id = None
        self._last_close_monotonic = time.monotonic()
        if version_id is None:
            return
        response_text = "\n".join(_response_text(event) for event in turn.responses)
        response_ref = (
            store.write_object(self.repo_root, response_text) if response_text else None
        )
        with store.connect(self.repo_root) as conn:
            conn.execute(
                """
                UPDATE versions
                SET response_ref = ?,
                    model = COALESCE(model, ?),
                    temperature = COALESCE(temperature, ?)
                WHERE id = ?
                """,
                (response_ref, turn.model, turn.temperature, version_id),
            )
            conn.commit()
        self._finalize(version_id)

    def _finalize(self, version_id: str, confidence: str | None = None) -> None:
        with store.connect(self.repo_root) as conn:
            version = store.get_version(conn, version_id)
            if version is None:
                return
            tree_after = git_ops.write_tree(self.repo_root)
            diff = git_ops.diff_trees(self.repo_root, version.tree_before, tree_after)
            diff_ref = store.write_object(self.repo_root, diff) if diff.strip() else None
            conn.execute(
                """
                UPDATE versions
                SET tree_after = ?,
                    diff_ref = ?,
                    confidence = COALESCE(?, confidence)
                WHERE id = ?
                """,
                (tree_after, diff_ref, confidence, version_id),
            )
            conn.commit()

    def _create_human_version(self) -> None:
        branch = git_ops.current_branch(self.repo_root)
        head_id = store.read_head(self.repo_root, branch)
        tree_after = git_ops.write_tree(self.repo_root)
        tree_before = tree_after
        if head_id is not None:
            with store.connect(self.repo_root) as conn:
                parent = store.get_version(conn, head_id)
            if parent is not None:
                tree_before = parent.tree_after or parent.tree_before
        if tree_before == tree_after:
            return
        diff = git_ops.diff_trees(self.repo_root, tree_before, tree_after)
        diff_ref = store.write_object(self.repo_root, diff) if diff.strip() else None
        with store.connect(self.repo_root) as conn:
            version = store.create_version(
                conn,
                type="human",
                git_branch=branch,
                parent_id=head_id,
                tree_before=tree_before,
                tree_after=tree_after,
                diff_ref=diff_ref,
            )
        store.write_head(self.repo_root, branch, version.id)

    def _within_window(self) -> bool:
        if self._last_close_monotonic is None:
            return False
        return (time.monotonic() - self._last_close_monotonic) <= self.correlation_window


def _response_text(event: CaptureEvent) -> str:
    return "".join(
        block.get("text", "")
        for block in event.content_blocks
        if block.get("type") == "text"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(host: str = "127.0.0.1", port: int = 7845) -> None:
    """Run a standalone capture session (proxy + watcher) for manual testing.

    Run this, then point Claude Code at the proxy with ANTHROPIC_BASE_URL.
    """
    from .adapters.claude_code_proxy import ClaudeCodeProxyAdapter
    from .watcher import Watcher

    repo_root = git_ops.repo_root(Path.cwd())
    if not store.is_initialized(repo_root):
        raise SystemExit("profact is not initialized; run `profact init` first")

    session = CaptureSession(repo_root)
    adapter = ClaudeCodeProxyAdapter(host=host, port=port)
    adapter.on_event(session.handle_event)
    watcher = Watcher(repo_root, session)

    adapter.start()
    watcher.start()
    print(f"profact proxy listening on http://{host}:{port}")
    print(f"  ANTHROPIC_BASE_URL=http://{host}:{port} claude")
    print("  Ctrl-C to stop")
    try:
        while True:
            time.sleep(1.0)
            session.check_idle()
    except KeyboardInterrupt:
        pass
    finally:
        session.flush()
        watcher.stop()
        adapter.stop()


def run_claude(repo_root: Path, host: str = "127.0.0.1", port: int = 7845) -> int:
    """Start the proxy + watcher, then launch `claude` pointed at the proxy.

    Blocks until `claude` exits, then tears everything down. Returns claude's
    exit code. Used by `profact start --claude`.
    """
    import os
    import shutil
    import subprocess

    from .adapters.claude_code_proxy import ClaudeCodeProxyAdapter
    from .watcher import Watcher

    claude_bin = shutil.which("claude")
    if claude_bin is None:
        raise SystemExit(
            "`claude` CLI not found on PATH. Install Claude Code, or use the "
            "default `profact start` (Cursor hooks) instead."
        )

    session = CaptureSession(repo_root)
    adapter = ClaudeCodeProxyAdapter(host=host, port=port)
    adapter.on_event(session.handle_event)
    watcher = Watcher(repo_root, session)

    adapter.start()
    watcher.start()

    env = {**os.environ, "ANTHROPIC_BASE_URL": f"http://{host}:{port}"}
    stop_idle = threading.Event()

    def idle_loop() -> None:
        while not stop_idle.wait(1.0):
            session.check_idle()

    idle_thread = threading.Thread(target=idle_loop, daemon=True)
    idle_thread.start()

    print(f"profact: capturing Claude Code via proxy on http://{host}:{port}")
    try:
        result = subprocess.run([claude_bin], env=env, cwd=repo_root)
        return result.returncode
    finally:
        stop_idle.set()
        session.flush()
        watcher.stop()
        adapter.stop()


if __name__ == "__main__":
    run()
