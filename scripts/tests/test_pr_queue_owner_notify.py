"""A queue action tells the session that OWNS the branch, not the one that ran it.

Every per-PR outcome pr-queue.sh produced went to `PR_QUEUE_DETACHED`/`FORGE_TOOLS_WAKE_PID`: the
drain's exit message, the gate-watch verdict, the prune refusal. That is the session that INVOKED
the queue, which owns no worktree on the branch. Measured: four refused prunes and two un-drafts
reported that way, one of them merged by another session's drain with its author never told.

Neither git nor the forge can answer "who owns this branch": every agent commits under one identity
and every PR is opened by one account. /proc can, and nothing in the conversation writes it.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
QUEUE = REPO / "scripts" / "pr-queue.sh"


def _owner_pid(hub: Path, branch: str, proc: Path | None = None, pr: str = "", repo: str = "",
               registry: Path | None = None) -> str:
    """Call the helper out of the real script, so the test measures shipped code."""
    body = subprocess.run(["sed", "-n", "/^_owner_pid() {/,/^}/p", str(QUEUE)],
                          capture_output=True, text=True, check=True).stdout
    assert "_owner_pid()" in body, "the helper was renamed; this test would silently measure nothing"
    # A `sed` range ending at /^}/ stops at the FIRST column-0 `}`, so a line like that added inside
    # the body truncates the extraction and this file would measure a fragment -- which starts with
    # `_owner_pid() {` and satisfies the assert above. Parsing it is what catches that: a truncated
    # function leaves its heredoc unterminated and `sh -n` refuses.
    chk = subprocess.run(["sh", "-n", "-c", body], capture_output=True, text=True)
    assert chk.returncode == 0, (
        f"the extracted function does not parse, so the range truncated it:\n{chk.stderr}")
    # AGENT_COMMS at its default, whatever the running session's environment says.
    env = {**os.environ, "HUB": str(hub), "TOOLS_DIR": str(QUEUE.parent),
           "FORGE_TOOLS_AGENT_COMMS": ""}
    if proc is not None:
        env["PR_QUEUE_PROC"] = str(proc)
    env["REPO"] = repo
    env["GATE_WATCH_REGISTRY"] = str(registry or hub / "no-registry.jsonl")
    return subprocess.run(["sh", "-c", f'HUB="$HUB"\nTOOLS_DIR="$TOOLS_DIR"\n{body}\n_owner_pid "$1" "$2"',
                           "_", branch, pr],
                          capture_output=True, text=True, timeout=60, env=env).stdout.strip()


@pytest.fixture
def world(tmp_path):
    """A repo with a real worktree on a branch, plus a /proc fixture we control."""
    hub = tmp_path / "hub"
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True)
    subprocess.run(["git", "-C", str(hub), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(hub), "config", "user.name", "t"], check=True)
    (hub / "f.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(hub), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(hub), "commit", "-qm", "base"], check=True)
    # `_owner_pid` reads `agent_comms.AGENT_COMMS` and gate-watch.py's registry helpers out of
    # $TOOLS_DIR -- the shipped scripts/ here -- which is how it avoids deciding for itself what
    # counts as an agent. Without them the helper bails -- indistinguishable from "no owner", which
    # is exactly how a missing module once read as a cwd problem.
    wt = tmp_path / "wt-feat"
    subprocess.run(["git", "-C", str(hub), "worktree", "add", "-q", "-b", "agent/feat", str(wt)], check=True)
    proc = tmp_path / "proc"
    proc.mkdir()
    return {"hub": hub, "wt": wt, "proc": proc}


def _fake_pid(proc: Path, pid: str, comm: str, cwd: Path):
    d = proc / pid
    d.mkdir()
    (d / "comm").write_text(comm + "\n")
    (d / "cwd").symlink_to(cwd)


def test_the_session_whose_cwd_is_the_worktree_is_the_owner(world):
    _fake_pid(world["proc"], "4242", "claude", world["wt"])
    assert _owner_pid(world["hub"], "agent/feat", world["proc"]) == "4242"


def test_a_pid_in_a_SUBDIRECTORY_of_the_worktree_still_owns_it(world):
    sub = world["wt"] / "scripts"
    sub.mkdir()
    _fake_pid(world["proc"], "4243", "claude", sub)
    assert _owner_pid(world["hub"], "agent/feat", world["proc"]) == "4243"


def test_a_non_agent_process_in_the_worktree_is_NOT_an_owner(world):
    """The control that stops this reporting `pytest` or a shell as a session. `AGENT_COMMS`
    decides, read from `comm` -- never matched against command text."""
    _fake_pid(world["proc"], "4244", "pytest", world["wt"])
    assert _owner_pid(world["hub"], "agent/feat", world["proc"]) == ""


def test_an_agent_OUTSIDE_the_worktree_is_not_its_owner(world):
    """The case this session is itself in: launched in the reference tree, working a worktree by
    `cd` per command. It resolves to nobody, which is why the caller must keep its fallback."""
    _fake_pid(world["proc"], "4245", "claude", world["hub"])
    assert _owner_pid(world["hub"], "agent/feat", world["proc"]) == ""


def test_an_unknown_branch_resolves_to_nobody(world):
    _fake_pid(world["proc"], "4246", "claude", world["wt"])
    assert _owner_pid(world["hub"], "agent/no-such-branch", world["proc"]) == ""


def test_a_refs_pull_head_ref_resolves_to_nobody(world):
    """Forgejo reports `head.ref` as `refs/pull/N/head` once the branch is retired. That matches no
    worktree, so the resolver finds nobody rather than guessing."""
    _fake_pid(world["proc"], "4247", "claude", world["wt"])
    assert _owner_pid(world["hub"], "refs/pull/99/head", world["proc"]) == ""



# --- the owner `pr create` recorded is an owner too --------------------------------------------
#
# `pr create` writes its author into gate-watch's registry as a subscription on (repo, PR number).
# The cwd scan above cannot see a session that works its worktree by per-command `cd`, and every
# drain log of one day said "#N's owner does not resolve" for such PRs, the batch members whose
# verdict then reached only the drain's session included.

def _live(proc: Path, pid: str, cwd: Path, start: str, comm: str = "claude"):
    _fake_pid(proc, pid, comm, cwd)
    # stat fields 4-21 sit between the state and starttime (field 22), as test_gate_watch.py's `_proc`.
    (proc / pid / "stat").write_text(f"{pid} ({comm}) S " + " ".join(["0"] * 18) + f" {start} 0 0\n")


def _registry(tmp_path: Path, *events: dict) -> Path:
    reg = tmp_path / "gate-watch.jsonl"
    reg.write_text("".join(json.dumps(e) + "\n" for e in events))
    return reg


def _sub(pid: int, pr: str = "12", start: str = "777", repo: str = "o/r", event: str = "subscribe",
         at: str = "2026-09-22T22:00:00Z"):
    return {"at": at, "event": event, "repo": repo, "pr": pr, "pid": pid, "proc_start": start}


def test_a_live_subscriber_owns_the_PR_when_no_worktree_cwd_resolves(world, tmp_path):
    _live(world["proc"], "5151", world["hub"], "777")   # outside the worktree, as this session was
    reg = _registry(tmp_path, _sub(5151))
    assert _owner_pid(world["hub"], "agent/feat", world["proc"], "12", "o/r", reg) == "5151"


def test_a_retired_branch_still_resolves_through_its_subscription(world, tmp_path):
    """`head.ref` reads `refs/pull/N/head` once the branch is gone, which no worktree matches. The
    PR number still names the author."""
    _live(world["proc"], "5151", world["hub"], "777")
    reg = _registry(tmp_path, _sub(5151))
    assert _owner_pid(world["hub"], "refs/pull/12/head", world["proc"], "12", "o/r", reg) == "5151"


def test_a_RECYCLED_subscriber_pid_is_not_the_owner(world, tmp_path):
    """PASSES ON BASE: the control. Same pid, different process start: messaging it would reach a
    stranger, so it resolves to nobody."""
    _live(world["proc"], "5151", world["hub"], "999")
    reg = _registry(tmp_path, _sub(5151, start="777"))
    assert _owner_pid(world["hub"], "agent/feat", world["proc"], "12", "o/r", reg) == ""


def test_an_ended_or_foreign_subscription_is_not_the_owner(world, tmp_path):
    """PASSES ON BASE: an unsubscribed author, another PR's author, and another repo's author all
    resolve to nobody."""
    _live(world["proc"], "5151", world["hub"], "777")
    ended = _registry(tmp_path, _sub(5151), _sub(5151, event="unsubscribe", at="2026-09-22T22:10:00Z"))
    assert _owner_pid(world["hub"], "agent/feat", world["proc"], "12", "o/r", ended) == ""
    foreign = _registry(tmp_path, _sub(5151, pr="13"), _sub(5151, repo="o/other"))
    assert _owner_pid(world["hub"], "agent/feat", world["proc"], "12", "o/r", foreign) == ""


def test_the_worktree_session_still_wins_over_a_subscriber(world, tmp_path):
    """PASSES ON BASE: the cwd arm is unchanged and consulted first."""
    _fake_pid(world["proc"], "4242", "claude", world["wt"])
    _live(world["proc"], "5151", world["hub"], "777")
    reg = _registry(tmp_path, _sub(5151))
    assert _owner_pid(world["hub"], "agent/feat", world["proc"], "12", "o/r", reg) == "4242"


def test_every_caller_that_knows_the_PR_passes_it():
    """The arm is reachable only through callers that hand it the number. Every call that derives the
    branch from a PR with `_head_ref_of` has that PR in hand, so each must pass it on."""
    import re
    calls = re.findall(r'_(?:owner_pid|notify_owner) "\$\(_head_ref_of "\$(\w+)"\)"(.*)', QUEUE.read_text())
    assert len(calls) >= 4, "the scan matched %d calls -- it is not reading the call sites" % len(calls)
    dropped = [(var, rest) for var, rest in calls if '"$%s"' % var not in rest]
    assert not dropped, "these calls drop the PR number they derived the branch from: %s" % dropped


def test_a_held_branch_refusal_reaches_the_PR_author():
    """`delete_merged_branch` refuses while a worktree holds the branch and tells the owner -- but it
    passed no PR number, so the subscription arm never ran and an author working its worktree from
    outside heard nothing (measured 2026-09-26). Every caller must hand it the PR it merged, and
    it must hand that on."""
    import re
    src = QUEUE.read_text()
    calls = re.findall(r'^\s*(?:\[.*\] && )?delete_merged_branch (.*)$', src, re.M)
    assert len(calls) >= 3, "the scan matched %d calls -- it is not reading the call sites" % len(calls)
    one_arg = [c for c in calls if len(re.findall(r'"\$\w+"', c)) < 2]
    assert not one_arg, "these calls drop the PR number: %s" % one_arg
    m = re.search(r'^delete_merged_branch\(\) \{\n(.*?)^\}', src, re.M | re.S)
    assert m, "delete_merged_branch was renamed; this test would measure nothing"
    body = m.group(1)
    notify = re.findall(r'_notify_owner .*', body)
    assert notify and all(n.rstrip().endswith('"$_dpr"') for n in notify), notify


# --- the helper existed, was correct, and was not REACHABLE on the approve path -----------------
#
# Everything above extracts `_owner_pid` with `sed` and runs it in isolation, so it measures the
# helper's LOGIC and never its availability. The helper was fine. `_notify_owner`, which calls it,
# was defined 942 lines BELOW its only call site -- and `sh` binds a function name when the
# definition is EXECUTED, not when the file is parsed. This script's verb dispatch blocks are
# interleaved with its definitions, so which helpers exist depends on which verb ran.
#
# MEASURED in both directions, same code, same branch:
#   approve  -> `pr-queue.sh: 1072: _notify_owner: not found`; the merge succeeded, exit 0,
#               and the owner was never told -- the exact outcome the owner notice exists to prevent.
#   drain    -> `no live session resolves for agent/... -- nobody was messaged about: ...`
#                    i.e. the function RAN, because `drain` dispatches below the definition.


def _called_before_defined(path):
    """(violations, functions_seen) -- every function called at a line before its definition."""
    import re
    lines = Path(path).read_text().split("\n")
    defs = {}
    for i, l in enumerate(lines, 1):
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{', l)
        if m:
            defs.setdefault(m.group(1), i)
    out = []
    for name, dline in defs.items():
        for i, l in enumerate(lines, 1):
            if i == dline or re.match(r'^%s\(\)' % re.escape(name), l) or l.lstrip().startswith("#"):
                continue
            if re.search(r'(^|[^A-Za-z0-9_$.])%s(\s|$|")' % re.escape(name), l):
                if i < dline:
                    out.append((name, dline, i))
                break
    return out, len(defs)


def test_pr_queue_defines_every_function_before_it_calls_it():
    bad, seen = _called_before_defined(QUEUE)
    assert seen > 30, "the scan found %d functions -- it is not reading the script" % seen
    assert not bad, (
        "called before defined, so absent on any verb whose dispatch sits above the definition: %s"
        % bad)


def test_the_ordering_scan_can_actually_fail__control(tmp_path):
    """THE CONTROL. A regex sweep over a file is the exact shape that reports a clean tree after it
    has quietly stopped matching anything, so the all-clear above is only worth what this proves."""
    planted = tmp_path / "planted.sh"
    planted.write_text(
        "#!/bin/sh\ncaller() {\n    _helper x\n}\n_helper() {\n    echo hi\n}\n"
        + "\n".join("f%d() { :; }" % n for n in range(40)) + "\n")
    bad, seen = _called_before_defined(planted)
    assert seen > 30, seen
    assert [b[0] for b in bad] == ["_helper"], bad


def _run_is_live(pid: str) -> subprocess.CompletedProcess:
    body = subprocess.run(["sed", "-n", "/^_run_is_live() {/,/^}/p", str(QUEUE)],
                          capture_output=True, text=True, check=True).stdout
    assert "_run_is_live()" in body, "the helper was renamed; this test would silently measure nothing"
    return subprocess.run(["sh", "-c", f'{body}\n_run_is_live "$1"', "_", pid],
                          capture_output=True, text=True, timeout=30)


def test_a_dead_claimant_is_not_live_and_says_nothing():
    """`2>/dev/null` after the `<` came too late: the failed open of a dead pid's cmdline
    printed `cannot open /proc/<pid>/cmdline` into every drain log that followed a dead claimant."""
    r = _run_is_live("999999999")
    assert r.returncode != 0, "a dead pid read as a live queue run"
    assert r.stderr == "", "a dead claimant's pid leaked the shell's open error:\n" + r.stderr


def test_a_live_pr_queue_run_is_live__control():
    """THE CONTROL, and it PASSES ON BASE deliberately: a process whose argv names pr-queue.sh is
    live, so the quiet redirect did not also blind the check."""
    live = subprocess.Popen(["python3", "-c", "import signal; signal.pause()", "pr-queue.sh"])
    try:
        assert _run_is_live(str(live.pid)).returncode == 0
    finally:
        live.kill(); live.wait()


def test_seat_of_pid_labels_a_registered_seat_and_stays_silent_otherwise(tmp_path):
    """The owner line reads `pid N (seat CC-1)` when Session-Notify's registry has the pid;
    with no registry command on PATH, or a pid that registered no seat, the pid stands alone."""
    body = subprocess.run(["sed", "-n", "/^_seat_of_pid() {/,/^}/p", str(QUEUE)], capture_output=True, text=True).stdout
    assert "_seat_of_pid()" in body, "the helper was renamed; this test would silently measure nothing"
    b = tmp_path / "bin"; b.mkdir()
    (b / "session-notify-list").write_text('#!/bin/sh\ncat <<\'J\'\n[{"handle":"claude-code:4242","meta":{"pid":"4242","seat":"CC-1"}},{"handle":"omp:x","meta":{"pid":"5"}}]\nJ\n')
    (b / "session-notify-list").chmod(0o755)
    def run(pid, path):
        return subprocess.run(["sh", "-c", body + '\n_seat_of_pid "$1"', "_", pid], capture_output=True, text=True,
                              env={"PATH": path}).stdout
    assert run("4242", f"{b}:/usr/bin:/bin") == " (seat CC-1)\n"
    assert run("5", f"{b}:/usr/bin:/bin") == ""            # registered, no seat
    assert run("4242", "/usr/bin:/bin") == ""              # no registry on PATH
