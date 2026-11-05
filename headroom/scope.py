"""Put running processes into their own memory-capped systemd --user scope.

We never rewrite the command to do this. After Hermes spawns the shell for an approved
command, we find that shell (and anything it already forked) and ask the user's systemd
manager, over D-Bus, to adopt those PIDs into a transient ``headroom-*.scope`` with
``MemoryMax``/``MemorySwapMax`` set and ``OOMPolicy=continue``. Children forked afterwards
inherit the scope. If the job outgrows its cap, the kernel's cgroup OOM killer picks a victim
*inside the scope*; the Hermes process stays outside it and keeps running.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import probe

MB = probe.MB

_UNIT_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _busctl() -> Optional[str]:
    return shutil.which("busctl")


def _systemctl() -> Optional[str]:
    return shutil.which("systemctl")


def new_unit_name(label: str = "") -> str:
    label = _UNIT_SAFE.sub("-", label)[:24].strip("-.")
    suffix = uuid.uuid4().hex[:8]
    return f"headroom-{label + '-' if label else ''}{suffix}.scope"


def adopt(
    pids: Iterable[int],
    unit: str,
    *,
    memory_max: int,
    memory_high: Optional[int] = None,
    swap_max: Optional[int] = None,
    description: str = "",
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """Create ``unit`` containing ``pids``. Returns (ok, error text)."""
    busctl = _busctl()
    if not busctl:
        return False, "busctl not found"
    pid_list = sorted({int(p) for p in pids if int(p) > 1})
    if not pid_list:
        return False, "no pids"
    props: List[str] = []
    count = 0

    def add(*parts: str) -> None:
        nonlocal count
        props.extend(parts)
        count += 1

    add("PIDs", "au", str(len(pid_list)), *map(str, pid_list))
    add("MemoryMax", "t", str(int(memory_max)))
    if memory_high:
        add("MemoryHigh", "t", str(int(memory_high)))
    if swap_max is not None and swap_max >= 0:
        add("MemorySwapMax", "t", str(int(swap_max)))
    add("OOMPolicy", "s", "continue")
    # Delegation lets us move late-forked children in with AttachProcessesToUnit.
    add("Delegate", "b", "true")
    add("Description", "s", (description or "headroom job")[:200])
    argv = [busctl, "--user", "--no-pager", "--quiet", "call", "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "StartTransientUnit",
            "ssa(sv)a(sa(sv))", unit, "fail", str(count), *props, "0"]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=probe._bus_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout).strip()[:300]
    # StartTransientUnit only queues a job; the PIDs move when systemd runs it. Wait for that,
    # otherwise callers would read the old cgroup.
    if not wait_in_unit(pid_list[0], unit, timeout=3.0):
        return False, f"systemd accepted {unit} but did not move pid {pid_list[0]} into it"
    return True, ""


def wait_in_unit(pid: int, unit: str, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        path = probe.cgroup_of(pid)
        if path is None:  # process already gone
            return True
        if path.rstrip("/").endswith("/" + unit):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def attach(unit: str, pids: Iterable[int], timeout: float = 5.0) -> bool:
    """Move more PIDs into an existing scope (``AttachProcessesToUnit``, systemd >= 238)."""
    busctl = _busctl()
    pid_list = sorted({int(p) for p in pids if int(p) > 1})
    if not busctl or not pid_list:
        return False
    argv = [busctl, "--user", "--no-pager", "--quiet", "call", "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "AttachProcessesToUnit",
            "ssau", unit, "", str(len(pid_list)), *map(str, pid_list)]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=probe._bus_env())
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def unit_cgroup(pid: int) -> Optional[Path]:
    path = probe.cgroup_of(pid)
    if not path:
        return None
    return probe.cgroup_dir(path)


def kill_unit(unit: str, signal: str = "SIGKILL", timeout: float = 5.0) -> bool:
    systemctl = _systemctl()
    if not systemctl:
        return False
    try:
        done = subprocess.run(
            [systemctl, "--user", "kill", f"--signal={signal}", "--kill-whom=all", unit],
            capture_output=True, text=True, timeout=timeout, env=probe._bus_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def kill_group(pgid: int, sig: int = 9) -> bool:
    """Fallback when there is no scope: Hermes starts each command in its own session, so the
    shell's process group is exactly the job."""
    if pgid <= 1 or pgid == os.getpgid(0):
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except OSError:
        return False


def reset_failed(unit: str) -> None:
    systemctl = _systemctl()
    if not systemctl:
        return
    try:
        subprocess.run([systemctl, "--user", "reset-failed", unit], capture_output=True,
                       timeout=3, env=probe._bus_env())
    except (OSError, subprocess.SubprocessError):
        pass


def cgroup_stats(directory: Optional[Path]) -> Dict[str, int]:
    if directory is None or not directory.exists():
        return {}
    stats: Dict[str, int] = {}
    current = probe.read_cgroup_int(directory, "memory.current")
    if current is not None:
        stats["current"] = current
    peak = probe.read_cgroup_int(directory, "memory.peak")
    if peak is not None:
        stats["peak"] = peak
    swap = probe.read_cgroup_int(directory, "memory.swap.current")
    if swap is not None:
        stats["swap"] = swap
    events = probe.read_cgroup_events(directory)
    for key in ("oom_kill", "oom", "max", "high"):
        if key in events:
            stats[f"ev_{key}"] = events[key]
    return stats


def cgroup_pids(directory: Optional[Path]) -> List[int]:
    if directory is None:
        return []
    try:
        return [int(x) for x in (directory / "cgroup.procs").read_text().split()]
    except (OSError, ValueError):
        return []
