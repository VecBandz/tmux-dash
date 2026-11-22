"""Shared tmux backend for the three Textual dashboards.

Talks to tmux through its CLI using -F format strings, so it needs no extra
Python packages. Every call is wrapped so a missing tmux server gives an empty
result rather than a crash.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

SEP = "‖¦‖"  # printable delimiter (tmux escapes control chars)


class TmuxError(RuntimeError):
    pass


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def run(*args: str, check: bool = True) -> str:
    """Run a tmux command and return stdout."""
    try:
        proc = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=5
        )
    except FileNotFoundError as exc:
        raise TmuxError("tmux is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise TmuxError("tmux command timed out") from exc
    if check and proc.returncode != 0:
        msg = proc.stderr.strip() or f"tmux {' '.join(args)} failed"
        raise TmuxError(msg)
    return proc.stdout


def _query(cmd: str, fmt_keys: list[str], *extra: str) -> list[dict[str, str]]:
    fmt = SEP.join("#{" + k + "}" for k in fmt_keys)
    try:
        out = run(cmd, *extra, "-F", fmt, check=True)
    except TmuxError as exc:
        # "no server running" is normal - treat as empty
        if "no server" in str(exc) or "error connecting" in str(exc):
            return []
        raise
    rows = []
    for line in out.splitlines():
        parts = line.split(SEP)
        if len(parts) == len(fmt_keys):
            rows.append(dict(zip(fmt_keys, parts)))
    return rows


# ---------------------------------------------------------------- data model
@dataclass
class Pane:
    id: str
    index: int
    active: bool
    command: str
    path: str
    pid: str
    width: int
    height: int
    title: str
    session: str
    window_index: int

    @property
    def target(self) -> str:
        return self.id


@dataclass
class Window:
    id: str
    index: int
    name: str
    active: bool
    layout: str
    session: str
    panes: list[Pane] = field(default_factory=list)

    @property
    def target(self) -> str:
        return f"{self.session}:{self.index}"


@dataclass
class Session:
    id: str
    name: str
    attached: int
    created: int
    activity: int
    windows: list[Window] = field(default_factory=list)

    @property
    def target(self) -> str:
        return self.name

    @property
    def pane_count(self) -> int:
        return sum(len(w.panes) for w in self.windows)

    @property
    def age(self) -> str:
        return humanize_seconds(time.time() - self.created)

    @property
    def idle(self) -> str:
        return humanize_seconds(time.time() - self.activity)


def humanize_seconds(sec: float) -> str:
    sec = max(0, int(sec))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= size:
            return f"{sec // size}{unit}"
    return f"{sec}s"


def _int(v: str, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def snapshot() -> list[Session]:
    """Full tree of sessions -> windows -> panes."""
    s_rows = _query(
        "list-sessions",
        ["session_id", "session_name", "session_attached",
         "session_created", "session_activity"],
    )
    w_rows = _query(
        "list-windows",
        ["window_id", "window_index", "window_name", "window_active",
         "window_layout", "session_name"],
        "-a",
    )
    p_rows = _query(
        "list-panes",
        ["pane_id", "pane_index", "pane_active", "pane_current_command",
         "pane_current_path", "pane_pid", "pane_width", "pane_height",
         "pane_title", "session_name", "window_index"],
        "-a",
    )
    sessions = {
        r["session_name"]: Session(
            id=r["session_id"], name=r["session_name"],
            attached=_int(r["session_attached"]),
            created=_int(r["session_created"]),
            activity=_int(r["session_activity"]),
        )
        for r in s_rows
    }
    windows: dict[tuple[str, int], Window] = {}
    for r in w_rows:
        w = Window(
            id=r["window_id"], index=_int(r["window_index"]),
            name=r["window_name"], active=r["window_active"] == "1",
            layout=r["window_layout"], session=r["session_name"],
        )
        windows[(w.session, w.index)] = w
        if w.session in sessions:
            sessions[w.session].windows.append(w)
    for r in p_rows:
        p = Pane(
            id=r["pane_id"], index=_int(r["pane_index"]),
            active=r["pane_active"] == "1",
            command=r["pane_current_command"], path=r["pane_current_path"],
            pid=r["pane_pid"], width=_int(r["pane_width"]),
            height=_int(r["pane_height"]), title=r["pane_title"],
            session=r["session_name"], window_index=_int(r["window_index"]),
        )
        key = (p.session, p.window_index)
        if key in windows:
            windows[key].panes.append(p)
    return list(sessions.values())


def all_panes(sessions: list[Session]) -> list[Pane]:
    return [p for s in sessions for w in s.windows for p in w.panes]


# ------------------------------------------------------------------- actions
def new_session(name: str, path: str | None = None) -> None:
    args = ["new-session", "-d", "-s", name]
    if path:
        args += ["-c", path]
    run(*args)


def new_window(session: str, name: str | None = None) -> None:
    args = ["new-window", "-t", f"{session}:"]
    if name:
        args += ["-n", name]
    run(*args)


def split_pane(target: str, vertical: bool = False) -> None:
    run("split-window", "-v" if vertical else "-h", "-t", target)


def rename_session(old: str, new: str) -> None:
    run("rename-session", "-t", old, new)


def rename_window(target: str, new: str) -> None:
    run("rename-window", "-t", target, new)


def kill(kind: str, target: str) -> None:
    run(f"kill-{kind}", "-t", target)


def send_keys(target: str, text: str, enter: bool = True) -> None:
    args = ["send-keys", "-t", target, "-l", text]
    run(*args)
    if enter:
        run("send-keys", "-t", target, "Enter")


def capture(target: str, lines: int = 200) -> str:
    try:
        return run("capture-pane", "-p", "-J", "-t", target, "-S", f"-{lines}")
    except TmuxError as exc:
        return f"[capture failed: {exc}]"


def select_layout(target: str, layout: str) -> None:
    run("select-layout", "-t", target, layout)


def switch_or_attach_cmd(session: str) -> list[str]:
    """Command to run after the TUI exits to land in a session."""
    import os
    if os.environ.get("TMUX"):
        return ["tmux", "switch-client", "-t", session]
    return ["tmux", "attach-session", "-t", session]


# ---------------------------------------------------- snapshots (save/restore)
SNAP_DIR = Path.home() / ".config" / "tmux-dash" / "snapshots"


def session_to_spec(s: Session) -> dict:
    return {
        "name": s.name,
        "saved_at": int(time.time()),
        "windows": [
            {
                "name": w.name,
                "layout": w.layout,
                "panes": [
                    {"path": p.path, "command": p.command} for p in w.panes
                ],
            }
            for w in s.windows
        ],
    }


SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh"}


def restore_spec(spec: dict, name: str | None = None,
                 rerun_commands: bool = False) -> str:
    """Rebuild a session from a saved spec. Returns the session name used."""
    name = name or spec["name"]
    existing = {s.name for s in snapshot()}
    base, n = name, 2
    while name in existing:
        name = f"{base}-{n}"
        n += 1
    windows = spec.get("windows") or [{"name": "main", "panes": [{}]}]
    for wi, w in enumerate(windows):
        panes = w.get("panes") or [{}]
        first_path = panes[0].get("path") or str(Path.home())
        wname = w.get("name") or f"w{wi}"
        if wi == 0:
            first = run("new-session", "-d", "-P", "-F", "#{pane_id}",
                        "-s", name, "-n", wname, "-c", first_path).strip()
        else:
            first = run("new-window", "-P", "-F", "#{pane_id}",
                        "-t", f"{name}:", "-n", wname, "-c", first_path).strip()
        pane_ids = [first]
        for p in panes[1:]:
            pid = run("split-window", "-P", "-F", "#{pane_id}", "-t", first,
                      "-c", p.get("path") or str(Path.home())).strip()
            pane_ids.append(pid)
            select_layout(first, "tiled")  # keep room for the next split
        if w.get("layout"):
            try:
                select_layout(first, w["layout"])
            except TmuxError:
                select_layout(first, "tiled")
        if rerun_commands:
            for pid, spec_p in zip(pane_ids, panes):
                cmd = spec_p.get("command", "")
                if cmd and cmd not in SHELLS:
                    send_keys(pid, cmd)
    return name


def save_snapshot(s: Session, label: str | None = None) -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    spec = session_to_spec(s)
    spec["label"] = label or s.name
    fname = f"{spec['label']}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path = SNAP_DIR / fname
    path.write_text(json.dumps(spec, indent=2))
    return path


def list_snapshots() -> list[tuple[Path, dict]]:
    if not SNAP_DIR.exists():
        return []
    out = []
    for f in sorted(SNAP_DIR.glob("*.json"), reverse=True):
        try:
            out.append((f, json.loads(f.read_text())))
        except (OSError, json.JSONDecodeError):
            continue
    return out
