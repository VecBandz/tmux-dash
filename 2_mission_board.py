#!/usr/bin/env python3
"""tmux MISSION BOARD - concept 2

An at-a-glance ops dashboard: KPI tiles, a busy-pane sparkline, a card per
session, and a sortable / filterable table of every pane. Mark several panes
and broadcast one command to all of them at once.

Keys:  space mark/unmark pane   m mark all visible   u unmark all
       b broadcast to marked     f focus filter       c capture pane to file
       1-5 sort by column        x kill pane          r refresh   q quit
"""
from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.widgets import DataTable, Footer, Header, Input, Sparkline, Static

import tmuxlib as tm
from common import ConfirmScreen, PromptScreen

COLUMNS = [("", 2), ("Session", 14), ("Win", 14), ("Pane", 6),
           ("Command", 12), ("Path", 36), ("Size", 9), ("PID", 8)]
SORT_KEYS = {
    "1": lambda p, w: p.session,
    "2": lambda p, w: (w.get(p.id, ""), p.index),
    "3": lambda p, w: p.command,
    "4": lambda p, w: p.path,
    "5": lambda p, w: -(p.width * p.height),
}


class Tile(Static):
    DEFAULT_CSS = """
    Tile { width: 1fr; height: 5; border: round $primary; content-align: center middle;
           text-align: center; }
    """

    def set(self, label: str, value, color: str = "white") -> None:
        self.update(f"[b {color}]{value}[/]\n[dim]{label}[/]")


class SessionCard(Static):
    DEFAULT_CSS = """
    SessionCard { width: 26; height: 7; border: round $secondary; padding: 0 1;
                  margin-right: 1; }
    SessionCard.attached { border: round $success; }
    """


class MissionBoard(App):
    TITLE = "tmux Mission Board"
    CSS = """
    #tiles { height: 5; }
    #spark-box { height: 5; border: round $accent; padding: 0 1; width: 2fr; }
    #spark { height: 2; }
    #cards { height: 7; margin: 0 0 0 0; }
    #filter { margin: 0 0; }
    #table { height: 1fr; border: round $primary; }
    #status { height: 1; color: $text-muted; padding: 0 1; }
    """
    BINDINGS = [
        ("space", "toggle_mark", "Mark"),
        ("m", "mark_all", "Mark all"),
        ("u", "unmark", "Unmark"),
        ("b", "broadcast", "Broadcast"),
        ("f", "focus_filter", "Filter"),
        ("c", "capture", "Capture→file"),
        ("x", "kill_pane", "Kill pane"),
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
        *[(k, f"sort('{k}')", "") for k in SORT_KEYS],
    ]

    def __init__(self):
        super().__init__()
        self.sessions: list[tm.Session] = []
        self.marked: set[str] = set()
        self.history: deque[float] = deque([0.0] * 40, maxlen=40)
        self.sort_key = "1"
        self.filter_text = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="tiles"):
            yield Tile(id="t-sessions")
            yield Tile(id="t-windows")
            yield Tile(id="t-panes")
            yield Tile(id="t-busy")
            with Vertical(id="spark-box"):
                yield Static("[dim]busy panes (non-shell) · last 2 min[/]")
                yield Sparkline(list(self.history), id="spark")
        yield HorizontalScroll(id="cards")
        yield Input(placeholder="f  filter panes by session / command / path…", id="filter")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        for name, width in COLUMNS:
            t.add_column(name, width=width, key=name or "mark")
        self.query_one("#table").border_title = "All panes"
        self.refresh_data()
        self.set_interval(3, self.refresh_data)
        t.focus()

    # ------------------------------------------------------------- data
    def refresh_data(self) -> None:
        try:
            self.sessions = tm.snapshot()
        except tm.TmuxError as exc:
            self.notify(str(exc), severity="error")
            return
        panes = tm.all_panes(self.sessions)
        live_ids = {p.id for p in panes}
        self.marked &= live_ids
        busy = [p for p in panes if p.command not in tm.SHELLS]
        n_win = sum(len(s.windows) for s in self.sessions)
        attached = sum(1 for s in self.sessions if s.attached)

        self.query_one("#t-sessions", Tile).set(f"sessions · {attached} attached",
                                                len(self.sessions), "green")
        self.query_one("#t-windows", Tile).set("windows", n_win, "cyan")
        self.query_one("#t-panes", Tile).set("panes", len(panes), "magenta")
        self.query_one("#t-busy", Tile).set("running jobs", len(busy), "yellow")
        self.history.append(float(len(busy)))
        self.query_one(Sparkline).data = list(self.history)

        self._render_cards()
        self._render_table(panes)

    def _render_cards(self) -> None:
        box = self.query_one("#cards", HorizontalScroll)
        box.remove_children()
        if not self.sessions:
            box.mount(Static("[dim]  No tmux server running. Start one with `tmux new -d`.[/]"))
            return
        for s in self.sessions:
            jobs = sorted({p.command for w in s.windows for p in w.panes
                           if p.command not in tm.SHELLS})
            card = SessionCard(
                f"[b]{s.name}[/] {'[green]● attached[/]' if s.attached else '[dim]○[/]'}\n"
                f"{len(s.windows)} win · {s.pane_count} panes\n"
                f"[dim]age {s.age} · idle {s.idle}[/]\n"
                f"[yellow]{', '.join(jobs)[:22] or '—'}[/]",
                classes="attached" if s.attached else "")
            box.mount(card)

    def _render_table(self, panes: list[tm.Pane]) -> None:
        t = self.query_one(DataTable)
        wname = {p.id: f"{p.window_index}:{w.name}"
                 for s in self.sessions for w in s.windows for p in w.panes}
        q = self.filter_text.lower()
        if q:
            panes = [p for p in panes if q in
                     f"{p.session} {wname.get(p.id,'')} {p.command} {p.path}".lower()]
        panes.sort(key=lambda p: SORT_KEYS[self.sort_key](p, wname))
        cursor_key = None
        if t.row_count:
            try:
                cursor_key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
            except Exception:
                cursor_key = None
        t.clear()
        for p in panes:
            busy = p.command not in tm.SHELLS
            t.add_row(
                Text("◆", style="bold magenta") if p.id in self.marked else "",
                Text(p.session, style="bold"),
                wname.get(p.id, ""),
                Text(p.id, style="cyan" if p.active else "dim"),
                Text(p.command, style="bold yellow" if busy else ""),
                p.path.replace(str(Path.home()), "~"),
                f"{p.width}x{p.height}",
                p.pid,
                key=p.id,
            )
        if cursor_key:
            try:
                t.move_cursor(row=t.get_row_index(cursor_key))
            except Exception:
                pass
        self.query_one("#status", Static).update(
            f"{len(panes)} shown · {len(self.marked)} marked · sort: key {self.sort_key}"
            f"{' · filter: ' + self.filter_text if q else ''}")

    def _current_pane(self) -> str | None:
        t = self.query_one(DataTable)
        if not t.row_count:
            return None
        return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value

    # ---------------------------------------------------------- actions
    def action_refresh(self) -> None:
        self.refresh_data()

    def action_sort(self, key: str) -> None:
        self.sort_key = key
        self.refresh_data()

    def action_toggle_mark(self) -> None:
        pid = self._current_pane()
        if pid:
            self.marked ^= {pid}
            self.refresh_data()
            self.query_one(DataTable).action_cursor_down()

    def action_mark_all(self) -> None:
        t = self.query_one(DataTable)
        self.marked |= {k.value for k in t.rows}
        self.refresh_data()

    def action_unmark(self) -> None:
        self.marked.clear()
        self.refresh_data()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.filter_text = event.value
            self.refresh_data()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(DataTable).focus()

    def action_broadcast(self) -> None:
        targets = sorted(self.marked) or ([self._current_pane()] if self._current_pane() else [])
        if not targets:
            return
        def done(cmd):
            if not cmd:
                return
            ok = 0
            for pid in targets:
                try:
                    tm.send_keys(pid, cmd)
                    ok += 1
                except tm.TmuxError as exc:
                    self.notify(f"{pid}: {exc}", severity="error")
            self.notify(f"Sent to {ok}/{len(targets)} panes: {cmd}")
        self.push_screen(PromptScreen(f"Command to send to {len(targets)} pane(s):"), done)

    def action_capture(self) -> None:
        pid = self._current_pane()
        if not pid:
            return
        out_dir = Path.home() / "tmux-captures"
        out_dir.mkdir(exist_ok=True)
        f = out_dir / f"pane{pid.strip('%')}-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        f.write_text(tm.capture(pid, 5000))
        self.notify(f"Saved {f}")

    def action_kill_pane(self) -> None:
        pid = self._current_pane()
        if not pid:
            return
        def done(ok):
            if ok:
                try:
                    tm.kill("pane", pid)
                except tm.TmuxError as exc:
                    self.notify(str(exc), severity="error")
                self.refresh_data()
        self.push_screen(ConfirmScreen(f"Kill pane {pid}?"), done)


if __name__ == "__main__":
    if not tm.tmux_available():
        sys.exit("tmux is not installed.")
    MissionBoard().run()
