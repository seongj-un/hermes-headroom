"""What headroom did, for the user (``/headroom events``) and for the model (next-turn notes)."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

_MAX_LINES = 2000


class EventLog:
    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._recent: Deque[Dict] = deque(maxlen=200)
        self._pending: Dict[str, List[str]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded or self.path is None:
            self._loaded = True
            return
        self._loaded = True
        try:
            lines = self.path.read_text().splitlines()[-200:]
        except OSError:
            return
        for line in lines:
            try:
                self._recent.append(json.loads(line))
            except ValueError:
                continue

    def record(self, kind: str, *, session_id: str = "", note_for_model: str = "", **data) -> Dict:
        event = {"t": round(time.time(), 3), "kind": kind, **data}
        if session_id:
            event["session"] = session_id
        with self._lock:
            self._load()
            self._recent.append(event)
            if note_for_model and session_id:
                self._pending.setdefault(session_id, []).append(note_for_model)
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                    self._maybe_rotate()
                except OSError:
                    pass
        return event

    def _maybe_rotate(self) -> None:
        assert self.path is not None
        try:
            if self.path.stat().st_size < 1_000_000:
                return
            lines = self.path.read_text().splitlines()[-_MAX_LINES // 2:]
            self.path.write_text("\n".join(lines) + "\n")
        except OSError:
            pass

    def recent(self, limit: int = 20, kinds: Optional[set] = None) -> List[Dict]:
        with self._lock:
            self._load()
            items = [e for e in self._recent if kinds is None or e.get("kind") in kinds]
        return items[-limit:]

    def take_notes(self, session_id: str) -> List[str]:
        with self._lock:
            return self._pending.pop(session_id, [])

    def counts(self, since: float = 0.0) -> Dict[str, int]:
        with self._lock:
            self._load()
            out: Dict[str, int] = {}
            for e in self._recent:
                if e.get("t", 0) >= since:
                    out[e["kind"]] = out.get(e["kind"], 0) + 1
        return out
