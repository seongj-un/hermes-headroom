"""Cross-process heavy-job slots.

Every Hermes process on the machine (gateway, CLI sessions, kanban workers, other profiles)
shares the same slot directory under the user's runtime dir, so "one heavy job at a time"
holds machine-wide, not per session. A slot is an ``flock`` on ``slot-N.lock``; the kernel
drops it if the holding process dies, so a crashed gateway can never wedge the queue.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional


def default_dir() -> Path:
    """``$XDG_RUNTIME_DIR/hermes-headroom`` (per-user, tmpfs). Without a runtime dir, fall
    back to ``/tmp/hermes-headroom-<uid>`` only if it is a real directory we own with mode
    0700; a symlink or someone else's directory there is refused."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime) and os.access(runtime, os.W_OK):
        base = Path(runtime) / "hermes-headroom"
    else:
        base = Path("/tmp") / f"hermes-headroom-{os.getuid()}"
    ensure_private_dir(base)
    return base


class UnsafeSlotDir(RuntimeError):
    pass


def ensure_private_dir(path: Path) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise UnsafeSlotDir(f"{path} is not a plain directory")
    if st.st_uid != os.getuid():
        raise UnsafeSlotDir(f"{path} is owned by uid {st.st_uid}, not {os.getuid()}")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)


@dataclass
class Slot:
    index: int
    fd: int
    path: Path
    acquired_at: float
    _released: bool = False

    def describe(self, **meta) -> None:
        """Record who holds the slot, for the queue message other sessions see."""
        if self.fd < 0:
            return
        info = {"pid": os.getpid(), "since": self.acquired_at, **meta}
        try:
            self.path.with_suffix(".json").write_text(json.dumps(info))
        except OSError:
            pass

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.fd < 0:
            return
        try:
            self.path.with_suffix(".json").unlink(missing_ok=True)
        except OSError:
            pass
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


class SlotPool:
    def __init__(self, directory: Optional[Path] = None) -> None:
        self.unavailable = ""
        self._local = threading.Lock()
        try:
            if directory is None:
                self.directory = default_dir()
            else:
                directory.parent.mkdir(parents=True, exist_ok=True)
                ensure_private_dir(directory)
                self.directory = directory
        except (UnsafeSlotDir, OSError) as exc:
            # Never share lock files through a directory we do not control: run without
            # cross-process queueing (isolation, caps and the watchdog still apply).
            self.unavailable = str(exc)
            self.directory = Path("/nonexistent")

    def _path(self, index: int) -> Path:
        return self.directory / f"slot-{index}.lock"

    def try_acquire(self, count: int) -> Optional[Slot]:
        if self.unavailable:
            return Slot(-1, -1, self.directory / "unqueued", time.time())
        with self._local:
            for index in range(max(1, count)):
                path = self._path(index)
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    continue
                return Slot(index, fd, path, time.time())
        return None

    def holders(self, count: int) -> List[Dict]:
        """Who holds each busy slot (best effort; metadata is advisory)."""
        found: List[Dict] = []
        if self.unavailable:
            return found
        for index in range(max(1, count)):
            meta_path = self._path(index).with_suffix(".json")
            try:
                info = json.loads(meta_path.read_text())
            except (OSError, ValueError):
                continue
            pid = info.get("pid")
            if isinstance(pid, int) and not Path(f"/proc/{pid}").exists():
                continue
            info["slot"] = index
            found.append(info)
        return found

    def acquire(
        self,
        count: int,
        *,
        timeout: float,
        ready: Callable[[], Optional[str]] = lambda: None,
        cancelled: Callable[[], bool] = lambda: False,
        poll: float = 1.0,
        on_wait: Optional[Callable[[str], None]] = None,
    ) -> tuple[Optional[Slot], str, float]:
        """Wait for a free slot *and* for ``ready()`` to return None (enough memory).

        Returns ``(slot, reason, waited_seconds)``. ``slot`` is None on timeout/cancel and
        ``reason`` then says what we were waiting for.
        """
        start = time.monotonic()
        reason = ""
        announced = False
        while True:
            blocker = ready()
            if blocker is None:
                slot = self.try_acquire(count)
                if slot is not None:
                    # Memory may have dropped while we waited for the lock.
                    blocker = ready()
                    if blocker is None:
                        return slot, "", time.monotonic() - start
                    slot.release()
                else:
                    blocker = "busy"
            reason = blocker
            if not announced and on_wait is not None:
                on_wait(reason)
                announced = True
            waited = time.monotonic() - start
            if waited >= timeout or cancelled():
                return None, reason, waited
            time.sleep(min(poll, max(0.05, timeout - waited)))
