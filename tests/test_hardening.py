import os
import re
import time

import pytest

from headroom import classify as cls
from headroom import probe
from headroom.slots import SlotPool, UnsafeSlotDir, ensure_private_dir

PATHOLOGICAL = [
    "A=" + "'" * 5000,
    "A=" + '"' * 5000,
    "A=" + "\\" * 5000,
    "A=" + "'\"" * 2500,
    "A=" + "x" * 5000,
    "A=" + "$(" * 2500,
    "time " * 2000 + "npm test",
    "env -" + " -x" * 2000,
    "make " + "-j " * 3000,
    "pytest " + "-n " * 3000,
    "cargo build " + "-- " * 3000,
    "npm " * 3000 + "--version",
]


@pytest.mark.parametrize("command", PATHOLOGICAL, ids=lambda c: c[:12] + f"..{len(c)}")
def test_no_catastrophic_backtracking(command):
    t0 = time.monotonic()
    cls.classify(command)
    cls.lighten(command, budget_mb=500, cpus=4, low_memory=True)
    cls.command_words(command)
    assert time.monotonic() - t0 < 1.0


def test_env_assign_still_matches_quoted_values():
    for command in ("A='a b' cmd", 'A="x y" npm i', "A=1 B=2 make", 'A="q\\"x" make'):
        words, _ = cls.command_words(command)
        assert words.split()[0] in ("cmd", "npm", "make"), (command, words)
    assert cls.classify("CI='1' npm ci").heavy


def test_slot_dir_refuses_symlink(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "slots"
    link.symlink_to(target)
    with pytest.raises(UnsafeSlotDir):
        ensure_private_dir(link)
    pool = SlotPool(link)
    assert pool.unavailable
    slot = pool.try_acquire(1)  # still usable, just not queued across processes
    assert slot is not None
    slot.describe(command="x")
    slot.release()
    assert pool.holders(1) == []
    assert not any(target.iterdir())


def test_slot_dir_tightens_mode(tmp_path):
    d = tmp_path / "slots"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    ensure_private_dir(d)
    assert (os.stat(d).st_mode & 0o777) == 0o700


def test_bus_env_is_minimal(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    env = probe._bus_env()
    assert set(env) <= {"PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    assert "sk-secret" not in " ".join(env.values())
