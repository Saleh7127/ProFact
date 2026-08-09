"""Prompt blame: attribute each line of a file to the profact version — and
thus the *prompt* — that last produced it.

Where `git blame` answers "which commit last touched this line", prompt blame
answers the question that matters in AI-assisted work: "which prompt made the AI
write this, and what did I actually ask for?" Every line is traced back to the
intent (or human edit, or pre-existing code) that created it.

Attribution strategy
---------------------
profact stores a *tree snapshot* per version, not a commit chain, so there is no
history for Git to blame directly. Instead we replay the target version's
ancestry as a throwaway chain of commit objects (each wrapping that version's
stored tree) and run Git's own blame engine over it. This reuses Git's
battle-tested line tracking — reverts, re-additions, and moved lines all resolve
correctly — and then we map each blamed commit back to its profact version.

The synthetic commits are unreachable (no ref points at them) and never enter
the user's real history; Git garbage-collects them.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import git_ops, prompt_text, store

_SHA_HEADER = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)")


class BlameError(RuntimeError):
    """Raised for user-facing blame failures (unknown version, missing file)."""


@dataclass
class BlameLine:
    lineno: int
    content: str
    version: store.Version | None  # None only if attribution could not resolve


@dataclass
class BlameResult:
    rel_path: str
    target: store.Version
    lines: list[BlameLine]


def ancestry_chain(conn: sqlite3.Connection, version_id: str) -> list[store.Version]:
    """The lineage of ``version_id`` oldest-first — the slice of the DAG that
    could have contributed lines to the target snapshot. Thin alias for
    :func:`store.ancestry`."""
    return store.ancestry(conn, version_id)


def _relative_path(repo_root: Path, file: str) -> str:
    """Normalize a user-supplied path to a repo-relative POSIX path."""
    candidate = Path(file)
    absolute = candidate if candidate.is_absolute() else (Path.cwd() / candidate)
    try:
        rel = os.path.relpath(absolute, repo_root)
    except ValueError as exc:  # e.g. different drive on Windows
        raise BlameError(f"'{file}' is outside the repository") from exc
    if rel.startswith(".."):
        raise BlameError(f"'{file}' is outside the repository")
    return Path(rel).as_posix()


def _version_tree(version: store.Version) -> str:
    return version.tree_after or version.tree_before


def blame_file(
    repo_root: Path,
    conn: sqlite3.Connection,
    *,
    file: str,
    version_id: str,
) -> BlameResult:
    """Attribute every line of ``file`` at ``version_id`` to a profact version."""
    target = store.get_version(conn, version_id)
    if target is None:
        raise BlameError(f"no such version '{version_id}'")

    rel_path = _relative_path(repo_root, file)
    target_tree = _version_tree(target)
    if not git_ops.path_in_tree(repo_root, target_tree, rel_path):
        raise BlameError(f"'{rel_path}' does not exist in {target.id}")

    chain = ancestry_chain(conn, version_id)

    # Replay the lineage as a synthetic commit chain over the stored trees.
    commit_to_version: dict[str, store.Version] = {}
    parent_sha: str | None = None
    tip_sha: str | None = None
    for version in chain:
        sha = git_ops.commit_tree(
            repo_root,
            _version_tree(version),
            message=version.id,
            parent=parent_sha,
            date=version.timestamp,
        )
        commit_to_version[sha] = version
        parent_sha = sha
        tip_sha = sha

    assert tip_sha is not None  # chain always contains at least the target

    porcelain = git_ops.blame_porcelain(repo_root, tip_sha, rel_path)
    lines = _parse_porcelain(porcelain, commit_to_version)
    return BlameResult(rel_path=rel_path, target=target, lines=lines)


def _parse_porcelain(
    porcelain: str, commit_to_version: dict[str, store.Version]
) -> list[BlameLine]:
    """Parse ``git blame --line-porcelain`` output into ordered BlameLines."""
    lines: list[BlameLine] = []
    current_version: store.Version | None = None
    current_lineno = 0
    for raw in porcelain.splitlines():
        header = _SHA_HEADER.match(raw)
        if header:
            current_version = commit_to_version.get(header.group(1))
            current_lineno = int(header.group(2))
        elif raw.startswith("\t"):
            lines.append(
                BlameLine(
                    lineno=current_lineno,
                    content=raw[1:],
                    version=current_version,
                )
            )
    return lines


def slice_range(lines: list[BlameLine], start: int, end: int | None) -> list[BlameLine]:
    end = end if end is not None else start
    return [line for line in lines if start <= line.lineno <= (end or start)]


def parse_line_range(spec: str) -> tuple[int, int | None]:
    """Parse a ``-L`` spec: ``N`` or ``N:M`` (M optional / open-ended)."""
    parts = spec.split(":", 1)
    try:
        start = int(parts[0])
        end = int(parts[1]) if len(parts) == 2 and parts[1] else None
    except ValueError as exc:
        raise BlameError(f"invalid line range '{spec}' (use N or N:M)") from exc
    if start < 1 or (end is not None and end < start):
        raise BlameError(f"invalid line range '{spec}'")
    return start, end


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

_TYPE_ABBREV = {
    "prompt": "prompt",
    "anchor": "anchor",
    "human": "human ",
    "checkpoint": "chkpnt",
}


def origin_label(repo_root: Path, version: store.Version | None) -> str:
    """A short, honest description of where a line came from (Tier 3)."""
    if version is None:
        return "(unattributed)"
    if version.type == "anchor":
        return "pre-existing (before profact)"
    if version.type == "human":
        return "human edit (no prompt)"
    if version.type == "checkpoint":
        return "auto-checkpoint"
    if version.prompt_ref:
        text = store.read_object(repo_root, version.prompt_ref) or ""
        text = prompt_text.user_request(text) or ""
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        return first.strip() or "(empty prompt)"
    return "(no prompt text)"


def _short(text: str, length: int = 34) -> str:
    text = text.strip()
    return text if len(text) <= length else text[: length - 1] + "\u2026"


def render_blame(repo_root: Path, result: BlameResult) -> str:
    """Tier 1/3: one annotated line per source line (git-blame style)."""
    out: list[str] = []
    width = max((len(str(line.lineno)) for line in result.lines), default=1)
    for line in result.lines:
        version = line.version
        vid = version.id if version else "?"
        vtype = _TYPE_ABBREV.get(version.type, "?") if version else "?     "
        date = version.timestamp[:10] if version else "----------"
        summary = _short(origin_label(repo_root, version))
        out.append(
            f"{vid:<5} {vtype} {date} {summary:<34} "
            f"{line.lineno:>{width}}| {line.content}"
        )
    return "\n".join(out)


def _file_section(diff_text: str, rel_path: str) -> str:
    """Extract the ``diff --git`` section for ``rel_path`` from a unified diff."""
    section: list[str] = []
    capturing = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            capturing = line.endswith(f" b/{rel_path}") or f" b/{rel_path}" in line
            if capturing:
                section.append(line)
            continue
        if capturing:
            section.append(line)
    return "\n".join(section)


def render_show(repo_root: Path, result: BlameResult) -> str:
    """Tier 2: expand each distinct contributing version to its full intent —
    prompt, response excerpt, and the exact hunk that touched this file."""
    out: list[str] = []
    seen: set[str] = set()
    # Preserve first-appearance order of versions across the (possibly sliced) lines.
    ordered: list[store.Version] = []
    for line in result.lines:
        if line.version and line.version.id not in seen:
            seen.add(line.version.id)
            ordered.append(line.version)

    for version in ordered:
        out.append("=" * 72)
        out.append(f"{version.id} [{version.type}]  {version.timestamp}")
        if version.model:
            out.append(f"  model: {version.model}")
        prompt = (
            store.read_object(repo_root, version.prompt_ref)
            if version.prompt_ref
            else None
        )
        prompt = prompt_text.user_request(prompt)
        if prompt:
            out.append("\nprompt:")
            out.append("\n".join("  " + ln for ln in prompt.splitlines()))
        response = (
            store.read_object(repo_root, version.response_ref)
            if version.response_ref
            else None
        )
        if response:
            out.append("\nresponse (excerpt):")
            excerpt = _short_response(response)
            out.append("\n".join("  " + ln for ln in excerpt.splitlines()))
        diff = (
            store.read_object(repo_root, version.diff_ref)
            if version.diff_ref
            else None
        )
        section = _file_section(diff, result.rel_path) if diff else ""
        out.append(f"\nchange to {result.rel_path}:")
        out.append(section if section.strip() else "  (no recorded hunk for this file)")
        out.append("")
    return "\n".join(out)


def _short_response(text: str, max_lines: int = 12) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n… ({len(lines) - max_lines} more lines)"
