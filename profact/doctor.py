"""Audit and safely repair profact history artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import git_ops, log_view, store


@dataclass(frozen=True)
class DoctorReport:
    phantom_checkpoints: list[store.Version]
    dangling_open_turns: list[store.Version]
    orphaned_objects: list[Path]
    dangling_tags: list[store.Tag]

    @property
    def has_safe_repairs(self) -> bool:
        return bool(
            self.phantom_checkpoints
            or self.orphaned_objects
            or self.dangling_tags
        )


@dataclass(frozen=True)
class RepairResult:
    removed_versions: int
    removed_objects: int
    removed_tags: int


def audit(repo_root: Path) -> DoctorReport:
    """Inspect history without changing the repository."""
    with store.connect(repo_root) as conn:
        versions = store.all_versions(conn)
        phantom = [
            version
            for version in versions
            if _is_phantom_checkpoint(repo_root, version)
        ]
        dangling = store.dangling_open_turns(conn)
        dangling_tags = store.dangling_tags(conn)
        referenced = store.referenced_object_ids(conn)

    return DoctorReport(
        phantom_checkpoints=phantom,
        dangling_open_turns=dangling,
        orphaned_objects=_orphan_objects(repo_root, referenced),
        dangling_tags=dangling_tags,
    )


def repair(repo_root: Path, report: DoctorReport) -> RepairResult:
    """Apply only the conservative repairs identified by ``audit``."""
    removed_versions = 0
    with store.connect(repo_root) as conn:
        for version in report.phantom_checkpoints:
            # Keep the graph connected and move references off the deleted row.
            store.reparent_children(conn, version.id, version.parent_id)
            for branch in store.heads_pointing_to(conn, version.id):
                if version.parent_id:
                    store.write_head(repo_root, branch, version.parent_id)
            store.retarget_tags(conn, version.id, version.parent_id)
            store.delete_version(conn, version.id)
            removed_versions += 1

        removed_tags = 0
        for item in store.dangling_tags(conn):
            if store.delete_tag(conn, item.name):
                removed_tags += 1
        referenced = store.referenced_object_ids(conn)

    store.clear_global_head_if_missing(repo_root)

    removed_objects = 0
    for path in _orphan_objects(repo_root, referenced):
        path.unlink()
        removed_objects += 1

    return RepairResult(
        removed_versions=removed_versions,
        removed_objects=removed_objects,
        removed_tags=removed_tags,
    )


def _is_phantom_checkpoint(repo_root: Path, version: store.Version) -> bool:
    """Return whether a checkpoint contains no real user work."""
    if version.type != "checkpoint":
        return False
    diff = store.read_object(repo_root, version.diff_ref) if version.diff_ref else ""
    paths = log_view.diff_paths(diff or "")
    return all(git_ops.is_infra_path(path) for path in paths)


def _orphan_objects(repo_root: Path, referenced: set[str]) -> list[Path]:
    objects_dir = store.objects_dir(repo_root)
    if not objects_dir.exists():
        return []
    return [
        path
        for path in objects_dir.iterdir()
        if path.is_file() and path.name not in referenced
    ]
