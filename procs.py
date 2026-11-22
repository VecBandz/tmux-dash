"""Process info for tmux panes.

Uses psutil when installed (accurate live CPU %), otherwise falls back to
`ps`, which needs nothing extra (CPU % is then a lifetime average).
"""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None


@dataclass
class Proc:
    pid: int
    ppid: int
    cpu: float      # percent
    mem_mb: float
    elapsed: int    # seconds
    name: str
    cmdline: str


_ps_cache: dict[int, "psutil.Process"] = {}


def process_table() -> dict[int, Proc]:
    if psutil:
        out: dict[int, Proc] = {}
        seen = set()
        import time
        now = time.time()
        for p in psutil.process_iter(["pid", "ppid", "name", "memory_info",
                                      "create_time", "cmdline"]):
            pid = p.info["pid"]
            seen.add(pid)
            proc = _ps_cache.setdefault(pid, p)
            try:
                cpu = proc.cpu_percent(None)  # delta since last call
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                cpu = 0.0
            mi = p.info.get("memory_info")
            out[pid] = Proc(
                pid=pid, ppid=p.info["ppid"] or 0, cpu=cpu,
                mem_mb=(mi.rss / 1048576) if mi else 0.0,
                elapsed=int(now - (p.info.get("create_time") or now)),
                name=p.info["name"] or "?",
                cmdline=" ".join(p.info.get("cmdline") or []) or p.info["name"] or "",
            )
        for dead in set(_ps_cache) - seen:
            _ps_cache.pop(dead, None)
        return out
    # ---- fallback: ps
    try:
        raw = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,pcpu=,rss=,etimes=,comm=,args="],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in raw.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 6:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            out[pid] = Proc(pid, ppid, float(parts[2]), int(parts[3]) / 1024,
                            int(parts[4]), parts[5],
                            parts[6] if len(parts) > 6 else parts[5])
        except ValueError:
            continue
    return out


def descendants(table: dict[int, Proc], root: int) -> list[Proc]:
    """Root plus every child, grandchild, … (depth-first order)."""
    kids: dict[int, list[int]] = {}
    for p in table.values():
        kids.setdefault(p.ppid, []).append(p.pid)
    out, stack = [], [root]
    while stack:
        pid = stack.pop()
        if pid in table:
            out.append(table[pid])
        stack.extend(sorted(kids.get(pid, []), reverse=True))
    return out


def tree_depths(table: dict[int, Proc], root: int) -> list[tuple[int, Proc]]:
    kids: dict[int, list[int]] = {}
    for p in table.values():
        kids.setdefault(p.ppid, []).append(p.pid)
    out = []

    def walk(pid: int, depth: int) -> None:
        if pid in table:
            out.append((depth, table[pid]))
        for k in sorted(kids.get(pid, [])):
            walk(k, depth + 1)
    walk(root, 0)
    return out


def signal_pid(pid: int, sig: int = signal.SIGTERM) -> None:
    os.kill(pid, sig)


def fmt_mb(mb: float) -> str:
    return f"{mb / 1024:.1f}G" if mb >= 1024 else f"{mb:.0f}M"
