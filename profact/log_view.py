"""Query preparation and terminal rendering for ``profact log``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from . import git_ops, store, testrunner

_GRAPH_GUTTER = 8


class LogQueryError(ValueError):
    """A user-facing query validation error with its CLI exit code."""

    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class PreparedLog:
    branch: str
    head: str | None
    base_versions: list[store.Version]
    versions: list[store.Version]
    tests: dict[str, store.TestResult] | None
    tags: dict[str, list[str]]
    omitted: int
    filtered_out: int
    filters: list[str]


def prepare_query(
    repo_root: Path,
    *,
    path_mode: bool,
    show_tests: bool,
    tag: str | None,
    version_type: str | None,
    branch_filter: str | None,
    model: str | None,
    changed_path: str | None,
    since: str | None,
    test_status: str | None,
) -> PreparedLog:
    """Validate options, load branch history, and apply all requested filters."""
    branch = branch_filter or git_ops.current_branch(repo_root)
    head = store.read_head(repo_root, branch, seed=False)

    normalized_type = version_type.lower() if version_type else None
    if normalized_type and normalized_type not in store.VERSION_TYPES:
        raise LogQueryError(
            f"unknown version type '{normalized_type}'; "
            f"choose from {', '.join(store.VERSION_TYPES)}",
            exit_code=2,
        )

    normalized_test_status = normalize_test_filter(test_status)
    if test_status and normalized_test_status is None:
        raise LogQueryError(
            f"unknown test status '{test_status}'; "
            "choose pass, fail, error, or untested",
            exit_code=2,
        )

    try:
        since_cutoff = parse_since(since) if since else None
        normalized_path = normalize_path(repo_root, changed_path) if changed_path else None
    except ValueError as exc:
        raise LogQueryError(str(exc), exit_code=2) from exc

    include_tests = show_tests or normalized_test_status is not None
    with store.connect(repo_root) as conn:
        if path_mode:
            base_versions = ancestry(conn, head)
            visible = store.list_branch_versions(conn, branch, head=head)
            omitted = max(0, len(visible) - len(base_versions))
        else:
            base_versions = store.list_branch_versions(conn, branch, head=head)
            omitted = 0
        tests = store.all_test_results(conn) if include_tests else None
        tags = store.tags_by_version(conn)
        if tag and store.get_tag(conn, tag) is None:
            raise LogQueryError(f"no such tag '{tag}'", exit_code=1)

    base_versions = sorted(
        base_versions, key=lambda version: version.timestamp, reverse=True
    )
    versions = filter_versions(
        repo_root,
        base_versions,
        tags=tags,
        tests=tests or {},
        tag=tag,
        version_type=normalized_type,
        model=model,
        changed_path=normalized_path,
        since_cutoff=since_cutoff,
        test_status=normalized_test_status,
    )
    filters = filter_description(
        branch_filter=branch_filter,
        tag=tag,
        version_type=normalized_type,
        model=model,
        changed_path=normalized_path,
        since=since,
        test_status=normalized_test_status,
    )
    return PreparedLog(
        branch=branch,
        head=head,
        base_versions=base_versions,
        versions=versions,
        tests=tests,
        tags=tags,
        omitted=omitted,
        filtered_out=len(base_versions) - len(versions),
        filters=filters,
    )


def ancestry(conn, head_id: str | None) -> list[store.Version]:
    chain: list[store.Version] = []
    seen: set[str] = set()
    current = head_id
    while current and current not in seen:
        seen.add(current)
        version = store.get_version(conn, current)
        if version is None:
            break
        chain.append(version)
        current = version.parent_id
    return chain


def summary(repo_root: Path, version: store.Version) -> str:
    if version.type == "anchor":
        where = f" @ {version.commit_hash[:8]}" if version.commit_hash else ""
        return f"start of history{where}"
    if version.type == "checkpoint":
        return "auto-saved work before a checkout"
    if version.type == "human":
        return "manual edit (no prompt)"
    if version.prompt_ref:
        text = store.read_object(repo_root, version.prompt_ref) or ""
        first = text.strip().splitlines()[0] if text.strip() else ""
        return _short(first) or "(empty prompt)"
    return "(no prompt text)"


def filter_versions(
    repo_root: Path,
    versions: list[store.Version],
    *,
    tags: dict[str, list[str]],
    tests: dict[str, store.TestResult],
    tag: str | None,
    version_type: str | None,
    model: str | None,
    changed_path: str | None,
    since_cutoff: datetime | None,
    test_status: str | None,
) -> list[store.Version]:
    result: list[store.Version] = []
    model_query = model.casefold() if model else None
    for version in versions:
        if tag and tag not in tags.get(version.id, []):
            continue
        if version_type and version.type != version_type:
            continue
        if model_query and model_query not in (version.model or "").casefold():
            continue
        if since_cutoff and _version_timestamp(version) < since_cutoff:
            continue
        if changed_path and not _version_touches_path(repo_root, version, changed_path):
            continue
        if test_status:
            test_result = tests.get(version.id)
            actual_status = test_result.status if test_result else "untested"
            if actual_status != test_status:
                continue
        result.append(version)
    return result


def parse_since(value: str) -> datetime:
    duration = re.fullmatch(r"(\d+)([mhdw])", value.strip(), flags=re.IGNORECASE)
    if duration:
        amount = int(duration.group(1))
        unit = duration.group(2).lower()
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return datetime.now(timezone.utc) - delta
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"invalid --since value '{value}'; use 7d, 12h, 2w, or an ISO date"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_path(repo_root: Path, value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(repo_root.resolve())
        except ValueError as exc:
            raise ValueError("--file must be inside the repository") from exc
    normalized = candidate.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized == "." or normalized.startswith("../"):
        raise ValueError("--file must be a repository-relative file or directory")
    return normalized


def normalize_test_filter(value: str | None) -> str | None:
    if value is None:
        return None
    aliases = {
        "pass": "pass",
        "passed": "pass",
        "fail": "fail",
        "failed": "fail",
        "error": "error",
        "err": "error",
        "none": "untested",
        "untested": "untested",
    }
    return aliases.get(value.casefold())


def filter_description(
    *,
    branch_filter: str | None,
    tag: str | None,
    version_type: str | None,
    model: str | None,
    changed_path: str | None,
    since: str | None,
    test_status: str | None,
) -> list[str]:
    filters: list[str] = []
    for label, value in (
        ("branch", branch_filter),
        ("tag", tag),
        ("type", version_type),
        ("model", model),
        ("file", changed_path),
        ("since", since),
        ("test", test_status),
    ):
        if value:
            filters.append(f"{label}={value}")
    return filters


def diff_paths(diff_text: str) -> set[str]:
    paths: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git ") and " b/" in line:
            paths.add(line.split(" b/", 1)[1].strip())
    return paths


def render(
    repo_root: Path,
    versions: list[store.Version],
    *,
    shape_versions: list[store.Version],
    head: str | None,
    branch: str,
    tests: dict[str, store.TestResult] | None,
    tags: dict[str, list[str]],
    graph: bool,
    path_mode: bool,
    omitted: int,
    filtered_out: int,
    filters: list[str],
) -> None:
    if filters:
        typer.echo(f"filters: {' · '.join(filters)}\n")
    if graph:
        _print_graph(
            repo_root,
            versions,
            head,
            branch,
            tests,
            tags,
            path_mode=path_mode,
            omitted=omitted,
            filtered_out=filtered_out,
            shape_versions=shape_versions,
        )
    else:
        if path_mode:
            typer.echo(
                f"HEAD ancestry for branch '{branch}'"
                f"{f' (HEAD = {head})' if head else ''}\n"
            )
        for version in versions:
            typer.echo(_row_text(repo_root, version, head, tests, tags))
            typer.echo(_indent(_row_detail(repo_root, version, tests), prefix="      "))
        typer.echo("")
        if path_mode:
            typer.echo(_log_count_summary(len(versions), omitted, filtered_out))
        else:
            if filtered_out:
                typer.echo(
                    f"{len(versions)} matching version"
                    f"{'' if len(versions) == 1 else 's'} shown; "
                    f"{filtered_out} filtered out"
                )
            _print_history_shape(
                shape_versions,
                head,
                prefix="history " if filtered_out else "",
            )

    if tests is not None:
        _print_test_summary(versions, tests)


def _version_timestamp(version: store.Version) -> datetime:
    try:
        parsed = datetime.fromisoformat(version.timestamp)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _version_touches_path(
    repo_root: Path, version: store.Version, changed_path: str
) -> bool:
    diff = store.read_object(repo_root, version.diff_ref) if version.diff_ref else ""
    needle = changed_path.rstrip("/")
    for path in diff_paths(diff or ""):
        if (
            path == needle
            or path.endswith(f"/{needle}")
            or path.startswith(f"{needle}/")
        ):
            return True
    return False


def _row_text(
    repo_root: Path,
    version: store.Version,
    head: str | None,
    tests: dict[str, store.TestResult] | None = None,
    tags: dict[str, list[str]] | None = None,
) -> str:
    marker = "HEAD " if version.id == head else "     "
    branch = f"({version.git_branch})"
    prefix = ""
    if tests is not None:
        result = tests.get(version.id)
        prefix = (testrunner.marker(result.status) if result else "[none]") + " "
    tag_suffix = ""
    if tags and tags.get(version.id):
        tag_suffix = f"  <{', '.join(tags[version.id])}>"
    return (
        f"{prefix}{version.id:<5} {marker}[{version.type:<10}] "
        f"{branch:<14} {summary(repo_root, version)}{tag_suffix}"
    )


def _row_detail(
    repo_root: Path,
    version: store.Version,
    tests: dict[str, store.TestResult] | None = None,
) -> str:
    details = [_format_log_time(version.timestamp)]
    details.append(f"parent: {version.parent_id or '-'}")

    diff = store.read_object(repo_root, version.diff_ref) if version.diff_ref else None
    if version.type != "anchor":
        details.append(_diff_summary(diff or ""))
    if version.model:
        details.append(f"model: {_short(version.model, 36)}")
    if version.type == "prompt":
        details.append(f"thinking: {version.thinking_source or 'unknown'}")
    if version.confidence and version.confidence != "high":
        details.append(f"confidence: {version.confidence}")

    if tests is not None:
        result = tests.get(version.id)
        if result:
            code = "" if result.exit_code is None else f" exit {result.exit_code}"
            test_detail = f"test: {result.status}{code}"
            if result.command:
                test_detail += f" ({_short(result.command, 40)})"
            details.append(test_detail)
        else:
            details.append("test: untested")

    return " · ".join(details)


def _format_log_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    except ValueError:
        return value


def _diff_summary(diff_text: str) -> str:
    paths = diff_paths(diff_text)
    additions = 0
    deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    if not paths:
        return "no file changes"
    noun = "file" if len(paths) == 1 else "files"
    return f"{len(paths)} {noun} +{additions} -{deletions}"


def _print_graph(
    repo_root: Path,
    versions: list[store.Version],
    head: str | None,
    branch: str,
    tests: dict[str, store.TestResult] | None = None,
    tags: dict[str, list[str]] | None = None,
    *,
    path_mode: bool = False,
    omitted: int = 0,
    filtered_out: int = 0,
    shape_versions: list[store.Version] | None = None,
) -> None:
    ids = {version.id for version in versions}
    head_label = f"  (HEAD = {head})" if head else ""
    title = "HEAD ancestry graph" if path_mode else "DAG"
    typer.echo(f"{title} for branch '{branch}'{head_label}\n")

    lanes: list[str | None] = []
    for version in versions:
        if version.id in lanes:
            col = lanes.index(version.id)
        elif None in lanes:
            col = lanes.index(None)
            lanes[col] = version.id
        else:
            lanes.append(version.id)
            col = len(lanes) - 1

        node = "".join(
            "*" if index == col else ("|" if lane is not None else " ")
            for index, lane in enumerate(lanes)
        )
        typer.echo(
            f"{node:<{_GRAPH_GUTTER}} "
            f"{_row_text(repo_root, version, head, tests, tags)}"
        )
        detail_lanes = list(lanes)
        if version.parent_id not in ids:
            detail_lanes[col] = None
        continuation = "".join(
            "|" if lane is not None else " " for lane in detail_lanes
        )
        typer.echo(
            f"{continuation:<{_GRAPH_GUTTER}} "
            f"{_row_detail(repo_root, version, tests)}"
        )

        parent = version.parent_id if version.parent_id in ids else None
        merge: tuple[int, int] | None = None
        if parent is None:
            lanes[col] = None
        elif parent in lanes and lanes.index(parent) != col:
            other = lanes.index(parent)
            keep, drop = min(col, other), max(col, other)
            lanes[keep] = parent
            lanes[drop] = None
            merge = (keep, drop)
        else:
            lanes[col] = parent

        if merge is not None:
            keep, drop = merge
            connector = "".join(
                "/" if index == drop else ("|" if lanes[index] is not None or index == keep else " ")
                for index in range(len(lanes))
            )
            typer.echo(connector)

        while lanes and lanes[-1] is None:
            lanes.pop()

    typer.echo("")
    if path_mode:
        typer.echo(_log_count_summary(len(versions), omitted, filtered_out))
    else:
        if filtered_out:
            typer.echo(
                f"{len(versions)} matching version"
                f"{'' if len(versions) == 1 else 's'} shown; "
                f"{filtered_out} filtered out"
            )
        _print_history_shape(
            shape_versions or versions,
            head,
            prefix="history " if filtered_out else "",
        )


def _log_count_summary(shown: int, omitted: int, filtered_out: int) -> str:
    suffix = "version" if shown == 1 else "versions"
    side = "version" if omitted == 1 else "versions"
    result = f"{shown} {suffix} shown"
    if filtered_out:
        result += f"; {filtered_out} filtered out"
    return f"{result}; {omitted} side-path {side} omitted"


def _print_history_shape(
    versions: list[store.Version], head: str | None, *, prefix: str = ""
) -> None:
    ids = {version.id for version in versions}
    child_count: dict[str, int] = {}
    for version in versions:
        if version.parent_id in ids:
            child_count[version.parent_id] = child_count.get(version.parent_id, 0) + 1
    tips = [
        version.id + (" (HEAD)" if version.id == head else "")
        for version in versions
        if child_count.get(version.id, 0) == 0
    ]
    branch_points = [
        version.id for version in versions if child_count.get(version.id, 0) > 1
    ]
    typer.echo(f"{prefix}tips: {', '.join(tips) if tips else '-'}")
    typer.echo(
        f"{prefix}branch points: "
        f"{', '.join(branch_points) if branch_points else '-'}"
    )


def _print_test_summary(
    versions: list[store.Version], tests: dict[str, store.TestResult]
) -> None:
    counts = {status: 0 for status in store.TEST_STATUSES}
    untested = 0
    for version in versions:
        result = tests.get(version.id)
        if result is None:
            untested += 1
        else:
            counts[result.status] = counts.get(result.status, 0) + 1
    typer.echo(
        "tests: "
        f"{counts['pass']} passed, {counts['fail']} failed, "
        f"{counts['error']} errors, {untested} untested"
    )


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _short(text: str, length: int = 72) -> str:
    text = text.strip()
    return text if len(text) <= length else text[: length - 1] + "…"
