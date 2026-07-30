"""Framework-free screen models for the ProFact TUI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import graph_layout, health, log_view, recovery, store, version_detail


@dataclass(frozen=True)
class HistoryQuery:
    branch: str | None = None
    path_mode: bool = False
    search: str = ""
    tag: str | None = None
    version_type: str | None = None
    model: str | None = None
    changed_path: str | None = None
    since: str | None = None
    test_status: str | None = None


@dataclass(frozen=True)
class HistoryRow:
    version: store.Version
    summary: str
    tags: tuple[str, ...]
    test_result: store.TestResult | None
    is_head: bool
    graph_node: str
    graph_continuation: str
    graph_connector: str | None
    parent_visible: bool


@dataclass(frozen=True)
class HistoryModel:
    branch: str
    head_id: str | None
    branches: tuple[str, ...]
    rows: tuple[HistoryRow, ...]
    total_visible: int
    filtered_out: int
    omitted: int
    filters: tuple[str, ...]
    path_mode: bool
    search: str
    shape: graph_layout.HistoryShape


def load_history(repo_root: Path, query: HistoryQuery) -> HistoryModel:
    """Build one immutable history view using the existing log query rules."""
    prepared = log_view.prepare_query(
        repo_root,
        path_mode=query.path_mode,
        show_tests=True,
        tag=query.tag,
        version_type=query.version_type,
        branch_filter=query.branch,
        model=query.model,
        changed_path=query.changed_path,
        since=query.since,
        test_status=query.test_status,
    )
    versions = prepared.versions
    search_filtered = 0
    needle = query.search.strip().casefold()
    if needle:
        before = len(versions)
        versions = [
            version
            for version in versions
            if _matches_search(
                repo_root,
                version,
                prepared.tags.get(version.id, []),
                needle,
            )
        ]
        search_filtered = before - len(versions)

    graph_rows = {
        row.version.id: row for row in graph_layout.layout(versions)
    }
    tests = prepared.tests or {}
    rows = tuple(
        HistoryRow(
            version=version,
            summary=log_view.summary(repo_root, version),
            tags=tuple(prepared.tags.get(version.id, [])),
            test_result=tests.get(version.id),
            is_head=version.id == prepared.head,
            graph_node=graph_rows[version.id].node,
            graph_continuation=graph_rows[version.id].continuation,
            graph_connector=graph_rows[version.id].connector,
            parent_visible=graph_rows[version.id].parent_visible,
        )
        for version in versions
    )

    conn = store.connect(repo_root)
    try:
        branch_names = {
            version.git_branch for version in store.all_versions(conn)
        } | set(store.all_heads(conn))
    finally:
        conn.close()

    filters = list(prepared.filters)
    if needle:
        filters.append(f"search={query.search.strip()}")
    return HistoryModel(
        branch=prepared.branch,
        head_id=prepared.head,
        branches=tuple(sorted(branch_names)),
        rows=rows,
        total_visible=len(prepared.base_versions),
        filtered_out=prepared.filtered_out + search_filtered,
        omitted=prepared.omitted,
        filters=tuple(filters),
        path_mode=query.path_mode,
        search=query.search.strip(),
        shape=graph_layout.history_shape(prepared.base_versions),
    )


def load_detail(
    repo_root: Path, version_id: str, *, branch: str
) -> version_detail.VersionDetail:
    """Load detail for a concrete version in the context of a viewed branch."""
    conn = store.connect(repo_root)
    try:
        version = store.get_version(conn, version_id)
        if version is None:
            raise ValueError(f"no such version '{version_id}'")
        tags = tuple(store.tags_by_version(conn).get(version_id, []))
        result = store.get_test_result(conn, version_id)
    finally:
        conn.close()
    return version_detail.load(
        repo_root,
        version,
        head_id=store.read_head(repo_root, branch, seed=False),
        tags=tags,
        test_result=result,
    )


def load_health(repo_root: Path) -> health.HealthReport:
    return health.analyze(repo_root)


def overview_text(detail: version_detail.VersionDetail) -> str:
    version = detail.version
    lines = [
        f"Version: {version.id}{' (HEAD)' if detail.is_head else ''}",
        f"Type: {version.type}",
        f"Origin branch: {version.git_branch}",
        f"Parent: {version.parent_id or '-'}",
        f"Timestamp: {version.timestamp}",
        f"Finalized: {'yes' if version.tree_after is not None else 'no (open turn)'}",
        f"Confidence: {version.confidence or 'unknown'}",
    ]
    if version.model:
        model = version.model
        if version.temperature is not None:
            model += f" @ temp={version.temperature}"
        lines.append(f"Model: {model}")
    if version.thinking_source:
        lines.append(f"Thinking: {version.thinking_source}")
    if version.commit_hash:
        lines.append(f"Git commit: {version.commit_hash}")
    lines.append(f"Tags: {', '.join(detail.tags) if detail.tags else '-'}")
    if detail.test_result:
        result = detail.test_result
        code = "-" if result.exit_code is None else str(result.exit_code)
        lines.extend(
            [
                f"Test: {result.status} (exit {code})",
                f"Test command: {result.command or '-'}",
                f"Tested at: {result.timestamp}",
            ]
        )
    else:
        lines.append("Test: untested")
    if version.confidence == recovery.INCOMPLETE_CONFIDENCE:
        lines.extend(
            [
                "",
                "Warning: capture ended before its artifact was recovered;",
                "working-tree changes were not attributed to this prompt.",
            ]
        )
    elif version.confidence and version.confidence != "high":
        lines.extend(
            [
                "",
                "Warning: files changed concurrently with this turn;",
                "attribution may be imprecise.",
            ]
        )
    return "\n".join(lines)


def tests_text(detail: version_detail.VersionDetail) -> str:
    result = detail.test_result
    if result is None:
        return "Untested\n\nNo test result has been recorded for this version."
    code = "-" if result.exit_code is None else str(result.exit_code)
    lines = [
        f"Status: {result.status}",
        f"Command: {result.command or '-'}",
        f"Exit code: {code}",
        f"Recorded: {result.timestamp}",
        "",
        "This is one recorded run; it is not a claim about the current tree.",
        "",
        "Output:",
        detail.test_output or "(no output captured)",
    ]
    return "\n".join(lines)


def _matches_search(
    repo_root: Path,
    version: store.Version,
    tags: list[str],
    needle: str,
) -> bool:
    fields = [
        version.id,
        version.type,
        version.git_branch,
        version.model or "",
        log_view.summary(repo_root, version),
        *tags,
    ]
    return any(needle in field.casefold() for field in fields)