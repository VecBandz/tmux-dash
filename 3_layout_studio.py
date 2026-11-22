#!/usr/bin/env python3
"""tmux LAYOUT STUDIO - concept 3

A data manager for tmux *workspaces*:
  • Live      - pick a session/window, save it as a snapshot, apply layout presets
  • Vault     - browse saved snapshots (JSON), restore, rename, delete
  • Templates - write a workspace spec in JSON and spin it up as a new session

Snapshots live in ~/.config/tmux-dash/snapshots/
Templates live in ~/.config/tmux-dash/templates/

Keys:  ctrl+s save   enter restore/launch   d delete   q quit   tab switch panel
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from rich.syntax import Syntax
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (Button, Checkbox, Footer, Header, Label, ListItem,
                             ListView, Static, TabbedContent, TabPane, TextArea)

import tmuxlib as tm
from common import ConfirmScreen, PromptScreen

TPL_DIR = Path.home() / ".config" / "tmux-dash" / "templates"

PRESETS = {
    "even-horizontal": "┌──┬──┬──┐\n│  │  │  │\n│  │  │  │\n└──┴──┴──┘",
    "even-vertical":   "┌────────┐\n├────────┤\n├────────┤\n└────────┘",
    "main-vertical":   "┌────┬───┐\n│    ├───┤\n│    ├───┤\n└────┴───┘",
    "main-horizontal": "┌────────┐\n│        │\n├──┬──┬──┤\n└──┴──┴──┘",
    "tiled":           "┌────┬───┐\n├────┼───┤\n│    │   │\n└────┴───┘",
}

STARTER_TEMPLATE = {
    "name": "dev",
    "windows": [
        {"name": "editor", "layout": "main-vertical",
         "panes": [{"path": "~", "command": ""},
                   {"path": "~", "command": "htop"},
                   {"path": "~", "command": ""}]},
        {"name": "logs", "layout": "even-horizontal",
         "panes": [{"path": "/var/log", "command": "ls -la"},
                   {"path": "~", "command": ""}]},
    ],
}


def expand_paths(spec: dict) -> dict:
    for w in spec.get("windows", []):
        for p in w.get("panes", []):
            if p.get("path"):
                p["path"] = str(Path(p["path"]).expanduser())
    return spec


class LayoutStudio(App):
    TITLE = "tmux Layout Studio"
    CSS = """
    TabbedContent { height: 1fr; }
    .col-list { width: 36; border: round $primary; }
    .col-main { width: 1fr; border: round $secondary; padding: 0 1; }
    ListView { background: transparent; }
    #presets { height: auto; }
    .preset { width: 19; height: 7; margin-right: 1; }
    .row { height: auto; margin-top: 1; }
    .row Button { margin-right: 1; }
    #tpl-editor { height: 1fr; }
    #tpl-name { height: 1; color: $accent; }
    """
    BINDINGS = [
        ("ctrl+s", "save", "Save"),
        ("d", "delete", "Delete"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.sessions: list[tm.Session] = []
        self.snaps: list[tuple[Path, dict]] = []
        self.tpls: list[Path] = []
        self.live_sel: tm.Session | None = None
        self.snap_sel: tuple[Path, dict] | None = None
        self.tpl_sel: Path | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="live"):
            with TabPane("Live", id="live"):
                with Horizontal():
                    yield ListView(id="live-list", classes="col-list")
                    with VerticalScroll(classes="col-main", id="live-main"):
                        yield Static("Pick a session on the left.", id="live-info")
                        yield Label("\n[b]Apply layout preset to the active window[/]")
                        with Horizontal(id="presets"):
                            for name, art in PRESETS.items():
                                yield Button(f"{art}\n{name}", id=f"preset-{name}",
                                             classes="preset")
                        with Horizontal(classes="row"):
                            yield Button("Save snapshot  ctrl+s", variant="primary",
                                         id="btn-save")
                            yield Button("Save as template", id="btn-save-tpl")
            with TabPane("Vault", id="vault"):
                with Horizontal():
                    yield ListView(id="snap-list", classes="col-list")
                    with Vertical(classes="col-main"):
                        yield VerticalScroll(Static("", id="snap-view"))
                        yield Checkbox("Re-run saved commands (htop, vim, …) on restore",
                                       id="rerun")
                        with Horizontal(classes="row"):
                            yield Button("Restore", variant="success", id="btn-restore")
                            yield Button("Rename", id="btn-rename")
                            yield Button("Delete", variant="error", id="btn-delete")
            with TabPane("Templates", id="templates"):
                with Horizontal():
                    yield ListView(id="tpl-list", classes="col-list")
                    with Vertical(classes="col-main"):
                        yield Static("", id="tpl-name")
                        yield TextArea("", language="json", id="tpl-editor",
                                       show_line_numbers=True)
                        with Horizontal(classes="row"):
                            yield Button("Launch session", variant="success",
                                         id="btn-launch")
                            yield Button("Save  ctrl+s", variant="primary", id="btn-tpl-save")
                            yield Button("New template", id="btn-tpl-new")
                            yield Button("Delete", variant="error", id="btn-tpl-del")
        yield Footer()

    def on_mount(self) -> None:
        TPL_DIR.mkdir(parents=True, exist_ok=True)
        if not any(TPL_DIR.glob("*.json")):
            (TPL_DIR / "dev.json").write_text(json.dumps(STARTER_TEMPLATE, indent=2))
        self.query_one("#live-list").border_title = "Running sessions"
        self.query_one("#snap-list").border_title = "Snapshots"
        self.query_one("#tpl-list").border_title = "Templates"
        self.action_refresh()
        self.set_interval(4, self.load_live)

    # ------------------------------------------------------------ loaders
    def action_refresh(self) -> None:
        self.load_live()
        self.load_snaps()
        self.load_tpls()

    def _fill(self, lv_id: str, labels: list[str], keep: int | None) -> None:
        lv = self.query_one(lv_id, ListView)
        lv.clear()
        for text in labels:
            lv.append(ListItem(Label(text)))
        if labels:
            lv.index = min(keep or 0, len(labels) - 1)

    def load_live(self) -> None:
        old = [s.name for s in self.sessions]
        try:
            self.sessions = tm.snapshot()
        except tm.TmuxError as exc:
            self.notify(str(exc), severity="error")
            return
        if [s.name for s in self.sessions] == old and old:
            self._show_live()  # refresh details only
            return
        keep = self.query_one("#live-list", ListView).index
        self._fill("#live-list", [
            f"{'[green]●[/]' if s.attached else '○'} {s.name}  "
            f"[dim]{len(s.windows)}w/{s.pane_count}p[/]" for s in self.sessions] or
            ["[dim]no sessions running[/]"], keep)

    def load_snaps(self) -> None:
        keep = self.query_one("#snap-list", ListView).index
        self.snaps = tm.list_snapshots()
        self._fill("#snap-list", [
            f"[b]{d.get('label', d['name'])}[/]\n[dim]"
            f"{time.strftime('%b %d %H:%M', time.localtime(d.get('saved_at', 0)))} · "
            f"{len(d.get('windows', []))} windows[/]" for _, d in self.snaps] or
            ["[dim]vault is empty - save from the Live tab[/]"], keep)
        if not self.snaps:
            self.snap_sel = None
            self.query_one("#snap-view", Static).update("")

    def load_tpls(self, select: Path | None = None) -> None:
        keep = self.query_one("#tpl-list", ListView).index
        self.tpls = sorted(TPL_DIR.glob("*.json"))
        if select and select in self.tpls:
            keep = self.tpls.index(select)
        self._fill("#tpl-list", [p.stem for p in self.tpls] or ["[dim]none[/]"], keep)

    # --------------------------------------------------------- selection
    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is None:
            return
        lid = event.list_view.id
        if lid == "live-list" and idx < len(self.sessions):
            self.live_sel = self.sessions[idx]
            self._show_live()
        elif lid == "snap-list" and idx < len(self.snaps):
            self.snap_sel = self.snaps[idx]
            body = json.dumps(self.snap_sel[1], indent=2)
            self.query_one("#snap-view", Static).update(
                Syntax(body, "json", theme="monokai", line_numbers=False))
        elif lid == "tpl-list" and idx < len(self.tpls):
            self.tpl_sel = self.tpls[idx]
            self.query_one("#tpl-name", Static).update(f"editing {self.tpl_sel.name}")
            self.query_one("#tpl-editor", TextArea).text = self.tpl_sel.read_text()

    def _show_live(self) -> None:
        s = self.live_sel and next((x for x in self.sessions
                                    if x.name == self.live_sel.name), None)
        if not s:
            return
        self.live_sel = s
        lines = [f"[b]{s.name}[/] · {'attached' if s.attached else 'detached'}  "
                 f"[dim]age {s.age} · idle {s.idle}[/]\n"]
        for w in s.windows:
            lines.append(f"{'[yellow]*[/]' if w.active else ' '} [b]{w.index}:{w.name}[/]"
                         f"  [dim]{len(w.panes)} panes[/]")
            for p in w.panes:
                lines.append(f"     {p.id:<5} [cyan]{p.command:<10}[/] [dim]{p.path}[/]")
        self.query_one("#live-info", Static).update("\n".join(lines))

    def _active_window(self) -> tm.Window | None:
        if not self.live_sel:
            return None
        return next((w for w in self.live_sel.windows if w.active),
                    self.live_sel.windows[0] if self.live_sel.windows else None)

    # ------------------------------------------------------------ actions
    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("preset-"):
            w = self._active_window()
            if not w:
                return self.notify("Pick a session first", severity="warning")
            try:
                tm.select_layout(w.target, bid.removeprefix("preset-"))
                self.notify(f"{w.target} → {bid.removeprefix('preset-')}")
            except tm.TmuxError as exc:
                self.notify(str(exc), severity="error")
            return
        handler = {
            "btn-save": self.save_snapshot, "btn-save-tpl": self.save_as_template,
            "btn-restore": self.restore, "btn-rename": self.rename_snap,
            "btn-delete": self.action_delete, "btn-launch": self.launch_tpl,
            "btn-tpl-save": self.save_tpl, "btn-tpl-new": self.new_tpl,
            "btn-tpl-del": self.action_delete,
        }.get(bid)
        if handler:
            handler()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "snap-list":
            self.restore()
        elif event.list_view.id == "tpl-list":
            self.query_one("#tpl-editor").focus()

    def _tab(self) -> str:
        return self.query_one(TabbedContent).active

    def action_save(self) -> None:
        {"live": self.save_snapshot, "templates": self.save_tpl}.get(self._tab(),
                                                                      lambda: None)()

    def action_delete(self) -> None:
        tab = self._tab()
        if tab == "vault" and self.snap_sel:
            path = self.snap_sel[0]
        elif tab == "templates" and self.tpl_sel:
            path = self.tpl_sel
        else:
            return
        def done(ok):
            if ok:
                path.unlink(missing_ok=True)
                self.notify(f"Deleted {path.name}")
                self.load_snaps() if tab == "vault" else self.load_tpls()
        self.push_screen(ConfirmScreen(f"Delete {path.name}?"), done)

    # live
    def save_snapshot(self) -> None:
        s = self.live_sel
        if not s:
            return self.notify("Pick a session first", severity="warning")
        def done(label):
            if label:
                p = tm.save_snapshot(s, label)
                self.notify(f"Saved {p.name}")
                self.load_snaps()
        self.push_screen(PromptScreen("Snapshot label:", s.name), done)

    def save_as_template(self) -> None:
        s = self.live_sel
        if not s:
            return self.notify("Pick a session first", severity="warning")
        def done(name):
            if name:
                spec = tm.session_to_spec(s)
                spec.pop("saved_at", None)
                spec["name"] = name
                path = TPL_DIR / f"{name}.json"
                path.write_text(json.dumps(spec, indent=2))
                self.load_tpls(select=path)
                self.query_one(TabbedContent).active = "templates"
        self.push_screen(PromptScreen("Template name:", s.name), done)

    # vault
    def restore(self) -> None:
        if not self.snap_sel:
            return
        rerun = self.query_one("#rerun", Checkbox).value
        try:
            name = tm.restore_spec(self.snap_sel[1], rerun_commands=rerun)
            self.notify(f"Restored as session “{name}”")
            self.load_live()
        except tm.TmuxError as exc:
            self.notify(str(exc), severity="error")

    def rename_snap(self) -> None:
        if not self.snap_sel:
            return
        path, data = self.snap_sel
        def done(label):
            if label:
                data["label"] = label
                path.write_text(json.dumps(data, indent=2))
                self.load_snaps()
        self.push_screen(PromptScreen("New label:", data.get("label", "")), done)

    # templates
    def _editor_spec(self) -> dict | None:
        try:
            spec = json.loads(self.query_one("#tpl-editor", TextArea).text)
            assert isinstance(spec.get("windows"), list), "'windows' must be a list"
            return spec
        except (json.JSONDecodeError, AssertionError, AttributeError) as exc:
            self.notify(f"Invalid template: {exc}", severity="error")
            return None

    def save_tpl(self) -> None:
        if not self.tpl_sel or (spec := self._editor_spec()) is None:
            return
        self.tpl_sel.write_text(json.dumps(spec, indent=2))
        self.notify(f"Saved {self.tpl_sel.name}")

    def new_tpl(self) -> None:
        def done(name):
            if name:
                path = TPL_DIR / f"{name}.json"
                spec = dict(STARTER_TEMPLATE, name=name)
                path.write_text(json.dumps(spec, indent=2))
                self.load_tpls(select=path)
        self.push_screen(PromptScreen("Template name:"), done)

    def launch_tpl(self) -> None:
        if (spec := self._editor_spec()) is None:
            return
        try:
            name = tm.restore_spec(expand_paths(spec), rerun_commands=True)
            self.notify(f"Launched session “{name}”")
            self.load_live()
        except tm.TmuxError as exc:
            self.notify(str(exc), severity="error")


if __name__ == "__main__":
    if not tm.tmux_available():
        sys.exit("tmux is not installed.")
    LayoutStudio().run()
