"""Shared Textual widgets: modal prompt + confirm dialogs."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

DIALOG_CSS = """
PromptScreen, ConfirmScreen { align: center middle; }
#dialog {
    width: 60; height: auto; padding: 1 2;
    border: thick $accent; background: $surface;
}
#dialog Label { width: 100%; margin-bottom: 1; }
#dialog Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
#dialog Button { margin-left: 1; }
"""


class PromptScreen(ModalScreen[str | None]):
    """Ask for a line of text. Dismisses with the string or None."""

    DEFAULT_CSS = DIALOG_CSS
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, question: str, default: str = "", placeholder: str = ""):
        super().__init__()
        self.question, self.default, self.placeholder = question, default, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question)
            yield Input(value=self.default, placeholder=self.placeholder, id="answer")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("OK", variant="primary", id="ok")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one(Input).value.strip() or None)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = DIALOG_CSS
    BINDINGS = [("escape", "no", "No"), ("y", "yes", "Yes"), ("n", "no", "No")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question)
            with Horizontal():
                yield Button("No", id="no")
                yield Button("Yes", variant="error", id="yes")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
