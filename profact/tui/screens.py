"""Modal and secondary screens used by the ProFact TUI."""

from __future__ import annotations

from dataclasses import replace

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, RichLog

from .presenter import HistoryQuery


class BranchItem(ListItem):
    def __init__(self, branch: str) -> None:
        super().__init__(Label(branch))
        self.branch = branch


class BranchScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel"), ("q", "cancel", "Cancel")]

    def __init__(self, branches: tuple[str, ...], current: str) -> None:
        super().__init__()
        self.branches = branches
        self.current = current

    def compose(self) -> ComposeResult:
        with Container(id="branch-dialog", classes="dialog"):
            yield Label("View captured branch", classes="dialog-title")
            yield Label(
                "This changes only the displayed history; it does not run git switch.",
                classes="dialog-note",
            )
            yield ListView(
                *(BranchItem(branch) for branch in self.branches),
                initial_index=(
                    self.branches.index(self.current)
                    if self.current in self.branches
                    else 0
                ),
                id="branch-options",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, BranchItem):
            self.dismiss(event.item.branch)

    def action_cancel(self) -> None:
        self.dismiss(None)


class FilterScreen(ModalScreen[HistoryQuery | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, query: HistoryQuery) -> None:
        super().__init__()
        self.history_query = query

    def compose(self) -> ComposeResult:
        with Container(id="filter-dialog", classes="dialog"):
            yield Label("Filter captured history", classes="dialog-title")
            with VerticalScroll(id="filter-fields"):
                yield Label("Tag")
                yield Input(value=self.history_query.tag or "", id="filter-tag")
                yield Label("Version type (prompt, human, checkpoint, anchor)")
                yield Input(value=self.history_query.version_type or "", id="filter-type")
                yield Label("Model contains")
                yield Input(value=self.history_query.model or "", id="filter-model")
                yield Label("Changed file or directory")
                yield Input(value=self.history_query.changed_path or "", id="filter-file")
                yield Label("Since (for example 7d or 2026-07-01)")
                yield Input(value=self.history_query.since or "", id="filter-since")
                yield Label("Test status (pass, fail, error, untested)")
                yield Input(value=self.history_query.test_status or "", id="filter-test")
            with Horizontal(classes="dialog-actions"):
                yield Button("Apply", variant="primary", id="apply-filter")
                yield Button("Clear", id="clear-filter")
                yield Button("Cancel", id="cancel-filter")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-filter":
            self.dismiss(None)
        elif event.button.id == "clear-filter":
            self.dismiss(
                replace(
                    self.history_query,
                    tag=None,
                    version_type=None,
                    model=None,
                    changed_path=None,
                    since=None,
                    test_status=None,
                )
            )
        elif event.button.id == "apply-filter":
            self.dismiss(
                replace(
                    self.history_query,
                    tag=self._value("filter-tag"),
                    version_type=self._value("filter-type"),
                    model=self._value("filter-model"),
                    changed_path=self._value("filter-file"),
                    since=self._value("filter-since"),
                    test_status=self._value("filter-test"),
                )
            )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _value(self, identifier: str) -> str | None:
        value = self.query_one(f"#{identifier}", Input).value.strip()
        return value or None


class TextScreen(Screen[None]):
    BINDINGS = [("escape", "close", "Back"), ("q", "close", "Back")]

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.screen_title = title
        self.content = content

    def compose(self) -> ComposeResult:
        yield Label(self.screen_title, id="secondary-title")
        yield RichLog(
            wrap=True,
            auto_scroll=False,
            markup=False,
            id="secondary-content",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#secondary-content", RichLog).write(self.content)

    def action_close(self) -> None:
        self.app.pop_screen()


HELP_TEXT = """ProFact TUI keyboard reference

Up/Down or j/k   Select a version
Enter            Focus or open version detail
1..6             Overview, Prompt, Thinking, Response, Diff, Tests
g                Toggle DAG gutter
p                Toggle HEAD ancestry mode
/                Search id, prompt summary, model, or tag
f                Advanced filters
b                View another captured branch (does not switch Git)
h                Open history health
u                Jump to selected version's parent
H                Jump to viewed branch HEAD
r                Refresh history and working-tree status
?                Open this help
Escape           Cancel search, return from detail, or close a screen
q                Quit from the main screen

Selecting a version never moves ProFact HEAD or changes files.
"""