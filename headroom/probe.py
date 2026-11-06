"""Read-only probes of the host: memory, pressure, CPUs, process tree, cgroups.

Everything here reads /proc and /sys only. Functions return ``None`` (or empty values) on
platforms that lack the file instead of raising, so the plugin degrades on macOS, WSL1 and
containers without a writable cgroup tree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

MB = 1024 * 1024

_PROC = Path("/proc")
_CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class Memory:
    total: int
    available: int
    swap_total: int
    swap_free: int

    @property
    def total_mb(self) -> int:
        return self.total // MB

    @property
    def available_mb(self) -> int:
        return self.available // MB

    @property
    def swap_used_mb(self) -> int:
        return max(0, self.swap_total - self.swap_free) // MB

    @property
    def swap_total_mb(self) -> int:
        return self.swap_total // MB


def memory(proc: Path = _PROC) -> Optional[Memory]:
    """MemTotal/MemAvailable/Swap from /proc/meminfo, in bytes."""
    try:
        text = (proc / "meminfo").read_text()
    except OSError:
        return None
    values: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key.strip()] = int(parts[0]) * 1024
    if "MemTotal" not in values:
        return None
    available = values.get("MemAvailable")
    if available is None:  # very old kernels
        available = values.get("MemFree", 0) + values.get("Cached", 0)
    return Memory(
        total=values["MemTotal"],
        available=available,
        swap_total=values.get("SwapTotal", 0),
        swap_free=values.get("SwapFree", 0),
    )


@dataclass(frozen=True)
class Pressure:
    some_avg10: float
    full_avg10: float
    some_avg60: float
    full_avg60: float


def memory_pressure(proc: Path = _PROC) -> Optional[Pressure]:
    """Linux PSI for memory (``/proc/pressure/memory``); None when PSI is disabled."""
    return memory_pressure_file(proc / "pressure" / "memory")


def memory_pressure_file(path: Path) -> Optional[Pressure]:
    try:
        text = path.read_text()
    except OSError:
        return None
    rows: Dict[str, Dict[str, float]] = {}
    for line in text.splitlines():
        kind, *fields = line.split()
        row: Dict[str, float] = {}
        for field in fields:
            name, _, value = field.partition("=")
            try:
                row[name] = float(value)
            except ValueError:
                continue
        rows[kind] = row
    some, full = rows.get("some", {}), rows.get("full", {})
    return Pressure(
        some_avg10=some.get("avg10", 0.0),
        full_avg10=full.get("avg10", 0.0),
        some_avg60=some.get("avg60", 0.0),
        full_avg60=full.get("avg60", 0.0),
    )


def cpu_count() -> int:
    """CPUs this process may run on (affinity-aware), at least 1."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


# ---------------------------------------------------------------- process tree


def _stat_fields(pid: int, proc: Path = _PROC) -> Optional[List[str]]:
    try:
        raw = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    # comm sits in parentheses and may itself contain spaces or ')'.
    close = raw.rfind(")")
    if close < 0:
        return None
    return raw[close + 2:].split()


def ppid(pid: int, proc: Path = _PROC) -> Optional[int]:
    fields = _stat_fields(pid, proc)
    if not fields or len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def pgid(pid: int, proc: Path = _PROC) -> Optional[int]:
    fields = _stat_fields(pid, proc)
    if not fields or len(fields) < 3:
        return None
    try:
        return int(fields[2])
    except ValueError:
        return None


def rss_bytes(pid: int, proc: Path = _PROC) -> int:
    try:
        for line in (proc / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def cmdline(pid: int, proc: Path = _PROC) -> List[str]:
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def all_pids(proc: Path = _PROC) -> List[int]:
    try:
        return [int(name) for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        return []


def children_map(proc: Path = _PROC) -> Dict[int, List[int]]:
    """parent pid -> child pids for every process we can see."""
    tree: Dict[int, List[int]] = {}
    for pid in all_pids(proc):
        parent = ppid(pid, proc)
        if parent is not None:
            tree.setdefault(parent, []).append(pid)
    return tree


def direct_children(pid: int, proc: Path = _PROC) -> Optional[Set[int]]:
    """Direct children via ``/proc/PID/task/*/children`` (cheap: no full /proc scan).
    None when the kernel does not expose it."""
    try:
        tasks = os.listdir(proc / str(pid) / "task")
    except OSError:
        return None
    found: Set[int] = set()
    seen_file = False
    for tid in tasks:
        try:
            text = (proc / str(pid) / "task" / tid / "children").read_text()
        except OSError:
            continue
        seen_file = True
        found.update(int(x) for x in text.split() if x.isdigit())
    return found if seen_file else None


def cgroup_memory_pressure(directory: Path) -> Optional[Pressure]:
    """PSI of one cgroup (``memory.pressure``)."""
    return memory_pressure_file(directory / "memory.pressure")


def descendants(root: int, tree: Optional[Dict[int, List[int]]] = None, proc: Path = _PROC) -> Set[int]:
    tree = tree if tree is not None else children_map(proc)
    found: Set[int] = set()
    stack = list(tree.get(root, []))
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found.add(pid)
        stack.extend(tree.get(pid, []))
    return found


def alive(pids: Iterable[int], proc: Path = _PROC) -> Set[int]:
    return {pid for pid in pids if (proc / str(pid)).exists()}


def raise_oom_score(pid: int, value: int, proc: Path = _PROC) -> bool:
    """Make ``pid`` the kernel OOM killer's preferred victim. Raising needs no privilege."""
    path = proc / str(pid) / "oom_score_adj"
    try:
        current = int(path.read_text().strip())
        if current >= value:
            return True
        path.write_text(str(value))
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- cgroups / systemd


def cgroup_of(pid: int, proc: Path = _PROC) -> Optional[str]:
    """The unified (v2) cgroup path of ``pid``, e.g. ``/user.slice/.../app.slice/x.service``."""
    try:
        for line in (proc / str(pid) / "cgroup").read_text().splitlines():
            hierarchy, _, rest = line.partition(":")
            controllers, _, path = rest.partition(":")
            if hierarchy == "0" and controllers == "":
                return path
    except OSError:
        pass
    return None


def cgroup_dir(path: str, root: Path = _CGROUP_ROOT) -> Path:
    return root / path.lstrip("/")


def read_cgroup_int(directory: Path, name: str) -> Optional[int]:
    try:
        text = (directory / name).read_text().strip()
    except OSError:
        return None
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def read_cgroup_events(directory: Path) -> Dict[str, int]:
    events: Dict[str, int] = {}
    try:
        for line in (directory / "memory.events").read_text().splitlines():
            key, _, value = line.partition(" ")
            if value.strip().isdigit():
                events[key] = int(value)
    except OSError:
        pass
    return events


@dataclass(frozen=True)
class Capabilities:
    systemd_user: bool
    memory_controller: bool
    psi: bool
    reason: str

    @property
    def can_isolate(self) -> bool:
        return self.systemd_user and self.memory_controller


_caps_lock = threading.Lock()
_caps_cache: Optional[tuple[float, Capabilities]] = None


def _user_manager_cgroup(own: Optional[str]) -> Optional[str]:
    if not own:
        return None
    parts = own.strip("/").split("/")
    for index, part in enumerate(parts):
        if part.startswith("user@") and part.endswith(".service"):
            return "/" + "/".join(parts[: index + 1])
    return None


def capabilities(force: bool = False, ttl: float = 300.0) -> Capabilities:
    """Can this host put a job into its own memory-capped systemd --user scope?"""
    global _caps_cache
    with _caps_lock:
        if not force and _caps_cache and time.monotonic() - _caps_cache[0] < ttl:
            return _caps_cache[1]
        caps = _probe_capabilities()
        _caps_cache = (time.monotonic(), caps)
        return caps


def _probe_capabilities() -> Capabilities:
    psi = memory_pressure() is not None
    if not Path("/proc/self/cgroup").exists():
        return Capabilities(False, False, psi, "not Linux (no /proc/self/cgroup)")
    busctl = shutil.which("busctl")
    if not busctl:
        return Capabilities(False, False, psi, "busctl not found (systemd not installed)")
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not Path(runtime, "bus").exists():
        return Capabilities(False, False, psi, "no systemd user bus (enable lingering or log in)")
    try:
        done = subprocess.run(
            [busctl, "--user", "--no-pager", "get-property", "org.freedesktop.systemd1",
             "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "Version"],
            capture_output=True, text=True, timeout=3, env=_bus_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Capabilities(False, False, psi, f"systemd user bus unreachable: {exc}")
    if done.returncode != 0:
        return Capabilities(False, False, psi, "systemd user bus unreachable: " + done.stderr.strip()[:120])
    manager = _user_manager_cgroup(cgroup_of(os.getpid()))
    if manager is None:
        return Capabilities(True, False, psi, "this process is not under a systemd user manager")
    try:
        controllers = cgroup_dir(manager).joinpath("cgroup.controllers").read_text().split()
    except OSError:
        controllers = []
    if "memory" not in controllers:
        return Capabilities(True, False, psi, "memory controller not delegated to the user manager")
    return Capabilities(True, True, psi, "ok")


def _bus_env() -> Dict[str, str]:
    """Only what busctl/systemctl need to find the user bus; never the agent's secrets."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"}
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    return env


def top_processes(limit: int = 5, min_mb: int = 40, proc: Path = _PROC) -> List[Dict[str, object]]:
    """This user's largest processes, flagging the ones Hermes started (LSP servers, browsers,
    MCP servers, kernels). Those are often the real reason a small box has no headroom."""
    uid = os.getuid()
    rows = []
    hermes_roots: Set[int] = set()
    for pid in all_pids(proc):
        try:
            if (proc / str(pid)).stat().st_uid != uid:
                continue
        except OSError:
            continue
        argv = cmdline(pid, proc)
        joined = " ".join(argv)
        if "hermes_cli" in joined or (argv and os.path.basename(argv[0]) == "hermes"):
            hermes_roots.add(pid)
        rss = rss_bytes(pid, proc)
        if rss >= min_mb * MB:
            rows.append((rss, pid, argv))
    tree = children_map(proc)
    started_by_hermes: Set[int] = set()
    for root in hermes_roots:
        started_by_hermes |= descendants(root, tree, proc)
    out: List[Dict[str, object]] = []
    me = os.getpid()
    for rss, pid, argv in sorted((r for r in rows if r[1] != me), reverse=True)[:limit]:
        name = os.path.basename(argv[0]) if argv else "?"
        joined = " ".join(argv)
        if pid in hermes_roots:
            detail = "hermes gateway" if "gateway run" in joined else "hermes"
        else:
            script = next((os.path.basename(a) for a in argv[1:] if a.startswith("/")), "")
            detail = f"{name} {script}".strip()
        out.append({"pid": pid, "mb": rss // MB, "name": name, "command": detail[:80],
                    "hermes": pid in hermes_roots, "started_by_hermes": pid in started_by_hermes})
    return out
