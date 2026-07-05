from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROFACT_DIR = ".profact"
DB_NAME = "history.db"
HEAD_NAME = "HEAD"
OBJECTS_DIR = "objects"
TMP_INDEX = "tmp_index"

VERSION_TYPES = ("prompt", "anchor", "checkpoint", "human")

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id              TEXT PRIMARY KEY,
    parent_id       TEXT,
    type            TEXT NOT NULL DEFAULT 'prompt',
    git_branch      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    prompt_ref      TEXT,
    response_ref    TEXT,
    model           TEXT,
    temperature     REAL,
    diff_ref        TEXT,
    tree_before     TEXT NOT NULL,
    tree_after      TEXT,
    commit_hash     TEXT,
    confidence      TEXT DEFAULT 'high',
    generation_id   TEXT,
    conversation_id TEXT
);
"""

# Columns added after the initial schema; applied to existing DBs on connect.
_MIGRATION_COLUMNS = (("generation_id", "TEXT"), ("conversation_id", "TEXT"))


@dataclass
class Version:
    id: str
    parent_id: str | None
    type: str
    git_branch: str
    timestamp: str
    prompt_ref: str | None
    response_ref: str | None
    model: str | None
    temperature: float | None
    diff_ref: str | None
    tree_before: str
    tree_after: str | None
    commit_hash: str | None
    confidence: str
    generation_id: str | None = None
    conversation_id: str | None = None


def profact_dir(repo_root: Path) -> Path:
    return repo_root / PROFACT_DIR


def db_path(repo_root: Path) -> Path:
    return profact_dir(repo_root) / DB_NAME


def head_path(repo_root: Path) -> Path:
    return profact_dir(repo_root) / HEAD_NAME


def objects_dir(repo_root: Path) -> Path:
    return profact_dir(repo_root) / OBJECTS_DIR


def tmp_index_path(repo_root: Path) -> Path:
    return profact_dir(repo_root) / TMP_INDEX


def is_initialized(repo_root: Path) -> bool:
    return db_path(repo_root).exists()


def init_db(repo_root: Path) -> None:
    objects_dir(repo_root).mkdir(parents=True, exist_ok=True)
    with connect(repo_root) as conn:
        conn.executescript(SCHEMA)


def connect(repo_root: Path) -> sqlite3.Connection:
    profact_dir(repo_root).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(repo_root))
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotently add columns introduced after the initial schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(versions)")}
    if not existing:  # table not created yet (fresh DB)
        return
    changed = False
    for name, decl in _MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE versions ADD COLUMN {name} {decl}")
            changed = True
    if changed:
        conn.commit()


def write_object(repo_root: Path, text: str) -> str:
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    path = objects_dir(repo_root) / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return digest


def read_object(repo_root: Path, digest: str) -> str | None:
    path = objects_dir(repo_root) / digest
    return path.read_text() if path.exists() else None


def read_head(repo_root: Path) -> str | None:
    path = head_path(repo_root)
    if not path.exists():
        return None
    value = path.read_text().strip()
    return value or None


def write_head(repo_root: Path, version_id: str) -> None:
    head_path(repo_root).write_text(version_id)


def next_version_id(conn: sqlite3.Connection) -> str:
    highest = 0
    for row in conn.execute("SELECT id FROM versions"):
        identifier = row["id"]
        if identifier.startswith("v") and identifier[1:].isdigit():
            highest = max(highest, int(identifier[1:]))
    return f"v{highest + 1}"


def create_version(
    conn: sqlite3.Connection,
    *,
    git_branch: str,
    tree_before: str,
    type: str = "prompt",
    parent_id: str | None = None,
    tree_after: str | None = None,
    prompt_ref: str | None = None,
    response_ref: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    diff_ref: str | None = None,
    commit_hash: str | None = None,
    confidence: str = "high",
    generation_id: str | None = None,
    conversation_id: str | None = None,
) -> Version:
    if type not in VERSION_TYPES:
        raise ValueError(f"unknown version type: {type}")
    version = Version(
        id=next_version_id(conn),
        parent_id=parent_id,
        type=type,
        git_branch=git_branch,
        timestamp=datetime.now(timezone.utc).isoformat(),
        prompt_ref=prompt_ref,
        response_ref=response_ref,
        model=model,
        temperature=temperature,
        diff_ref=diff_ref,
        tree_before=tree_before,
        tree_after=tree_after,
        commit_hash=commit_hash,
        confidence=confidence,
        generation_id=generation_id,
        conversation_id=conversation_id,
    )
    conn.execute(
        """
        INSERT INTO versions (
            id, parent_id, type, git_branch, timestamp, prompt_ref, response_ref,
            model, temperature, diff_ref, tree_before, tree_after, commit_hash,
            confidence, generation_id, conversation_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            version.id,
            version.parent_id,
            version.type,
            version.git_branch,
            version.timestamp,
            version.prompt_ref,
            version.response_ref,
            version.model,
            version.temperature,
            version.diff_ref,
            version.tree_before,
            version.tree_after,
            version.commit_hash,
            version.confidence,
            version.generation_id,
            version.conversation_id,
        ),
    )
    conn.commit()
    return version


def get_version(conn: sqlite3.Connection, version_id: str) -> Version | None:
    row = conn.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    return _row_to_version(row) if row else None


def list_versions(conn: sqlite3.Connection, git_branch: str) -> list[Version]:
    rows = conn.execute(
        "SELECT * FROM versions WHERE git_branch = ? ORDER BY timestamp",
        (git_branch,),
    ).fetchall()
    return [_row_to_version(row) for row in rows]


def _row_to_version(row: sqlite3.Row) -> Version:
    return Version(
        id=row["id"],
        parent_id=row["parent_id"],
        type=row["type"],
        git_branch=row["git_branch"],
        timestamp=row["timestamp"],
        prompt_ref=row["prompt_ref"],
        response_ref=row["response_ref"],
        model=row["model"],
        temperature=row["temperature"],
        diff_ref=row["diff_ref"],
        tree_before=row["tree_before"],
        tree_after=row["tree_after"],
        commit_hash=row["commit_hash"],
        confidence=row["confidence"],
        generation_id=row["generation_id"] if "generation_id" in row.keys() else None,
        conversation_id=row["conversation_id"] if "conversation_id" in row.keys() else None,
    )
