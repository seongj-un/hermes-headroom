"""headroom: keep Hermes alive on small machines.

Heavy shell jobs (installs, builds, test suites, browsers, sub-agents) are queued machine-wide,
run inside their own memory-capped systemd scope with a raised OOM score, lightened where a
stable flag exists, and stopped by a watchdog if the host approaches OOM. Hermes itself stays
outside the blast radius, and the model is told what happened so it can retry sensibly.

Only public plugin surfaces are used: ``tool_request``/``tool_execution`` middleware, the
``pre_llm_call`` hook, one tool, one slash command and one CLI command.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .headroom import report
from .headroom.events import EventLog
from .headroom.guard import Guard
from .headroom.settings import CachedSettings

logger = logging.getLogger("headroom")

PLUGIN_NAME = "headroom"
_LOW_MEMORY_NOTE_EVERY_S = 600.0

_guard: Guard | None = None
_settings: CachedSettings | None = None
_last_low_note: dict[str, float] = {}


def _data_path():
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_NAME) / "events.jsonl"
    except Exception:  # running outside Hermes (tests, plugin doctor without storage)
        return None


def _interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted

        return bool(is_interrupted())
    except Exception:
        return False


STATUS_SCHEMA = {
    "name": "headroom_status",
    "description": (
        "Show this machine's memory headroom before starting heavy work: free RAM and swap, "
        "memory pressure, the memory cap the next heavy command would get, heavy jobs running "
        "now (here or in other Hermes processes), and recent OOM kills or queue waits. Call it "
        "before installs, builds, test suites, browsers or sub-agents on a small machine, or "
        "after a command was killed, to pick a lighter approach."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "explain": {
                "type": "string",
                "description": "Optional shell command: report whether headroom treats it as heavy, "
                               "the memory cap it would get and how it would be lightened.",
            },
        },
        "required": [],
    },
}


def _status_tool(args: dict | None = None, **_: Any) -> str:
    assert _guard is not None and _settings is not None
    s = _settings()
    data = report.snapshot(_guard, s)
    command = (args or {}).get("explain")
    if isinstance(command, str) and command.strip():
        data["explain"] = report.explain(command, s)
    return json.dumps(data, ensure_ascii=False)


def _pre_llm_call(session_id: str = "", **_: Any):
    """Tell the model about jobs headroom stopped, and warn once in a while when memory is
    too tight to start heavy work right away."""
    if _guard is None or _settings is None:
        return None
    s = _settings()
    if s.mode == "off":
        return None
    lines = list(_guard.events.take_notes(session_id)) if session_id else []
    try:
        from .headroom import probe

        mem = probe.memory()
        if mem is not None and session_id:
            need = s.reserve(mem) + s.min_start(mem)
            import time

            now = time.monotonic()
            if mem.available_mb < need and now - _last_low_note.get(session_id, -1e9) > _LOW_MEMORY_NOTE_EVERY_S:
                _last_low_note[session_id] = now
                lines.append(
                    f"Host memory is tight: {mem.available_mb} MB free of {mem.total_mb} MB. headroom will "
                    f"queue heavy commands until {need} MB are free; prefer single-worker or low-parallelism "
                    "options and avoid starting several heavy jobs at once."
                )
    except Exception:
        logger.debug("headroom low-memory probe failed", exc_info=True)
    if not lines:
        return None
    return {"context": "[headroom]\n" + "\n".join("- " + line for line in lines)}


def _slash(raw_args: str = "", **_: Any) -> str:
    assert _guard is not None and _settings is not None
    parts = (raw_args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else "status"
    rest = parts[1] if len(parts) > 1 else ""
    if sub in ("status", ""):
        return report.status_text(_guard, _settings())
    if sub == "events":
        return report.events_text(_guard)
    if sub == "explain":
        return report.explain(rest, _settings()) if rest else "usage: /headroom explain <command>"
    if sub == "selftest":
        from .headroom import selftest

        return selftest.run().text()
    return "usage: /headroom [status|events|explain <command>|selftest]"


def _cli_setup(subparser) -> None:
    subs = subparser.add_subparsers(dest="headroom_command")
    subs.add_parser("status", help="Memory headroom and running heavy jobs")
    subs.add_parser("events", help="Recent queue waits, caps and kills")
    explain = subs.add_parser("explain", help="What headroom would do with a command")
    explain.add_argument("cmd", nargs="+", help="the shell command, quoted")
    subs.add_parser("selftest", help="Prove isolation works: OOM a test process inside a tiny scope")
    subparser.set_defaults(func=_cli)


def _cli(args) -> None:
    assert _guard is not None and _settings is not None
    sub = getattr(args, "headroom_command", None) or "status"
    if sub == "status":
        print(report.status_text(_guard, _settings()))
    elif sub == "events":
        print(report.events_text(_guard, limit=40))
    elif sub == "explain":
        print(report.explain(" ".join(args.cmd), _settings()))
    elif sub == "selftest":
        from .headroom import selftest

        result = selftest.run()
        print(result.text())
        raise SystemExit(0 if result.ok else 1)


def register(ctx) -> None:
    global _guard, _settings
    _settings = CachedSettings(lambda key, default: ctx.get_config(key, default=default))
    _guard = Guard(_settings, EventLog(_data_path()), interrupted=_interrupted)

    ctx.register_middleware("tool_request", _guard.rewrite_request)
    ctx.register_middleware("tool_execution", _guard.execute)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_tool(name="headroom_status", toolset="headroom", schema=STATUS_SCHEMA,
                      handler=_status_tool, description="Memory headroom and heavy jobs", emoji="")
    ctx.register_command("headroom", handler=_slash, description="Memory headroom, heavy jobs and recent kills",
                         args_hint="[status|events|explain <cmd>|selftest]")
    ctx.register_cli_command(name="headroom", help="Memory guard for heavy jobs on small machines",
                             setup_fn=_cli_setup, handler_fn=_cli)
