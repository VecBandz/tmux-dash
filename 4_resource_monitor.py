#!/usr/bin/env python3
"""tmux RESOURCE MONITOR - concept 4  (embeddable widget + standalone app)

Shows what every pane is really costing: CPU and memory summed across each
pane's whole process tree, a live CPU graph for the selected pane, and its
process tree. Stop runaway jobs without leaving the dashboard.

Keys:  i  send Ctrl-C to pane        t  SIGTERM the pane's foreground job
       K  SIGKILL the job (asks)     s  sort cpu / memory / name
       r  refresh                    q  quit (standalone)

Install psutil (`pip install psutil`) for accurate live CPU %. Without it
the monitor still works using `ps`.
"""
from __future__ import annotations

import signal
import sys
from collections import deque

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Header, Sparkline, Static

import procs
import tmuxlib as tm
from common import ConfirmScreen

SORTS = ["cpu", "mem", "name"]


def bar(pct: float, width: int = 10) -> Text:
    pct = max(0.0, pct)
    filled = min(width, round(pct / 100 * width))
    color = "green" if pct < 40 else "yellow" if pct < 80 else "red"
    return Text("█" * filled, style=color) + Text("░" * (width - filled), style="dim")


class ResourceMonitor(Widget):
    """Drop-in panel. Posts ResourceMonitor.Hot when a pane passes 80% CPU."""

    PANEL_TITLE = "Resources"
    DEFAULT_CSS = """
    ResourceMonitor { layout: vertical; }
    ResourceMonitor #rm-summary { height: 1; padding: 0 1; color: $text-muted; }
    ResourceMonitor #rm-table { height: 1fr; border: round $primary; }
    ResourceMonitor #rm-bottom { height: 14; }
    ResourceMonitor #rm-graph { width: 1fr; border: round $accent; padding: 0 1; }
    ResourceMonitor #rm-spark { height: 1fr; }
    ResourceMonitor #rm-tree { width: 1fr; border: round $secondary; padding: 0 1;
                               overflow-y: auto; }
    """
    BINDINGS = [
        ("i", "interrupt", "Ctrl-C"),
        ("t", "term", "Stop job"),
        ("K", "kill", "Kill -9"),
        ("s", "cycle_sort", "Sort"),
        ("r", "refresh_now", "Refresh"),
    ]

    class Hot(Message):
        def __init__(self, pane: str, cpu: float):
            super().__init__()
            self.pane, self.cpu = pane, cpu

    def __init__(self, interval: float = 2.0, **kw):
        super().__init__(**kw)
        self.interval = interval
        self.sort = "cpu"
        self.rows: dict[str, dict] = {}
        self.history: dict[str, deque] = {}
        self.table_cache: dict[int, procs.Proc] = {}
        self._hot: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Static("", id="rm-summary")
        yield DataTable(id="rm-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="rm-bottom"):
            with Vertical(id="rm-graph"):
                yield Static("", id="rm-graph-label")
                yield Sparkline([0.0] * 60, id="rm-spark")
            yield Static("", id="rm-tree")

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        for label, w in [("Pane", 18), ("Job", 12), ("CPU", 7), ("", 10),
                         ("Mem", 7), ("Procs", 5), ("Up", 6), ("Path", 30)]:
            t.add_column(label, width=w)
        self.query_one("#rm-table").border_title = "Panes by cost"
        self.query_one("#rm-graph").border_title = "CPU · last 2 min"
        self.query_one("#rm-tree").border_title = "Process tree"
        procs.process_table()  # prime psutil's CPU deltas
        self.refresh_data()
        self.set_interval(self.interval, self.refresh_data)

    # ------------------------------------------------------------------ data
    def refresh_data(self) -> None:
        try:
            sessions = tm.snapshot()
        except tm.TmuxError as exc:
            self.notify(str(exc), severity="error")
            return
        table = procs.process_table()
        self.table_cache = table
        rows = {}
        for s in sessions:
            for w in s.windows:
                for p in w.panes:
                    try:
                        root = int(p.pid)
                    except ValueError:
                        continue
                    tree = procs.descendants(table, root)
                    cpu = sum(x.cpu for x in tree)
                    mem = sum(x.mem_mb for x in tree)
                    fg = tree[-1] if len(tree) > 1 else (tree[0] if tree else None)
                    rows[p.id] = dict(
                        pane=p, label=f"{s.name}:{w.index}.{p.index}",
                        cpu=cpu, mem=mem, n=len(tree), root=root,
                        fg=fg, job=p.command,
                    )
                    h = self.history.setdefault(p.id, deque([0.0] * 60, maxlen=60))
                    h.append(cpu)
                    if cpu >= 80 and p.id not in self._hot:
                        self._hot.add(p.id)
                        self.post_message(self.Hot(p.id, cpu))
                    elif cpu < 50:
                        self._hot.discard(p.id)
        for gone in set(self.history) - set(rows):
            self.history.pop(gone)
        self.rows = rows
        self._render_table()
        self._render_detail()

    def _render_table(self) -> None:
        t = self.query_one(DataTable)
        keep = self.current_pane()
        key = {"cpu": lambda r: -r["cpu"], "mem": lambda r: -r["mem"],
               "name": lambda r: r["label"]}[self.sort]
        t.clear()
        for pid, r in sorted(self.rows.items(), key=lambda kv: key(kv[1])):
            fg = r["fg"]
            t.add_row(
                Text(r["label"], style="bold"),
                Text(r["job"], style="yellow" if r["job"] not in tm.SHELLS else ""),
                f"{r['cpu']:5.1f}%", bar(r["cpu"]),
                procs.fmt_mb(r["mem"]), str(r["n"]),
                tm.humanize_seconds(fg.elapsed) if fg else "",
                r["pane"].path.replace(str(tm.Path.home()), "~"),
                key=pid,
            )
        if keep in self.rows:
            t.move_cursor(row=t.get_row_index(keep))
        total_cpu = sum(r["cpu"] for r in self.rows.values())
        total_mem = sum(r["mem"] for r in self.rows.values())
        src = "psutil" if procs.psutil else "ps (install psutil for live CPU)"
        self.query_one("#rm-summary", Static).update(
            f"[b]{len(self.rows)}[/] panes · total CPU [b]{total_cpu:.0f}%[/] · "
            f"memory [b]{procs.fmt_mb(total_mem)}[/] · sort: {self.sort} · source: {src}")

    def _render_detail(self) -> None:
        pid = self.current_pane()
        r = self.rows.get(pid or "")
        if not r:
            return
        self.query_one(Sparkline).data = list(self.history.get(pid, [0.0]))
        peak = max(self.history.get(pid, [0.0]))
        self.query_one("#rm-graph-label", Static).update(
            f"[b]{r['label']}[/]  now {r['cpu']:.1f}% · peak {peak:.1f}% · "
            f"mem {procs.fmt_mb(r['mem'])}")
        lines = []
        for depth, p in procs.tree_depths(self.table_cache, r["root"]):
            lines.append(f"{'  ' * depth}{'└ ' if depth else ''}[b]{p.name}[/] "
                         f"[dim]pid {p.pid}[/] · {p.cpu:.0f}% cpu · {procs.fmt_mb(p.mem_mb)}  "
                         f"[dim]{p.cmdline[:60]}[/]")
        self.query_one("#rm-tree", Static).update("\n".join(lines) or "[dim]no processes[/]")

    def current_pane(self) -> str | None:
        t = self.query_one(DataTable)
        if not t.row_count:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._render_detail()

    # --------------------------------------------------------------- actions
    def action_refresh_now(self) -> None:
        self.refresh_data()

    def action_cycle_sort(self) -> None:
        self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
        self._render_table()

    def action_interrupt(self) -> None:
        pid = self.current_pane()
        if pid:
            tm.run("send-keys", "-t", pid, "C-c", check=False)
            self.notify(f"Sent Ctrl-C to {self.rows[pid]['label']}")

    def _job(self) -> procs.Proc | None:
        r = self.rows.get(self.current_pane() or "")
        if not r or r["n"] < 2:
            self.notify("Nothing running in that pane (just the shell)",
                        severity="warning")
            return None
        return r["fg"]

    def _signal(self, job: procs.Proc, sig: int) -> None:
        try:
            procs.signal_pid(job.pid, sig)
            self.notify(f"Sent {signal.Signals(sig).name} to {job.name} ({job.pid})")
        except OSError as exc:
            self.notify(f"Could not signal {job.pid}: {exc}", severity="error")
        self.refresh_data()

    def action_term(self) -> None:
        if job := self._job():
            self._signal(job, signal.SIGTERM)

    def action_kill(self) -> None:
        if job := self._job():
            self.app.push_screen(
                ConfirmScreen(f"Force-kill [b]{job.name}[/] (pid {job.pid})?"),
                lambda ok: ok and self._signal(job, signal.SIGKILL))


class ResourceMonitorApp(App):
    TITLE = "tmux Resource Monitor"
    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ResourceMonitor()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#rm-table").focus()

    def on_resource_monitor_hot(self, msg: ResourceMonitor.Hot) -> None:
        self.notify(f"Pane {msg.pane} is using {msg.cpu:.0f}% CPU", severity="warning")


if __name__ == "__main__":
    if not tm.tmux_available():
        sys.exit("tmux is not installed.")
    ResourceMonitorApp().run()
