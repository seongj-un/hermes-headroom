"""``hermes headroom selftest``: prove on *this* machine that a runaway job dies alone.

Spawns a small Python process that allocates 20 MB at a time, adopts it into a 64 MB scope the
same way a real job is adopted, and checks that the kernel killed it inside the scope while this
process carried on. Nothing here goes through the agent's terminal tool.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import List

from . import probe, scope

MB = probe.MB

_HOG = (
    "import time\n"
    "keep = []\n"
    "for _ in range(40):\n"
    "    keep.append(bytearray(20 * 1024 * 1024))\n"
    "    time.sleep(0.05)\n"
    "print('survived', len(keep) * 20)\n"
)


@dataclass
class Result:
    ok: bool
    lines: List[str] = field(default_factory=list)

    def text(self) -> str:
        head = "headroom selftest: PASS" if self.ok else "headroom selftest: FAIL"
        return "\n".join([head, *("  " + line for line in self.lines)])


def run(cap_mb: int = 64) -> Result:
    lines: List[str] = []
    mem = probe.memory()
    if mem is None:
        return Result(False, ["no /proc/meminfo: headroom only enforces on Linux"])
    lines.append(f"memory: {mem.available_mb} MB available of {mem.total_mb} MB, {probe.cpu_count()} CPU")
    psi = probe.memory_pressure()
    lines.append("PSI memory pressure: " + ("available" if psi else "not available (watchdog uses MemAvailable only)"))
    caps = probe.capabilities(force=True)
    if not caps.can_isolate:
        lines.append(f"isolation: unavailable ({caps.reason})")
        lines.append("headroom will still queue heavy jobs, raise their OOM score and run the watchdog")
        return Result(True, lines)
    lines.append("isolation: systemd --user scopes with the memory controller")

    if mem.available_mb < cap_mb * 3:
        return Result(False, lines + [f"not enough free memory to run the test safely ({mem.available_mb} MB)"])

    proc = subprocess.Popen([sys.executable, "-c", _HOG], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    unit = scope.new_unit_name("selftest")
    keeper = subprocess.Popen(["sleep", "300"], start_new_session=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok, err = scope.adopt([proc.pid, keeper.pid], unit, memory_max=cap_mb * MB, swap_max=0,
                          description="headroom selftest")
    if not ok:
        for p in (proc, keeper):
            p.kill()
            p.wait()
        return Result(False, lines + [f"systemd refused the scope: {err}"])
    cgroup = scope.unit_cgroup(proc.pid)
    probe.raise_oom_score(proc.pid, 900)
    oom_kills = 0
    deadline = time.monotonic() + 20
    while proc.poll() is None and time.monotonic() < deadline:
        stats = scope.cgroup_stats(cgroup)
        oom_kills = max(oom_kills, stats.get("ev_oom_kill", 0))
        time.sleep(0.05)
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    oom_kills = max(oom_kills, scope.cgroup_stats(cgroup).get("ev_oom_kill", 0))
    keeper.kill()
    keeper.wait()
    scope.reset_failed(unit)
    out = proc.stdout.read().decode() if proc.stdout else ""
    killed = proc.returncode == -9
    lines.append(f"test process: tried to allocate 800 MB inside a {cap_mb} MB scope")
    if killed:
        lines.append(f"kernel OOM-killed it inside {unit} (cgroup oom_kill events: {oom_kills})")
        lines.append(f"this process (pid {os.getpid()}) was not touched")
        return Result(True, lines)
    lines.append(f"unexpected: exit code {proc.returncode}, output {out.strip()[:80]!r}")
    return Result(False, lines)
