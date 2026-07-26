from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

import typer

from . import git_ops, log_view, store, testrunner

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
        resolved_target = _resolve_version_token(repo_root, conn, branch, target)
        if resolved_target is None:
            typer.echo(f"error: no such version or tag '{target}'", err=True)
            raise typer.Exit(1)
        target = resolved_target
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
        # Probe: set PROFACT_HOOK_DEBUG=1 to record every raw hook payload to
        # .profact/hook-events.log. Use it to confirm exactly what your Cursor
        # version delivers (e.g. the afterAgentThought shape) without affecting
        # capture. Best-effort and never fatal.
        if os.environ.get("PROFACT_HOOK_DEBUG"):
            try:
                log_path = store.profact_dir(repo_root) / "hook-events.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event) + "\n")
            except Exception:
                pass
        response = cursor_hooks.handle(repo_root, event)
    except Exception:
        response = {}
    typer.echo(json.dumps(response))
    raise typer.Exit(0)


@app.command()
def log(
    graph: bool = typer.Option(False, "--graph", help="Render the version DAG with branch tips."),
    path: bool = typer.Option(False, "--path", help="Show only HEAD's ancestry (how you got here)."),
    tests: bool = typer.Option(
        False,
        "--tests",
        help="Show explicit test status/details and an outcome summary.",
    ),
    tag_filter: str = typer.Option(None, "--tag", help="Show versions carrying this tag."),
    type_filter: str = typer.Option(
        None, "--type", help="Filter by version type: prompt, anchor, checkpoint, or human."
    ),
    branch_filter: str = typer.Option(
        None, "--branch", help="Inspect another Git branch's profact history."
    ),
    model_filter: str = typer.Option(
        None, "--model", help="Filter by case-insensitive model-name substring."
    ),
    file_filter: str = typer.Option(
        None,
        "--file",
        "--changed-path",
        help="Filter versions whose diff touched this repository-relative path.",
    ),
    since: str = typer.Option(
        None, "--since", help="Filter by age (for example 7d, 12h, 2w) or ISO date."
    ),
    test_status: str = typer.Option(
        None,
        "--test-status",
        help="Filter by pass, fail, error, or untested; also enables --tests display.",
    ),
) -> None:
    """List and filter branch-scoped versions, newest first."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    current_branch = git_ops.current_branch(repo_root)
    branch = branch_filter or current_branch
    head = store.read_head(repo_root, branch, seed=False)

    type_filter = type_filter.lower() if type_filter else None
    if type_filter and type_filter not in store.VERSION_TYPES:
        typer.echo(
            f"error: unknown version type '{type_filter}'; "
            f"choose from {', '.join(store.VERSION_TYPES)}",
            err=True,
        )
        raise typer.Exit(2)
    normalized_test_status = log_view.normalize_test_filter(test_status)
    if test_status and normalized_test_status is None:
        typer.echo(
            f"error: unknown test status '{test_status}'; "
            "choose pass, fail, error, or untested",
            err=True,
        )
        raise typer.Exit(2)
    try:
        since_cutoff = log_view.parse_since(since) if since else None
        normalized_file = (
            log_view.normalize_path(repo_root, file_filter) if file_filter else None
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2)
    show_tests = tests or normalized_test_status is not None

    with store.connect(repo_root) as conn:
        if path:
            base_versions = log_view.ancestry(conn, head)
            visible = store.list_branch_versions(conn, branch, head=head)
            omitted = max(0, len(visible) - len(base_versions))
        else:
            base_versions = store.list_branch_versions(conn, branch, head=head)
            omitted = 0
        tests_map = store.all_test_results(conn) if show_tests else None
        tags_map = store.tags_by_version(conn)
        if tag_filter and store.get_tag(conn, tag_filter) is None:
            typer.echo(f"error: no such tag '{tag_filter}'", err=True)
            raise typer.Exit(1)

    if not base_versions:
        typer.echo(f"No versions yet on '{branch}'.")
        return

    base_versions = sorted(
        base_versions, key=lambda version: version.timestamp, reverse=True
    )
    versions = log_view.filter_versions(
        repo_root,
        base_versions,
        tags=tags_map,
        tests=tests_map or {},
        tag=tag_filter,
        version_type=type_filter,
        model=model_filter,
        changed_path=normalized_file,
        since_cutoff=since_cutoff,
        test_status=normalized_test_status,
    )
    filtered_out = len(base_versions) - len(versions)
    filter_description = log_view.filter_description(
        branch_filter=branch_filter,
        tag=tag_filter,
        version_type=type_filter,
        model=model_filter,
        changed_path=normalized_file,
        since=since,
        test_status=normalized_test_status,
    )
    if not versions:
        if filter_description:
            typer.echo(f"filters: {' · '.join(filter_description)}\n")
        typer.echo(f"No versions match the requested filters on '{branch}'.")
        return

    log_view.render(
        repo_root,
        versions,
        shape_versions=base_versions,
        head=head,
        branch=branch,
        tests=tests_map if show_tests else None,
        tags=tags_map,
        graph=graph,
        path_mode=path,
        omitted=omitted,
        filtered_out=filtered_out,
        filters=filter_description,
    )


@app.command()
def show(version_id: str) -> None:
    """Show the prompt, response, and diff for a single version."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)
    with store.connect(repo_root) as conn:
        resolved_id = _resolve_version_token(repo_root, conn, branch, version_id)
        version = store.get_version(conn, resolved_id) if resolved_id else None
        test_result = store.get_test_result(conn, resolved_id) if resolved_id else None
        tags = store.tags_by_version(conn).get(resolved_id, []) if resolved_id else []
    if version is None:
        typer.echo(f"error: no such version or tag '{version_id}'", err=True)
        raise typer.Exit(1)

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
    if tags:
        typer.echo(f"  tags:     {', '.join(tags)}")
    if test_result:
        typer.echo(f"  test:     {testrunner.format_result(test_result)}")
    if version.confidence and version.confidence != "high":
        typer.echo(
            f"  warning:  confidence is '{version.confidence}' — "
            "files changed concurrently with this turn; attribution may be imprecise"
        )

    prompt = store.read_object(repo_root, version.prompt_ref) if version.prompt_ref else None
    if prompt:
        typer.echo("\nprompt:")
        typer.echo(_indent(prompt))

    _echo_thinking(repo_root, version)

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
def tag(
    version_id: str = typer.Argument(
        None, help="Version id, tag, or HEAD to label. Omit both arguments to list tags."
    ),
    label: str = typer.Argument(None, help="New tag name."),
    delete: str = typer.Option(None, "--delete", "-d", help="Delete a tag by name."),
    force: bool = typer.Option(
        False, "--force", "-f", help="Move an existing tag to the requested version."
    ),
) -> None:
    """Create, list, delete, or deliberately move repository-wide version tags."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)

    if delete is not None:
        if version_id is not None or label is not None or force:
            typer.echo(
                "error: --delete cannot be combined with version, label, or --force",
                err=True,
            )
            raise typer.Exit(2)
        with store.connect(repo_root) as conn:
            removed = store.delete_tag(conn, delete)
        if not removed:
            typer.echo(f"error: no such tag '{delete}'", err=True)
            raise typer.Exit(1)
        typer.echo(f"deleted tag '{delete}'")
        return

    if version_id is None and label is None:
        if force:
            typer.echo("error: --force is only valid when creating/moving a tag", err=True)
            raise typer.Exit(2)
        with store.connect(repo_root) as conn:
            tags = store.list_tags(conn)
        if not tags:
            typer.echo("No tags yet.")
            return
        for item in tags:
            typer.echo(f"{item.name:<24} {item.version_id}")
        return

    if version_id is None or label is None:
        typer.echo(
            "error: creating a tag requires both <version> and <label>",
            err=True,
        )
        raise typer.Exit(2)

    error = _tag_name_error(label)
    if error:
        typer.echo(f"error: invalid tag '{label}': {error}", err=True)
        raise typer.Exit(2)

    with store.connect(repo_root) as conn:
        resolved_id = _resolve_version_token(repo_root, conn, branch, version_id)
        if resolved_id is None:
            typer.echo(f"error: no such version or tag '{version_id}'", err=True)
            raise typer.Exit(1)
        try:
            item = store.set_tag(conn, label, resolved_id, force=force)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1)
    typer.echo(f"tagged {item.version_id} as '{item.name}'")


@app.command()
def diff(
    a: str = typer.Argument(
        None, help="First version id (or HEAD). Omit to diff HEAD against the working tree."
    ),
    b: str = typer.Argument(
        None, help="Second version id (or HEAD). Omit to diff <a> against the working tree."
    ),
    stat: bool = typer.Option(False, "--stat", help="Show a diffstat summary instead of the full diff."),
    name_only: bool = typer.Option(False, "--name-only", help="Show only changed file names."),
    exit_code: bool = typer.Option(
        False, "--exit-code", help="Exit 1 if there are differences, 0 if none (for scripts)."
    ),
) -> None:
    """Diff two versions, a version against the working tree, or (no args) HEAD
    against the working tree. Read-only; works across git branches."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)

    if stat and name_only:
        typer.echo("error: --stat and --name-only are mutually exclusive", err=True)
        raise typer.Exit(2)

    with store.connect(repo_root) as conn:
        if a is None:
            before_tree, before_label = _head_tree(repo_root, conn, branch)
            after_tree, after_label = _working_tree(repo_root)
        elif b is None:
            before_tree, before_label = _resolve_diff_tree(repo_root, conn, branch, a)
            after_tree, after_label = _working_tree(repo_root)
        else:
            before_tree, before_label = _resolve_diff_tree(repo_root, conn, branch, a)
            after_tree, after_label = _resolve_diff_tree(repo_root, conn, branch, b)

    output = git_ops.diff_trees(
        repo_root, before_tree, after_tree, stat=stat, name_only=name_only
    )
    typer.echo(f"diff {before_label} -> {after_label}")
    has_changes = bool(output.strip())
    typer.echo(output if has_changes else "(no differences)")
    if exit_code and has_changes:
        raise typer.Exit(1)


def _working_tree(repo_root: Path) -> tuple[str, str]:
    return git_ops.write_tree(repo_root), "working tree"


def _head_tree(repo_root: Path, conn, branch: str) -> tuple[str, str]:
    head_id = store.read_head(repo_root, branch, seed=False)
    if not head_id:
        typer.echo("error: no HEAD on this branch to diff against", err=True)
        raise typer.Exit(1)
    return _resolve_diff_tree(repo_root, conn, branch, head_id)


def _resolve_diff_tree(repo_root: Path, conn, branch: str, token: str) -> tuple[str, str]:
    version_id = _resolve_version_token(repo_root, conn, branch, token)
    if token.upper() == "HEAD" and not version_id:
        typer.echo("error: no HEAD on this branch", err=True)
        raise typer.Exit(1)
    version = store.get_version(conn, version_id) if version_id else None
    if version is None:
        typer.echo(f"error: no such version or tag '{token}'", err=True)
        raise typer.Exit(1)
    if version.tree_after is None:
        # Only an open (in-progress) prompt legitimately lacks a finalized tree;
        # for any other type a NULL tree_after means inconsistent data.
        if version.type == "prompt":
            typer.echo(
                f"note: {version.id} is an open turn; using its start snapshot", err=True
            )
        else:
            typer.echo(
                f"error: {version.id} [{version.type}] has no finalized snapshot", err=True
            )
            raise typer.Exit(1)
    tree = version.tree_after or version.tree_before
    return tree, f"{version.id} [{version.type}]"


@app.command()
def blame(
    file: str = typer.Argument(..., help="Path to the file to blame."),
    version_id: str = typer.Argument(
        None, help="Version to blame at (or HEAD). Defaults to the branch HEAD."
    ),
    line: str = typer.Option(
        None, "-L", "--line", help="Restrict to a line range: N or N:M."
    ),
    show: bool = typer.Option(
        False, "--show", help="Expand each contributing prompt, response, and hunk."
    ),
) -> None:
    """Attribute each line of a file to the prompt (version) that produced it.

    Traces every line back to its intent: an AI prompt, a human edit, or
    pre-existing code. Read-only; blames a version's snapshot, not unsaved edits.
    """
    from . import blame as blame_mod

    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)

    target_id = _resolve_blame_version(repo_root, branch, version_id)

    try:
        with store.connect(repo_root) as conn:
            result = blame_mod.blame_file(
                repo_root, conn, file=file, version_id=target_id
            )
        if line:
            start, end = blame_mod.parse_line_range(line)
            result.lines = blame_mod.slice_range(result.lines, start, end)
    except blame_mod.BlameError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    except git_ops.GitError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)

    if not result.lines:
        typer.echo("(no lines to blame)")
        return

    typer.echo(f"blame {result.rel_path} @ {result.target.id} [{result.target.type}]")
    typer.echo(blame_mod.render_blame(repo_root, result))
    if show:
        typer.echo("")
        typer.echo(blame_mod.render_show(repo_root, result))


@app.command()
def why(
    target: str = typer.Argument(
        ..., help="A file:line reference, e.g. app.py:42."
    ),
    version_id: str = typer.Argument(
        None, help="Version to blame at (or HEAD). Defaults to the branch HEAD."
    ),
) -> None:
    """Explain a single line: the full prompt, response, and hunk behind it.

    Sugar for `profact blame <file> -L <line>:<line> --show`.
    """
    from . import blame as blame_mod

    if ":" not in target:
        typer.echo("error: expected <file>:<line> (e.g. app.py:42)", err=True)
        raise typer.Exit(2)
    file, _, lineno = target.rpartition(":")

    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)
    target_id = _resolve_blame_version(repo_root, branch, version_id)

    try:
        start, end = blame_mod.parse_line_range(lineno)
        with store.connect(repo_root) as conn:
            result = blame_mod.blame_file(
                repo_root, conn, file=file, version_id=target_id
            )
        result.lines = blame_mod.slice_range(result.lines, start, end)
    except blame_mod.BlameError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    except git_ops.GitError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)

    if not result.lines:
        typer.echo(f"error: no line {lineno} in {result.rel_path} @ {result.target.id}", err=True)
        raise typer.Exit(1)

    typer.echo(f"why {result.rel_path}:{lineno} @ {result.target.id}\n")
    typer.echo(blame_mod.render_blame(repo_root, result))
    typer.echo("")
    typer.echo(blame_mod.render_show(repo_root, result))


def _resolve_blame_version(repo_root: Path, branch: str, token: str | None) -> str:
    """Resolve a blame target token to a version id (HEAD- and tag-aware)."""
    if token is None or token.upper() == "HEAD":
        head = store.read_head(repo_root, branch, seed=False)
        if not head:
            typer.echo("error: no HEAD on this branch to blame", err=True)
            raise typer.Exit(1)
        return head
    with store.connect(repo_root) as conn:
        return store.resolve_version_id(conn, token) or token


@app.command()
def bisect(
    cmd: list[str] = typer.Argument(
        ...,
        help="Test command, after `--` (e.g. -- pytest -q). "
        "Exit 0 = good, 125 = skip, anything else = bad.",
    ),
    good: str = typer.Option(
        ..., "--good", "-g", help="A known-good version (must be an ancestor of bad)."
    ),
    bad: str = typer.Option(
        None, "--bad", "-b", help="A known-bad version (defaults to the branch HEAD)."
    ),
) -> None:
    """Binary-search the prompt lineage for the version that broke a test.

    Runs the test command at candidate snapshots between <good> and <bad> and
    reports the first failing version. Non-destructive: never creates versions or
    moves HEAD, and restores the working tree when done.
    """
    from . import bisect as bisect_mod

    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)

    bad_id = bad
    if bad_id is None:
        bad_id = store.read_head(repo_root, branch, seed=False)
        if not bad_id:
            typer.echo("error: no HEAD on this branch; pass --bad <version>", err=True)
            raise typer.Exit(1)
    if bad_id.upper() == "HEAD":
        bad_id = store.read_head(repo_root, branch, seed=False)
    good_id = good
    if good_id.upper() == "HEAD":
        good_id = store.read_head(repo_root, branch, seed=False)

    with store.connect(repo_root) as conn:
        if good_id:
            good_id = store.resolve_version_id(conn, good_id) or good_id
        if bad_id:
            bad_id = store.resolve_version_id(conn, bad_id) or bad_id

    def _on_step(step: bisect_mod.StepResult) -> None:
        typer.echo(
            f"  {step.outcome:<4} exit {step.exit_code:<3} "
            f"{step.version.id:<5} [{step.version.type:<6}] "
            f"{log_view.summary(repo_root, step.version)}"
        )

    typer.echo(f"bisecting '{branch}': good={good_id} bad={bad_id}")
    try:
        with store.connect(repo_root) as conn:
            result = bisect_mod.bisect(
                repo_root,
                conn,
                good_id=good_id,
                bad_id=bad_id,
                cmd=list(cmd),
                on_step=_on_step,
            )
    except bisect_mod.BisectError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    except git_ops.GitError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo("")
    if result.first_bad is None:
        lo, hi = result.inconclusive_between  # type: ignore[misc]
        typer.echo(
            f"inconclusive: the culprit is between {lo.id} and {hi.id}, but the "
            f"candidates in between were skipped."
        )
        raise typer.Exit(2)

    culprit = result.first_bad
    typer.echo(f"first bad version: {culprit.id} [{culprit.type}]")
    typer.echo(f"  {log_view.summary(repo_root, culprit)}")
    typer.echo(f"  tested {len(result.steps)} snapshot(s)")
    typer.echo("")
    typer.echo(f"inspect it:  profact show {culprit.id}")
    typer.echo(f"blame lines: profact blame <file> {culprit.id}")


@app.command()
def test(
    cmd: list[str] = typer.Argument(
        ..., help="Test command, after `--` (e.g. -- pytest -q)."
    ),
    version_id: str = typer.Option(
        None, "--version", "-v", help="Attach the result to this version (default: HEAD)."
    ),
) -> None:
    """Run a test command and record its pass/fail outcome on a version.

    Makes history behavior-aware: the result shows in `show`, `status`, and
    `log --tests`. Exits 0 if the tests pass, non-zero otherwise (CI-friendly).
    """
    repo_root = _resolve_repo_root()
    _require_init(repo_root)
    branch = git_ops.current_branch(repo_root)

    target = version_id or store.read_head(repo_root, branch, seed=False)
    if not target:
        typer.echo(
            "error: no HEAD on this branch to attach a result to; pass --version",
            err=True,
        )
        raise typer.Exit(1)

    with store.connect(repo_root) as conn:
        resolved_target = _resolve_version_token(repo_root, conn, branch, target)
        version = store.get_version(conn, resolved_target) if resolved_target else None
        if version is None:
            typer.echo(f"error: no such version or tag '{target}'", err=True)
            raise typer.Exit(1)
        # The command runs against the current working tree; warn if that isn't
        # the snapshot we're attributing the result to.
        version_tree = version.tree_after or version.tree_before
        if git_ops.write_tree(repo_root) != version_tree:
            typer.echo(
                f"note: working tree differs from {version.id}; the result reflects "
                f"your current files, not {version.id}'s snapshot",
                err=True,
            )
        try:
            outcome = testrunner.run_and_record(
                repo_root, conn, version_id=version.id, cmd=list(cmd)
            )
        except testrunner.TestRunError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2)

    typer.echo(f"{version.id}: {testrunner.format_result(outcome.result)}")
    raise typer.Exit(0 if outcome.result.status == testrunner.PASS else 1)


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
        resolved_id = _resolve_version_token(repo_root, conn, branch, version_id)
        target = store.get_version(conn, resolved_id) if resolved_id else None
        if target is None:
            typer.echo(f"error: no such version or tag '{version_id}'", err=True)
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
        test_result = store.get_test_result(conn, head_id)

    if head is None:
        typer.echo(f"HEAD:    {head_id} (missing from store)")
        return

    typer.echo(
        f"HEAD:    {head.id} [{head.type}] {log_view.summary(repo_root, head)}"
    )
    typer.echo(f"versions on branch: {count}")
    if test_result:
        typer.echo(f"tests:   {testrunner.format_result(test_result)}")

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
    dangling open turns/tags, and orphaned objects."""
    repo_root = _resolve_repo_root()
    _require_init(repo_root)

    with store.connect(repo_root) as conn:
        versions = store.all_versions(conn)
        phantom = [v for v in versions if _is_phantom_checkpoint(repo_root, v)]
        dangling = [
            v for v in versions if v.type == "prompt" and v.tree_after is None
        ]
        dangling_tag_rows = store.dangling_tags(conn)
        referenced = store.referenced_object_ids(conn)
    orphans = _orphan_objects(repo_root, referenced)

    typer.echo(f"phantom checkpoints:   {len(phantom)}")
    for v in phantom:
        typer.echo(f"  {v.id}  (parent {v.parent_id or '-'})")
    typer.echo(f"dangling open turns:   {len(dangling)}")
    for v in dangling:
        typer.echo(f"  {v.id}  {log_view.summary(repo_root, v)}")
    typer.echo(f"orphaned objects:      {len(orphans)}")
    typer.echo(f"dangling tags:         {len(dangling_tag_rows)}")
    for item in dangling_tag_rows:
        typer.echo(f"  {item.name} -> {item.version_id}")

    if not fix:
        if phantom or orphans or dangling_tag_rows:
            typer.echo("\nrun `profact doctor --fix` to prune the safe-to-remove items above")
        else:
            typer.echo("\nnothing to repair.")
        return

    removed_versions = 0
    with store.connect(repo_root) as conn:
        for v in phantom:
            # Rewire any children onto the phantom's parent so no chain breaks,
            # move any branch HEAD and tags off it, then delete the row.
            store.reparent_children(conn, v.id, v.parent_id)
            for br in store.heads_pointing_to(conn, v.id):
                if v.parent_id:
                    store.write_head(repo_root, br, v.parent_id)
            store.retarget_tags(conn, v.id, v.parent_id)
            store.delete_version(conn, v.id)
            removed_versions += 1
        removed_tags = 0
        for item in store.dangling_tags(conn):
            if store.delete_tag(conn, item.name):
                removed_tags += 1
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
        f"{removed_objects} orphaned object(s), and {removed_tags} dangling tag(s)."
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
    paths = log_view.diff_paths(diff or "")
    return all(git_ops.is_infra_path(p) for p in paths)


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


def _resolve_version_token(
    repo_root: Path, conn, branch: str, token: str
) -> str | None:
    """Resolve branch-local HEAD, a raw version id, or a repository-wide tag."""
    if token.upper() == "HEAD":
        return store.read_head(repo_root, branch, seed=False)
    return store.resolve_version_id(conn, token)


def _tag_name_error(name: str) -> str | None:
    if not name:
        return "name cannot be empty"
    if name.upper() == "HEAD" or re.fullmatch(r"v\d+", name, flags=re.IGNORECASE):
        return "HEAD and version-like names (for example v7) are reserved"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        return "use letters, numbers, '.', '_' or '-' (starting with a letter or number)"
    return None


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


_NO_THINKING_PLACEHOLDER = (
    "(no reasoning captured — the model has no thinking capability, "
    "or none was emitted for this turn)"
)


def _echo_thinking(repo_root: Path, version: store.Version) -> None:
    """Print the model's reasoning for a version, or an explicit placeholder.

    Only prompt turns carry reasoning; anchors/checkpoints/human edits have no
    ``thinking_source`` and are skipped silently.
    """
    thinking = (
        store.read_object(repo_root, version.thinking_ref)
        if version.thinking_ref
        else None
    )
    if thinking:
        typer.echo("\nthinking:")
        typer.echo(_indent(thinking))
    elif version.thinking_source == store.THINKING_NONE:
        typer.echo("\nthinking:")
        typer.echo(_indent(_NO_THINKING_PLACEHOLDER))


def _short(text: str, length: int = 72) -> str:
    text = text.strip()
    return text if len(text) <= length else text[: length - 1] + "…"


if __name__ == "__main__":
    app()
