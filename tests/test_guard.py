import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from headroom import probe
from headroom.events import EventLog
from headroom.guard import Guard
from headroom.settings import from_getter
from headroom.slots import SlotPool

HOG = Path(__file__).with_name("hog.py")


def make_guard(tmp_path, **overrides):
    values = {"extra_heavy_patterns": [r"hog\.py", r"^sleep 3\d"], **overrides}
    settings = from_getter(lambda key, default: values.get(key, default))
    return Guard(lambda: settings, EventLog(tmp_path / "events.jsonl"), pool=SlotPool(tmp_path / "slots"))


def hermes_like_spawn(command: str, background: bool = False):
    """Spawn the command the way Hermes' local terminal backend does: a fresh ``bash -c`` in its
    own session whose script evals the (quoted) command."""
    script = f"builtin cd -- {shlex.quote(os.getcwd())} || exit 126\neval {shlex.quote(command)}\n"
    proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    if background:
        return proc, json.dumps({"output": "Background process started", "session_id": "proc_x", "pid": proc.pid})
    out, _ = proc.communicate(timeout=60)
    return proc, json.dumps({"output": out.decode(errors="replace"), "exit_code": proc.returncode, "error": None})


def run(guard, command, **kw):
    seen = {}

    def next_call(args):
        seen["args"] = args
        proc, result = hermes_like_spawn(args["command"], background=bool(args.get("background")))
        seen["proc"] = proc
        return result

    out = guard.execute(tool_name="terminal", args={"command": command, **kw}, next_call=next_call,
                        session_id="s1", task_id="t1")
    return json.loads(out), seen


# ------------------------------------------------------------------ no systemd needed


def test_light_command_passes_through(tmp_path):
    guard = make_guard(tmp_path)
    calls = []
    out = guard.execute(tool_name="terminal", args={"command": "echo hi"},
                        next_call=lambda a: calls.append(a) or '{"output": "hi"}')
    assert out == '{"output": "hi"}' and calls == [{"command": "echo hi"}]


def test_other_tools_pass_through(tmp_path):
    guard = make_guard(tmp_path)
    assert guard.execute(tool_name="read_file", args={"path": "x"}, next_call=lambda a: "ok") == "ok"


def test_off_mode(tmp_path):
    guard = make_guard(tmp_path, mode="off")
    assert guard.execute(tool_name="terminal", args={"command": "npm install"},
                         next_call=lambda a: "raw") == "raw"


def test_observe_mode_records_without_touching(tmp_path):
    guard = make_guard(tmp_path, mode="observe")
    out = guard.execute(tool_name="terminal", args={"command": "npm install"}, next_call=lambda a: '{"x": 1}')
    assert out == '{"x": 1}'
    assert guard.events.recent(1)[0]["kind"] == "observed"


def test_blocked_when_slot_busy(tmp_path):
    guard = make_guard(tmp_path, queue_timeout_s=0, max_heavy_jobs=1)
    held = guard.pool.try_acquire(1)
    held.describe(command="npm run build", background=False)
    try:
        out = json.loads(guard.execute(tool_name="terminal", args={"command": "npm install"},
                                       next_call=lambda a: pytest.fail("must not run")))
    finally:
        held.release()
    assert out["exit_code"] == 1
    assert out["headroom"]["status"] == "not_started"
    assert "npm run build" in out["error"]


def test_waits_for_slot_then_runs(tmp_path):
    guard = make_guard(tmp_path, queue_timeout_s=10, max_heavy_jobs=1)
    held = guard.pool.try_acquire(1)
    threading.Timer(1.5, held.release).start()
    t0 = time.monotonic()
    out = json.loads(guard.execute(tool_name="terminal", args={"command": "npm install"},
                                   next_call=lambda a: '{"output": "", "exit_code": 0}'))
    assert time.monotonic() - t0 >= 1.0
    assert out["headroom"]["queued_s"] >= 1.0
    assert any(e["kind"] == "queued" for e in guard.events.recent(10))


def test_waits_for_memory(tmp_path):
    mem = probe.memory()
    guard = make_guard(tmp_path, queue_timeout_s=0, reserve_mb=mem.total_mb * 2)
    out = json.loads(guard.execute(tool_name="terminal", args={"command": "npm install"},
                                   next_call=lambda a: pytest.fail("must not run")))
    assert "memory available" in out["error"]


def test_request_rewrite_and_report(tmp_path):
    guard = make_guard(tmp_path, job_memory_max_mb=700)
    rewritten = guard.rewrite_request("terminal", {"command": "npx jest"})
    assert rewritten and "--runInBand" in rewritten["args"]["command"]
    assert rewritten["source"] == "headroom"
    out = json.loads(guard.execute(tool_name="terminal", args=rewritten["args"],
                                   next_call=lambda a: '{"output": "", "exit_code": 0}'))
    assert any("runInBand" in c for c in out["headroom"]["lightened"])


def test_rewrite_skipped_when_disabled(tmp_path):
    assert make_guard(tmp_path, lighten=False).rewrite_request("terminal", {"command": "npx jest"}) is None
    assert make_guard(tmp_path, mode="observe").rewrite_request("terminal", {"command": "npx jest"}) is None


def test_slot_released_after_exception(tmp_path):
    guard = make_guard(tmp_path, max_heavy_jobs=1)

    def boom(args):
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        guard.execute(tool_name="terminal", args={"command": "npm install"}, next_call=boom)
    slot = guard.pool.try_acquire(1)
    assert slot is not None
    slot.release()


# ------------------------------------------------------------------ real processes

needs_isolation = pytest.mark.skipif(not probe.capabilities(force=True).can_isolate,
                                     reason="systemd --user scopes with memory controller unavailable")


@needs_isolation
def test_runaway_job_is_oom_killed_inside_its_scope(tmp_path):
    guard = make_guard(tmp_path, job_memory_max_mb=96, swap_mb=0, min_start_mb=64)
    out, seen = run(guard, f"{sys.executable} {HOG} 600; echo after=$?")
    info = out["headroom"]
    assert info["isolated"] is True
    assert info.get("oom_killed") is True, info
    assert "after=137" in out["output"]  # bash survived and reported the kill
    assert "memory cap" in info["note"]
    events = [e["kind"] for e in guard.events.recent(10)]
    assert "started" in events and "finished" in events


@needs_isolation
def test_well_behaved_job_runs_normally(tmp_path):
    guard = make_guard(tmp_path, job_memory_max_mb=200, swap_mb=0, min_start_mb=64)
    # Real jobs ramp up over seconds; a short start delay keeps this from racing the adoption.
    out, _ = run(guard, f"sleep 0.3; {sys.executable} {HOG} 40")
    info = out["headroom"]
    assert out["exit_code"] == 0, out
    assert "survived 40" in out["output"]
    assert info["isolated"] and not info.get("oom_killed")
    assert info["peak_mb"] >= 30


@needs_isolation
def test_watchdog_stops_newest_job(tmp_path):
    guard = make_guard(tmp_path, job_memory_max_mb=200, min_start_mb=64)
    result = {}
    worker = threading.Thread(target=lambda: result.update(out=run(guard, "sleep 30")[0]))
    worker.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not any(j.unit for j in guard.active_jobs()):
        time.sleep(0.05)
    assert guard.active_jobs() and guard.active_jobs()[0].unit
    # Pretend the host is out of memory.
    guard.settings().critical_mb = 10 ** 7
    victim = guard.check_pressure()
    assert victim is not None
    worker.join(timeout=10)
    info = result["out"]["headroom"]
    assert info.get("stopped_by_watchdog") is True
    assert "headroom stopped" in info["note"]
    assert any(e["kind"] == "killed" for e in guard.events.recent(10))


@needs_isolation
def test_background_job_holds_slot_until_exit(tmp_path):
    guard = make_guard(tmp_path, job_memory_max_mb=200, min_start_mb=64, max_heavy_jobs=1)
    out, seen = run(guard, f"{sys.executable} {HOG} 20 1.5", background=True)
    assert out["headroom"]["note"].startswith("Heavy background job")
    assert guard.pool.try_acquire(1) is None  # still held by the background job
    seen["proc"].wait(timeout=20)
    deadline = time.monotonic() + 5
    slot = None
    while slot is None and time.monotonic() < deadline:
        slot = guard.pool.try_acquire(1)
        time.sleep(0.1)
    assert slot is not None
    slot.release()
    finished = [e for e in guard.events.recent(10) if e["kind"] == "finished"]
    assert finished and finished[-1]["isolated"] is True


@needs_isolation
def test_hermes_process_is_outside_the_scope(tmp_path):
    guard = make_guard(tmp_path, job_memory_max_mb=96, swap_mb=0, min_start_mb=64)
    before = probe.cgroup_of(os.getpid())
    run(guard, f"{sys.executable} {HOG} 600")
    assert probe.cgroup_of(os.getpid()) == before
    assert "headroom-" not in (before or "")


def test_job_label_uses_command_as_written(tmp_path):
    guard = make_guard(tmp_path, job_memory_max_mb=700)
    rewritten = guard.rewrite_request("terminal", {"command": "npm run build"})
    guard.execute(tool_name="terminal", args=rewritten["args"], original_args={"command": "npm run build"},
                  next_call=lambda a: '{"output": "", "exit_code": 0}')
    started = [e for e in guard.events.recent(10) if e["kind"] == "started"][-1]
    assert started["command"] == "npm run build"


def test_top_processes_includes_and_flags_own_process():
    rows = probe.top_processes(limit=50, min_mb=1)
    me = [row for row in rows if row["pid"] == os.getpid()]
    assert me and me[0]["hermes"] is True
    assert all({"pid", "mb", "command", "hermes", "started_by_hermes"} <= set(row) for row in rows)
