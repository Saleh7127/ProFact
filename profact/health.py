"""Read-only health analysis for a repository's captured profact history."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from . import git_ops, store


@dataclass(frozen=True)
class FileChurn:
    path: str
    version_ids: tuple[str, ...]
    additions: int
    deletions: int

    @property
    def touches(self) -> int:
        return len(self.version_ids)


@dataclass(frozen=True)
class TestSummary:
    passed: tuple[str, ...]
    failed: tuple[str, ...]
    errored: tuple[str, ...]
    untested: tuple[str, ...]


@dataclass(frozen=True)
class TestIssue:
    version_id: str
    status: str
    command: str | None


@dataclass(frozen=True)
class BisectCandidate:
    good_id: str
    bad_id: str
    command: str


@dataclass(frozen=True)
class HealthReport:
    repository: str
    branch: str
    head_id: str | None
    version_counts: dict[str, int]
    branch_names: tuple[str, ...]
    branch_points: tuple[str, ...]
    tips: tuple[str, ...]
    tests: TestSummary
    churn: tuple[FileChurn, ...]
    uncertain_versions: tuple[str, ...]
    dangling_turns: tuple[str, ...]
    side_path_tips: tuple[str, ...]
    test_issues: tuple[TestIssue, ...]
    bisect_candidates: tuple[BisectCandidate, ...]
    blame_path: str | None

    @property
    def total_versions(self) -> int:
        return sum(self.version_counts.values())


def analyze(repo_root: Path) -> HealthReport:
    """Calculate inspectable repository-wide history metrics without mutation."""
    branch = git_ops.current_branch(repo_root)
    head_id = store.read_head(repo_root, branch, seed=False)
    with store.connect(repo_root) as conn:
        versions = store.all_versions(conn)
        tests = store.all_test_results(conn)
        heads = store.all_heads(conn)
        tags = store.tags_by_version(conn)

    version_counts = {version_type: 0 for version_type in store.VERSION_TYPES}
    for version in versions:
        version_counts[version.type] = version_counts.get(version.type, 0) + 1

    version_by_id = {version.id: version for version in versions}
    child_count = {version.id: 0 for version in versions}
    for version in versions:
        if version.parent_id in child_count:
            child_count[version.parent_id] += 1

    branch_points = tuple(
        version.id for version in versions if child_count[version.id] > 1
    )
    tips = tuple(version.id for version in versions if child_count[version.id] == 0)

    referenced_tips = {version_id for version_id in heads.values() if version_id}
    referenced_tips.update(tags)
    side_path_tips = tuple(
        version.id
        for version in versions
        if child_count[version.id] == 0
        and version.parent_id is not None
        and version.tree_after is not None
        and version.id not in referenced_tips
    )

    finalized = [version for version in versions if version.tree_after is not None]
    passed: list[str] = []
    failed: list[str] = []
    errored: list[str] = []
    untested: list[str] = []
    test_issues: list[TestIssue] = []
    for version in finalized:
        result = tests.get(version.id)
        if result is None:
            untested.append(version.id)
        elif result.status == "pass":
            passed.append(version.id)
        elif result.status == "fail":
            failed.append(version.id)
            test_issues.append(TestIssue(version.id, result.status, result.command))
        else:
            errored.append(version.id)
            test_issues.append(TestIssue(version.id, result.status, result.command))

    churn = _file_churn(repo_root, finalized)
    uncertain_versions = tuple(
        version.id for version in versions if version.confidence != "high"
    )
    dangling_turns = tuple(
        version.id
        for version in versions
        if version.type == "prompt" and version.tree_after is None
    )
    bisect_candidates = _bisect_candidates(versions, tests, version_by_id)
    blame_path = _blame_path(repo_root, head_id, version_by_id, churn)

    branch_names = tuple(
        sorted({version.git_branch for version in versions} | set(heads))
    )
    return HealthReport(
        repository=repo_root.name,
        branch=branch or "(detached)",
        head_id=head_id,
        version_counts=version_counts,
        branch_names=branch_names,
        branch_points=branch_points,
        tips=tips,
        tests=TestSummary(
            passed=tuple(passed),
            failed=tuple(failed),
            errored=tuple(errored),
            untested=tuple(untested),
        ),
        churn=churn,
        uncertain_versions=uncertain_versions,
        dangling_turns=dangling_turns,
        side_path_tips=side_path_tips,
        test_issues=tuple(test_issues),
        bisect_candidates=bisect_candidates,
        blame_path=blame_path,
    )


def render(report: HealthReport, *, churn_limit: int = 5) -> str:
    """Render a compact report whose findings point back to concrete versions."""
    counts = report.version_counts
    lines = [
        "ProFact history health",
        f"Repository: {report.repository}",
        f"Current: {report.branch} @ {report.head_id or '(none)'}",
        "",
        "History",
        f"  versions: {report.total_versions}",
        f"  prompts: {counts.get('prompt', 0)}",
        f"  human edits: {counts.get('human', 0)}",
        f"  checkpoints: {counts.get('checkpoint', 0)}",
        f"  anchors: {counts.get('anchor', 0)}",
        "  captured Git branches: "
        f"{len(report.branch_names)} ({_items(report.branch_names)})",
        f"  branch points: {len(report.branch_points)} "
        f"({_items(report.branch_points)})",
        f"  tips: {len(report.tips)} ({_items(report.tips)})",
        "",
        "Tests (finalized versions)",
        f"  passed: {len(report.tests.passed)} ({_items(report.tests.passed)})",
        f"  failed: {len(report.tests.failed)} ({_items(report.tests.failed)})",
        f"  errored: {len(report.tests.errored)} ({_items(report.tests.errored)})",
        f"  untested: {len(report.tests.untested)} ({_items(report.tests.untested)})",
        "",
        "High-churn files (touched by multiple versions)",
    ]

    repeated = [item for item in report.churn if item.touches > 1]
    if repeated:
        for item in repeated[:churn_limit]:
            lines.append(
                f"  {item.path}: {item.touches} versions "
                f"({_items(item.version_ids)}), +{item.additions}/-{item.deletions}"
            )
        if len(repeated) > churn_limit:
            lines.append(f"  ... and {len(repeated) - churn_limit} more")
    else:
        lines.append("  (none yet)")

    lines.extend(["", "Attention"])
    attention_count = 0
    for issue in report.test_issues:
        command = f": {issue.command}" if issue.command else ""
        lines.append(f"  {issue.version_id}: tests {issue.status}{command}")
        attention_count += 1
    for version_id in report.uncertain_versions:
        lines.append(f"  {version_id}: attribution confidence is not high")
        attention_count += 1
    for version_id in report.dangling_turns:
        lines.append(f"  {version_id}: dangling prompt turn")
        attention_count += 1
    for version_id in report.side_path_tips:
        lines.append(f"  {version_id}: unreferenced side-path tip")
        attention_count += 1
    if attention_count == 0:
        lines.append("  (none)")

    suggestions = _suggestions(report)
    lines.extend(["", "Suggested inspection"])
    lines.extend(f"  {suggestion}" for suggestion in suggestions)
    if not suggestions:
        lines.append("  (nothing currently needs attention)")
    return "\n".join(lines)


def _file_churn(
    repo_root: Path, versions: list[store.Version]
) -> tuple[FileChurn, ...]:
    aggregate: dict[str, dict[str, object]] = {}
    for version in versions:
        diff = store.read_object(repo_root, version.diff_ref) if version.diff_ref else ""
        for path, (additions, deletions) in _diff_stats(diff or "").items():
            entry = aggregate.setdefault(
                path, {"versions": [], "additions": 0, "deletions": 0}
            )
            version_ids = entry["versions"]
            assert isinstance(version_ids, list)
            version_ids.append(version.id)
            entry["additions"] = int(entry["additions"]) + additions
            entry["deletions"] = int(entry["deletions"]) + deletions

    result = [
        FileChurn(
            path=path,
            version_ids=tuple(values["versions"]),
            additions=int(values["additions"]),
            deletions=int(values["deletions"]),
        )
        for path, values in aggregate.items()
    ]
    return tuple(
        sorted(
            result,
            key=lambda item: (
                -item.touches,
                -(item.additions + item.deletions),
                item.path,
            ),
        )
    )


def _diff_stats(diff_text: str) -> dict[str, tuple[int, int]]:
    stats: dict[str, list[int]] = {}
    current_path: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current_path = _diff_path(line)
            if current_path:
                stats.setdefault(current_path, [0, 0])
        elif current_path and line.startswith("+") and not line.startswith("+++"):
            stats[current_path][0] += 1
        elif current_path and line.startswith("-") and not line.startswith("---"):
            stats[current_path][1] += 1
    return {path: (values[0], values[1]) for path, values in stats.items()}


def _diff_path(header: str) -> str | None:
    try:
        parts = shlex.split(header)
    except ValueError:
        parts = []
    if len(parts) == 4 and parts[3].startswith("b/"):
        return parts[3][2:]
    if " b/" in header:
        return header.rsplit(" b/", 1)[1].strip().strip('"')
    return None


def _bisect_candidates(
    versions: list[store.Version],
    tests: dict[str, store.TestResult],
    version_by_id: dict[str, store.Version],
) -> tuple[BisectCandidate, ...]:
    candidates: list[BisectCandidate] = []
    for bad in versions:
        bad_result = tests.get(bad.id)
        if (
            bad.tree_after is None
            or bad_result is None
            or bad_result.status != "fail"
            or not bad_result.command
        ):
            continue
        current_id = bad.parent_id
        seen: set[str] = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            candidate = version_by_id.get(current_id)
            if candidate is None:
                break
            candidate_result = tests.get(candidate.id)
            if (
                candidate.tree_after is not None
                and candidate_result is not None
                and candidate_result.status == "pass"
                and candidate_result.command == bad_result.command
            ):
                candidates.append(
                    BisectCandidate(candidate.id, bad.id, bad_result.command)
                )
                break
            current_id = candidate.parent_id
    return tuple(candidates)


def _blame_path(
    repo_root: Path,
    head_id: str | None,
    version_by_id: dict[str, store.Version],
    churn: tuple[FileChurn, ...],
) -> str | None:
    head = version_by_id.get(head_id) if head_id else None
    if head is None:
        return None
    head_tree = head.tree_after or head.tree_before
    for item in churn:
        if item.touches > 1 and git_ops.path_in_tree(repo_root, head_tree, item.path):
            return item.path
    return None


def _suggestions(report: HealthReport) -> list[str]:
    attention_ids = list(
        dict.fromkeys(
            [issue.version_id for issue in report.test_issues]
            + list(report.uncertain_versions)
            + list(report.dangling_turns)
            + list(report.side_path_tips)
        )
    )
    suggestions = [f"profact show {version_id}" for version_id in attention_ids[:3]]
    if report.blame_path:
        suggestions.append(f"profact blame {_quote(report.blame_path)}")
    for candidate in report.bisect_candidates[:2]:
        suggestions.append(
            f"profact bisect --good {candidate.good_id} --bad {candidate.bad_id} "
            "-- <same test command>"
        )
    if report.dangling_turns:
        suggestions.append("profact doctor")
    return suggestions


def _items(values: tuple[str, ...], *, limit: int = 8) -> str:
    if not values:
        return "-"
    shown = ", ".join(values[:limit])
    remaining = len(values) - limit
    return f"{shown}, +{remaining} more" if remaining > 0 else shown


def _quote(value: str) -> str:
    return f'"{value}"' if any(character.isspace() for character in value) else value