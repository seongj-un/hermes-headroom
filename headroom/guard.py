"""The guard: admission, isolation, monitoring and the pressure watchdog.

Flow for one heavy ``terminal`` call (``tool_execution`` middleware, i.e. after Hermes'
approval pipeline has already accepted the command):

1. **Queue** until a machine-wide heavy-job slot is free *and* enough memory is available.
2. **Spawn** by calling ``next_call(args)``: Hermes runs the command exactly as approved.
3. **Adopt** the spawned shell (found by walking our own process tree) into a capped systemd
   scope and raise its OOM score, so the kernel sacrifices the job, never Hermes.
4. **Watch** the scope's cgroup counters while it runs; a global watchdog stops the newest
   job if the host as a whole approaches OOM or starts thrashing.
5. **Report** what happened inside the tool result, and leave a note for the model's next turn
   if something was killed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from . import classify as cls
from . import probe, scope
from .events import EventLog
from .settings import Settings
from .slots import Slot, SlotPool

logger = logging.getLogger("headroom")

MB = probe.MB
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_OOM_SCORE = 900

HINTS = {
    "js": "jest --runInBand / vitest --maxWorkers=1, a lower NODE_OPTIONS=--max-old-space-size, or building one package at a time",
    "python": "pytest -n 0, running a subset of tests, or installing fewer packages per pip/uv call",
    "rust": "cargo build -j 1, CARGO_PROFILE_DEV_DEBUG=0, or building a single crate (-p NAME)",
    "go": "go build -p 1 / go test -p 1",
    "jvm": "--max-workers=1 and a smaller -Xmx (org.gradle.jvmargs / MAVEN_OPTS)",
    "native": "make -j1 or a lower optimisation level",
    "media": "a smaller model/resolution, or processing the input in chunks",
    "browser": "a single browser page at a time, or a lighter fetch (curl) instead of a browser",
    "agent": "running the sub-agent later, when nothing else heavy is running",
    "container": "building with less parallelism",
    "custom": "a lighter variant of the command",
}


def _now() -> float:
    return time.time()


@dataclass
class Job:
    command: str
    session_id: str
    task_id: str
    background: bool
    rules: List[str]
    kinds: List[str]
    isolate: bool
    cap_mb: int
    swap_mb: Optional[int]
    slot: Optional[Slot]
    preexisting: Set[int]
    owner_pid: int = field(default_factory=os.getpid)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    started: float = field(default_factory=_now)
    unit: Optional[str] = None
    shell_pid: Optional[int] = None
    pgid: Optional[int] = None
    cgroup: Optional[Path] = None
    pids: Set[int] = field(default_factory=set)
    peak: int = 0
    current: int = 0
    oom_kills: int = 0
    watchdog_reason: str = ""
    isolation_error: str = ""
    finished_at: Optional[float] = None
    keeper: Optional[subprocess.Popen] = None
    spawned: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    tracker: Optional[threading.Thread] = None
    display: str = ""  # the command as the model wrote it (before lightening)

    @property
    def label(self) -> str:
        return cls.shorten(self.display or self.command, 60)

    def runtime(self) -> float:
        return (self.finished_at or _now()) - self.started


class Guard:
    def __init__(self, settings: Callable[[], Settings], events: EventLog,
                 pool: Optional[SlotPool] = None,
                 interrupted: Callable[[], bool] = lambda: False) -> None:
        self.settings = settings
        self.events = events
        self.pool = pool or SlotPool()
        self.interrupted = interrupted
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._rewrites: Dict[str, List[str]] = {}
        self._watchdog: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_kill = 0.0
        self._psi_strikes = 0

    # ------------------------------------------------------------------ public views

    def active_jobs(self) -> List[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if not j.done.is_set()]

    # ------------------------------------------------------------------ request middleware

    def rewrite_request(self, tool_name: str, args: Dict[str, Any], **_: Any) -> Optional[Dict[str, Any]]:
        """``tool_request`` middleware: lighten heavy commands *before* approval, so what the
        user approves is what runs."""
        if tool_name != "terminal" or not isinstance(args, dict):
            return None
        command = args.get("command")
        if not isinstance(command, str):
            return None
        s = self.settings()
        if s.mode != "enforce" or not s.lighten:
            return None
        mem = probe.memory()
        if mem is None:
            return None
        cap = s.job_cap(mem)
        rewrite = cls.lighten(command, budget_mb=cap, cpus=probe.cpu_count(), low_memory=cap < 1536,
                              extra=s.extra_heavy_patterns, exclude=s.exclude_patterns)
        if not rewrite.changed:
            return None
        with self._lock:
            if len(self._rewrites) > 256:
                self._rewrites.clear()
            self._rewrites[rewrite.command] = rewrite.changes
        return {"args": {**args, "command": rewrite.command}, "source": "headroom",
                "reason": "; ".join(rewrite.changes)}

    # ------------------------------------------------------------------ execution middleware

    def execute(self, tool_name: str = "", args: Optional[Dict[str, Any]] = None,
                next_call: Optional[Callable[..., Any]] = None, original_args: Optional[Dict[str, Any]] = None,
                session_id: str = "", task_id: str = "", **_: Any) -> Any:
        assert next_call is not None
        if tool_name != "terminal" or not isinstance(args, dict):
            return next_call(args)
        command = args.get("command")
        if not isinstance(command, str):
            return next_call(args)
        s = self.settings()
        if s.mode == "off":
            return next_call(args)
        result = cls.classify(command, s.extra_heavy_patterns, s.exclude_patterns)
        if not result.heavy:
            return next_call(args)
        mem = probe.memory()
        if mem is None:  # not Linux: nothing we can measure or enforce
            return next_call(args)
        with self._lock:
            changes = self._rewrites.pop(command, [])

        if s.mode == "observe":
            return self._observe(args, next_call, result, s, mem, session_id)

        slots = s.heavy_slots(mem)

        def ready() -> Optional[str]:
            m = probe.memory() or mem
            need = s.reserve(m) + s.min_start(m)
            if m.available_mb >= need:
                return None
            return f"only {m.available_mb} MB of memory available (needs {need} MB to start safely)"

        def on_wait(reason: str) -> None:
            self.events.record("queued", session_id=session_id, command=cls.shorten(command),
                               reason=self._describe_blocker(reason, slots))

        slot, reason, waited = self.pool.acquire(
            slots, timeout=max(0, s.queue_timeout_s), ready=ready, cancelled=self.interrupted,
            on_wait=on_wait,
        )
        if slot is None:
            why = self._describe_blocker(reason, slots)
            self.events.record("blocked", session_id=session_id, command=cls.shorten(command),
                               reason=why, waited_s=round(waited, 1))
            return json.dumps({
                "output": "",
                "exit_code": 1,
                "error": (f"headroom did not start this command: {why}. Waited {int(waited)}s. "
                          "Retry later, wait for the other job, or use a lighter command."),
                "headroom": {"status": "not_started", "waited_s": round(waited, 1), "reason": why},
            }, ensure_ascii=False)

        mem_now = probe.memory() or mem
        cap = s.job_cap(mem_now)
        job = Job(
            command=command, session_id=session_id, task_id=task_id,
            display=str((original_args or {}).get("command") or command),
            background=bool(args.get("background")), rules=result.rules, kinds=result.kinds,
            isolate=result.isolate and s.isolate, cap_mb=cap, swap_mb=s.swap_cap(), slot=slot,
            preexisting=probe.descendants(os.getpid()),
        )
        slot.describe(command=job.label, session=session_id, job=job.id, background=job.background)
        self._start(job)
        try:
            output = next_call(args)
        except BaseException:
            job.spawned.set()
            self._finish(job, foreground=True)
            raise
        job.spawned.set()
        if not job.background:
            self._finish(job, foreground=True)
        return self._annotate(output, job, waited, changes)

    # ------------------------------------------------------------------ internals

    def _describe_blocker(self, reason: str, slots: int) -> str:
        if reason != "busy":
            return reason
        holders = self.pool.holders(slots)
        if not holders:
            return "another heavy job is running"
        h = holders[0]
        age = int(_now() - float(h.get("since", _now())))
        where = " in the background" if h.get("background") else ""
        return f"another heavy job is running{where}: `{h.get('command', '?')}` (for {age}s)"

    def _observe(self, args, next_call, result, s: Settings, mem: probe.Memory, session_id: str):
        cap = s.job_cap(mem)
        self.events.record("observed", session_id=session_id, command=cls.shorten(args["command"]),
                           rules=result.rules, would_cap_mb=cap, available_mb=mem.available_mb)
        return next_call(args)

    def _start(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
            if len(self._jobs) > 64:
                for key in [k for k, j in self._jobs.items() if j.done.is_set()][:32]:
                    self._jobs.pop(key, None)
        self.events.record("started", session_id=job.session_id, job=job.id, command=job.label,
                           rules=job.rules, cap_mb=job.cap_mb, background=job.background)
        job.tracker = threading.Thread(target=self._track, args=(job,), name=f"headroom-{job.id}", daemon=True)
        job.tracker.start()
        self._ensure_watchdog()

    def _finish(self, job: Job, foreground: bool) -> None:
        if job.done.is_set():
            return
        job.done.set()
        if job.tracker is not None and job.tracker is not threading.current_thread():
            job.tracker.join(timeout=1.5)
        job.finished_at = _now()
        self._stop_keeper(job)
        if job.slot is not None:
            job.slot.release()
        if job.unit:
            scope.reset_failed(job.unit)
        self.events.record("finished", session_id=job.session_id, job=job.id, command=job.label,
                           peak_mb=job.peak // MB, cap_mb=job.cap_mb, oom_kills=job.oom_kills,
                           watchdog=job.watchdog_reason or None, isolated=bool(job.unit),
                           seconds=round(job.runtime(), 1))
        if job.background and (job.oom_kills or job.watchdog_reason):
            # Nobody is waiting on a background job's result, so tell the model next turn.
            self.events.record("note", session_id=job.session_id, note_for_model=self._note(job))

    # .................................................................. tracking thread

    def _find_shell(self, job: Job) -> Optional[int]:
        """The ``bash -c`` Hermes spawned for this command: a new direct child of this process
        whose script contains the command text."""
        marks = cls.needles(job.command)
        candidates = probe.direct_children(job.owner_pid)
        if candidates is None:  # kernel without /proc/PID/task/*/children
            candidates = probe.descendants(job.owner_pid)
        for pid in sorted(candidates - job.preexisting):
            argv = probe.cmdline(pid)
            if len(argv) < 2 or os.path.basename(argv[0]) not in _SHELLS or "-c" not in argv:
                continue
            body = "\0".join(argv)
            if not marks or any(m in body for m in marks):
                with self._lock:
                    if any(j.shell_pid == pid for j in self._jobs.values() if j is not job):
                        continue
                return pid
        return None

    def _track(self, job: Job) -> None:
        try:
            self._track_inner(job)
        except Exception:  # never let a tracker crash take anything else down
            logger.exception("headroom tracker failed for job %s", job.id)
        finally:
            if job.background and not job.done.is_set():
                self._finish(job, foreground=False)

    def _track_inner(self, job: Job) -> None:
        # Phase 1: find the shell Hermes spawned for this command.
        # Memory a process allocates before it is moved stays charged to Hermes' cgroup, so look
        # hard for the first moments, then back off.
        began = time.monotonic()
        deadline = began + 15.0
        shell = None
        while time.monotonic() < deadline and not job.done.is_set():
            shell = self._find_shell(job)
            if shell is not None:
                break
            if job.spawned.is_set() and not job.background:
                # next_call returned and we never saw the shell: it was too quick to matter.
                break
            time.sleep(0.005 if time.monotonic() - began < 2.0 else 0.05)
        if shell is None:
            if not job.done.is_set() and not job.spawned.is_set():
                job.isolation_error = "the command's process was not found (non-local terminal backend?)"
            return
        job.shell_pid = shell
        job.pgid = probe.pgid(shell)
        tree = probe.children_map()
        job.pids = {shell} | probe.descendants(shell, tree)
        for pid in job.pids:
            probe.raise_oom_score(pid, _OOM_SCORE)
        caps = probe.capabilities()
        if job.isolate and caps.can_isolate and job.cap_mb > 0:
            unit = scope.new_unit_name(job.rules[0] if job.rules else "job")
            # No MemoryHigh: throttling a job at a soft line makes it crawl instead of failing
            # fast with a clear OOM the model can react to.
            keeper = self._spawn_keeper()
            members = set(job.pids) | ({keeper.pid} if keeper else set())
            ok, err = scope.adopt(
                members, unit, memory_max=job.cap_mb * MB,
                swap_max=None if job.swap_mb is None else job.swap_mb * MB,
                description=f"headroom: {job.label}",
            )
            if not ok and keeper is not None:
                keeper.kill()
                keeper.wait()
                keeper = None
            job.keeper = keeper
            if ok:
                job.unit = unit
                directory = scope.unit_cgroup(shell)
                # Only ever watch (or judge liveness by) the scope's own cgroup.
                job.cgroup = directory if directory is not None and directory.name == unit else None
                self._attach_stragglers(job)
            else:
                job.isolation_error = err or "systemd refused the scope"
        elif job.isolate:
            job.isolation_error = caps.reason if not caps.can_isolate else ""

        # Phase 2: watch until the job ends.
        while True:
            self._sample(job)
            if job.background:
                if not self._job_alive(job):
                    break
            elif job.done.is_set():
                break
            job.done.wait(0.25)
            if job.done.is_set() and not job.background:
                self._sample(job)
                break

    def _attach_stragglers(self, job: Job) -> None:
        """Children forked between our process scan and the adoption stay in Hermes' cgroup;
        move them too."""
        if not job.unit or job.shell_pid is None:
            return
        tree = probe.children_map()
        everyone = {job.shell_pid} | probe.descendants(job.shell_pid, tree)
        inside = set(scope.cgroup_pids(job.cgroup))
        missing = sorted(p for p in everyone - inside if probe.alive([p]))
        if missing:
            scope.attach(job.unit, missing)
        for pid in everyone:
            probe.raise_oom_score(pid, _OOM_SCORE)
        job.pids = everyone

    def _job_alive(self, job: Job) -> bool:
        keeper = job.keeper.pid if job.keeper is not None else None
        if job.cgroup is not None and job.cgroup.exists():
            return any(pid != keeper for pid in scope.cgroup_pids(job.cgroup))
        return bool(probe.alive(job.pids))

    @staticmethod
    def _spawn_keeper() -> Optional[subprocess.Popen]:
        """A sleeping placeholder inside the scope. A scope disappears the moment its last
        process exits, taking its counters with it; the keeper holds it open until we have
        read the final peak and OOM-kill counts."""
        sleep = shutil.which("sleep")
        if not sleep:
            return None
        try:
            return subprocess.Popen([sleep, "2147483647"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True, close_fds=True)
        except OSError:
            return None

    def _stop_keeper(self, job: Job) -> None:
        if job.keeper is None:
            return
        self._sample(job)
        try:
            job.keeper.kill()
            job.keeper.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        job.keeper = None

    def _sample(self, job: Job) -> None:
        if job.cgroup is not None:
            stats = scope.cgroup_stats(job.cgroup)
            if stats:
                job.current = stats.get("current", 0)
                job.peak = max(job.peak, stats.get("peak", 0), job.current)
                job.oom_kills = max(job.oom_kills, stats.get("ev_oom_kill", 0))
                return
        if job.pids:
            live = probe.alive(job.pids | probe.descendants(job.shell_pid or 0))
            job.pids |= live
            job.current = sum(probe.rss_bytes(p) for p in live)
            job.peak = max(job.peak, job.current)

    # .................................................................. watchdog

    def _ensure_watchdog(self) -> None:
        with self._lock:
            if self._watchdog is not None and self._watchdog.is_alive():
                return
            self._watchdog = threading.Thread(target=self._watch, name="headroom-watchdog", daemon=True)
            self._watchdog.start()

    def _watch(self) -> None:
        idle_since: Optional[float] = None
        while not self._stop.is_set():
            jobs = self.active_jobs()
            if not jobs:
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since > 60:
                    with self._lock:
                        self._watchdog = None
                    return
                self._stop.wait(1.0)
                continue
            idle_since = None
            try:
                self.check_pressure(jobs)
            except Exception:
                logger.exception("headroom watchdog check failed")
            self._stop.wait(0.5)

    def check_pressure(self, jobs: Optional[List[Job]] = None) -> Optional[Job]:
        """Stop the newest heavy job if the host is about to OOM or is thrashing."""
        jobs = self.active_jobs() if jobs is None else jobs
        if not jobs:
            return None
        mem = probe.memory()
        if mem is None:
            return None
        s = self.settings()
        critical = s.critical(mem)
        reason = ""
        if mem.available_mb < critical:
            reason = f"host memory fell to {mem.available_mb} MB available (critical line {critical} MB)"
        else:
            stalled = self._hermes_stall()
            if stalled is not None and stalled >= 30.0:
                self._psi_strikes += 1
                if self._psi_strikes >= 4:
                    reason = (f"Hermes itself was stalling on memory ({stalled:.0f}% of the last 10s) "
                              "while the job ran")
            else:
                self._psi_strikes = 0
        if not reason or time.monotonic() - self._last_kill < 5.0:
            return None
        victims = [j for j in jobs if j.shell_pid is not None]
        if not victims:
            return None
        victim = max(victims, key=lambda j: j.started)
        killed = False
        if victim.unit:
            killed = scope.kill_unit(victim.unit)
        if not killed and victim.pgid:
            killed = scope.kill_group(victim.pgid)
        if not killed:
            return None
        self._last_kill = time.monotonic()
        self._psi_strikes = 0
        victim.watchdog_reason = reason
        self.events.record("killed", session_id=victim.session_id, job=victim.id, command=victim.label,
                           reason=reason, available_mb=mem.available_mb)
        return victim

    def _hermes_stall(self) -> Optional[float]:
        """How much of the last 10s *Hermes' own* tasks spent stalled on memory (cgroup PSI).

        System-wide PSI would also count the job thrashing inside its own scope, which is not a
        reason to kill it; Hermes' cgroup only stalls when Hermes is the one being hurt.
        Falls back to system-wide ``full`` pressure, with a higher bar, without cgroup PSI."""
        own = probe.cgroup_of(os.getpid())
        if own:
            pressure = probe.cgroup_memory_pressure(probe.cgroup_dir(own))
            if pressure is not None:
                return pressure.some_avg10
        pressure = probe.memory_pressure()
        if pressure is None:
            return None
        return pressure.full_avg10 / 2.0

    def shutdown(self) -> None:
        self._stop.set()

    # .................................................................. reporting

    def _note(self, job: Job) -> str:
        hint = HINTS.get(job.kinds[0] if job.kinds else "custom", HINTS["custom"])
        if job.watchdog_reason:
            return (f"headroom stopped `{job.label}`: {job.watchdog_reason}. Hermes kept running. "
                    f"Retry it with less parallelism, e.g. {hint}.")
        if job.oom_kills:
            return (f"`{job.label}` hit its {job.cap_mb} MB memory cap and the kernel killed its "
                    f"largest process (usually exit code 137, 'Killed'). Hermes was not affected. "
                    f"Retry with less parallelism or a smaller workload, e.g. {hint}.")
        return ""

    def _annotate(self, output: Any, job: Job, waited: float, changes: List[str]) -> Any:
        info: Dict[str, Any] = {"memory_cap_mb": job.cap_mb}
        if job.swap_mb is not None:
            info["swap_cap_mb"] = job.swap_mb
        info["isolated"] = bool(job.unit)
        if job.isolation_error and not job.unit:
            info["not_isolated_because"] = job.isolation_error
        if waited >= 1:
            info["queued_s"] = round(waited, 1)
        if job.peak and not job.background:
            info["peak_mb"] = job.peak // MB
        if changes:
            info["lightened"] = changes
        if job.background:
            info["note"] = "Heavy background job: it holds the heavy-job slot until it exits."
        if job.oom_kills or job.watchdog_reason:
            info["oom_killed" if job.oom_kills else "stopped_by_watchdog"] = True
            info["note"] = self._note(job)
        if isinstance(output, str):
            try:
                data = json.loads(output)
            except ValueError:
                data = None
            if isinstance(data, dict):
                data["headroom"] = info
                return json.dumps(data, ensure_ascii=False)
            return output + "\n[headroom] " + json.dumps(info, ensure_ascii=False)
        if isinstance(output, dict):
            return {**output, "headroom": info}
        return output
