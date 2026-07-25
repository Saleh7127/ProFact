"""Behavior-aware history: run a test command and record pass/fail per version.

profact already knows *what* each prompt changed and *why*; recording whether
the resulting code **passes its tests** makes the history outcome-aware. That
turns a prompt DAG into a debugging and analysis substrate: markers in `log`,
outcome context in `show`, and the foundation for later analytics (prompt
effectiveness, bug-causing-prompt analysis, session health).

The runner is deliberately thin: it shells out to a user-supplied command at the
repo root, classifies the exit code, captures the output as a content-addressed
blob, and upserts one result per version (a re-run overwrites the prior one).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import store

PASS = "pass"
FAIL = "fail"
ERROR = "error"

# Compact, ASCII-safe markers for `log`/`status` (no '*' or '/', which the graph
# renderer and its tests reserve).
_MARKERS = {PASS: "[pass]", FAIL: "[FAIL]", ERROR: "[err ]"}


class TestRunError(RuntimeError):
    """User-facing failure (e.g. empty command)."""


@dataclass
class RunOutcome:
    result: store.TestResult
    output: str


def classify(exit_code: int) -> str:
    return PASS if exit_code == 0 else FAIL


def marker(status: str | None) -> str:
    """A fixed-width marker for display; blank when a version was never tested."""
    return _MARKERS.get(status or "", "[  ? ]")


def run_and_record(
    repo_root: Path,
    conn,
    *,
    version_id: str,
    cmd: list[str],
) -> RunOutcome:
    """Run ``cmd`` at the repo root and record its outcome on ``version_id``.

    Exit 0 -> pass; any other exit -> fail; a command that cannot be launched is
    recorded as ``error`` (so a broken invocation is distinguishable from a real
    test failure).
    """
    if not cmd:
        raise TestRunError("no test command given")

    try:
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
        exit_code: int | None = proc.returncode
        output = (proc.stdout or "") + (proc.stderr or "")
        status = classify(proc.returncode)
    except (FileNotFoundError, OSError) as exc:
        exit_code = None
        output = f"could not run test command: {exc}\n"
        status = ERROR

    output_ref = store.write_object(repo_root, output) if output else None
    command_str = " ".join(cmd)
    result = store.set_test_result(
        conn,
        version_id,
        status=status,
        exit_code=exit_code,
        command=command_str,
        output_ref=output_ref,
    )
    return RunOutcome(result=result, output=output)


def format_result(result: store.TestResult) -> str:
    """One-line human summary of a recorded result."""
    code = "" if result.exit_code is None else f" (exit {result.exit_code})"
    cmd = f" — {result.command}" if result.command else ""
    return f"{marker(result.status)} {result.status}{code}{cmd}"
