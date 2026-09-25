"""`was_cancelled` must tell a CANCELLED run from a FAILED one, offline.

`hub-api.sh pr checks` reads the commit-status API, where a cancelled job renders as
`failure`. So a queue driver that force-pushes and then reads `pr checks` sees a red it
caused and halts on it. `was_cancelled` in `scripts/pr-queue.sh` is the discriminator, and
it reads a different endpoint (`/actions/runs?head_sha=`) whose `status` field carries the
real value.

These tests run the shell function against a **stub** `hub-api.sh` on PATH, so they need
no network and no forge. What they gate is the decision logic and the short-sha refusal --
the two places it can go quietly wrong. The live endpoint's behaviour was measured
separately (see the comment block in pr-queue.sh); a test cannot pin a remote API.

Note the deliberate asymmetry under test: "unreadable" and "zero runs" must BOTH be
distinct from "not cancelled". Collapsing either into a plain false is how this function
would come to answer a question it never measured.
"""

from __future__ import annotations

import pathlib
import subprocess
import textwrap


REPO = pathlib.Path(__file__).resolve().parents[2]
PR_QUEUE = REPO / "scripts/pr-queue.sh"

FULL = "6f7abffa3053ecb7e967697920fc782354b9ba14"  # 40 chars
SHORT = "6f7abffa30"

CANCELLED = '{"workflow_runs":[{"id":1291,"status":"cancelled"},{"id":1290,"status":"success"}]}'
FAILED = '{"workflow_runs":[{"id":1291,"status":"failure"},{"id":1290,"status":"success"}]}'
ALL_OK = '{"workflow_runs":[{"id":1290,"status":"success"},{"id":1289,"status":"success"}]}'
EMPTY = '{"workflow_runs":[]}'
GARBAGE = "not json at all"
# The two in-flight shapes. Neither existed before, because until then the function could not
# tell them apart -- which is why the in-flight case had no test and the change that broke the
# settled case still looked green locally.
STILL_RUNNING = '{"workflow_runs":[{"id":1291,"status":"cancelled"},{"id":1290,"status":"running"}]}'
ONLY_CANCELLED = '{"workflow_runs":[{"id":1291,"status":"cancelled"}]}'


def _run(tmp_path: pathlib.Path, payload: str, sha: str) -> int:
    """Source pr-queue.sh's function with a stubbed hub-api.sh and call it."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    # A stub standing in for a script must BE a script: real shebang, exec bit, on PATH.
    stub = stub_dir / "hub-api.sh"
    stub.write_text("#!/bin/sh\ncat <<'PAYLOAD_EOF'\n" + payload + "\nPAYLOAD_EOF\n")
    stub.chmod(0o755)

    # Take only the function definition: the script body would try to drain a real queue.
    #
    # BOUNDED AT THE CLOSING BRACE, and that is not tidiness. This read used to slice from the
    # marker to END OF FILE, so "the function" was the whole tail of pr-queue.sh -- and that tail
    # is passed below as a single `sh -c` argument. Linux caps ONE argv element at MAX_ARG_STRLEN
    # = 32 pages = 131,072 bytes, and the tail had grown to 129,594: 1,478 bytes of headroom, for
    # every one of these six tests. The next commit to touch anything below `was_cancelled` was
    # going to red this file no matter what it changed or why. One did, on 2026-09-20, adding
    # 2,910 bytes ~500 lines further down, and every failure read `OSError: [Errno 7] Argument
    # list too long: 'sh'` -- which names the kernel and not the cause.
    #
    # Bounded, the same extraction is 709 bytes. The comment above was always the intent; it just
    # was not what the code did.
    src = PR_QUEUE.read_text()
    marker = "was_cancelled() {"
    assert marker in src, "was_cancelled() not found in pr-queue.sh -- test would be vacuous"
    start = src.index(marker)
    end = src.index("\n}\n", start) + len("\n}\n")
    func = src[start:end]
    # A slice that silently grew is exactly what this replaced, so assert the shape rather than
    # trusting it: a function that no longer closes at column 0, or one that has genuinely become
    # enormous, must fail HERE naming the size -- not downstream as an errno the reader has to
    # map back to a string length.
    assert func.rstrip().endswith("}"), "extraction did not end at a closing brace"
    assert len(func) < 8192, (
        f"was_cancelled() extracted as {len(func)} bytes -- either it grew hugely or the brace "
        "bound broke and this is slicing to EOF again (the 2026-09-20 defect)")

    prog = textwrap.dedent(f"""\
        API={stub}
        REPO=owner/repo
        SELF_DIR={PR_QUEUE.parent}
        log() {{ printf '%s\\n' "$1" >&2; }}
        {func}
        was_cancelled "{sha}"
    """)
    return subprocess.run(["sh", "-c", prog], capture_output=True, text=True, cwd=tmp_path).returncode


def test_the_function_exists_so_these_tests_are_not_vacuous():
    assert PR_QUEUE.is_file(), f"{PR_QUEUE} missing"
    assert "was_cancelled() {" in PR_QUEUE.read_text()


def test_a_cancelled_run_is_detected(tmp_path):
    """THE CONTRACT. rc 0 means "a run was cancelled", and this assertion is older than the in-flight split --
    every caller written against it reads 0 that way. The split narrowed 0 to "cancelled AND
    settled", which keeps this true because the CANCELLED fixture is one cancelled + one success:
    a finished head, the only shape this file ever exercised. The first version MOVED 0 instead,
    to the in-flight case, and this test went red without the diff touching this file -- a contract
    broken underneath a check that still looked like it passed."""
    assert _run(tmp_path, CANCELLED, FULL) == 0


def test_a_genuinely_failed_run_is_NOT_reported_as_cancelled(tmp_path):
    """The arm that matters: a real red must stay red, or the queue merges over failures."""
    assert _run(tmp_path, FAILED, FULL) == 1


def test_all_successful_runs_are_not_cancelled(tmp_path):
    assert _run(tmp_path, ALL_OK, FULL) == 1


def test_zero_runs_is_not_answered_as_not_cancelled(tmp_path):
    """An empty result is not an absence -- it must be distinguishable from a real 'no'."""
    assert _run(tmp_path, EMPTY, FULL) == 2


def test_unreadable_response_is_not_answered_as_not_cancelled(tmp_path):
    assert _run(tmp_path, GARBAGE, FULL) == 2


def test_a_short_sha_is_refused_rather_than_silently_returning_zero(tmp_path):
    """The live endpoint returns 0 runs for a short sha, which reads as 'no runs'."""
    assert _run(tmp_path, CANCELLED, SHORT) == 2


def test_a_LONE_cancellation_is_the_re_queue_window_and_is_not_finished(tmp_path):
    """The condition that nearly went in wrong: "every run is terminal" is TRUE of a head whose
    only run is the cancelled one -- the moment BEFORE the forge re-queues it. A verdict
    (success/failure) must have been reached for the head to count as finished."""
    assert _run(tmp_path, ONLY_CANCELLED, FULL) == 3
