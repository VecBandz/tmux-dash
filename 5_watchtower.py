#!/usr/bin/env python3
"""tmux WATCHTOWER - concept 5  (embeddable widget + standalone app)

Watches the output of every pane and raises an alert when a new line matches
one of your rules (errors, "done", password prompts, …) or when a long job
finishes and the pane drops back to the shell.

Keys:  p  pause / resume watching     c  clear the alert feed
       a  add a rule                  d  delete selected rule
       space  turn rule on/off        enter  show context around an alert
       r  rescan now                  q  quit (standalone)

Rules are saved in ~/.config/tmux-dash/watch_rules.json
"""
from __future__ import annotations

import difflib
import fnmatch
import json
import re
import sys
import time
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Header, Static

import tmuxlib as tm
from common import ConfirmScreen, PromptScreen

RULES_FILE = Path.home() / ".config" / "tmux-dash" / "watch_rules.json"
DEFAULT_RULES = [
    {"name": "Errors", "pattern": r"(?i)\b(error|exception|traceback|fatal|failed)\b",
     "scope": "*", "level": "error", "on": True},
    {"name": "Warnings", "pattern": r"(?i)\bwarn(ing)?\b", "scope": "*",
     "level": "warning", "on": False},
    {"name": "Finished", "pattern": r"(?i)\b(done|finished|complete(d)?|success)\b",
     "scope": "*", "level": "information", "on": True},
    {"name": "Needs input", "pattern": r"(?i)(\[y/n\]|password:|passphrase|continue\?)",
     "scope": "*", "level": "warning", "on": True},
]
LEVEL_STYLE = {"error": "bold red", "warning": "yellow", "information": "green"}
# Lines that look like a shell prompt + typed command are skipped
PROMPT = re.compile(r"^\S*[@:]\S*\s?[$#%>❯]\s")
MIN_JOB_SECONDS = 5  # alert when a job that ran at least this long finishes


def load_rules() -> list[dict]:
    try:
        return json.loads(RULES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return [dict(r) for r in DEFAULT_RULES]


def save_rules(rules: list[dict]) -> None:
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    RULES_FILE.write_text(json.dumps(rules, indent=2))


def new_lines(old: list[str], new: list[str]) -> list[str]:
    """Lines that appeared in `new` since `old` (handles scrolling)."""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    added = []
    for op, _, _, j1, j2 in sm.get_opcodes():
        if op in ("insert", "replace"):
            added.extend(new[j1:j2])
    return [l for l in added if l.strip() and not PROMPT.match(l)]


class Watchtower(Widget):
    """Drop-in panel. Posts Watchtower.AlertRaised for every alert."""

    PANEL_TITLE = "Alerts"
    DEFAULT_CSS = """
    Watchtower { layout: horizontal; }
    Watchtower #wt-left { width: 2fr; }
    Watchtower #wt-status { height: 1; padding: 0 1; }
    Watchtower #wt-feed { height: 1fr; border: round $error; }
    Watchtower #wt-context { height: 12; border: round $secondary; padding: 0 1;
                             overflow-y: auto; }
    Watchtower #wt-rules { width: 1fr; min-width: 36; border: round $primary; }
    """
    BINDINGS = [
        ("p", "pause", "Pause"),
        ("c", "clear", "Clear"),
        ("a", "add_rule", "Add rule"),
        ("d", "del_rule", "Delete rule"),
        ("space", "toggle_rule", "Rule on/off"),
        ("r", "scan_now", "Rescan"),
    ]

    class AlertRaised(Message):
        def __init__(self, alert: dict, unread: int):
            super().__init__()
            self.alert, self.unread = alert, unread

    def __init__(self, interval: float = 2.0, **kw):
        super().__init__(**kw)
        self.interval = interval
        self.rules = load_rules()
        self.alerts: list[dict] = []
        self.buffers: dict[str, list[str]] = {}
        self.jobs: dict[str, tuple[str, float]] = {}  # pane -> (command, started)
        self.paused = False
        self.scans = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="wt-left"):
            yield Static("", id="wt-status")
            yield DataTable(id="wt-feed", cursor_type="row", zebra_stripes=True)
            yield Static("[dim]Select an alert and press Enter to see the lines "
                         "around it.[/]", id="wt-context")
        yield DataTable(id="wt-rules", cursor_type="row")

    def on_mount(self) -> None:
        feed = self.query_one("#wt-feed", DataTable)
        for label, w in [("Time", 8), ("Pane", 16), ("Rule", 12), ("Line", 60)]:
            feed.add_column(label, width=w)
        rules = self.query_one("#wt-rules", DataTable)
        for label, w in [("On", 3), ("Rule", 12), ("Pattern / scope", 40)]:
            rules.add_column(label, width=w)
        self.query_one("#wt-feed").border_title = "Alert feed"
        self.query_one("#wt-context").border_title = "Context"
        self.query_one("#wt-rules").border_title = "Watch rules"
        self._render_rules()
        self._render_status()
        self.scan()
        self.set_interval(self.interval, self.scan)

    # ------------------------------------------------------------- scanning
    @work(thread=True, exclusive=True)
    def scan(self) -> None:
        if self.paused:
            return
        try:
            sessions = tm.snapshot()
        except tm.TmuxError:
            return
        compiled = []
        for r in self.rules:
            if r.get("on"):
                try:
                    compiled.append((r, re.compile(r["pattern"])))
                except re.error:
                    continue
        first = self.scans == 0
        found = []
        live = set()
        for s in sessions:
            for w in s.windows:
                for p in w.panes:
                    live.add(p.id)
                    label = f"{s.name}:{w.index}.{p.index}"
                    # job-finished detection
                    prev = self.jobs.get(p.id)
                    if p.command not in tm.SHELLS:
                        if not prev or prev[0] != p.command:
                            self.jobs[p.id] = (p.command, time.time())
                    elif prev:
                        ran = time.time() - prev[1]
                        self.jobs.pop(p.id)
                        if ran >= MIN_JOB_SECONDS and not first:
                            found.append(dict(time=time.strftime("%H:%M:%S"),
                                              pane=p.id, label=label, rule="Job ended",
                                              level="information",
                                              line=f"{prev[0]} finished after "
                                                   f"{tm.humanize_seconds(ran)}"))
                    # output rules
                    lines = tm.capture(p.id, 80).rstrip("\n").splitlines()
                    old = self.buffers.get(p.id)
                    self.buffers[p.id] = lines
                    if old is None or first:
                        continue  # baseline - don't alert on what's already there
                    for line in new_lines(old, lines):
                        for r, rx in compiled:
                            scope = r.get("scope") or "*"
                            if not fnmatch.fnmatch(s.name, scope):
                                continue
                            if rx.search(line):
                                found.append(dict(time=time.strftime("%H:%M:%S"),
                                                  pane=p.id, label=label,
                                                  rule=r["name"],
                                                  level=r.get("level", "warning"),
                                                  line=line.strip()))
                                break
        for gone in set(self.buffers) - live:
            self.buffers.pop(gone, None)
            self.jobs.pop(gone, None)
        self.scans += 1
        self.app.call_from_thread(self._add_alerts, found)

    def _add_alerts(self, found: list[dict]) -> None:
        feed = self.query_one("#wt-feed", DataTable)
        for a in found:
            self.alerts.append(a)
            feed.add_row(a["time"], a["label"],
                         Text(a["rule"], style=LEVEL_STYLE.get(a["level"], "")),
                         a["line"][:200], key=str(len(self.alerts) - 1))
            self.post_message(self.AlertRaised(a, len(self.alerts)))
        if found:
            feed.move_cursor(row=feed.row_count - 1)
        self._render_status()

    # ------------------------------------------------------------- rendering
    def _render_status(self) -> None:
        state = "[b red]⏸ PAUSED[/]" if self.paused else "[b green]● watching[/]"
        on = sum(1 for r in self.rules if r.get("on"))
        self.query_one("#wt-status", Static).update(
            f"{state} · {len(self.buffers)} panes · {on}/{len(self.rules)} rules on · "
            f"{len(self.alerts)} alerts · every {self.interval:g}s")

    def _render_rules(self, keep: int | None = None) -> None:
        t = self.query_one("#wt-rules", DataTable)
        keep = t.cursor_row if keep is None else keep
        t.clear()
        for i, r in enumerate(self.rules):
            t.add_row(Text("●", style="green") if r.get("on") else Text("○", style="dim"),
                      Text(r["name"], style=LEVEL_STYLE.get(r.get("level"), "")),
                      Text(f"{r['pattern']}  [{r.get('scope', '*')}]", style="dim"),
                      key=str(i))
        if self.rules:
            t.move_cursor(row=min(keep or 0, len(self.rules) - 1))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "wt-feed":
            return
        a = self.alerts[int(event.row_key.value)]
        lines = tm.capture(a["pane"], 300).rstrip("\n").splitlines()
        idx = max((i for i, l in enumerate(lines) if a["line"] in l), default=None)
        if idx is None:
            body = Text(f"(line has scrolled out of {a['label']})", style="dim")
        else:
            body = Text()
            for i in range(max(0, idx - 5), min(len(lines), idx + 6)):
                body.append(lines[i] + "\n", style="bold reverse" if i == idx else "")
        self.query_one("#wt-context", Static).update(body)

    # --------------------------------------------------------------- actions
    def action_pause(self) -> None:
        self.paused = not self.paused
        self._render_status()

    def action_clear(self) -> None:
        self.alerts.clear()
        self.query_one("#wt-feed", DataTable).clear()
        self._render_status()

    def action_scan_now(self) -> None:
        self.scan()

    def action_toggle_rule(self) -> None:
        t = self.query_one("#wt-rules", DataTable)
        if self.rules:
            r = self.rules[t.cursor_row]
            r["on"] = not r.get("on")
            save_rules(self.rules)
            self._render_rules()
            self._render_status()

    def action_add_rule(self) -> None:
        def got_pattern(pattern):
            if not pattern:
                return
            try:
                re.compile(pattern)
            except re.error as exc:
                self.notify(f"Bad pattern: {exc}", severity="error")
                return

            def got_name(name):
                self.rules.append({"name": name or pattern[:12], "pattern": pattern,
                                   "scope": "*", "level": "warning", "on": True})
                save_rules(self.rules)
                self._render_rules(keep=len(self.rules) - 1)
                self._render_status()
            self.app.push_screen(PromptScreen("Rule name:"), got_name)
        self.app.push_screen(PromptScreen(
            "Text or regex to watch for (e.g. (?i)out of memory):"), got_pattern)

    def action_del_rule(self) -> None:
        t = self.query_one("#wt-rules", DataTable)
        if not self.rules:
            return
        r = self.rules[t.cursor_row]

        def done(ok):
            if ok:
                self.rules.remove(r)
                save_rules(self.rules)
                self._render_rules()
                self._render_status()
        self.app.push_screen(ConfirmScreen(f"Delete rule [b]{r['name']}[/]?"), done)


class WatchtowerApp(App):
    TITLE = "tmux Watchtower"
    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Watchtower()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#wt-feed").focus()

    def on_watchtower_alert_raised(self, msg: Watchtower.AlertRaised) -> None:
        a = msg.alert
        self.notify(f"{a['label']}: {a['line'][:80]}", title=a["rule"],
                    severity=a["level"])
        self.bell()


if __name__ == "__main__":
    if not tm.tmux_available():
        sys.exit("tmux is not installed.")
    WatchtowerApp().run()
