#!/usr/bin/env python3
"""tmux RUNBOOK - concept 6  (embeddable widget + standalone app)

A library of saved commands, grouped by category. Run one into a new tmux
window (it stays open so you can read the output) and the run history tracks
whether it's still running, passed, or failed, plus how long it took.

Use {{name}} in a command to be asked for a value each run, e.g.
    ssh {{host}}        git checkout {{branch}}

Keys:  enter  run selected        n  new command     e  edit
       d  delete                  t  pick target session
       g  go to the run's window (inside tmux)       q  quit (standalone)

Saved in ~/.config/tmux-dash/runbook.json · run results in …/runs/
"""
from __future__ import annotations

import json
import re
import shlex
import sys
import time
import uuid
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             Static, Tree)

import tmuxlib as tm
from common import DIALOG_CSS, ConfirmScreen, PromptScreen

BASE = Path.home() / ".config" / "tmux-dash"
BOOK = BASE / "runbook.json"
RUNS = BASE / "runs"
VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")

STARTER = [
    {"name": "Disk usage", "category": "System", "cwd": "~",
     "command": "df -h && du -sh ~/* 2>/dev/null | sort -h | tail -15"},
    {"name": "Update packages", "category": "System", "cwd": "~",
     "command": "sudo apt update && apt list --upgradable"},
    {"name": "Listening ports", "category": "Network", "cwd": "~",
     "command": "ss -tulpn"},
    {"name": "Ping host", "category": "Network", "cwd": "~",
     "command": "ping -c 5 {{host}}"},
    {"name": "Git status (all repos)", "category": "Dev", "cwd": "~",
     "command": "for d in */.git; do [ -e \"$d\" ] || continue; "
                "echo \"== ${d%/.git}\"; git -C \"${d%/.git}\" status -sb; done"},
    {"name": "Serve folder on :8000", "category": "Dev", "cwd": "~",
     "command": "python3 -m http.server 8000"},
]


def load_book() -> list[dict]:
    try:
        return json.loads(BOOK.read_text())
    except (OSError, json.JSONDecodeError):
        return [dict(x) for x in STARTER]


def save_book(book: list[dict]) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    BOOK.write_text(json.dumps(book, indent=2))


class EditScreen(ModalScreen[dict | None]):
    DEFAULT_CSS = DIALOG_CSS + """
    EditScreen { align: center middle; }
    EditScreen #dialog { width: 80; }
    EditScreen Input { margin-bottom: 1; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, entry: dict | None = None):
        super().__init__()
        self.entry = dict(entry or {"name": "", "category": "General",
                                    "cwd": "~", "command": ""})

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[b]Command[/]  (use {{var}} for values asked at run time)")
            yield Input(self.entry["name"], placeholder="Name", id="f-name")
            yield Input(self.entry["category"], placeholder="Category", id="f-cat")
            yield Input(self.entry["cwd"], placeholder="Run in folder", id="f-cwd")
            yield Input(self.entry["command"], placeholder="Shell command", id="f-cmd")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def _collect(self) -> dict | None:
        e = {k: self.query_one(f"#f-{f}", Input).value.strip()
             for k, f in [("name", "name"), ("category", "cat"),
                          ("cwd", "cwd"), ("command", "cmd")]}
        if not e["name"] or not e["command"]:
            self.notify("Name and command are required", severity="error")
            return None
        e["category"] = e["category"] or "General"
        e["cwd"] = e["cwd"] or "~"
        return e

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            if (e := self._collect()) is not None:
                self.dismiss(e)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "f-cmd" and (e := self._collect()) is not None:
            self.dismiss(e)
        else:
            self.focus_next()

    def action_cancel(self) -> None:
        self.dismiss(None)


class Runbook(Widget):
    """Drop-in panel. Posts Runbook.RunFinished when a run completes."""

    PANEL_TITLE = "Runbook"
    DEFAULT_CSS = """
    Runbook { layout: vertical; }
    Runbook #rb-top { height: 1fr; }
    Runbook #rb-tree { width: 40; border: round $primary; }
    Runbook #rb-detail { width: 1fr; border: round $secondary; padding: 0 1; }
    Runbook #rb-runs { height: 12; border: round $accent; }
    """
    BINDINGS = [
        ("enter", "run", "Run"),
        ("n", "new", "New"),
        ("e", "edit", "Edit"),
        ("d", "delete", "Delete"),
        ("t", "target", "Target"),
        ("g", "goto", "Go to run"),
    ]

    class RunFinished(Message):
        def __init__(self, run: dict):
            super().__init__()
            self.run = run

    def __init__(self, **kw):
        super().__init__(**kw)
        self.book = load_book()
        self.runs: list[dict] = []
        self.target: str | None = None
        self.sel: int | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="rb-top"):
            tree: Tree = Tree("Runbook", id="rb-tree")
            tree.show_root = False
            yield tree
            yield Static("", id="rb-detail")
        yield DataTable(id="rb-runs", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        RUNS.mkdir(parents=True, exist_ok=True)
        if not BOOK.exists():
            save_book(self.book)
        runs = self.query_one("#rb-runs", DataTable)
        for label, w in [("Started", 9), ("Command", 24), ("Window", 18),
                         ("Status", 12), ("Took", 7)]:
            runs.add_column(label, width=w, key=label)
        self.query_one("#rb-tree").border_title = "Commands"
        self.query_one("#rb-detail").border_title = "Details"
        self.query_one("#rb-runs").border_title = "Run history"
        self._render_tree()
        self.set_interval(1.5, self.poll_runs)

    # -------------------------------------------------------------- render
    def _render_tree(self, select: int | None = None) -> None:
        tree = self.query_one(Tree)
        tree.clear()
        cats: dict[str, list[int]] = {}
        for i, e in enumerate(self.book):
            cats.setdefault(e.get("category", "General"), []).append(i)
        target_node = None
        for cat in sorted(cats):
            cn = tree.root.add(f"[b]{cat}[/] [dim]{len(cats[cat])}[/]", expand=True)
            for i in cats[cat]:
                e = self.book[i]
                vars_ = "[magenta] {…}[/]" if VAR.search(e["command"]) else ""
                leaf = cn.add_leaf(f"{e['name']}{vars_}", data=i)
                if i == select:
                    target_node = leaf
        if target_node:
            self.call_after_refresh(tree.move_cursor, target_node)
        self._render_detail()

    def _render_detail(self) -> None:
        box = self.query_one("#rb-detail", Static)
        tgt = self.target or self._default_session() or "a new session"
        if self.sel is None or self.sel >= len(self.book):
            box.update(f"Pick a command on the left.\n\nRuns go to: [b]{tgt}[/]  (t to change)")
            return
        e = self.book[self.sel]
        vars_ = sorted(set(VAR.findall(e["command"])))
        box.update(Text.from_markup(
            f"[b]{e['name']}[/]   [dim]{e.get('category')}[/]\n\n"
            f"folder   {e.get('cwd', '~')}\n"
            f"asks for {', '.join(vars_) if vars_ else '[dim]nothing[/]'}\n"
            f"runs in  new window in [b]{tgt}[/]  [dim](t to change)[/]\n\n"
            f"[dim]command[/]\n") + Text(e["command"], style="bold cyan"))

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self.sel = event.node.data if isinstance(event.node.data, int) else None
        self._render_detail()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if isinstance(event.node.data, int):
            self.action_run()

    # ----------------------------------------------------------------- runs
    def _default_session(self) -> str | None:
        try:
            ss = tm.snapshot()
        except tm.TmuxError:
            return None
        attached = [s for s in ss if s.attached]
        return (attached or ss or [None])[0] and (attached or ss)[0].name

    def action_run(self) -> None:
        if self.sel is None:
            return
        e = self.book[self.sel]
        names = list(dict.fromkeys(VAR.findall(e["command"])))
        values: dict[str, str] = {}

        def ask(i: int) -> None:
            if i == len(names):
                self._launch(e, VAR.sub(lambda m: values[m.group(1)], e["command"]))
                return

            def got(v):
                if v is None:
                    return self.notify("Run cancelled", severity="warning")
                values[names[i]] = v
                ask(i + 1)
            self.app.push_screen(PromptScreen(f"Value for [b]{names[i]}[/]:"), got)
        ask(0)

    def _launch(self, e: dict, command: str) -> None:
        run_id = uuid.uuid4().hex[:8]
        status = RUNS / f"{run_id}.exit"
        cwd = str(Path(e.get("cwd") or "~").expanduser())
        # Run, record the exit code, then keep a shell open to read output.
        wrapped = (f"printf '\\033[1;36m▶ %s\\033[0m\\n' {shlex.quote(command)}; "
                   f"{command}; ec=$?; echo $ec > {shlex.quote(str(status))}; "
                   f"printf '\\n\\033[1m■ exited %s\\033[0m\\n' $ec; exec \"${{SHELL:-sh}}\"")
        session = self.target or self._default_session()
        wname = re.sub(r"[^\w-]", "", e["name"].replace(" ", "-"))[:16] or "run"
        try:
            if session:
                win = tm.run("new-window", "-d", "-P", "-F", "#{session_name}:#{window_index}",
                             "-t", f"{session}:", "-n", wname, "-c", cwd,
                             "sh", "-c", wrapped).strip()
            else:
                tm.run("new-session", "-d", "-s", "runbook", "-n", wname, "-c", cwd,
                       "sh", "-c", wrapped)
                win, self.target = "runbook:0", "runbook"
        except tm.TmuxError as exc:
            self.notify(str(exc), severity="error")
            return
        run = dict(id=run_id, name=e["name"], window=win, started=time.time(),
                   status="running", took=None)
        self.runs.append(run)
        self.query_one("#rb-runs", DataTable).add_row(
            time.strftime("%H:%M:%S"), e["name"], win, Text("● running", style="yellow"),
            "", key=run_id)
        self.notify(f"Started “{e['name']}” in {win}")

    def poll_runs(self) -> None:
        t = self.query_one("#rb-runs", DataTable)
        for run in self.runs:
            elapsed = time.time() - run["started"]
            if run["status"] != "running":
                continue
            f = RUNS / f"{run['id']}.exit"
            if f.exists():
                try:
                    code = int(f.read_text().strip() or "1")
                except ValueError:
                    code = 1
                f.unlink(missing_ok=True)
                run["status"] = "ok" if code == 0 else f"failed ({code})"
                run["took"] = elapsed
                t.update_cell(run["id"], "Status",
                              Text("✔ ok", style="green") if code == 0
                              else Text(f"✖ exit {code}", style="bold red"))
                self.post_message(self.RunFinished(run))
            t.update_cell(run["id"], "Took", tm.humanize_seconds(elapsed))

    # -------------------------------------------------------------- library
    def action_new(self) -> None:
        def done(e):
            if e:
                self.book.append(e)
                save_book(self.book)
                self._render_tree(select=len(self.book) - 1)
        self.app.push_screen(EditScreen(), done)

    def action_edit(self) -> None:
        if self.sel is None:
            return
        idx = self.sel

        def done(e):
            if e:
                self.book[idx] = e
                save_book(self.book)
                self._render_tree(select=idx)
        self.app.push_screen(EditScreen(self.book[idx]), done)

    def action_delete(self) -> None:
        if self.sel is None:
            return
        idx = self.sel

        def done(ok):
            if ok:
                self.book.pop(idx)
                save_book(self.book)
                self.sel = None
                self._render_tree()
        self.app.push_screen(ConfirmScreen(f"Delete [b]{self.book[idx]['name']}[/]?"),
                             done)

    def action_target(self) -> None:
        def done(name):
            if name:
                self.target = name
                self._render_detail()
                self.notify(f"Runs will open in session “{name}”")
        cur = self.target or self._default_session() or ""
        self.app.push_screen(PromptScreen(
            "Session to run commands in (created if missing):", cur), done)

    def action_goto(self) -> None:
        t = self.query_one("#rb-runs", DataTable)
        if not t.row_count:
            return
        rid = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        run = next(r for r in self.runs if r["id"] == rid)
        if not __import__("os").environ.get("TMUX"):
            return self.notify(f"Attach with: tmux attach -t {run['window']}")
        tm.run("switch-client", "-t", run["window"], check=False)


class RunbookApp(App):
    TITLE = "tmux Runbook"
    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Runbook()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#rb-tree").focus()

    def on_runbook_run_finished(self, msg: Runbook.RunFinished) -> None:
        r = msg.run
        self.notify(f"{r['name']}: {r['status']}",
                    severity="information" if r["status"] == "ok" else "error")


if __name__ == "__main__":
    if not tm.tmux_available():
        sys.exit("tmux is not installed.")
    RunbookApp().run()
