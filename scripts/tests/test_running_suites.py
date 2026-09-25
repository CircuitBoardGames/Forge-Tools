"""`running-suites`: one `<pid>\t<tree>` row per pytest run over `scripts/tests`, exit 0; non-zero
when it could not report.

Driven against REAL
processes, because the thing under test is that the /proc scan finds what is actually there. The
decoys only carry a pytest-shaped argv; nothing here runs a suite.
"""

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1] / "running-suites"
TAG = "decoy-" + uuid.uuid4().hex[:12]
# `python -c CODE pytest scripts/tests/<tag>` satisfies both tokens of the predicate.
TAIL = ["pytest", "scripts/tests/" + TAG]
SLEEPER = "import time; time.sleep(60)"
SPAWNER = ("import subprocess, sys, time\n"
           "subprocess.Popen([sys.executable, '-c', %r] + sys.argv[1:])\n"
           "time.sleep(60)\n" % SLEEPER)


def _start(argv, cwd):
    # Its own session, so teardown can kill the whole group without touching the runner.
    return subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def _kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait(timeout=30)


def _raw_matches():
    """The predicate with no wrapper rule: every pid whose argv carries this file's tag."""
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if any(TAG in a for a in argv) and "pytest" in argv:
            hits.append(pid)
    return hits


def _rows():
    r = subprocess.run([str(PROBE)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"a probe that ran must exit 0: rc={r.returncode} {r.stderr}"
    return [tuple(l.split("\t")) for l in r.stdout.splitlines()]


def _wait_for(n):
    deadline = time.time() + 30
    while time.time() < deadline:
        raw = _raw_matches()
        if len(raw) >= n:
            return raw
        time.sleep(0.2)
    return _raw_matches()


def test_a_running_suite_is_one_pid_tab_tree_row(tmp_path):
    tree = tmp_path / "SomeTree"
    tree.mkdir()
    proc = _start([sys.executable, "-c", SLEEPER, *TAIL], tree)
    try:
        assert _wait_for(1), "the decoy never appeared in /proc, so this would measure nothing"
        rows = _rows()
        assert (str(proc.pid), "SomeTree") in rows, f"the decoy is not a `<pid>\\t<tree>` row: {rows}"
    finally:
        _kill(proc)


def test_a_wrapper_and_the_child_it_spawned_are_ONE_suite(tmp_path):
    """`uv run ... pytest` is two processes and was reported as two suites."""
    proc = _start([sys.executable, "-c", SPAWNER, *TAIL], tmp_path)
    try:
        raw = _wait_for(2)
        # THE CONTROL: without the two-pid shape there is nothing to collapse.
        assert len(raw) >= 2, f"fixture did not produce parent+child (raw {raw}); vacuous"
        mine = [pid for pid, _ in _rows() if pid in raw]
        assert len(mine) == 1 and mine[0] != str(proc.pid), (
            f"parent+child reported as {mine}; want the child alone, not the wrapper {proc.pid}")
    finally:
        _kill(proc)


def test_a_QUEUED_resource_lock_wrapper_is_not_a_running_suite(tmp_path):
    wrapper = tmp_path / "resource-lock.sh"
    wrapper.write_text("#!/bin/sh\nsleep 60\n")
    proc = _start(["sh", str(wrapper), "suite", "--", *TAIL], tmp_path)
    try:
        raw = _wait_for(1)
        assert str(proc.pid) in raw, f"the wrapper never matched the raw predicate: {raw}"
        assert str(proc.pid) not in [pid for pid, _ in _rows()], "a queued lock wrapper was counted"
    finally:
        _kill(proc)


def test_a_probe_that_cannot_report_exits_NON_zero(tmp_path):
    """An empty result and a failed probe must stay distinguishable: `resource-lock.sh` reads a
    non-zero exit as COULD NOT MEASURE and an exit-0 empty as EXCLUSIVE."""
    proc = _start([sys.executable, "-c", SLEEPER, *TAIL], tmp_path)
    try:
        assert _wait_for(1), "no row to write, so the write could not fail; vacuous"
        with open("/dev/full", "w") as full:
            r = subprocess.run([str(PROBE)], stdout=full, stderr=subprocess.PIPE, timeout=60)
        assert r.returncode != 0, "rows that could not be written still exited 0"
    finally:
        _kill(proc)
