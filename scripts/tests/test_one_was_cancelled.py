"""ONE cancellation probe, three callers.

`hub-api.sh` (`pr await`), `pr-queue.sh` (the drain) and `forge.sh` (`pr gate`) each carried their
own copy of "was this red a cancellation?", and they had drifted: on runs `cancelled,running` they
answered 0 / 3 / 0. The body now lives once, in `forge.sh`, and the other two call it.

This feeds the drift's reproduction to each caller's function -- extracted the way it was measured,
against one stub client -- and asserts every caller gets the SAME answer. On the pre-fix code the
first row reads 0 / 3 / 0 and this fails.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
FULL = "6f7abffa3053ecb7e967697920fc782354b9ba14"

# file -> (function, how that caller calls it)
CALLERS = {
    "hub-api.sh": ("was_cancelled", f"was_cancelled o/r {FULL}"),
    "pr-queue.sh": ("was_cancelled", f"was_cancelled {FULL}"),
    "forge.sh": ("hub_was_cancelled", f"hub_was_cancelled {FULL}"),
}


def _runs(*statuses):
    return '{"workflow_runs":[%s]}' % ",".join('{"status":"%s"}' % s for s in statuses)


def _extract(src: str, name: str) -> str:
    marker = f"\n{name}() {{"
    assert marker in src, f"{name}() not found -- this test would be vacuous"
    start = src.index(marker) + 1
    end = src.index("\n}\n", start) + len("\n}\n")
    func = src[start:end]
    assert len(func) < 8192, f"{name}() extracted as {len(func)} bytes -- the brace bound broke"
    return func


def _answer(tmp_path: pathlib.Path, file: str, payload: str) -> int:
    name, call = CALLERS[file]
    stub = tmp_path / f"stub-{file}"
    stub.write_text("#!/bin/sh\ncat <<'PAYLOAD_EOF'\n" + payload + "\nPAYLOAD_EOF\n")
    stub.chmod(0o755)
    # Every name any version of the three bodies reads, pointed at the one stub.
    prelude = (f"API={stub}\nHUB_API={stub}\nREPO=o/r\nSELF_DIR={SCRIPTS}\n"
               f"FORGE_HUB_API={stub}; export FORGE_HUB_API\n"
               "log() { printf '%s\\n' \"$1\" >&2; }\n"
               f"api() {{ \"{stub}\" \"$@\"; }}\n")
    prog = prelude + _extract((SCRIPTS / file).read_text(), name) + call + "\n"
    # `$0` is the real script, so a body that finds its siblings from `$0` finds them.
    return subprocess.run(["sh", "-c", prog, str(SCRIPTS / file)],
                          capture_output=True, text=True, cwd=tmp_path).returncode


@pytest.mark.parametrize("statuses, want", [
    (("cancelled", "running"), 3),              # the drift row: was 0 / 3 / 0
    (("cancelled",), 3),                        # a lone cancellation: the re-queue window
    (("cancelled", "success", "success"), 0),   # cancelled and settled
    (("failure", "success"), 1),                # a real red
    ((), 2),                                    # nothing measured
])
def test_every_caller_gets_the_same_answer(tmp_path, statuses, want):
    got = {}
    for file in CALLERS:
        d = tmp_path / file
        d.mkdir()
        got[file] = _answer(d, file, _runs(*statuses))
    assert len(set(got.values())) == 1, f"callers disagree on {statuses}: {got}"
    assert set(got.values()) == {want}, f"{statuses}: every caller answered {got}, want {want}"
