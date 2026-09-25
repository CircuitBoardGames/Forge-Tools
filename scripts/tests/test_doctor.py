"""`scripts/doctor.sh` -- one read-only verb; every diagnosis proven able to fail.

Every instrument the doctor composes is a stub here, because each real one has its own test file
and its own measured fail-opens: what THIS file proves is that the doctor reads them, relays what
they say, counts it, and that its exit code cannot read as clean when a section measured nothing.
For each diagnosis: the fault injected -> FINDING and exit 1; the fault removed -> OK and exit 0.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCTOR = REPO / "scripts/doctor.sh"

# A stub hub-api.sh: routes are decided by files the test drops beside it, so a test changes ONE
# fact (a review landed, a PR closed) and nothing else.
API_STUB = r"""#!/bin/sh
d="$STUB_DIR"
case "$1" in
  */pulls?state=open*)     cat "$d/open.json" ;;
  */pulls/*/reviews)       n=${1##*/pulls/}; n=${n%%/*}; [ -f "$d/reviews.$n.json" ] && cat "$d/reviews.$n.json" || echo '[]' ;;
  */issues/*/dependencies) n=${1##*/issues/}; n=${n%%/*}; [ -f "$d/deps.$n.json" ] && cat "$d/deps.$n.json" || echo '[]' ;;
  *) echo '{}' ;;
esac
"""
REAP_STUB = "#!/bin/sh\ncat \"$STUB_DIR/reap.txt\"\n"
CHECK_STUB = "#!/bin/sh\n[ \"$1\" = --check ] || exit 0\n[ -f \"$STUB_DIR/unhealthy\" ] && { echo 'MISSING .claude/settings.local.json'; exit 1; }\nexit 0\n"
PRUNE_STUB = "#!/bin/sh\necho \"$@\" >> \"$STUB_DIR/prune.log\"\ncat \"$STUB_DIR/prune.txt\"\n"


def pr(n, state="open", merged=False, labels=(), ref="feat", sha="a" * 40):
    return json.dumps({"number": n, "state": state, "merged": merged, "head": {"ref": ref, "sha": sha},
                       "labels": [{"name": l} for l in labels]})


@pytest.fixture
def env(tmp_path):
    """A clean world: one open queued PR, matching local branch, no lock, nothing to prune."""
    d = tmp_path / "stub"; d.mkdir()
    for name, body in (("api.sh", API_STUB), ("reap.sh", REAP_STUB), ("check.sh", CHECK_STUB), ("prune.sh", PRUNE_STUB)):
        p = d / name; p.write_text(body); p.chmod(0o755)
    # A scratch repo whose `feat` branch is the PR head, so the divergence section has a subject.
    hub = tmp_path / "hub"; hub.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True)
    for kv in (["user.email", "t@t"], ["user.name", "t"]):
        subprocess.run(["git", "config", *kv], cwd=hub, check=True)
    (hub / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=hub, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=hub, check=True)
    subprocess.run(["git", "branch", "feat"], cwd=hub, check=True)
    sha = subprocess.run(["git", "rev-parse", "feat"], cwd=hub, capture_output=True, text=True, check=True).stdout.strip()
    (d / "open.json").write_text("[" + pr(42, sha=sha) + "]")
    (d / "reap.txt").write_text("[worktree-reap] LIVE    /x/CC-a [agent/a]\n[worktree-reap] scanned 3 processes\n")
    (d / "prune.txt").write_text("[prune] KEEP feat — no merged PR\n")
    wts = tmp_path / "worktrees.txt"; wts.write_text("")
    proc = tmp_path / "proc"; proc.mkdir()
    e = dict(os.environ)
    e.update({"STUB_DIR": str(d), "DOCTOR_HUB": str(hub), "DOCTOR_API": str(d / "api.sh"),
              "DOCTOR_REAP": str(d / "reap.sh"), "DOCTOR_CHECK": str(d / "check.sh"),
              "DOCTOR_PRUNE": str(d / "prune.sh"),
              "DOCTOR_WORKTREES": str(wts), "DOCTOR_FETCH": "0",
              # The literals doctor.sh defaulted to before they became configuration.
              "DOCTOR_REPO": "acme/app", "DOCTOR_REMOTE": "hub",
              "TMPDIR": str(tmp_path)})
    return {"env": e, "d": d, "hub": hub, "sha": sha, "wts": wts, "proc": proc}


def run(env, *args):
    return subprocess.run(["sh", str(DOCTOR), *args], capture_output=True, text=True, env=env["env"], timeout=120)


def test_a_clean_world_is_OK_in_every_section_and_exits_0(env):
    r = run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("OK           ") == 4, r.stdout
    assert "FINDING" not in r.stdout and "NOT CHECKED" not in r.stdout
    assert "findings=0 not-checked=0" in r.stdout


def test_an_unreadable_open_pr_listing_is_NOT_CHECKED_and_exits_2_not_0(env):
    """The exit code that separates 'checked, clean' from 'measured nothing'. The queue IS the open
    PRs (a hand-kept fence was removed), so an unreadable listing is an unmeasured queue, not an empty one."""
    env["d"].joinpath("open.json").write_text('{"message":"token expired"}')
    r = run(env)
    assert r.returncode == 2, r.stdout
    assert "NOT CHECKED  the open PR listing could not be read, so no queued PR was checked" in r.stdout
    # an EMPTY listing is measured and clean -- the control that the refusal is about readability
    env["d"].joinpath("open.json").write_text("[]")
    r = run(env)
    assert r.returncode == 0 and "order: (no open PRs)" in r.stdout, r.stdout


def test_a_held_pr_whose_approving_review_landed_is_a_stale_hold(env):
    d = env["d"]
    d.joinpath("open.json").write_text("[" + pr(42, sha=env["sha"], labels=["queue:needs-human-review"]) + "]")
    d.joinpath("reviews.42.json").write_text(json.dumps([{"state": "APPROVED", "user": {"login": "alice"}, "dismissed": False}]))
    r = run(env)
    assert r.returncode == 1
    assert "APPROVED review by alice already landed" in r.stdout and "approve 42" in r.stdout
    # the same hold with NO approval is the hold doing its job, not a finding
    d.joinpath("reviews.42.json").write_text("[]")
    r = run(env)
    assert r.returncode == 0 and "no approving review has landed" in r.stdout, r.stdout
    # and a DISMISSED approval does not count
    d.joinpath("reviews.42.json").write_text(json.dumps([{"state": "APPROVED", "user": {"login": "alice"}, "dismissed": True}]))
    assert run(env).returncode == 0


def test_a_dependency_edge_to_a_closed_pr_and_a_cycle_are_reported(env):
    d = env["d"]
    d.joinpath("deps.42.json").write_text(json.dumps([{"number": 7, "state": "closed"}]))
    r = run(env)
    assert r.returncode == 1 and "blocked by #7, which is closed" in r.stdout, r.stdout
    # a cycle: 42 -> 43 -> 42, both open and queued
    d.joinpath("open.json").write_text("[" + pr(43, ref="feat2", sha=env["sha"]) + "," + pr(42, sha=env["sha"]) + "]")
    d.joinpath("deps.42.json").write_text(json.dumps([{"number": 43, "state": "open"}]))
    d.joinpath("deps.43.json").write_text(json.dumps([{"number": 42, "state": "open"}]))
    r = run(env)
    assert r.returncode == 1 and "CYCLE" in r.stdout, r.stdout
    d.joinpath("deps.43.json").write_text("[]")
    r = run(env)
    assert "CYCLE" not in r.stdout and r.returncode == 0, r.stdout


def test_an_unreadable_dependency_listing_is_NOT_CHECKED_never_clean(env):
    env["d"].joinpath("deps.42.json").write_text("not json")
    r = run(env)
    assert r.returncode == 2 and "dependency edges could not be read" in r.stdout, r.stdout


def test_a_local_branch_ahead_of_its_pr_head_is_reported_and_stops(env):
    hub = env["hub"]
    subprocess.run(["git", "checkout", "-q", "feat"], cwd=hub, check=True)
    (hub / "g").write_text("more")
    subprocess.run(["git", "add", "-A"], cwd=hub, check=True)
    subprocess.run(["git", "commit", "-qm", "unpushed"], cwd=hub, check=True)
    r = run(env)
    assert r.returncode == 1, r.stdout
    assert "local branch feat is 1 commit(s) AHEAD of PR #42" in r.stdout and "STOP" in r.stdout
    # --fix must not touch it: nothing in the fix path pushes or resets
    r = run(env, "--fix")
    assert "AHEAD" in r.stdout
    assert subprocess.run(["git", "rev-list", "--count", "main..feat"], cwd=hub, capture_output=True, text=True).stdout.strip() == "1"


def test_a_pr_head_this_tree_does_not_have_is_reported_as_such_not_as_ahead(env):
    """The queue rebased the PR (or someone pushed from elsewhere): the PR head is a commit this
    tree has never seen. No ahead/behind count is honest, and calling it AHEAD would send the
    reader to push work that is not there."""
    d = env["d"]
    unknown = "b" * 40
    d.joinpath("open.json").write_text("[" + pr(42, sha=unknown) + "]")
    r = run(env)
    assert r.returncode == 1 and "NOT IN THIS TREE" in r.stdout and "AHEAD" not in r.stdout, r.stdout


def test_a_pr_head_that_moved_past_the_local_branch_is_reported(env):
    hub, d = env["hub"], env["d"]
    (hub / "h").write_text("landed")
    subprocess.run(["git", "add", "-A"], cwd=hub, check=True)
    subprocess.run(["git", "commit", "-qm", "queue rebased feat onto this"], cwd=hub, check=True)
    moved = subprocess.run(["git", "rev-parse", "HEAD"], cwd=hub, capture_output=True, text=True, check=True).stdout.strip()
    d.joinpath("open.json").write_text("[" + pr(42, sha=moved) + "]")
    r = run(env)
    assert r.returncode == 1 and "PR #42's head moved 1 commit(s) past local feat" in r.stdout, r.stdout


def test_reaper_verdicts_are_relayed_and_counted_and_fix_calls_delete(env):
    d = env["d"]
    d.joinpath("reap.txt").write_text(
        "[worktree-reap] LIVE    /x/CC-a [agent/a]\n"
        "[worktree-reap] ORPHAN  /x/CC-b [agent/b] — the ownership test matched nothing\n"
        "[worktree-reap] REFUSED /x/CC-c [agent/c] — contains modified or untracked files\n"
        "[worktree-reap] HELD    /x/CC-d [agent/d] — hub lists NO merged PR\n"
        "[worktree-reap] 1 live agent(s) have the reference tree as cwd. Those sessions own worktrees this test CANNOT locate\n")
    r = run(env)
    assert r.returncode == 1
    assert "clean orphan worktree: /x/CC-b" in r.stdout
    assert "DIRTY orphan (reported, STOP" in r.stdout and "/x/CC-c" in r.stdout
    assert "held worktree" in r.stdout and "/x/CC-d" in r.stdout
    assert "CAVEAT" in r.stdout and "CANNOT locate" in r.stdout, "the reaper's own ceiling must travel with its verdicts"
    assert "findings=3" in r.stdout
    # --fix delegates to the reaper's --delete and nothing else; the stub records the argv it got
    d.joinpath("reap.sh").write_text("#!/bin/sh\necho \"$@\" >> \"$STUB_DIR/reap.log\"\ncat \"$STUB_DIR/reap.txt\"\n")
    run(env, "--fix")
    assert "--delete" in d.joinpath("reap.log").read_text()


def test_the_health_check_runs_on_every_listed_worktree(env):
    env["wts"].write_text(str(env["hub"]) + "\n")
    env["d"].joinpath("unhealthy").write_text("")
    r = run(env)
    assert r.returncode == 1 and "fails its health check" in r.stdout and "settings.local.json" in r.stdout, r.stdout
    env["d"].joinpath("unhealthy").unlink()
    assert run(env).returncode == 0


def test_merged_but_unpruned_branches_are_relayed_and_fix_calls_delete(env):
    d = env["d"]
    d.joinpath("prune.txt").write_text("[prune] WOULD DELETE feat(abc) — merged per hub (rerun with --delete)\n")
    r = run(env)
    assert r.returncode == 1 and "merged per hub, not pruned" in r.stdout, r.stdout
    run(env, "--fix")
    assert "prune-merged --delete" in d.joinpath("prune.log").read_text()


def test_a_pruner_refusal_is_NOT_CHECKED(env):
    env["d"].joinpath("prune.txt").write_text("REFUSING: this checkout is 3 behind hub/main\n")
    r = run(env)
    assert r.returncode == 2 and "prune-merged refused" in r.stdout, r.stdout


def test_an_interrupted_drain_shows_as_a_rebase_in_the_queue_worktree(env, tmp_path):
    """The honest interrupted-drain signal: the queue's own worktree mid-rebase, even when that
    worktree is not in the listed set."""
    qwt = tmp_path / "CC-merge"
    subprocess.run(["git", "init", "-q", str(qwt)], check=True)
    (qwt / ".git" / "rebase-apply").mkdir()
    env["env"]["PR_QUEUE_WT"] = str(qwt)
    r = run(env)
    assert r.returncode == 1 and "rebase IN PROGRESS in %s" % qwt in r.stdout, r.stdout


def test_a_rebase_in_progress_is_reported_with_the_skill_pointer_and_never_touched(env):
    hub = env["hub"]
    env["wts"].write_text(str(hub) + "\n")
    marker = hub / ".git" / "rebase-merge"; marker.mkdir()
    r = run(env, "--fix")
    assert r.returncode == 1 and "rebase IN PROGRESS" in r.stdout and "resolving-merge-conflicts" in r.stdout, r.stdout
    assert marker.is_dir(), "--fix must not touch an in-progress rebase"
    marker.rmdir()
    assert run(env).returncode == 0


def test_every_section_names_what_it_read(env):
    r = run(env)
    for head in ("== worktrees  (read:", "== queue  (read:", "== forge/local divergence  (read:", "== mid-flight  (read:"):
        assert head in r.stdout, head


def test_an_unknown_argument_is_refused_before_anything_runs(env):
    """Pins the usage text, not just the code: `sh` exits 2 for a MISSING script too, so on a base
    without doctor.sh a code-only assertion passed and the new-tests gate said so."""
    r = run(env, "--force")
    assert r.returncode == 2 and "SUMMARY" not in r.stdout
    assert "--fix" in r.stderr and "read-only" in r.stderr, r.stderr
