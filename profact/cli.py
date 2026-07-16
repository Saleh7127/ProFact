from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import typer

from . import git_ops, store

app = typer.Typer(help="Prompt-native version control for AI-assisted development.")


def _resolve_repo_root() -> Path:
    try:
        return git_ops.repo_root(Path.cwd())
    except git_ops.GitError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)


def _ensure_gitignore(repo_root: Path, *entries: str) -> None:
    gitignore = repo_root / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    to_add = [e for e in entries if e not in lines]
    if not to_add:
        return
    with gitignore.open("a") as handle:
        if lines and lines[-1].strip():
            handle.write("\n")
        for entry in to_add:
            handle.write(f"{entry}\n")


@app.command()
def init() -> None:
    """Initialize profact in the current repository."""
    repo_root = _resolve_repo_root()
    if store.is_initialized(repo_root):
        typer.echo("profact is already initialized")
        return

    store.init_db(repo_root)
    _ensure_gitignore(repo_root, f"{store.PROFACT_DIR}/")
    branch = git_ops.current_branch(repo_root)

    with store.connect(repo_root) as conn:
        if git_ops.has_commits(repo_root):
            tree = git_ops.write_tree(repo_root)
            commit = git_ops.head_commit(repo_root)
            anchor = store.create_version(
                conn,
                type="anchor",
                git_branch=branch,
                tree_before=tree,
                tree_after=tree,
                commit_hash=commit,
            )
            store.write_head(repo_root, branch, anchor.id)
            typer.echo(
                f"Initialized profact: anchor {anchor.id} at {commit[:8]} on '{branch}'"
            )
        else:
            typer.echo(
                f"Initialized profact on '{branch}' (no commits yet; "
                "history starts at the first version)"
            )


@app.command()
def start(
    claude: bool = typer.Option(
        False, "--claude", help="Capture Claude Code via the local proxy and launch `claude`."
    ),
    port: int = typer.Option(7845, "--port", help="Proxy port (Claude mode only)."),
) -> None:
    """Start capture. Default installs Cursor hooks; --claude runs the Claude proxy."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)

    if claude:
        from . import session

        code = session.run_claude(repo_root, port=port)
        raise typer.Exit(code)

    script_rel = _install_cursor_hooks(repo_root)
    typer.echo("profact capture is wired into Cursor for this repository.")
    typer.echo(f"  hooks config: {repo_root / '.cursor' / 'hooks.json'}")
    typer.echo(f"  hook runner:  {repo_root / script_rel}")
    typer.echo("Reload Cursor (or restart it) so it picks up the hooks, then just")
    typer.echo("chat as usual — each prompt becomes a profact version. Use `profact log`.")


@app.command()
def note(
    commit: str = typer.Argument(..., help="The Git commit to annotate."),
    version_id: str = typer.Argument(None, help="Version id (defaults to HEAD)."),
) -> None:
    """Attach a profact version id to a Git commit as a note (optional; §10)."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)
    target = version_id or store.read_head(repo_root, branch, seed=False)
    if not target:
        typer.echo("error: no version to link (HEAD is empty)", err=True)
        raise typer.Exit(1)
    with store.connect(repo_root) as conn:
        if store.get_version(conn, target) is None:
            typer.echo(f"error: no such version '{target}'", err=True)
            raise typer.Exit(1)
    try:
        git_ops.add_note(repo_root, commit, json.dumps({"version_id": target}))
    except git_ops.GitError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"linked {target} to commit {commit[:8]} (git notes --ref=profact)")


@app.command(hidden=True)
def hook() -> None:
    """Internal: consume one Cursor hook event from stdin. Always fails open."""
    from .adapters import cursor_hooks

    response: dict = {}
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        roots = event.get("workspace_roots") or []
        start_dir = Path(roots[0]) if roots else Path.cwd()
        repo_root = git_ops.repo_root(start_dir)
        response = cursor_hooks.handle(repo_root, event)
    except Exception:
        response = {}
    typer.echo(json.dumps(response))
    raise typer.Exit(0)


@app.command()
def log(
    graph: bool = typer.Option(False, "--graph", help="Render the version DAG with branch tips."),
    path: bool = typer.Option(False, "--path", help="Show only HEAD's ancestry (how you got here)."),
) -> None:
    """List versions on the current Git branch (branch-scoped, newest first)."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)
    head = store.read_head(repo_root, branch, seed=False)

    with store.connect(repo_root) as conn:
        if path:
            versions = _ancestry(conn, head)
        else:
            versions = store.list_branch_versions(conn, branch, head=head)

    if not versions:
        typer.echo(f"No versions yet on '{branch}'.")
        return

    versions = sorted(versions, key=lambda v: v.timestamp, reverse=True)

    if graph:
        _print_graph(repo_root, versions, head, branch)
    else:
        for version in versions:
            typer.echo(_row_text(repo_root, version, head))


@app.command()
def show(version_id: str) -> None:
    """Show the prompt, response, and diff for a single version."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    with store.connect(repo_root) as conn:
        version = store.get_version(conn, version_id)
    if version is None:
        typer.echo(f"error: no such version '{version_id}'", err=True)
        raise typer.Exit(1)

    branch = git_ops.current_branch(repo_root)
    head = store.read_head(repo_root, branch, seed=False)
    is_head = " (HEAD)" if version.id == head else ""
    typer.echo(f"version {version.id}{is_head}  [{version.type}]")
    typer.echo(f"  branch:   {version.git_branch}")
    typer.echo(f"  parent:   {version.parent_id or '-'}")
    typer.echo(f"  time:     {version.timestamp}")
    if version.model:
        temp = f" @ temp={version.temperature}" if version.temperature is not None else ""
        typer.echo(f"  model:    {version.model}{temp}")
    if version.commit_hash:
        typer.echo(f"  commit:   {version.commit_hash[:8]}")
    if version.confidence and version.confidence != "high":
        typer.echo(
            f"  warning:  confidence is '{version.confidence}' — "
            "files changed concurrently with this turn; attribution may be imprecise"
        )

    prompt = store.read_object(repo_root, version.prompt_ref) if version.prompt_ref else None
    if prompt:
        typer.echo("\nprompt:")
        typer.echo(_indent(prompt))

    response = store.read_object(repo_root, version.response_ref) if version.response_ref else None
    if response:
        typer.echo("\nresponse:")
        typer.echo(_indent(response))

    diff = store.read_object(repo_root, version.diff_ref) if version.diff_ref else None
    typer.echo("\ndiff:")
    if diff:
        typer.echo(diff)
    else:
        typer.echo("  (no file changes)")


@app.command()
def checkout(
    version_id: str,
    force: bool = typer.Option(
        False, "--force", "-f", help="Skip the cross-branch confirmation prompt."
    ),
) -> None:
    """Restore the working directory to a version (auto-checkpoints first; §8)."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)

    with store.connect(repo_root) as conn:
        target = store.get_version(conn, version_id)
        if target is None:
            typer.echo(f"error: no such version '{version_id}'", err=True)
            raise typer.Exit(1)
        if target.tree_after is None:
            typer.echo(
                f"error: '{version_id}' has no finalized snapshot to restore", err=True
            )
            raise typer.Exit(1)

        head_id = store.read_head(repo_root, branch)

        # A version is part of this branch's history if it was created here or is
        # reachable as a carried-over ancestor (of the branch or of HEAD). If not,
        # it lives on another branch's line — offer to bring it over rather than
        # hard-refusing, so the user stays in control.
        reachable = {v.id for v in store.list_branch_versions(conn, branch, head=head_id)}
        if target.id not in reachable:
            typer.echo(
                f"note: '{version_id}' is not part of branch '{branch}' history "
                f"(it was created on '{target.git_branch}' and isn't an ancestor "
                f"of this branch)."
            )
            if not force and not typer.confirm(
                f"Check it out into '{branch}' anyway?", default=False
            ):
                typer.echo("aborted; no changes made")
                raise typer.Exit(1)

        head_version = store.get_version(conn, head_id) if head_id else None
        current_tree = git_ops.write_tree(repo_root)

        if head_version is not None:
            head_tree = head_version.tree_after or head_version.tree_before
            if current_tree != head_tree:
                diff = git_ops.diff_trees(repo_root, head_tree, current_tree)
                diff_ref = store.write_object(repo_root, diff) if diff.strip() else None
                checkpoint = store.create_version(
                    conn,
                    type="checkpoint",
                    git_branch=branch,
                    parent_id=head_id,
                    tree_before=head_tree,
                    tree_after=current_tree,
                    diff_ref=diff_ref,
                )
                typer.echo(
                    f"saved uncommitted work as checkpoint {checkpoint.id} "
                    f"(restore it with `profact checkout {checkpoint.id}`)"
                )

    git_ops.checkout_tree(repo_root, target.tree_after, prev_tree=current_tree)
    store.write_head(repo_root, branch, target.id)
    typer.echo(f"HEAD is now {target.id} [{target.type}]")


@app.command()
def status() -> None:
    """Show current HEAD, branch, and whether the working tree is dirty."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)
    head_id = store.read_head(repo_root, branch, seed=False)

    typer.echo(f"branch:  {branch}")
    if head_id is None:
        typer.echo("HEAD:    (none yet)")
        return

    with store.connect(repo_root) as conn:
        head = store.get_version(conn, head_id)
        count = len(store.list_branch_versions(conn, branch, head=head_id))

    if head is None:
        typer.echo(f"HEAD:    {head_id} (missing from store)")
        return

    typer.echo(f"HEAD:    {head.id} [{head.type}] {_summary(repo_root, head)}")
    typer.echo(f"versions on branch: {count}")

    head_tree = head.tree_after or head.tree_before
    current_tree = git_ops.write_tree(repo_root)
    if current_tree == head_tree:
        typer.echo("working tree: clean (matches HEAD)")
    else:
        typer.echo("working tree: dirty (differs from HEAD — uncommitted changes present)")


@app.command()
def doctor(
    fix: bool = typer.Option(
        False, "--fix", help="Apply safe repairs (prune artifacts). Default is report-only."
    ),
) -> None:
    """Report (and with --fix, prune) history artifacts: phantom checkpoints,
    dangling open turns, and orphaned objects."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)

    with store.connect(repo_root) as conn:
        versions = store.all_versions(conn)
        phantom = [v for v in versions if _is_phantom_checkpoint(repo_root, v)]
        dangling = [
            v for v in versions if v.type == "prompt" and v.tree_after is None
        ]
        referenced = store.referenced_object_ids(conn)
    orphans = _orphan_objects(repo_root, referenced)

    typer.echo(f"phantom checkpoints:   {len(phantom)}")
    for v in phantom:
        typer.echo(f"  {v.id}  (parent {v.parent_id or '-'})")
    typer.echo(f"dangling open turns:   {len(dangling)}")
    for v in dangling:
        typer.echo(f"  {v.id}  {_summary(repo_root, v)}")
    typer.echo(f"orphaned objects:      {len(orphans)}")

    if not fix:
        if phantom or orphans:
            typer.echo("\nrun `profact doctor --fix` to prune the safe-to-remove items above")
        else:
            typer.echo("\nnothing to repair.")
        return

    removed_versions = 0
    with store.connect(repo_root) as conn:
        for v in phantom:
            # Rewire any children onto the phantom's parent so no chain breaks,
            # then move any branch HEAD off it, then delete the row.
            store.reparent_children(conn, v.id, v.parent_id)
            for br in store.heads_pointing_to(conn, v.id):
                if v.parent_id:
                    store.write_head(repo_root, br, v.parent_id)
            store.delete_version(conn, v.id)
            removed_versions += 1
        referenced = store.referenced_object_ids(conn)

    # If the repo-wide pointer still names a deleted version, repoint it.
    global_head = store._read_global_head(repo_root)
    if global_head:
        with store.connect(repo_root) as conn:
            if store.get_version(conn, global_head) is None:
                store._write_global_head(repo_root, "")

    removed_objects = 0
    for path in _orphan_objects(repo_root, referenced):
        path.unlink()
        removed_objects += 1

    typer.echo(
        f"\nremoved {removed_versions} phantom checkpoint(s) and "
        f"{removed_objects} orphaned object(s)."
    )
    if dangling:
        typer.echo(
            f"left {len(dangling)} dangling open turn(s) untouched (real work; "
            "they finalize on the next prompt)."
        )


def _is_phantom_checkpoint(repo_root: Path, version: store.Version) -> bool:
    """A checkpoint that recorded no real work — empty, or touching only profact's
    own infrastructure (an artifact of the pre-fix snapshot bug)."""
    if version.type != "checkpoint":
        return False
    diff = store.read_object(repo_root, version.diff_ref) if version.diff_ref else ""
    paths = _diff_paths(diff or "")
    return all(git_ops.is_infra_path(p) for p in paths)


def _diff_paths(diff_text: str) -> set[str]:
    paths: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git ") and " b/" in line:
            paths.add(line.split(" b/", 1)[1].strip())
    return paths


def _orphan_objects(repo_root: Path, referenced: set[str]) -> list[Path]:
    objects_dir = store.objects_dir(repo_root)
    if not objects_dir.exists():
        return []
    return [
        p for p in objects_dir.iterdir() if p.is_file() and p.name not in referenced
    ]


def _install_cursor_hooks(repo_root: Path) -> str:
    """Write the hook runner script and merge profact entries into .cursor/hooks.json.

    Returns the project-relative path of the runner script. Existing, unrelated
    hooks are preserved; profact entries are added idempotently.
    """
    from .adapters import cursor_hooks

    cursor_dir = repo_root / ".cursor"
    hooks_dir = cursor_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # The hook files are machine-specific (interpreter path + PYTHONPATH) and are
    # profact's own capture machinery, so keep them out of the versioned tree.
    # Otherwise `checkout` to a version created before `start` would delete them
    # and silently disable capture.
    _ensure_gitignore(repo_root, ".cursor/hooks.json", ".cursor/hooks/")

    script_rel = ".cursor/hooks/profact-hook.sh"
    script_path = repo_root / script_rel
    pkg_parent = Path(__file__).resolve().parent.parent
    script = (
        "#!/usr/bin/env bash\n"
        "# Auto-generated by `profact start`. Forwards Cursor hook events to profact.\n"
        f'export PYTHONPATH="{pkg_parent}:$PYTHONPATH"\n'
        f'exec "{sys.executable}" -m profact.cli hook\n'
    )
    script_path.write_text(script)
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    config_path = cursor_dir / "hooks.json"
    config: dict = {"version": 1, "hooks": {}}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text())
            if isinstance(loaded, dict):
                config = loaded
        except json.JSONDecodeError:
            pass
    config.setdefault("version", 1)
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = config["hooks"] = {}

    for event in cursor_hooks.HANDLED_EVENTS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            entries = hooks[event] = []
        already = any(
            isinstance(e, dict) and e.get("command") == script_rel for e in entries
        )
        if not already:
            entries.append({"command": script_rel})

    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return script_rel


def _require_init(repo_root: Path) -> None:
    if not store.is_initialized(repo_root):
        typer.echo("error: profact is not initialized; run `profact init` first", err=True)
        raise typer.Exit(1)


def _ancestry(conn, head_id: str | None) -> list[store.Version]:
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


def _summary(repo_root: Path, version: store.Version) -> str:
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


def _row_text(repo_root: Path, version: store.Version, head: str | None) -> str:
    marker = "HEAD " if version.id == head else "     "
    branch = f"({version.git_branch})"
    return (
        f"{version.id:<5} {marker}[{version.type:<10}] "
        f"{branch:<14} {_summary(repo_root, version)}"
    )


def _print_graph(
    repo_root: Path, versions: list[store.Version], head: str | None, branch: str
) -> None:
    """Render the version DAG as an ASCII graph.

    ``versions`` is newest-first. Because the history is a tree (each version has
    one parent, but may have several children), a parent always sorts after all
    its children, so when we reach a version its lane may already be claimed by a
    sibling that shares the same parent — that is how forks and their merges get
    drawn with ``*`` (a node), ``|`` (a lane passing through) and ``/`` (a lane
    converging left onto a shared parent).
    """
    ids = {v.id for v in versions}
    child_count: dict[str, int] = {}
    for version in versions:
        if version.parent_id in ids:
            child_count[version.parent_id] = child_count.get(version.parent_id, 0) + 1

    head_label = f"  (HEAD = {head})" if head else ""
    typer.echo(f"DAG for branch '{branch}'{head_label}\n")

    lanes: list[str | None] = []  # each lane holds the version id it next expects
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
            "*" if i == col else ("|" if lane is not None else " ")
            for i, lane in enumerate(lanes)
        )
        typer.echo(f"{node:<{_GRAPH_GUTTER}} {_row_text(repo_root, version, head)}")

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
                "/" if i == drop else ("|" if lanes[i] is not None or i == keep else " ")
                for i in range(len(lanes))
            )
            typer.echo(connector)

        while lanes and lanes[-1] is None:
            lanes.pop()

    tips = [
        v.id + (" (HEAD)" if v.id == head else "")
        for v in versions
        if child_count.get(v.id, 0) == 0
    ]
    branch_points = [v.id for v in versions if child_count.get(v.id, 0) > 1]
    typer.echo("")
    typer.echo(f"tips: {', '.join(tips) if tips else '-'}")
    typer.echo(f"branch points: {', '.join(branch_points) if branch_points else '-'}")


# Width reserved for the ASCII graph gutter so version rows line up.
_GRAPH_GUTTER = 8


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _short(text: str, length: int = 72) -> str:
    text = text.strip()
    return text if len(text) <= length else text[: length - 1] + "…"


if __name__ == "__main__":
    app()
