from __future__ import annotations

import threading
from pathlib import Path

from watchfiles import watch

from .session import CaptureSession

DEFAULT_QUIET_PERIOD = 2.0
DEFAULT_MAX_WAIT = 30.0
STARTUP_TIMEOUT = 5.0
READY_PULSE_MS = 50


def _ignore(_change, path: str) -> bool:
    normalized = path.replace("\\", "/")
    return "/.git/" not in normalized and "/.profact/" not in normalized


class Watcher:
    """Watches the repo and calls session.settle() once changes go quiet.

    watchfiles groups events and yields after `step` ms of quiet (the quiet
    period), or after `debounce` ms at most (the cap). Each yielded batch is one
    settle. .git and .profact writes are filtered out to avoid feedback loops.
    """

    def __init__(
        self,
        repo_root: Path,
        session: CaptureSession,
        quiet_period: float = DEFAULT_QUIET_PERIOD,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.session = session
        self._step = max(int(quiet_period * 1000), 1)
        self._debounce = max(int(max_wait * 1000), self._step)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        if not self._ready.wait(STARTUP_TIMEOUT):
            raise RuntimeError("profact watcher did not become ready")

    def run(self) -> None:
        try:
            for changes in watch(
                self.repo_root,
                step=self._step,
                debounce=self._debounce,
                watch_filter=_ignore,
                stop_event=self._stop,
                rust_timeout=READY_PULSE_MS,
                yield_on_timeout=True,
            ):
                self._ready.set()
                if changes:
                    self.session.settle([path for _, path in changes])
        finally:
            self._ready.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
