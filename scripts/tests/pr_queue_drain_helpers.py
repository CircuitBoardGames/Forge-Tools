# The drain-test helpers test_late_hold_is_honoured imports.
"""The drain lands every READY PR from the head of the queue's order downward.

WHAT THIS FILE IS ACTUALLY ABOUT. The drain's design is one decision: which answers SKIP a PR and
keep going, and which STOP the run. `merge-requested` stops on any non-zero and says why -- the
assumption the next merge rests on is gone. The drain cannot use that rule, because the case it
exists for (a PR at the head nobody can land right now) is exactly the case that rule halts on. So
every test here is one cell of that matrix, and the two halves must be proven separately: a file
that only tested skips would pass against a drain that never stops, and vice versa.

The stub is the forge. It cannot tell us Forgejo's real behaviour -- see the ceiling note on
test_pr_queue_merges_what_it_opens.py, which applies here unchanged.
"""

import os
import subprocess
import time
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE = REPO_ROOT / "scripts" / "pr-queue.sh"


def _listing(order):
    """The open-PR listing the drain derives its order from, as the forge answers it.
    `None` is an answer that is not a listing -- an unreadable queue, never an empty one."""
    if order is None:
        return '{"message":"listing unreadable"}'
    return json.dumps([{"number": n, "head": {"ref": "b%d" % n}} for n in order])


def _stub(tmp_path, *, order, pulls, labels, blockers, merge_code="200", red="", runs=""):
    """A stub hub-api.sh serving every read the drain makes.

    `order` is the open-PR listing (a tuple of PR numbers, or None for an unreadable one), served
    for every page: the drain pages until a page adds nothing new, so page 2 ends the read.
    """
    log = tmp_path / "stub.log"
    api = tmp_path / "stub-api.sh"
    # NOT textwrap.dedent. The interpolated fragments below contain lines at column 0 (the per-PR
    # arms), so the common leading prefix is "" and dedent silently strips nothing -- leaving
    # `#!/bin/sh` indented, which is not a shebang at all. That surfaced as `Exec format error`
    # on ten tests at once. Written at column 0 instead, so there is nothing to strip.
    api.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "%s"\n'
        'case "$1" in\n'
        # every merging verb reads the wiki listing first, and an unanswered route is
        # CANNOT TELL, which refuses. An empty listing is the no-freeze answer.
        '"/api/v1/repos/"*"/wiki/pages") echo "[]" ;;\n'
        '"/api/v1/repos/"*"/pulls?state=open"*) echo \'%s\' ;;\n'
        # `was_cancelled` reads this endpoint. Unserved by default: the fall-through prints
        # nothing, which that function reports as "unreadable" -- NOT as "not cancelled" -- and
        # that is the state every test written before the cancellation read already ran under.
        '"/api/v1/repos/"*"/actions/runs"*)\n'
        'case "$1" in\n%s\n*) : ;;\nesac ;;\n'
        '"/api/v1/repos/"*"/pulls/"*)\n'
        'n=${1##*/}\n'
        'case "$n" in\n%s\n*) exit 1 ;;\nesac ;;\n'
        '"/api/v1/repos/"*"/issues/"*"/labels")\n'
        'rest=${1#*/issues/}; n=${rest%%%%/*}\n'
        'case "$n" in\n%s\n*) echo "[]" ;;\nesac ;;\n'
        "issue)\n"
        'case "$4" in\n%s\n*) echo "#$4 open_blockers=0" ;;\nesac ;;\n'
        "pr)\n"
        'case "$2" in\n'
        # `pr checks` keyed on the SHA ($4), so one PR in an ordered list can be red while the
        # rest are green. Unconditional green here was why the rc=7 arm was unreachable:
        # the natural stub makes the other ten tests pass and silently covers nothing.
        'checks)\n'
        'case "$4" in\n%s\n'
        '*) echo "OK: 1 registered, 0 skipped, 1 actually ran"; exit 0 ;;\n'
        'esac ;;\n'
        'merge)  echo "merged: http=%s"; exit 0 ;;\n'
        "esac ;;\n"
        "esac\n"
        "exit 0\n"
        % (log, _listing(order), runs, pulls, labels, blockers, red, merge_code)
    )
    api.chmod(0o755)
    return api, log


def _hub_repo(tmp_path):
    """A throwaway checkout level with its own `hub` remote.

    NOT the real tree. The staleness refusal above the verb dispatch is one of the earned
    refusals the drain keeps, and it fires correctly whenever the working tree is behind
    `hub/main` -- which any live worktree is, minutes after a peer merges. Pointing these tests at
    the real repo made all ten fail on that refusal, which was the guard working, not a bug. A
    throwaway repo is level with its own remote by construction, so the tests exercise the drain
    rather than the freshness of whatever tree happens to be running them.
    """
    hub = tmp_path / "hub"
    (hub / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True)
    for kv in (["user.email", "t@t"], ["user.name", "t"]):
        subprocess.run(["git", "config", *kv], cwd=hub, check=True)
    (hub / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=hub, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=hub, check=True)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "hub", str(bare)], cwd=hub, check=True)
    subprocess.run(["git", "push", "-q", "hub", "main"], cwd=hub, check=True)
    # The real live-run check, which the drain reads before it starts.
    (hub / "scripts" / "live-drains.sh").write_text(
        (REPO_ROOT / "scripts" / "live-drains.sh").read_text())
    # ...and the config reader it sources, which a real install keeps beside it.
    (hub / "scripts" / "ft-config.sh").write_text(
        (REPO_ROOT / "scripts" / "ft-config.sh").read_text())
    return hub


def _run(tmp_path, api, *args, **envextra):
    env = dict(
        os.environ,
        PR_QUEUE_FOREGROUND="1",  # never detach under a session-launched pytest
        PR_QUEUE_API=str(api),
        PR_QUEUE_HUB=str(_hub_repo(tmp_path)),
        PR_QUEUE_TOOLS_DIR=str(tmp_path / "hub" / "scripts"),  # siblings stay the fixture's
        PR_QUEUE_WT=str(tmp_path / "merge-wt"),
        # Where live runs are looked for: the fixture's, never this box's real ones.
        PR_QUEUE_WT_BASE=str(tmp_path / "CC-merge"),
        PR_QUEUE_LOG_DIR=str(tmp_path / "logs"),
        PR_QUEUE_REWAIT_SECS="0",
        PR_QUEUE_REWAIT_POLL_SECS="0",
        PR_QUEUE_POLL_SECS="0",
        PR_QUEUE_REPO="o/r",
        FORGE_TOOLS_REMOTE="hub",  # the fixture's forge remote, the name pr-queue.sh defaulted to
    )
    env.update(envextra)
    return subprocess.run(["sh", str(QUEUE), "drain", *args],
                          capture_output=True, text=True, env=env, timeout=180)


OPEN = '{}) echo \'{{"state":"open","title":"t","head":{{"sha":"%s","ref":"b%s"}}}}\' ;;'


def _open_pr(n):
    sha = str(n) * 40
    return ('%s) echo \'{"state":"open","title":"pr %s","head":{"sha":"%s","ref":"b%s"}}\' ;;'
            % (n, n, sha[:40], n))


# ------------------------------------------------------------------ refusals before any merge

