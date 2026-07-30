"""Audit and explicitly redact secrets already stored in ProFact objects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config, privacy, store


@dataclass(frozen=True)
class StoredFinding:
    version_id: str
    section: str
    rule: str
    line: int
    masked_excerpt: str


@dataclass(frozen=True)
class PrivacyReport:
    findings: tuple[StoredFinding, ...]
    scanned_objects: int
    updated_references: int
    replaced_objects: int
    removed_unsafe_objects: int


@dataclass(frozen=True)
class _Reference:
    table: str
    row_id: str
    column: str
    section: str
    digest: str


def scan(repo_root: Path, *, fix: bool = False) -> PrivacyReport:
    """Scan referenced text objects, optionally replacing unsafe references.

    Findings contain masked excerpts only. ``fix`` writes new redacted objects
    and updates references transactionally; old blobs are left for the existing
    doctor orphan cleanup rather than deleted during the privacy operation.
    """
    config.load(repo_root, strict=True)
    conn = store.connect(repo_root)
    try:
        references = _references(conn)
        references_by_digest: dict[str, list[_Reference]] = {}
        for reference in references:
            references_by_digest.setdefault(reference.digest, []).append(reference)
        cache: dict[str, tuple[list[privacy.Finding], str | None]] = {}
        findings: list[StoredFinding] = []

        object_paths = [
            path for path in store.objects_dir(repo_root).iterdir() if path.is_file()
        ]
        for path in object_paths:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                cache[path.name] = ([], None)
                continue
            detected = privacy.scan_for(repo_root, text, strict_config=True)
            redacted_text, _ = privacy.redact_for(
                repo_root, text, strict_config=True
            )
            object_references = references_by_digest.get(path.name, [])
            new_digest = None
            if fix and object_references and redacted_text != text:
                new_digest = store.write_object(repo_root, redacted_text)
            cache[path.name] = (detected, new_digest)

            if object_references:
                for reference in object_references:
                    findings.extend(
                        StoredFinding(
                            version_id=reference.row_id,
                            section=reference.section,
                            rule=finding.rule,
                            line=finding.line,
                            masked_excerpt=finding.masked_excerpt,
                        )
                        for finding in detected
                    )
            else:
                findings.extend(
                    StoredFinding(
                        version_id="-",
                        section="orphan-object",
                        rule=finding.rule,
                        line=finding.line,
                        masked_excerpt=finding.masked_excerpt,
                    )
                    for finding in detected
                )

        updated_references = 0
        replaced_objects: set[str] = set()
        if fix:
            for reference in references:
                new_digest = cache[reference.digest][1]
                if new_digest and new_digest != reference.digest:
                    id_column = "id" if reference.table == "versions" else "version_id"
                    cursor = conn.execute(
                        f"UPDATE {reference.table} SET {reference.column} = ? "
                        f"WHERE {id_column} = ? AND {reference.column} = ?",
                        (new_digest, reference.row_id, reference.digest),
                    )
                    updated_references += cursor.rowcount
                    replaced_objects.add(reference.digest)
            conn.commit()

        removed_unsafe_objects = 0
        if fix:
            referenced_after = store.referenced_object_ids(conn)
            for digest, (detected, _new_digest) in cache.items():
                if not detected or digest in referenced_after:
                    continue
                path = store.objects_dir(repo_root) / digest
                if path.exists():
                    path.unlink()
                    removed_unsafe_objects += 1

        return PrivacyReport(
            findings=tuple(findings),
            scanned_objects=len(cache),
            updated_references=updated_references,
            replaced_objects=len(replaced_objects),
            removed_unsafe_objects=removed_unsafe_objects,
        )
    finally:
        conn.close()


def _references(conn) -> list[_Reference]:
    references: list[_Reference] = []
    for row in conn.execute(
        "SELECT id, prompt_ref, thinking_ref, response_ref, diff_ref FROM versions"
    ):
        for column, section in (
            ("prompt_ref", "prompt"),
            ("thinking_ref", "thinking"),
            ("response_ref", "response"),
            ("diff_ref", "diff"),
        ):
            if row[column]:
                references.append(
                    _Reference("versions", row["id"], column, section, row[column])
                )
    for row in conn.execute("SELECT version_id, output_ref FROM test_results"):
        if row["output_ref"]:
            references.append(
                _Reference(
                    "test_results",
                    row["version_id"],
                    "output_ref",
                    "test-output",
                    row["output_ref"],
                )
            )
    return references