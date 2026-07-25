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

-- One HEAD per Git branch (branch-local HEADs). The legacy .profact/HEAD file
-- is retained as a repo-wide "last active" pointer used only to seed the HEAD of
-- a brand-new branch (carry-over from the branch point; see read_head).
CREATE TABLE IF NOT EXISTS heads (
    git_branch  TEXT PRIMARY KEY,
    version_id  TEXT
);

-- Test outcome per version (behavior-aware history). One row per version; a
-- re-run overwrites the previous result. output_ref points at a content-addressed
-- blob holding the captured stdout+stderr.
CREATE TABLE IF NOT EXISTS test_results (
    version_id  TEXT PRIMARY KEY,
    status      TEXT NOT NULL,       -- 'pass' | 'fail' | 'error'
    exit_code   INTEGER,
    command     TEXT,
    output_ref  TEXT,
    timestamp   TEXT NOT NULL
);
"""

TEST_STATUSES = ("pass", "fail", "error")

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


@dataclass
class TestResult:
    version_id: str
    status: str  # one of TEST_STATUSES
    exit_code: int | None
    command: str | None
    output_ref: str | None
    timestamp: str


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
    """Idempotently upgrade DBs created by earlier profact versions."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(versions)")}
    if not existing:  # table not created yet (fresh DB)
        return
    changed = False
    for name, decl in _MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE versions ADD COLUMN {name} {decl}")
            changed = True
    # Branch-local HEADs were added after the initial schema; ensure the table
    # exists for DBs that predate it. The old global .profact/HEAD is used to
    # seed each branch on first use (see read_head), so this is non-destructive.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS heads (git_branch TEXT PRIMARY KEY, version_id TEXT)"
    )
    # Behavior-aware history (test outcomes) was added after the initial schema.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS test_results ("
        "version_id TEXT PRIMARY KEY, status TEXT NOT NULL, exit_code INTEGER, "
        "command TEXT, output_ref TEXT, timestamp TEXT NOT NULL)"
    )
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


def read_head(repo_root: Path, branch: str, *, seed: bool = True) -> str | None:
    """Return the HEAD version for ``branch``.

    HEADs are branch-local: each Git branch has its own row in the ``heads``
    table. If the branch has no HEAD yet and ``seed`` is True, fall back to the
    repo-wide "last active" pointer (``.profact/HEAD``). This implements the
    carry-over rule: a brand-new branch inherits the version you branched off
    from, so new work on it chains onto that lineage (like Git branching).

    Display commands pass ``seed=False`` so an untouched branch honestly reports
    "no HEAD yet" instead of borrowing another branch's position.
    """
    with connect(repo_root) as conn:
        row = conn.execute(
            "SELECT version_id FROM heads WHERE git_branch = ?", (branch,)
        ).fetchone()
    if row and row["version_id"]:
        return row["version_id"]
    if not seed:
        return None
    return _read_global_head(repo_root)


def write_head(repo_root: Path, branch: str, version_id: str) -> None:
    """Set ``branch``'s HEAD and advance the repo-wide "last active" pointer."""
    with connect(repo_root) as conn:
        conn.execute(
            """
            INSERT INTO heads (git_branch, version_id) VALUES (?, ?)
            ON CONFLICT(git_branch) DO UPDATE SET version_id = excluded.version_id
            """,
            (branch, version_id),
        )
        conn.commit()
    _write_global_head(repo_root, version_id)


def _read_global_head(repo_root: Path) -> str | None:
    path = head_path(repo_root)
    if not path.exists():
        return None
    value = path.read_text().strip()
    return value or None


def _write_global_head(repo_root: Path, version_id: str) -> None:
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


def list_branch_versions(
    conn: sqlite3.Connection, git_branch: str, head: str | None = None
) -> list[Version]:
    """Versions visible on ``git_branch``: those created on it plus their
    ancestors (which may have been created on a parent branch and carried over).

    This is the branch's slice of the shared DAG. Under the carry-over rule, a
    branch's early history lives in rows whose ``git_branch`` is the parent
    branch, so a plain ``git_branch =`` filter would drop it — we walk parents to
    reattach the inherited trunk.

    If ``head`` is given, its ancestry is also included. This keeps the view
    consistent after a confirmed cross-branch checkout, where the current HEAD
    can point at a version that has no descendant created on this branch yet.
    """
    result: dict[str, Version] = {}
    stack: list[str] = []
    for row in conn.execute(
        "SELECT * FROM versions WHERE git_branch = ?", (git_branch,)
    ):
        version = _row_to_version(row)
        result[version.id] = version
        if version.parent_id:
            stack.append(version.parent_id)
    if head:
        stack.append(head)
    while stack:
        parent_id = stack.pop()
        if parent_id in result:
            continue
        version = get_version(conn, parent_id)
        if version is None:
            continue
        result[parent_id] = version
        if version.parent_id:
            stack.append(version.parent_id)
    return sorted(result.values(), key=lambda v: v.timestamp)


def ancestry(conn: sqlite3.Connection, version_id: str) -> list[Version]:
    """Return the lineage of ``version_id`` oldest-first (root .. version).

    Follows ``parent_id`` links and is cycle-safe. This is the linear slice of
    the DAG leading to a version — the exact search space for blame attribution
    and for `bisect` (which needs a known-good ancestor of a known-bad version).
    """
    chain: list[Version] = []
    seen: set[str] = set()
    current: str | None = version_id
    while current and current not in seen:
        seen.add(current)
        version = get_version(conn, current)
        if version is None:
            break
        chain.append(version)
        current = version.parent_id
    chain.reverse()
    return chain


def all_versions(conn: sqlite3.Connection) -> list[Version]:
    rows = conn.execute("SELECT * FROM versions ORDER BY timestamp").fetchall()
    return [_row_to_version(row) for row in rows]


def referenced_object_ids(conn: sqlite3.Connection) -> set[str]:
    """Every object digest still referenced by a version (prompt/response/diff)
    or a test result (captured output)."""
    refs: set[str] = set()
    for row in conn.execute(
        "SELECT prompt_ref, response_ref, diff_ref FROM versions"
    ):
        refs.update(value for value in row if value)
    for row in conn.execute("SELECT output_ref FROM test_results"):
        if row["output_ref"]:
            refs.add(row["output_ref"])
    return refs


def set_test_result(
    conn: sqlite3.Connection,
    version_id: str,
    *,
    status: str,
    exit_code: int | None,
    command: str | None,
    output_ref: str | None,
    timestamp: str | None = None,
) -> TestResult:
    """Record (upserting) the test outcome for ``version_id``."""
    if status not in TEST_STATUSES:
        raise ValueError(f"unknown test status: {status}")
    result = TestResult(
        version_id=version_id,
        status=status,
        exit_code=exit_code,
        command=command,
        output_ref=output_ref,
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
    )
    conn.execute(
        """
        INSERT INTO test_results
            (version_id, status, exit_code, command, output_ref, timestamp)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(version_id) DO UPDATE SET
            status = excluded.status,
            exit_code = excluded.exit_code,
            command = excluded.command,
            output_ref = excluded.output_ref,
            timestamp = excluded.timestamp
        """,
        (
            result.version_id,
            result.status,
            result.exit_code,
            result.command,
            result.output_ref,
            result.timestamp,
        ),
    )
    conn.commit()
    return result


def get_test_result(conn: sqlite3.Connection, version_id: str) -> TestResult | None:
    row = conn.execute(
        "SELECT * FROM test_results WHERE version_id = ?", (version_id,)
    ).fetchone()
    return _row_to_test_result(row) if row else None


def all_test_results(conn: sqlite3.Connection) -> dict[str, TestResult]:
    """Map of version_id -> latest TestResult (for markers and summaries)."""
    return {
        row["version_id"]: _row_to_test_result(row)
        for row in conn.execute("SELECT * FROM test_results")
    }


def _row_to_test_result(row: sqlite3.Row) -> TestResult:
    return TestResult(
        version_id=row["version_id"],
        status=row["status"],
        exit_code=row["exit_code"],
        command=row["command"],
        output_ref=row["output_ref"],
        timestamp=row["timestamp"],
    )


def heads_pointing_to(conn: sqlite3.Connection, version_id: str) -> list[str]:
    return [
        row["git_branch"]
        for row in conn.execute(
            "SELECT git_branch FROM heads WHERE version_id = ?", (version_id,)
        )
    ]


def reparent_children(conn: sqlite3.Connection, old_parent: str, new_parent: str | None) -> None:
    conn.execute(
        "UPDATE versions SET parent_id = ? WHERE parent_id = ?",
        (new_parent, old_parent),
    )
    conn.commit()


def delete_version(conn: sqlite3.Connection, version_id: str) -> None:
    conn.execute("DELETE FROM versions WHERE id = ?", (version_id,))
    conn.commit()


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
