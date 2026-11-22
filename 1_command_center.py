#!/usr/bin/env python3
"""tmux COMMAND CENTER - concept 1

Tree explorer of every session/window/pane with a live preview of the
selected pane, a details card, and a command bar that types into the pane.

Keys:  n new session   w new window   s / v split horiz / vert
       e rename        x kill         a attach (exits TUI)
       / focus command bar           r refresh       q quit
"""
from __future__ import annotations

import os
import sys

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Static, Tree
from textual.widgets.tree import TreeNode

import tmuxlib as tm
from common import ConfirmScreen, PromptScreen


class CommandCenter(App):
    TITLE = "tmux Command Center"
    CSS = """
    #left { width: 38%; min-width: 30; border: round $primary; }
    #left Tree { background: transparent; }
    #right { width: 1fr; }
    #details { height: auto; max-height: 9; border: round $secondary; padding: 0 1; }
    #preview { height: 1fr; border: round $accent; padding: 0 1; overflow-y: auto; }
    #cmd { dock: bottom; margin: 0 1; }
    .title { color: $text-muted; }
    """
    BINDINGS = [
        ("n", "new_session", "New session"),
        ("w", "new_window", "New window"),
        ("s", "split(False)", "Split ⇆"),
        ("v", "split(True)", "Split ⇅"),
        ("e", "rename", "Rename"),
        ("x", "kill", "Kill"),
        ("a", "attach", "Attach"),
        ("slash", "focus_cmd", "Command"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.sessions: list[tm.Session] = []
        self._sig = None
        self.selected: tuple[str, object] | None = None
        self.attach_to: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left"):
                tree: Tree = Tree("tmux server", id="tree")
                tree.show_root = False
                yield tree
            with Vertical(id="right"):
                yield Static("Select a session, window or pane.", id="details")
                yield Static("", id="preview")
        yield Input(placeholder="/ type a command, Enter sends it to the selected pane",
                    id="cmd")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#left").border_title = "Sessions"
        self.query_one("#details").border_title = "Details"
        self.query_one("#preview").border_title = "Live preview"
        self.refresh_tree()
        self.set_interval(3, self.refresh_tree)
        self.set_interval(1, self.refresh_preview)
        self.query_one(Tree).focus()

    # ------------------------------------------------------------ tree
    def refresh_tree(self) -> None:
        try:
            self.sessions = tm.snapshot()
        except tm.TmuxError as exc:
            self.notify(str(exc), severity="error")
            return
        sig = [(s.name, s.attached, [(w.index, w.name, w.active,
                [(p.id, p.command, p.active) for p in w.panes])
                for w in s.windows]) for s in self.sessions]
        if sig == self._sig:
            return
        self._sig = sig
        tree = self.query_one(Tree)
        expanded = {n.data[1] for n in self._walk(tree.root)
                    if n.data and n.is_expanded}
        cursor = tree.cursor_node.data if tree.cursor_node else None
        tree.clear()
        restore: TreeNode | None = None
        if not self.sessions:
            tree.root.add_leaf("[dim]no tmux server - press n to start one[/]")
        for s in self.sessions:
            badge = "[green]●[/]" if s.attached else "[dim]○[/]"
            sn = tree.root.add(f"{badge} [b]{s.name}[/]  [dim]{len(s.windows)}w "
                               f"{s.pane_count}p[/]", data=("session", s.name))
            if s.name in expanded or not expanded:
                sn.expand()
            for w in s.windows:
                star = "[yellow]*[/]" if w.active else " "
                wn = sn.add(f"{star}{w.index}: {w.name}", data=("window", w.target))
                if w.target in expanded:
                    wn.expand()
                for p in w.panes:
                    act = "[cyan]▸[/]" if p.active else " "
                    pn = wn.add_leaf(f"{act}{p.index} [b]{p.command}[/] "
                                     f"[dim]{p.width}x{p.height}[/]",
                                     data=("pane", p.id))
                    if cursor and cursor[1] == p.id:
                        restore = pn
                if cursor and cursor[1] == w.target:
                    restore = wn
            if cursor and cursor[1] == s.name:
                restore = sn
        if restore is not None:
            self.call_after_refresh(tree.move_cursor, restore)

    def _walk(self, node: TreeNode):
        for child in node.children:
            yield child
            yield from self._walk(child)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data:
            self.selected = event.node.data
            self.show_details()
            self.refresh_preview()

    # ---------------------------------------------------- lookups
    def _find(self):
        if not self.selected:
            return None, None, None
        kind, key = self.selected
        for s in self.sessions:
            if kind == "session" and s.name == key:
                return s, None, None
            for w in s.windows:
                if kind == "window" and w.target == key:
                    return s, w, None
                for p in w.panes:
                    if kind == "pane" and p.id == key:
                        return s, w, p
        return None, None, None

    def _pane_target(self) -> str | None:
        s, w, p = self._find()
        if p:
            return p.id
        if w:
            return next((x.id for x in w.panes if x.active), w.target)
        if s:
            return s.name
        return None

    def show_details(self) -> None:
        s, w, p = self._find()
        box = self.query_one("#details", Static)
        if p:
            box.update(
                f"[b]Pane {p.id}[/]  in {s.name}:{w.index}\n"
                f"command  [b cyan]{p.command}[/]   pid {p.pid}\n"
                f"path     {p.path}\n"
                f"size     {p.width} x {p.height}   title: {p.title}")
        elif w:
            box.update(
                f"[b]Window {w.target}[/]  “{w.name}”\n"
                f"panes    {len(w.panes)}\n"
                f"layout   [dim]{w.layout}[/]")
        elif s:
            box.update(
                f"[b]Session {s.name}[/] · {'[green]attached[/]' if s.attached else 'detached'}\n"
                f"windows  {len(s.windows)}   panes {s.pane_count}\n"
                f"age      {s.age}   idle {s.idle}")

    @work(thread=True, exclusive=True)
    def refresh_preview(self) -> None:
        target = self._pane_target()
        text = tm.capture(target, 120) if target else ""
        lines = text.rstrip("\n").splitlines()[-60:]
        from rich.text import Text
        self.call_from_thread(self.query_one("#preview", Static).update,
                              Text("\n".join(lines)))

    # ------------------------------------------------------ actions
    def _safe(self, fn, *a) -> bool:
        try:
            fn(*a)
            return True
        except tm.TmuxError as exc:
            self.notify(str(exc), severity="error")
            return False

    def action_refresh(self) -> None:
        self._sig = None
        self.refresh_tree()

    def action_new_session(self) -> None:
        def done(name):
            if name and self._safe(tm.new_session, name, os.getcwd()):
                self.notify(f"Created session {name}")
                self.action_refresh()
        self.push_screen(PromptScreen("New session name:"), done)

    def action_new_window(self) -> None:
        s, _, _ = self._find()
        if not s:
            return self.notify("Select a session first", severity="warning")
        def done(name):
            if self._safe(tm.new_window, s.name, name):
                self.action_refresh()
        self.push_screen(PromptScreen(f"New window in {s.name} (blank = default):"), done)

    def action_split(self, vertical: bool) -> None:
        t = self._pane_target()
        if t and self._safe(tm.split_pane, t, vertical):
            self.action_refresh()

    def action_rename(self) -> None:
        s, w, p = self._find()
        if w and not p:
            self.push_screen(PromptScreen("Rename window:", w.name),
                             lambda n: n and self._safe(tm.rename_window, w.target, n)
                             and self.action_refresh())
        elif s and not w:
            def done(n):
                if n and self._safe(tm.rename_session, s.name, n):
                    self.selected = ("session", n)
                    self.action_refresh()
            self.push_screen(PromptScreen("Rename session:", s.name), done)
        else:
            self.notify("Select a session or window to rename", severity="warning")

    def action_kill(self) -> None:
        if not self.selected:
            return
        kind, key = self.selected
        def done(ok):
            if ok and self._safe(tm.kill, kind, key):
                self.notify(f"Killed {kind} {key}")
                self.action_refresh()
        self.push_screen(ConfirmScreen(f"Kill {kind} [b]{key}[/]?"), done)

    def action_attach(self) -> None:
        s, _, _ = self._find()
        if s:
            self.attach_to = s.name
            self.exit()

    def action_focus_cmd(self) -> None:
        self.query_one("#cmd", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        t = self._pane_target()
        if t and event.value and self._safe(tm.send_keys, t, event.value):
            self.notify(f"→ {t}: {event.value}")
        event.input.value = ""
        self.query_one(Tree).focus()


if __name__ == "__main__":
    if not tm.tmux_available():
        sys.exit("tmux is not installed.")
    app = CommandCenter()
    app.run()
    if app.attach_to:
        os.execvp("tmux", tm.switch_or_attach_cmd(app.attach_to))
