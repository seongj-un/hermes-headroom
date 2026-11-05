"""Settings with machine-aware defaults.

Every knob defaults to ``0``/auto, which is resolved against the host each time a job starts,
so the same config works on a 1 GB VPS and on a 64 GB workstation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from . import probe

MODES = ("enforce", "observe", "off")

DEFAULTS = {
    "mode": "enforce",
    "max_heavy_jobs": 0,
    "reserve_mb": 0,
    "job_memory_max_mb": 0,
    "swap_mb": 256,
    "min_start_mb": 0,
    "queue_timeout_s": 300,
    "critical_mb": 0,
    "lighten": True,
    "isolate": True,
    "extra_heavy_patterns": [],
    "exclude_patterns": [],
}


@dataclass
class Settings:
    mode: str = "enforce"
    max_heavy_jobs: int = 0
    reserve_mb: int = 0
    job_memory_max_mb: int = 0
    swap_mb: int = 256
    min_start_mb: int = 0
    queue_timeout_s: int = 300
    critical_mb: int = 0
    lighten: bool = True
    isolate: bool = True
    extra_heavy_patterns: List[str] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=list)

    # ---- resolution against the host -------------------------------------------------

    def heavy_slots(self, mem: Optional[probe.Memory]) -> int:
        if self.max_heavy_jobs > 0:
            return self.max_heavy_jobs
        total = mem.total_mb if mem else 0
        if total < 4096:
            return 1
        if total < 16384:
            return 2
        return 4

    def reserve(self, mem: Optional[probe.Memory]) -> int:
        """MB kept free for Hermes itself and the rest of the system."""
        if self.reserve_mb > 0:
            return self.reserve_mb
        total = mem.total_mb if mem else 2048
        return int(max(256, min(2048, total * 0.15)))

    def critical(self, mem: Optional[probe.Memory]) -> int:
        """MemAvailable (MB) below which the watchdog stops the newest heavy job."""
        if self.critical_mb > 0:
            return self.critical_mb
        total = mem.total_mb if mem else 2048
        return int(max(96, min(512, total * 0.05)))

    def min_start(self, mem: Optional[probe.Memory]) -> int:
        """A heavy job waits until at least this many MB are available beyond the reserve."""
        if self.min_start_mb > 0:
            return self.min_start_mb
        total = mem.total_mb if mem else 2048
        return int(max(192, min(1024, total * 0.10)))

    def job_cap(self, mem: Optional[probe.Memory]) -> int:
        """MemoryMax (MB) for a job starting now."""
        if self.job_memory_max_mb > 0:
            return self.job_memory_max_mb
        if mem is None:
            return 0
        return max(self.min_start(mem), mem.available_mb - self.reserve(mem))

    def swap_cap(self) -> Optional[int]:
        """MemorySwapMax in MB; None = leave unlimited."""
        return None if self.swap_mb < 0 else self.swap_mb


def _coerce(value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, list):
        if isinstance(value, str):
            return [v for v in (s.strip() for s in value.split("\n")) if v]
        return [str(v) for v in value] if isinstance(value, (list, tuple)) else default
    return value if isinstance(value, type(default)) else default


def from_getter(get: Callable[[str, Any], Any]) -> Settings:
    values = {}
    for key, default in DEFAULTS.items():
        try:
            raw = get(key, default)
        except Exception:
            raw = default
        values[key] = _coerce(raw if raw is not None else default, default)
    if values["mode"] not in MODES:
        values["mode"] = "enforce"
    return Settings(**values)


class CachedSettings:
    """Re-read plugin settings at most every ``ttl`` seconds (config edits apply live)."""

    def __init__(self, get: Callable[[str, Any], Any], ttl: float = 10.0) -> None:
        self._get = get
        self._ttl = ttl
        self._value: Optional[Settings] = None
        self._at = 0.0

    def __call__(self) -> Settings:
        now = time.monotonic()
        if self._value is None or now - self._at > self._ttl:
            self._value = from_getter(self._get)
            self._at = now
        return self._value

    def invalidate(self) -> None:
        self._value = None
