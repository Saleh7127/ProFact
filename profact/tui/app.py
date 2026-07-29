"""Textual application for interactive, read-only ProFact history browsing."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import (
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from .. import health, status_view, testrunner, version_detail
from . import presenter
from .screens import BranchScreen, FilterScreen, HELP_TEXT, TextScreen


class HistoryItem(ListItem):
    def __init__(self, row: presenter.HistoryRow, *, show_graph: bool) -> None:
        self.row = row
        super().__init__(Static(self._render_label(show_graph), markup=False))
        self._set_status_class()

    def update_graph(self, show_graph: bool) -> None:
        self.query_one(Static).update(self._render_label(show_graph))

    def _render_label(self, show_graph: bool) -> Text:
        row = self.row
        graph = row.graph_node if show_graph else ""
        continuation = row.graph_continuation if show_graph else ""
        test_marker = (
            testrunner.marker(row.test_result.status)
            if row.test_result
            else "[none]"
        )
        text = Text()
        text.append(f"{graph:<5}", style="bold #65d1a5")
        text.append(f"{row.version.id:<6}", style="bold #e8e8e8")
        text.append(f"{'HEAD' if row.is_head else '':<5}", style="bold #f5c451")
        text.append(f"{test_marker:<7}")
        text.append(f"[{row.version.type:<10}] ", style="#8fd3c1")
        text.append(row.summary)
        text.append("\n")
        text.append(f"{continuation:<5}", style="#65d1a5")
        text.append(f"branch:{row.version.git_branch}", style="#a8a8a8")
        text.append(f"  parent:{row.version.parent_id or '-'}", style="#a8a8a8")
        if row.tags:
            text.append(f"  tags:{','.join(row.tags)}", style="#f5c451")
        if row.version.type == "prompt":
            text.append(
                f"  thinking:{row.version.thinking_source or 'unknown'}",
                style="#a8a8a8",
            )
        if row.version.confidence and row.version.confidence != "high":
            text.append(f"  confidence:{row.version.confidence}", style="bold #ff8b7b")
        if show_graph and row.graph_connector:
            text.append(f"\n{row.graph_connector}", style="bold #65d1a5")
        return text

    def _set_status_class(self) -> None:
        status = self.row.test_result.status if self.row.test_result else "untested"
        self.add_class(f"test-{status}")
        if self.row.is_head:
            self.add_class("is-head")


class WorkspaceLoaded(Message):
    def __init__(
        self,
        model: presenter.HistoryModel,
        status: status_view.StatusReport,
    ) -> None:
        super().__init__()
        self.model = model
        self.status = status


class DetailLoaded(Message):
    def __init__(self, detail: version_detail.VersionDetail) -> None:
        super().__init__()
        self.detail = detail


class HealthLoaded(Message):
    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content


class LoadFailed(Message):
    def __init__(self, operation: str, error: Exception) -> None:
        super().__init__()
        self.operation = operation
        self.error = error


class ProFactApp(App[None]):
    TITLE = "ProFact history"
    SUB_TITLE = "Prompt-native version control"

    CSS = """
    Screen {
        background: #171918;
        color: #e8e8e8;
    }

    #status-line {
        dock: top;
        height: 2;
        padding: 0 1;
        background: #252927;
        color: #d7e6df;
        border-bottom: solid #4b5a54;
    }

    #message-line {
        dock: bottom;
        height: 1;
        padding: 0 1;
        color: #ffb2a8;
        background: #252927;
    }

    #workspace {
        height: 1fr;
    }

    #history-pane {
        width: 43%;
        min-width: 34;
        height: 1fr;
        border-right: solid #4b5a54;
    }

    #detail-pane {
        width: 1fr;
        height: 1fr;
    }

    .pane-title {
        height: 1;
        padding: 0 1;
        background: #202321;
        color: #8fd3c1;
        text-style: bold;
    }

    #search-input {
        display: none;
        height: 3;
        border: tall #65d1a5;
    }

    #search-input.visible {
        display: block;
    }

    #history-list {
        height: 1fr;
        background: #171918;
    }

    HistoryItem {
        height: auto;
        padding: 0 1;
        border-bottom: solid #303532;
    }

    HistoryItem.is-head {
        border-left: thick #f5c451;
    }

    HistoryItem.test-fail, HistoryItem.test-error {
        border-right: thick #ff8b7b;
    }

    TabbedContent {
        height: 1fr;
    }

    TabPane {
        height: 1fr;
        padding: 0;
    }

    .detail-content {
        height: 1fr;
        padding: 1;
        background: #171918;
    }

    #diff-content {
        overflow-x: auto;
    }

    .dialog {
        width: 68;
        max-width: 92%;
        height: auto;
        max-height: 90%;
        margin: 2 4;
        padding: 1 2;
        background: #252927;
        border: solid #65d1a5;
    }

    .dialog-title, #secondary-title {
        height: 2;
        color: #8fd3c1;
        text-style: bold;
    }

    .dialog-note {
        color: #b9c2be;
        margin-bottom: 1;
    }

    .dialog-actions {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }

    .dialog-actions Button {
        margin-left: 1;
    }

    #branch-options {
        height: auto;
        max-height: 16;
    }

    #filter-fields {
        height: 1fr;
        min-height: 18;
    }

    #secondary-title {
        dock: top;
        padding: 0 1;
        background: #252927;
    }

    #secondary-content {
        height: 1fr;
        padding: 1 2;
    }

    Screen.narrow #workspace {
        layout: vertical;
    }

    Screen.narrow #history-pane {
        width: 1fr;
        min-width: 0;
        border-right: none;
    }

    Screen.narrow #detail-pane {
        display: none;
        width: 1fr;
    }

    Screen.narrow.show-detail #history-pane {
        display: none;
    }

    Screen.narrow.show-detail #detail-pane {
        display: block;
    }

    Screen.medium #history-pane {
        width: 38%;
        min-width: 30;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("j", "move(1)", "Next"),
        ("k", "move(-1)", "Previous"),
        ("g", "toggle_graph", "Graph"),
        ("p", "toggle_path", "Ancestry"),
        ("/", "search", "Search"),
        ("f", "filters", "Filters"),
        ("b", "branches", "Branch"),
        ("h", "health", "Health"),
        ("u", "parent", "Parent"),
        ("H", "head", "HEAD"),
        ("r", "refresh", "Refresh"),
        ("?", "help", "Help"),
        ("escape", "back", "Back"),
        Binding("1", "tab('overview-tab')", "Overview", show=False),
        Binding("2", "tab('prompt-tab')", "Prompt", show=False),
        Binding("3", "tab('thinking-tab')", "Thinking", show=False),
        Binding("4", "tab('response-tab')", "Response", show=False),
        Binding("5", "tab('diff-tab')", "Diff", show=False),
        Binding("6", "tab('tests-tab')", "Tests", show=False),
    ]

    def __init__(self, repo_root: Path) -> None:
        super().__init__()
        self.repo_root = Path(repo_root)
        self.history_query = presenter.HistoryQuery()
        self.history_model: presenter.HistoryModel | None = None
        self.status_report: status_view.StatusReport | None = None
        self.selected_version_id: str | None = None
        self.show_graph = True

    def compose(self) -> ComposeResult:
        yield Static("Loading ProFact history...", id="status-line")
        with Horizontal(id="workspace"):
            with Vertical(id="history-pane"):
                yield Label("Captured history", classes="pane-title")
                yield Input(
                    placeholder="Search version, prompt, model, or tag; Enter applies",
                    id="search-input",
                )
                yield ListView(id="history-list", initial_index=None)
            with Vertical(id="detail-pane"):
                yield Label("Select a version", id="detail-title", classes="pane-title")
                with TabbedContent(initial="overview-tab", id="detail-tabs"):
                    with TabPane("Overview", id="overview-tab"):
                        yield RichLog(
                            wrap=True,
                            auto_scroll=False,
                            id="overview-content",
                            classes="detail-content",
                        )
                    with TabPane("Prompt", id="prompt-tab"):
                        yield RichLog(
                            wrap=True,
                            auto_scroll=False,
                            id="prompt-content",
                            classes="detail-content",
                        )
                    with TabPane("Thinking", id="thinking-tab"):
                        yield RichLog(
                            wrap=True,
                            auto_scroll=False,
                            id="thinking-content",
                            classes="detail-content",
                        )
                    with TabPane("Response", id="response-tab"):
                        yield RichLog(
                            wrap=True,
                            auto_scroll=False,
                            id="response-content",
                            classes="detail-content",
                        )
                    with TabPane("Diff", id="diff-tab"):
                        yield RichLog(
                            wrap=False,
                            auto_scroll=False,
                            min_width=100,
                            id="diff-content",
                            classes="detail-content",
                        )
                    with TabPane("Tests", id="tests-tab"):
                        yield RichLog(
                            wrap=True,
                            auto_scroll=False,
                            id="tests-content",
                            classes="detail-content",
                        )
        yield Static("", id="message-line")
        yield Footer()

    def on_mount(self) -> None:
        self._set_responsive_classes(self.size.width)
        self.refresh_workspace(include_dirty=True)

    def on_resize(self, event: events.Resize) -> None:
        self._set_responsive_classes(event.size.width)

    @work(
        group="workspace",
        exclusive=True,
        thread=True,
        exit_on_error=False,
    )
    def refresh_workspace(self, *, include_dirty: bool) -> None:
        try:
            model = presenter.load_history(self.repo_root, self.history_query)
            report = status_view.inspect(
                self.repo_root, include_dirty=include_dirty
            )
            self.post_message(WorkspaceLoaded(model, report))
        except Exception as error:
            self.post_message(LoadFailed("history", error))

    @work(group="detail", exclusive=True, thread=True, exit_on_error=False)
    def load_selected_detail(self, version_id: str, branch: str) -> None:
        try:
            self.post_message(
                DetailLoaded(
                    presenter.load_detail(
                        self.repo_root, version_id, branch=branch
                    )
                )
            )
        except Exception as error:
            self.post_message(LoadFailed("version detail", error))

    @work(group="health", exclusive=True, thread=True, exit_on_error=False)
    def load_health(self) -> None:
        try:
            report = presenter.load_health(self.repo_root)
            self.post_message(HealthLoaded(health.render(report)))
        except Exception as error:
            self.post_message(LoadFailed("health", error))

    async def on_workspace_loaded(self, message: WorkspaceLoaded) -> None:
        if message.status.dirty is None and self.status_report is not None:
            message.status = replace(
                message.status, dirty=self.status_report.dirty
            )
        self.history_model = message.model
        self.status_report = message.status
        self._update_status_line()
        await self._rebuild_history()
        self._set_message("")

    def on_detail_loaded(self, message: DetailLoaded) -> None:
        detail = message.detail
        if detail.version.id != self.selected_version_id:
            return
        try:
            title = self.query_one("#detail-title", Label)
        except NoMatches:
            return
        title.update(
            f"{detail.version.id} [{detail.version.type}]"
            f"{'  HEAD' if detail.is_head else ''}"
        )
        self._write_log("overview-content", presenter.overview_text(detail))
        self._write_log("prompt-content", detail.section("prompt"))
        self._write_log("thinking-content", detail.section("thinking"))
        self._write_log("response-content", detail.section("response"))
        self._write_log("diff-content", _styled_diff(detail.section("diff")))
        self._write_log("tests-content", presenter.tests_text(detail))

    def on_health_loaded(self, message: HealthLoaded) -> None:
        self.push_screen(TextScreen("ProFact history health", message.content))

    def on_load_failed(self, message: LoadFailed) -> None:
        self._set_message(f"Could not load {message.operation}: {message.error}")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "history-list":
            return
        if not isinstance(event.item, HistoryItem) or self.history_model is None:
            return
        self.selected_version_id = event.item.row.version.id
        self.load_selected_detail(
            self.selected_version_id, self.history_model.branch
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "history-list":
            return
        if self.screen.has_class("narrow"):
            self.screen.add_class("show-detail")
        self.query_one("#detail-tabs", TabbedContent).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search-input":
            return
        self.history_query = replace(
            self.history_query, search=event.value.strip()
        )
        event.input.remove_class("visible")
        self.query_one("#history-list", ListView).focus()
        self.refresh_workspace(include_dirty=False)

    def action_move(self, amount: int) -> None:
        history = self.query_one("#history-list", ListView)
        if not history.children:
            return
        current = history.index if history.index is not None else 0
        history.index = max(0, min(len(history.children) - 1, current + amount))

    def action_toggle_graph(self) -> None:
        self.show_graph = not self.show_graph
        for item in self.query("#history-list > HistoryItem").results():
            item.update_graph(self.show_graph)

    def action_toggle_path(self) -> None:
        self.history_query = replace(
            self.history_query,
            path_mode=not self.history_query.path_mode,
        )
        self.refresh_workspace(include_dirty=False)

    def action_search(self) -> None:
        search = self.query_one("#search-input", Input)
        search.add_class("visible")
        search.value = self.history_query.search
        search.focus()

    def action_filters(self) -> None:
        self.push_screen(
            FilterScreen(self.history_query), self._filters_closed
        )

    def action_branches(self) -> None:
        if self.history_model is None or not self.history_model.branches:
            self._set_message("No captured branches are available.")
            return
        self.push_screen(
            BranchScreen(self.history_model.branches, self.history_model.branch),
            self._branch_closed,
        )

    def action_health(self) -> None:
        self._set_message("Loading history health...")
        self.load_health()

    def action_help(self) -> None:
        self.push_screen(TextScreen("ProFact TUI help", HELP_TEXT))

    def action_parent(self) -> None:
        if self.history_model is None or self.selected_version_id is None:
            return
        selected = next(
            (
                row
                for row in self.history_model.rows
                if row.version.id == self.selected_version_id
            ),
            None,
        )
        if selected is None or selected.version.parent_id is None:
            self._set_message("The selected version has no visible parent.")
            return
        self._select_version(selected.version.parent_id)

    def action_head(self) -> None:
        if self.history_model and self.history_model.head_id:
            self._select_version(self.history_model.head_id)

    def action_refresh(self) -> None:
        self._set_message("Refreshing history...")
        self.refresh_workspace(include_dirty=True)

    def action_tab(self, tab_id: str) -> None:
        self.query_one("#detail-tabs", TabbedContent).active = tab_id
        if self.screen.has_class("narrow"):
            self.screen.add_class("show-detail")

    def action_back(self) -> None:
        if self.screen.has_class("show-detail"):
            self.screen.remove_class("show-detail")
            self.query_one("#history-list", ListView).focus()

    def _filters_closed(self, query: presenter.HistoryQuery | None) -> None:
        if query is not None:
            self.history_query = query
            self.refresh_workspace(include_dirty=False)

    def _branch_closed(self, branch: str | None) -> None:
        if branch is not None and branch != self.history_query.branch:
            self.history_query = replace(self.history_query, branch=branch)
            self.selected_version_id = None
            self.refresh_workspace(include_dirty=False)

    async def _rebuild_history(self) -> None:
        assert self.history_model is not None
        history = self.query_one("#history-list", ListView)
        preferred = self.selected_version_id or self.history_model.head_id
        await history.clear()
        if not self.history_model.rows:
            message = (
                f"No versions match the requested filters on "
                f"'{self.history_model.branch}'."
                if self.history_model.filters
                else f"No versions yet on '{self.history_model.branch}'."
            )
            await history.append(ListItem(Static(message), disabled=True))
            self.selected_version_id = None
            self.query_one("#detail-title", Label).update("No version selected")
            self._clear_detail()
            return

        items = [
            HistoryItem(row, show_graph=self.show_graph)
            for row in self.history_model.rows
        ]
        await history.extend(items)
        identifiers = [row.version.id for row in self.history_model.rows]
        history.index = identifiers.index(preferred) if preferred in identifiers else 0
        search = self.query_one("#search-input", Input)
        if search.has_class("visible"):
            search.focus()
        else:
            history.focus()

    def _update_status_line(self) -> None:
        assert self.history_model is not None and self.status_report is not None
        dirty = (
            "dirty"
            if self.status_report.dirty
            else "clean"
            if self.status_report.dirty is False
            else "unchecked"
        )
        open_turn = " | open turn" if self.status_report.open_turn else ""
        filters = (
            f" | filters: {', '.join(self.history_model.filters)}"
            if self.history_model.filters
            else ""
        )
        omitted = (
            f" | {self.history_model.omitted} side paths omitted"
            if self.history_model.path_mode
            else ""
        )
        result_count = (
            f" | {len(self.history_model.rows)}/{self.history_model.total_visible} shown"
        )
        filtered = (
            f" | {self.history_model.filtered_out} filtered"
            if self.history_model.filtered_out
            else ""
        )
        self.query_one("#status-line", Static).update(
            f"{self.repo_root.name} | Git: {self.status_report.branch} | "
            f"viewing: {self.history_model.branch} | "
            f"HEAD: {self.history_model.head_id or '-'} | {dirty}{open_turn}"
            f"{result_count}{filtered}{filters}{omitted}"
        )

    def _select_version(self, version_id: str) -> None:
        if self.history_model is None:
            return
        for index, row in enumerate(self.history_model.rows):
            if row.version.id == version_id:
                self.query_one("#history-list", ListView).index = index
                return
        self._set_message(f"{version_id} is not visible in this view.")

    def _write_log(self, identifier: str, content: str | Text) -> None:
        log = self.query_one(f"#{identifier}", RichLog)
        log.clear()
        log.write(content)
        log.scroll_home(animate=False)

    def _clear_detail(self) -> None:
        for identifier in (
            "overview-content",
            "prompt-content",
            "thinking-content",
            "response-content",
            "diff-content",
            "tests-content",
        ):
            self.query_one(f"#{identifier}", RichLog).clear()

    def _set_message(self, message: str) -> None:
        self.query_one("#message-line", Static).update(message)

    def _set_responsive_classes(self, width: int) -> None:
        self.screen.set_class(width < 80, "narrow")
        self.screen.set_class(80 <= width < 100, "medium")
        if width >= 80:
            self.screen.remove_class("show-detail")


def _styled_diff(diff: str) -> Text:
    text = Text()
    for line in diff.splitlines():
        style = None
        if line.startswith("diff --git") or line.startswith("index "):
            style = "bold #8fd3c1"
        elif line.startswith("@@"):
            style = "bold #f5c451"
        elif line.startswith("+") and not line.startswith("+++"):
            style = "#8fe3a8"
        elif line.startswith("-") and not line.startswith("---"):
            style = "#ff9c91"
        text.append(line + "\n", style=style)
    return text


def run(repo_root: Path) -> None:
    ProFactApp(repo_root).run()