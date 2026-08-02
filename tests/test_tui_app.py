from __future__ import annotations

import threading
from pathlib import Path

from typer.testing import CliRunner

from profact import git_ops, store
from profact.cli import app as cli_app
from profact.tui.app import HistoryItem, ProFactApp
from profact.tui import presenter
from profact.tui.screens import BranchScreen, FilterScreen, TextScreen
from textual.widgets import Input, ListView, RichLog, Static

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


def _realistic_history(repo: Path) -> tuple[store.Version, store.Version]:
    with store.connect(repo) as conn:
        anchor = store.get_version(conn, "v1")
        assert anchor is not None and anchor.tree_after is not None
        parent_id = anchor.id
        main_versions: list[store.Version] = []
        branch_parent_id = anchor.id
        for index in range(1, 13):
            prompt = (
                f"Implement account workflow step {index}\n\n"
                + "Validate behavior and preserve existing APIs.\n" * 48
            )
            response = f"Completed step {index}.\n" + "Checked edge cases.\n" * 24
            diff = (
                "diff --git a/src/account.py b/src/account.py\n"
                "--- a/src/account.py\n"
                "+++ b/src/account.py\n"
                f"@@ -{index},20 +{index},20 @@\n"
                + "".join(
                    f"-old_value_{line} = {line}\n+new_value_{line} = {line + index}\n"
                    for line in range(28)
                )
            )
            version = store.create_version(
                conn,
                git_branch="main",
                parent_id=parent_id,
                tree_before=anchor.tree_after,
                tree_after=anchor.tree_after,
                prompt_ref=store.write_object(repo, prompt),
                response_ref=store.write_object(repo, response),
                diff_ref=store.write_object(repo, diff),
                model="claude-test" if index % 2 else "cursor-test",
                confidence="uncertain" if index == 7 else "high",
                thinking_source=(
                    store.THINKING_CAPTURED if index % 3 == 0 else store.THINKING_NONE
                ),
            )
            if version.thinking_source == store.THINKING_CAPTURED:
                thinking_ref = store.write_object(
                    repo, "Consider compatibility.\n" * 16
                )
                conn.execute(
                    "UPDATE versions SET thinking_ref = ? WHERE id = ?",
                    (thinking_ref, version.id),
                )
                conn.commit()
            main_versions.append(version)
            parent_id = version.id
            if index == 5:
                branch_parent_id = version.id

        store.set_tag(conn, "working", main_versions[-1].id)
        store.set_tag(conn, "review", main_versions[6].id)
        store.set_test_result(
            conn,
            main_versions[-1].id,
            status="pass",
            exit_code=0,
            command="pytest -q",
            output_ref=store.write_object(repo, "119 passed\n" + "ok\n" * 30),
        )
        store.set_test_result(
            conn,
            main_versions[6].id,
            status="fail",
            exit_code=1,
            command="pytest -q",
            output_ref=store.write_object(repo, "one failure\n"),
        )
        experiment = store.create_version(
            conn,
            git_branch="experiment",
            parent_id=branch_parent_id,
            tree_before=anchor.tree_after,
            tree_after=anchor.tree_after,
            prompt_ref=store.write_object(repo, "Try an alternate account workflow"),
            response_ref=store.write_object(repo, "Alternative completed."),
            thinking_source=store.THINKING_NONE,
        )

    store.write_head(repo, "main", main_versions[-1].id)
    store.write_head(repo, "experiment", experiment.id)
    return main_versions[-1], experiment


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


async def test_tui_focuses_active_detail_content_for_keyboard_scrolling(
    git_repo: Path,
) -> None:
    _prompt(git_repo)
    app = ProFactApp(git_repo)

    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)

        await pilot.press("enter")
        assert isinstance(app.focused, RichLog)
        assert app.focused.id == "overview-content"

        await pilot.press("5")
        assert app.query_one("#detail-tabs").active == "diff-tab"
        assert isinstance(app.focused, RichLog)
        assert app.focused.id == "diff-content"


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


async def test_tui_escape_cancels_search_without_changing_results(
    git_repo: Path,
) -> None:
    _prompt(git_repo)
    app = ProFactApp(git_repo)

    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        original_ids = [item.row.version.id for item in app.query(HistoryItem)]

        await pilot.press("/")
        search = app.query_one("#search-input", Input)
        search.value = "no-match"
        await pilot.press("escape")

        assert not search.has_class("visible")
        assert search.value == ""
        assert app.focused is app.query_one("#history-list", ListView)
        assert [item.row.version.id for item in app.query(HistoryItem)] == original_ids
        assert app.history_query.search == ""


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


async def test_tui_realistic_history_renders_and_scrolls_at_wide_and_narrow_sizes(
    git_repo: Path,
) -> None:
    head, experiment = _realistic_history(git_repo)
    render_dir = store.profact_dir(git_repo) / "test-renders"
    render_dir.mkdir()

    wide_app = ProFactApp(git_repo)
    async with wide_app.run_test(size=(120, 38)) as pilot:
        await _settle(wide_app, pilot)
        assert len(list(wide_app.query(HistoryItem))) == 13
        assert wide_app.selected_version_id == head.id

        await pilot.press("enter", "2")
        prompt_log = wide_app.query_one("#prompt-content", RichLog)
        assert wide_app.focused is prompt_log
        assert prompt_log.virtual_size.height > prompt_log.size.height
        await pilot.press("pagedown")
        await pilot.pause()
        assert prompt_log.scroll_offset.y > 0

        await pilot.press("5")
        diff_log = wide_app.query_one("#diff-content", RichLog)
        assert wide_app.focused is diff_log
        assert diff_log.virtual_size.height > diff_log.size.height

        wide_svg = wide_app.export_screenshot(title="ProFact TUI wide stability")
        assert "<svg" in wide_svg and head.id in wide_svg
        (render_dir / "profact-tui-wide.svg").write_text(wide_svg, encoding="utf-8")

        await pilot.press("b")
        assert isinstance(wide_app.screen, BranchScreen)
        branch_list = wide_app.screen.query_one("#branch-options", ListView)
        branch_list.index = 0
        await pilot.press("enter")
        await _settle(wide_app, pilot)
        assert wide_app.history_model is not None
        assert wide_app.history_model.branch == "experiment"
        assert wide_app.selected_version_id == experiment.id

    narrow_app = ProFactApp(git_repo)
    async with narrow_app.run_test(size=(70, 26)) as pilot:
        await _settle(narrow_app, pilot)
        assert narrow_app.screen.has_class("narrow")
        assert max(item.size.height for item in narrow_app.query(HistoryItem)) <= 4
        history_svg = narrow_app.export_screenshot(title="ProFact TUI narrow history")
        assert "<svg" in history_svg
        assert f"HEAD:&#160;{head.id}" in history_svg
        assert "|&#160;clean&#160;|" in history_svg

        await pilot.press("enter", "5")
        assert narrow_app.screen.has_class("show-detail")
        detail_svg = narrow_app.export_screenshot(title="ProFact TUI narrow detail")
        assert "<svg" in detail_svg and "Diff" in detail_svg
        (render_dir / "profact-tui-narrow.svg").write_text(
            detail_svg, encoding="utf-8"
        )


async def test_tui_handles_empty_history_and_missing_content(git_repo: Path) -> None:
    with store.connect(git_repo) as conn:
        conn.execute("DELETE FROM heads")
        conn.execute("DELETE FROM versions")
        conn.commit()

    empty_app = ProFactApp(git_repo)
    async with empty_app.run_test(size=(90, 26)) as pilot:
        await _settle(empty_app, pilot)
        assert empty_app.selected_version_id is None
        assert not list(empty_app.query(HistoryItem))
        empty_list = empty_app.query_one("#history-list", ListView)
        assert "No versions yet on 'main'." in str(
            empty_list.children[0].query_one(Static).render()
        )

    tree = git_ops.write_tree(git_repo)
    missing_ref = "f" * 64
    with store.connect(git_repo) as conn:
        version = store.create_version(
            conn,
            git_branch="main",
            tree_before=tree,
            tree_after=tree,
            prompt_ref=missing_ref,
        )
    store.write_head(git_repo, "main", version.id)

    missing_app = ProFactApp(git_repo)
    async with missing_app.run_test(size=(90, 26)) as pilot:
        await _settle(missing_app, pilot)
        await pilot.press("enter", "2")
        assert "(no&#160;prompt&#160;recorded)" in missing_app.export_screenshot()


async def test_tui_quits_cleanly_while_startup_worker_is_active(
    git_repo: Path,
    monkeypatch,
) -> None:
    release = threading.Event()
    original_load = presenter.load_history

    def delayed_load(repo_root: Path, query: presenter.HistoryQuery):
        release.wait(timeout=2)
        return original_load(repo_root, query)

    monkeypatch.setattr(presenter, "load_history", delayed_load)
    app = ProFactApp(git_repo)

    try:
        async with app.run_test(size=(90, 26)) as pilot:
            await pilot.pause()
            await pilot.press("q")
            release.set()
    finally:
        release.set()


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