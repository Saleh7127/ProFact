from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from profact import git_ops, store
from profact.cli import app as cli_app
from profact.tui.app import HistoryItem, ProFactApp
from profact.tui.screens import BranchScreen, FilterScreen, TextScreen
from textual.widgets import Input, ListView

runner = CliRunner()


def _prompt(repo: Path) -> store.Version:
    branch = git_ops.current_branch(repo)
    tree_before = git_ops.write_tree(repo)
    parent_id = store.read_head(repo, branch)
    (repo / "app.py").write_text("print('hello')\n")
    tree_after = git_ops.write_tree(repo)
    diff = git_ops.diff_trees(repo, tree_before, tree_after)
    with store.connect(repo) as conn:
        version = store.create_version(
            conn,
            git_branch=branch,
            parent_id=parent_id,
            tree_before=tree_before,
            tree_after=tree_after,
            prompt_ref=store.write_object(repo, "add a greeting"),
            response_ref=store.write_object(repo, "done"),
            diff_ref=store.write_object(repo, diff),
            thinking_source=store.THINKING_NONE,
        )
    store.write_head(repo, branch, version.id)
    return version


def _child(
    repo: Path,
    *,
    parent_id: str,
    prompt: str,
    branch: str = "main",
) -> store.Version:
    with store.connect(repo) as conn:
        parent = store.get_version(conn, parent_id)
        assert parent is not None and parent.tree_after is not None
        version = store.create_version(
            conn,
            git_branch=branch,
            parent_id=parent_id,
            tree_before=parent.tree_after,
            tree_after=parent.tree_after,
            prompt_ref=store.write_object(repo, prompt),
            response_ref=store.write_object(repo, "done"),
            thinking_source=store.THINKING_NONE,
        )
    return version


async def _settle(app: ProFactApp, pilot) -> None:
    for _ in range(4):
        await pilot.pause()
        active = list(app.workers)
        if active:
            await app.workers.wait_for_complete(active)
        await pilot.pause()
        if not app.workers:
            break


async def test_tui_browses_versions_tabs_and_health(git_repo: Path) -> None:
    version = _prompt(git_repo)
    app = ProFactApp(git_repo)

    async with app.run_test(size=(120, 38)) as pilot:
        await _settle(app, pilot)

        items = list(app.query(HistoryItem))
        assert len(items) == 2
        assert app.selected_version_id == version.id
        assert version.id in str(app.query_one("#detail-title").render())
        assert app.query_one("#detail-tabs").active == "overview-tab"

        await pilot.press("2")
        assert app.query_one("#detail-tabs").active == "prompt-tab"

        await pilot.press("h")
        await _settle(app, pilot)
        assert isinstance(app.screen, TextScreen)
        assert "ProFact history health" in str(
            app.screen.query_one("#secondary-title").render()
        )
        await pilot.press("escape")
        assert app.screen is app.screen_stack[0]


async def test_tui_uses_narrow_history_then_detail(git_repo: Path) -> None:
    _prompt(git_repo)
    app = ProFactApp(git_repo)

    async with app.run_test(size=(70, 30)) as pilot:
        await _settle(app, pilot)
        assert app.screen.has_class("narrow")
        assert not app.screen.has_class("show-detail")

        await pilot.press("enter")
        assert app.screen.has_class("show-detail")
        await pilot.press("escape")
        assert not app.screen.has_class("show-detail")


async def test_tui_search_path_and_refresh_preserve_selection(
    git_repo: Path,
) -> None:
    first = _prompt(git_repo)
    second = _child(
        git_repo,
        parent_id="v1",
        prompt="alternate dashboard",
    )
    store.write_head(git_repo, "main", second.id)
    app = ProFactApp(git_repo)

    async with app.run_test(size=(120, 38)) as pilot:
        await _settle(app, pilot)
        assert len(list(app.query(HistoryItem))) == 3

        await pilot.press("p")
        await _settle(app, pilot)
        assert [
            item.row.version.id for item in app.query(HistoryItem)
        ] == [second.id, "v1"]
        assert "1 side paths omitted" in str(
            app.query_one("#status-line").render()
        )

        await pilot.press("p", "/")
        search = app.query_one("#search-input", Input)
        search.value = "greeting"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert [
            item.row.version.id for item in app.query(HistoryItem)
        ] == [first.id]
        assert "2 filtered" in str(app.query_one("#status-line").render())

        selected_before = app.selected_version_id
        await pilot.press("r")
        await _settle(app, pilot)
        assert app.selected_version_id == selected_before


async def test_tui_views_another_branch_without_switching_git(
    git_repo: Path,
) -> None:
    main = _prompt(git_repo)
    experiment = _child(
        git_repo,
        parent_id=main.id,
        prompt="experiment",
        branch="experiment",
    )
    store.write_head(git_repo, "experiment", experiment.id)
    app = ProFactApp(git_repo)

    async with app.run_test(size=(120, 38)) as pilot:
        await _settle(app, pilot)
        assert app.history_model is not None
        assert app.history_model.branch == "main"

        await pilot.press("b")
        assert isinstance(app.screen, BranchScreen)
        branch_list = app.screen.query_one("#branch-options", ListView)
        branch_list.index = 0
        await pilot.press("enter")
        await _settle(app, pilot)

        assert app.history_model is not None
        assert app.history_model.branch == "experiment"
        assert git_ops.current_branch(git_repo) == "main"
        status = str(app.query_one("#status-line").render())
        assert "Git: main" in status and "viewing: experiment" in status


async def test_tui_surfaces_invalid_filter_without_losing_history(
    git_repo: Path,
) -> None:
    _prompt(git_repo)
    app = ProFactApp(git_repo)

    async with app.run_test(size=(120, 38)) as pilot:
        await _settle(app, pilot)
        original_ids = [item.row.version.id for item in app.query(HistoryItem)]

        await pilot.press("f")
        assert isinstance(app.screen, FilterScreen)
        app.screen.query_one("#filter-type", Input).value = "alien"
        await pilot.click("#apply-filter")
        await _settle(app, pilot)

        assert [item.row.version.id for item in app.query(HistoryItem)] == original_ids
        assert "unknown version type 'alien'" in str(
            app.query_one("#message-line").render()
        )


def test_tui_command_is_listed_and_launches_thin_runner(
    git_repo: Path, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo)
    launched: list[Path] = []
    monkeypatch.setattr("profact.tui.app.run", lambda repo: launched.append(repo))

    help_result = runner.invoke(cli_app, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "tui" in help_result.output

    result = runner.invoke(cli_app, ["tui"])
    assert result.exit_code == 0, result.output
    assert launched == [git_repo]