"""Status snapshots for the ``headroom_status`` tool, ``/headroom`` and ``hermes headroom``."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from . import classify as cls
from . import probe
from .settings import Settings


def snapshot(guard, settings: Settings) -> Dict[str, Any]:
    mem = probe.memory()
    psi = probe.memory_pressure()
    caps = probe.capabilities()
    data: Dict[str, Any] = {"mode": settings.mode}
    if mem is not None:
        data["memory"] = {
            "total_mb": mem.total_mb,
            "available_mb": mem.available_mb,
            "swap_used_mb": mem.swap_used_mb,
            "swap_total_mb": mem.swap_total_mb,
        }
        data["budget"] = {
            "next_job_cap_mb": settings.job_cap(mem),
            "reserved_for_hermes_mb": settings.reserve(mem),
            "start_needs_available_mb": settings.reserve(mem) + settings.min_start(mem),
            "critical_mb": settings.critical(mem),
            "heavy_job_slots": settings.heavy_slots(mem),
        }
    if psi is not None:
        data["pressure"] = {"some_avg10": psi.some_avg10, "full_avg10": psi.full_avg10}
    data["cpus"] = probe.cpu_count()
    data["isolation"] = {"available": caps.can_isolate, "detail": caps.reason}
    jobs = []
    for job in guard.active_jobs():
        jobs.append({
            "command": job.label, "running_s": int(job.runtime()), "cap_mb": job.cap_mb,
            "using_mb": job.current // probe.MB, "peak_mb": job.peak // probe.MB,
            "isolated": bool(job.unit), "background": job.background,
        })
    data["running_here"] = jobs
    if mem is not None:
        others = [h for h in guard.pool.holders(settings.heavy_slots(mem))
                  if h.get("job") not in {j.id for j in guard.active_jobs()}]
        if others:
            data["running_elsewhere"] = [
                {"command": h.get("command"), "running_s": int(time.time() - float(h.get("since", time.time()))),
                 "pid": h.get("pid")} for h in others
            ]
    data["top_processes"] = probe.top_processes()
    data["recent"] = guard.events.recent(8, kinds={"blocked", "killed", "finished", "queued"})
    return data


def _mb(value: Optional[int]) -> str:
    if value is None:
        return "?"
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value} MB"


def status_text(guard, settings: Settings) -> str:
    snap = snapshot(guard, settings)
    lines: List[str] = []
    mem = snap.get("memory")
    if mem:
        lines.append(f"headroom ({snap['mode']}): {_mb(mem['available_mb'])} free of {_mb(mem['total_mb'])}, "
                     f"swap {_mb(mem['swap_used_mb'])}/{_mb(mem['swap_total_mb'])}, {snap['cpus']} CPU")
    else:
        lines.append(f"headroom ({snap['mode']}): memory figures unavailable on this platform")
    pressure = snap.get("pressure")
    if pressure:
        lines.append(f"memory pressure (PSI avg10): some {pressure['some_avg10']:.1f}%, full {pressure['full_avg10']:.1f}%")
    budget = snap.get("budget")
    if budget:
        lines.append(f"next heavy job: cap {_mb(budget['next_job_cap_mb'])}, starts when {_mb(budget['start_needs_available_mb'])} "
                     f"are free, {budget['heavy_job_slots']} at a time; watchdog line {_mb(budget['critical_mb'])}")
    iso = snap["isolation"]
    lines.append("isolation: systemd --user scopes" if iso["available"] else f"isolation: off ({iso['detail']}); queueing and OOM priority still apply")
    for job in snap["running_here"]:
        lines.append(f"running: `{job['command']}` {job['running_s']}s, {_mb(job['using_mb'])} of {_mb(job['cap_mb'])}"
                     f"{' (background)' if job['background'] else ''}")
    for job in snap.get("running_elsewhere", []):
        lines.append(f"running in another Hermes process: `{job['command']}` {job['running_s']}s")
    if not snap["running_here"] and not snap.get("running_elsewhere"):
        lines.append("no heavy jobs running")
    top = snap.get("top_processes") or []
    if top:
        parts = []
        for row in top:
            tag = " [Hermes]" if row["hermes"] else (" [started by Hermes]" if row["started_by_hermes"] else "")
            parts.append(f"{row['command']} {_mb(row['mb'])}{tag}")
        lines.append("largest processes: " + "; ".join(parts))
    return "\n".join(lines)


def events_text(guard, limit: int = 15) -> str:
    items = guard.events.recent(limit)
    if not items:
        return "headroom: no events yet"
    out = []
    for e in items:
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(e.get("t", 0)))
        kind = e.get("kind")
        command = e.get("command", "")
        if kind == "finished":
            extra = f"peak {_mb(e.get('peak_mb'))} / cap {_mb(e.get('cap_mb'))}, {e.get('seconds')}s"
            if e.get("oom_kills"):
                extra += f", OOM-killed inside its scope x{e['oom_kills']}"
            if e.get("watchdog"):
                extra += ", stopped by watchdog"
        elif kind in ("blocked", "queued", "killed"):
            extra = e.get("reason", "")
        elif kind == "started":
            extra = f"cap {_mb(e.get('cap_mb'))}" + (" (background)" if e.get("background") else "")
        elif kind == "observed":
            extra = f"would cap at {_mb(e.get('would_cap_mb'))}"
        else:
            extra = ""
        out.append(f"{stamp} {kind:<8} `{cls.shorten(command, 50)}` {extra}".rstrip())
    return "\n".join(out)


def explain(command: str, settings: Settings) -> str:
    """What headroom would do with ``command`` right now (``hermes headroom explain``)."""
    mem = probe.memory()
    result = cls.classify(command, settings.extra_heavy_patterns, settings.exclude_patterns)
    if not result.heavy:
        return "light: runs untouched"
    lines = [f"heavy ({result.summary()})"]
    if mem is not None:
        cap = settings.job_cap(mem)
        rewrite = cls.lighten(command, budget_mb=cap, cpus=probe.cpu_count(), low_memory=cap < 1536,
                              extra=settings.extra_heavy_patterns, exclude=settings.exclude_patterns)
        lines.append(f"memory cap if started now: {_mb(cap)} (+{_mb(settings.swap_cap())} swap)"
                     if settings.swap_cap() is not None else f"memory cap if started now: {_mb(cap)}")
        if rewrite.changed and settings.lighten:
            lines.append("rewritten to: " + rewrite.command)
            lines.extend("  - " + c for c in rewrite.changes)
        else:
            lines.append("runs as written")
    if not result.isolate:
        lines.append("note: the work runs in a daemon outside Hermes' process tree, so only queueing applies")
    return "\n".join(lines)
