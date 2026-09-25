"""`scripts/forge.sh` — the client seam.

The seam's whole claim is that NOTHING ABOVE IT BRANCHES ON WHICH FORGE IT IS TALKING TO. That
claim is only worth as much as the three differences it absorbs, and every one of them fails
SILENTLY rather than loudly if the absorption is wrong:

  * the merge subject — a bare subject on Forgejo lands without `(#N)` and `main` refuses
    force-push, so the number is permanently missing and nothing errors;
  * the gate — zero registered checks reads as a pass on BOTH forges (`total_count: 0` with an
    empty state on Forgejo, `total_count: 0` with HTTP 200 on GitHub), and on GitHub a re-run
    APPENDS a check-run rather than replacing one, so a dead failure pins a verdict red forever;
  * a cancellation — Forgejo renders a cancelled run as `failure`, so a driver that rebases at
    admission reads a red it caused itself and stops the queue.

So these drive the real script with BOTH clients stubbed, and assert on the argv the seam
produced and the exit code it chose. Stubbing is the point rather than a compromise: the
behaviour under test is what `forge.sh` ASKS each client for, and a live forge would let a
correct-looking answer come from the wrong endpoint.

WHAT THESE TESTS DO NOT COVER, stated here so a green run cannot be read as covering it: the
GitHub arm's WRITE paths have never run against github.com. The token on this box is read-only
on third-party repos — every read succeeds, every write 403s, and only the write attempt
reveals it. `test_github_land_*` asserts the argv `gh` would have been given, not that GitHub
accepted it. Clearance is not capability.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FORGE = REPO / "scripts" / "forge.sh"

SHA = "1111111111111111111111111111111111111111"
OTHER = "2222222222222222222222222222222222222222"

# One stub stands in for both `hub-api.sh` and `gh`. It logs its argv and answers from a table
# of substring matches, so a test states the request it expects to see and the reply to give.
# A request matching NOTHING exits 1 with a loud message — an unstubbed call must not look like
# an empty result, which is the failure mode this whole file is about.
STUB = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["STUB_DIR"]
argv = " ".join(sys.argv[1:])
with open(os.path.join(d, os.path.basename(sys.argv[0]) + ".argv"), "a") as fh:
    fh.write(argv + "\n")
table = json.load(open(os.path.join(d, "responses.json")))
for key, reply in table.items():
    if key in argv:
        sys.stdout.write(reply.get("out", ""))
        sys.stderr.write(reply.get("err", ""))
        raise SystemExit(reply.get("rc", 0))
sys.stderr.write("STUB: no canned reply for %r\n" % argv)
raise SystemExit(1)
'''


def make_stubs(tmp_path: Path, responses: dict) -> dict:
    d = tmp_path / "stub"
    d.mkdir(parents=True, exist_ok=True)
    (d / "responses.json").write_text(json.dumps(responses))
    for name in ("hub-api.sh", "gh"):
        p = d / name
        p.write_text(STUB)
        p.chmod(0o755)
    return {"STUB_DIR": str(d), "FORGE_HUB_API": str(d / "hub-api.sh"), "FORGE_GH": str(d / "gh")}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def make_tree(tmp_path: Path, remotes: dict, name: str = "tree") -> Path:
    """A git work tree carrying the given remotes. Nothing is fetched; the seam resolves its
    forge from the remote URLs alone, which is the property being tested."""
    root = tmp_path / name
    root.mkdir()
    git(root.parent, "init", "-q", str(root))
    for rname, url in remotes.items():
        git(root, "remote", "add", rname, url)
    return root


def run(tree: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.pop("FORGE_KIND", None)
    e.pop("FORGE_REPO", None)
    # The forge's hosts and remote name are configuration: an ssh alias, the https host, an address.
    e.update(FORGE_TOOLS_FORGE_HOSTS="forge-ssh forge.example.org 192.0.2.10", FORGE_TOOLS_REMOTE="hub")
    e.update(env or {})
    return subprocess.run([str(FORGE), *args], cwd=str(tree), capture_output=True, text=True, env=e)


def argv_log(env: dict, client: str) -> str:
    p = Path(env["STUB_DIR"]) / f"{client}.argv"
    return p.read_text() if p.exists() else ""


HUB_URL = "git@forge-ssh:acme/app.git"
GH_URL = "git@github.com:acme/vendored.git"


# ---------------------------------------------------------------------------------------
# Resolution — the branch the seam removes from every caller above this file.
# ---------------------------------------------------------------------------------------

def test_hub_remote_resolves_to_hub(tmp_path):
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    out = run(tree, "where").stdout
    assert "forge hub" in out
    assert "repo  acme/app" in out


def test_github_remote_resolves_to_github(tmp_path):
    tree = make_tree(tmp_path, {"origin": GH_URL})
    out = run(tree, "where").stdout
    assert "forge github" in out
    assert "repo  acme/vendored" in out


def test_both_remotes_resolve_to_hub_and_say_so(tmp_path):
    """The mirror-only rule, enforced by resolution order rather than by a rule
    somebody has to remember. This repo has both remotes; hub must win, and the GitHub remote
    that lost must be NAMED — a silent precedence is indistinguishable from not having noticed."""
    tree = make_tree(tmp_path, {"hub": HUB_URL, "origin": "git@github.com:acme/app.git"})
    out = run(tree, "where").stdout
    assert "forge hub" in out
    assert "mirror-only" in out and "origin" in out


def test_unrecognised_remote_refuses_rather_than_defaulting(tmp_path):
    """A forge the seam does not know must not fall through to a default. Guessing the forge is
    the exact branch this file exists to remove, and a wrong guess acts on somebody's repo."""
    tree = make_tree(tmp_path, {"origin": "git@gitlab.com:someone/thing.git"})
    r = run(tree, "where")
    assert r.returncode != 0
    assert "no remote" in r.stderr and "FORGE_KIND" in r.stderr


# ---------------------------------------------------------------------------------------
# The forge is named by the AUTHORITY, never by a path segment.
#
# `remote_kind` used to glob the whole URL, so a token anywhere in it named the forge while git
# dialled the host. Found by checking worktrunk's 0.74.0 `ssh://` fix against this file.
#
# Nothing else here asserts this property, and the two cases fail DIFFERENTLY, which is the
# reason they are separate tests rather than one parametrised pair:
#
#   * the github token in a path was NOT caught at all — `forge where` printed a confident
#     `forge github` / `repo github.com/repo`;
#   * the hub token in a path WAS caught, by `remote_slug`'s arity check — a guard that is
#     absent, standing next to something that happens to fail first. That check is about the
#     shape of a slug and knows nothing about hosts, so it must not be cited as the control
#     for this property. The assertion below is on WHICH refusal fires, for exactly that
#     reason: if resolution stops rejecting these, the arity check would keep the second test
#     green while the property it names is gone.
# ---------------------------------------------------------------------------------------

def test_github_token_in_the_path_does_not_name_the_forge(tmp_path):
    tree = make_tree(tmp_path, {"origin": "ssh://git@attacker.invalid/github.com/repo.git"})
    r = run(tree, "where")
    assert r.returncode != 0, f"a path segment named the forge: {r.stdout!r}"
    assert "forge github" not in r.stdout
    assert "no remote" in r.stderr and "FORGE_KIND" in r.stderr


def test_hub_token_in_the_path_is_refused_by_RESOLUTION_not_by_the_slug_check(tmp_path):
    tree = make_tree(tmp_path, {"origin": "https://attacker.invalid/forge.example.org/owner/repo.git"})
    r = run(tree, "where")
    assert r.returncode != 0, f"a path segment named the forge: {r.stdout!r}"
    assert "forge hub" not in r.stdout
    assert "no remote" in r.stderr and "FORGE_KIND" in r.stderr
    # The pre-fix refusal, and the thing this must NOT be relying on.
    assert "does not name a plain owner/repo" not in r.stderr


@pytest.mark.parametrize("url,kind,slug", [
    ("git@forge-ssh:acme/app.git", "hub", "acme/app"),
    ("https://forge.example.org/acme/app.git", "hub", "acme/app"),
    ("ssh://git@forge.example.org:2222/acme/app.git", "hub", "acme/app"),
    ("git@github.com:acme/vendored.git", "github", "acme/vendored"),
    ("https://github.com/acme/vendored.git", "github", "acme/vendored"),
])
def test_real_url_forms_still_resolve(tmp_path, url, kind, slug):
    """The positive control for the change above. Narrowing a match is the direction that breaks
    valid input silently, so every URL form this box actually uses — scp, https, and a port —
    is asserted to still resolve rather than assumed to."""
    tree = make_tree(tmp_path, {"origin": url})
    out = run(tree, "where").stdout
    assert f"forge {kind}" in out
    assert f"repo  {slug}" in out


# ---------------------------------------------------------------------------------------
# The gate — one verdict shape, two entirely different endpoints underneath.
# ---------------------------------------------------------------------------------------

def hub_status(rows, total=None):
    return json.dumps({"total_count": total if total is not None else len(rows),
                       "state": "", "statuses": [{"context": c, "status": s} for c, s in rows]})


def gh_runs(rows):
    """rows: (name, status, conclusion, started_at)"""
    return json.dumps([{"total_count": len(rows),
                        "check_runs": [{"name": n, "status": st, "conclusion": c,
                                        "started_at": t, "id": i}
                                       for i, (n, st, c, t) in enumerate(rows)]}])


def test_hub_gate_green_exits_zero(tmp_path):
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "success"), ("lint", "skipped")])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "gate: GREEN  registered=2 skipped=1 pending=0 failed=0" in r.stdout
    assert f"measured {SHA}" in r.stdout


def test_gate_never_claims_what_it_cannot_measure(tmp_path):
    """`hub-api.sh pr checks` ends `N registered, K skipped, M actually ran`, and that
    last number is wrong in the direction that matters: a job can start, skip all its steps
    internally and report success, and the commit-status API cannot see inside a job. The seam
    wraps that layer and must not inherit the claim."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "success"), ("eslint", "success")])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 0
    assert "actually ran" not in (r.stdout + r.stderr)
    # And the seam must not have got its answer from the verb that says it.
    assert "pr checks" not in argv_log(env, "hub-api.sh")


@pytest.mark.parametrize("exists,expected", [
    (True, "has NO checks registered"),
    (False, "is not a commit"),
])
def test_hub_gate_zero_registered_refuses_and_discriminates(tmp_path, exists, expected):
    """Zero registered checks is the shape of a workflow that never triggered, and Forgejo
    answers it `total_count: 0` with an EMPTY state — so every natural test calls it a pass.

    The refusal must also say WHICH zero it is. A typo'd sha and the registration race look
    identical from `total_count`, and their correct responses are opposite: one is "fix the
    sha", the other is "wait". Getting this backwards tells a caller to wait forever."""
    responses = {f"/commits/{SHA}/status": {"out": hub_status([], total=0)}}
    responses[f"/git/commits/{SHA}"] = {"out": "{}", "rc": 0 if exists else 22}
    env = make_stubs(tmp_path, responses)
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert expected in r.stderr


def test_hub_gate_pending_is_not_red(tmp_path):
    """`hub-api.sh pr checks` returns 1 for `pending` and 1 for `failure` alike, which is why
    `pr-queue.sh` has to parse its printed state instead of reading its status. The seam gives
    waiting its own code so a driver can branch on `$?`."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "pending"), ("lint", "success")])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "gate: PENDING" in r.stdout


def test_hub_gate_real_failure_is_red(tmp_path):
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "failure")])},
        # The run exists and did NOT cancel: this red is a test failure and must stay one.
        f"/actions/runs?head_sha={SHA}": {"out": json.dumps({"workflow_runs": [{"status": "failure"}]})},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "gate: RED" in r.stdout


def test_hub_gate_cancellation_is_not_a_test_failure(tmp_path):
    """Forgejo renders a CANCELLED run as `failure` in the commit-status API and supersedes the
    previous run on a push. A drain that rebases at admission therefore reads a red it caused
    itself. Same statuses as the test above; only the run's own status differs."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "failure")])},
        f"/actions/runs?head_sha={SHA}": {"out": json.dumps({"workflow_runs": [{"status": "cancelled"}]})},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "gate: CANCELLED" in r.stdout


def test_hub_gate_unreadable_run_list_stays_red(tmp_path):
    """An unreachable or unparseable run listing is NOT evidence of "not cancelled" — but it is
    also not evidence of a cancellation. The safe direction is the louder verdict: stay RED."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "failure")])},
        f"/actions/runs?head_sha={SHA}": {"out": "not json at all"},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 1, r.stdout + r.stderr


def test_github_gate_reads_check_runs_not_commit_status(tmp_path):
    """Measured 2026-08-24 on `cli/cli@5d3c4817f1`, a commit carrying 1251 check-runs: GitHub's
    COMBINED STATUS API answers `state: "pending"`, `total_count: 0`, because Actions writes
    check-RUNS and not commit statuses. Reading the endpoint that works on Forgejo would measure
    nothing here and say so confidently."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/check-runs": {"out": gh_runs([("test", "completed", "success", "2026-01-01T00:00:00Z")])},
    })
    tree = make_tree(tmp_path, {"origin": GH_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    log = argv_log(env, "gh")
    assert "check-runs" in log
    assert f"/commits/{SHA}/status" not in log


def test_github_gate_keeps_only_the_latest_run_per_name(tmp_path):
    """GitHub APPENDS a check-run for every re-run and every scheduled workflow on the same sha
    rather than replacing the old one — measured: 96 runs named `conclusion` on one commit,
    1251 in total. Counting them raw pins the verdict on a dead failure forever."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/check-runs": {"out": gh_runs([
            ("test", "completed", "failure", "2026-01-01T00:00:00Z"),   # the stale re-run
            ("test", "completed", "success", "2026-01-02T00:00:00Z"),   # the live one
        ])},
    })
    tree = make_tree(tmp_path, {"origin": GH_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "registered=1" in r.stdout, "the two runs share a name and must collapse to one"


def test_github_gate_zero_registered_refuses(tmp_path):
    """`gh api .../check-runs` exits 0 with `total_count: 0`, and `gh pr checks` prints
    `no checks reported` and also exits 0 — measured on
    a vendored fork's commit `94732e1646`. Zero must refuse on BOTH forges."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/check-runs": {"out": gh_runs([])},
        f"/git/commits/{SHA}": {"out": "{}", "rc": 0},
    })
    tree = make_tree(tmp_path, {"origin": GH_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "has NO checks registered" in r.stderr


def test_github_gate_cancelled_conclusion_is_not_red(tmp_path):
    """Same absorption as the hub arm, one API call cheaper: GitHub reports `cancelled` on the
    run itself, so no second request is needed. Same verdict, same exit code."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/check-runs": {"out": gh_runs([("test", "completed", "cancelled", "2026-01-01T00:00:00Z")])},
    })
    tree = make_tree(tmp_path, {"origin": GH_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "gate: CANCELLED" in r.stdout


def test_github_gate_missing_commit_says_waiting_will_not_help(tmp_path):
    """GitHub answers a sha the repo does not have with HTTP 422 and `No commit found for SHA`
    (measured). That is the typo arm, not the registration race."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/check-runs": {"err": "No commit found for SHA: " + SHA, "rc": 1},
    })
    tree = make_tree(tmp_path, {"origin": GH_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "waiting will never change it" in r.stderr


def test_a_refusal_and_a_measured_failure_use_DIFFERENT_exit_codes(tmp_path):
    """The exit-code contract's actual requirement, and the one an exit-code change can quietly break.

    Asserting only that a refusal is 2 would still pass if EVERYTHING became 2. The contract is that
    the two are DISTINGUISHABLE: a refusal to measure is 2, a measurement that came back red is 1.
    Found by building the failure -- a gate poller branched on non-zero, could not tell the cases
    apart, and spun every 30s for ~7 minutes on a 10-char sha while its PR went green in about two.

    ONE tree and ONE stub for both arms, which also shows the refusal happens BEFORE any call: the
    short-sha run never reaches the stub that is standing by to answer red.
    """
    env = make_stubs(tmp_path, {"pr checks": {"out": "total_count=1 state='failure'\n", "rc": 1}})
    tree = make_tree(tmp_path, {"hub": HUB_URL})

    refusal = run(tree, "pr", "gate", SHA[:12], env=env)
    assert refusal.returncode == 2, refusal.stdout + refusal.stderr

    measured = run(tree, "pr", "gate", SHA, env=env)
    assert measured.returncode != 2, (
        "a measured failure answers 2, so it is indistinguishable from a refusal again:\n"
        + measured.stdout + measured.stderr)


@pytest.mark.parametrize("bad,why", [("main", "is a ref"), (SHA[:12], "short sha")])
def test_gate_refuses_anything_that_is_not_a_full_sha(tmp_path, bad, why):
    """A ref SILENTLY resolves to its current tip, so the verdict re-aims itself as the ref
    moves; a short sha resolves correctly and still cannot be compared to a PR head later, which
    is the entire reason a verdict names its commit.

    EXIT 2, NOT 1: this is a refusal to MEASURE, and 1 is what a measurement that
    came back red uses. With both at 1 a caller cannot tell "this input will never work" from "the
    gate is not ready yet", and a poller treats a permanent error as transient.
    """
    env = make_stubs(tmp_path, {})
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", bad, env=env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert why in r.stderr
    assert argv_log(env, "hub-api.sh") == "", "refused BEFORE the call, or the forge answered about the wrong tree"


# ---------------------------------------------------------------------------------------
# `pr land` — the merge-subject rules are opposite, and the caller writes no number at all.
# ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("reply,rc,expect", [
    ("merged: http=200\n", 0, "landed #42"),
    ("merged: http=405\n", 1, "did NOT merge"),
    ("something went sideways\n", 1, "UNKNOWN"),
])
def test_hub_land_reads_the_http_code_not_the_exit_status(tmp_path, reply, rc, expect):
    """`hub-api.sh pr merge` PRINTS the HTTP code and EXITS 0 REGARDLESS, so a caller gating on
    its status reads a REFUSAL as a merge. 405 is `block_on_outdated_branch` firing because
    something landed while our checks ran — the most likely non-2xx there, and the one whose
    repair (rebase, re-gate) is nothing like "it merged". `pr-queue.sh` learned to parse the
    code; a seam that did not would hand the same trap to every future caller."""
    env = make_stubs(tmp_path, {"pr merge": {"out": reply, "rc": 0}})
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "land", "42", "wiki: file the case", env=env)
    assert r.returncode == rc, r.stdout + r.stderr
    assert expect in (r.stdout + r.stderr)


def test_hub_land_passes_the_subject_through_untouched(tmp_path):
    """`hub-api.sh pr merge` already appends `(#N)` and refuses a trailing ref naming a
    DIFFERENT PR. The seam must not append as well, or that rule now lives in two
    places and they can disagree."""
    env = make_stubs(tmp_path, {"pr merge": {"out": "merged: http=200\n"}})
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "land", "42", "wiki: file the case", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    log = argv_log(env, "hub-api.sh").strip()
    assert log == "pr merge acme/app 42 wiki: file the case", log


def test_github_land_always_passes_a_subject_carrying_the_number(tmp_path):
    """GitHub appends `(#N)` ITSELF only when a PR has two or more non-merge commits, and
    `--subject` suppresses that append — so a bare `--subject` is what CAUSES a missing number,
    and whether it does depends on a commit count the caller never sees. The seam removes the
    dependency: it always passes `--subject`, and always writes the number.

    UNPROVEN AGAINST GITHUB. This asserts the argv `gh` would receive. The token here is
    read-only on third-party repos and the write has never been attempted."""
    env = make_stubs(tmp_path, {"pr merge": {"out": ""}})
    tree = make_tree(tmp_path, {"origin": GH_URL})
    r = run(tree, "pr", "land", "42", "wiki: file the case", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    log = argv_log(env, "gh").strip()
    assert "--squash" in log
    assert "--subject wiki: file the case (#42)" in log


def test_github_land_does_not_double_a_number_already_present(tmp_path):
    env = make_stubs(tmp_path, {"pr merge": {"out": ""}})
    tree = make_tree(tmp_path, {"origin": GH_URL})
    r = run(tree, "pr", "land", "42", "wiki: file the case (#42)", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "(#42) (#42)" not in argv_log(env, "gh")


def test_github_land_refuses_a_subject_naming_a_different_pr(tmp_path):
    """A trailing ref for another PR is refused, not rewritten: only the caller knows whether
    the wrong half is the number or the subject, and a commit pointing at the wrong PR is as
    permanent as a missing number."""
    env = make_stubs(tmp_path, {"pr merge": {"out": ""}})
    tree = make_tree(tmp_path, {"origin": GH_URL})
    r = run(tree, "pr", "land", "42", "wiki: file the case (#99)", env=env)
    assert r.returncode == 1
    assert "(#99)" in r.stderr and "#42" in r.stderr
    assert argv_log(env, "gh") == "", "refused BEFORE the merge, or a wrong subject already landed"


# ---------------------------------------------------------------------------------------
# `pr open` — one strictness, both forges.
# ---------------------------------------------------------------------------------------

def tree_with_bare_remote(tmp_path, remotes: dict) -> tuple[Path, Path]:
    """A work tree plus a real bare repo it can `ls-remote`, so the push check is exercised
    against git rather than mocked. The forge remote is named separately from the push remote
    via FORGE_REMOTE, which is how a fork pushes to `origin` and targets an upstream."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    root = make_tree(tmp_path, remotes)
    git(root, "config", "user.email", "t@t"); git(root, "config", "user.name", "t")
    (root / "f").write_text("one")
    git(root, "add", "f"); git(root, "commit", "-qm", "one")
    git(root, "checkout", "-qb", "feat/x")
    git(root, "remote", "add", "pushremote", str(bare))
    git(root, "push", "-q", "pushremote", "feat/x")
    return root, bare


def test_pr_open_refuses_when_the_local_head_is_ahead_of_the_remote(tmp_path):
    """A PR CAN BE OPENED AGAINST A HEAD MISSING PART OF THE CHANGE, and the runs that register
    are not wrong — they measure the pushed tree accurately. Green is earned and misleading at
    once. `gh pr create` has no such check, so the seam does it above the split."""
    env = make_stubs(tmp_path, {"pr create": {"out": "PR #1 open\n"}})
    env["FORGE_REMOTE"] = "pushremote"
    root, _ = tree_with_bare_remote(tmp_path, {"hub": HUB_URL})

    # Control first: with local and remote equal, the verb proceeds.
    ok = run(root, "pr", "open", "feat/x", "main", "a title", env=env)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "pr create" in argv_log(env, "hub-api.sh")

    # Inject the fault: commit locally without pushing.
    (root / "f").write_text("two")
    git(root, "add", "f"); git(root, "commit", "-qm", "two")
    bad = run(root, "pr", "open", "feat/x", "main", "a title", env=env)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "REFUSING" in bad.stderr and "Push first" in bad.stderr

    # And clear it: pushing makes the same command work again.
    git(root, "push", "-q", "pushremote", "feat/x")
    cleared = run(root, "pr", "open", "feat/x", "main", "a title", env=env)
    assert cleared.returncode == 0, cleared.stdout + cleared.stderr


def test_pr_open_names_the_head_exactly_once_on_both_arms(tmp_path):
    """Found by opening this change's own PR with this verb: `hub-api.sh pr create` prints its
    own `head <sha>` line and `gh pr create` prints none, so passing both through unfiltered
    makes the hub arm emit the line twice. A caller able to COUNT LINES can tell the forges
    apart, and the layer above will eventually branch on it."""
    env = make_stubs(tmp_path, {
        "pr create": {"out": "PR #1 open mergeable=True\nhead abc (feat/x)\n"}})
    env["FORGE_REMOTE"] = "pushremote"
    root, _ = tree_with_bare_remote(tmp_path, {"hub": HUB_URL})
    r = run(root, "pr", "open", "feat/x", "main", "a title", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    heads = [l for l in r.stdout.splitlines() if l.startswith("head ")]
    assert len(heads) == 1, r.stdout


def test_pr_open_takes_a_body_file_not_a_body_string(tmp_path):
    """Backticks inside a double-quoted shell argument EXECUTE, so a PR body pasted into `"..."`
    runs whatever a code span contains. Taking a PATH removes the hazard from every caller at
    once rather than asking each of them to remember `"$(cat file)"`."""
    env = make_stubs(tmp_path, {"pr create": {"out": "PR #1 open\n"}})
    env["FORGE_REMOTE"] = "pushremote"
    root, _ = tree_with_bare_remote(tmp_path, {"hub": HUB_URL})
    body = tmp_path / "body.md"
    body.write_text("see `date` and $(whoami) — neither must run\n")
    r = run(root, "pr", "open", "feat/x", "main", "a title", str(body), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    log = argv_log(env, "hub-api.sh")
    assert "`date`" in log and "$(whoami)" in log, "the body was mangled or evaluated on the way through"


def test_fork_pr_names_the_head_as_owner_colon_branch(tmp_path):
    """A fork's PR to its parent names the head `owner:branch`; a same-repo PR must NOT, or
    GitHub 422s on a head it reads as a branch containing a colon. This is the one thing the hub
    half has no analogue for — the seam's fork side."""
    env = make_stubs(tmp_path, {"pr create": {"out": ""}})
    env["FORGE_REMOTE"] = "pushremote"
    root, _ = tree_with_bare_remote(tmp_path, {
        "origin": GH_URL, "upstream": "https://github.com/upstreamorg/vendored.git"})

    same = run(root, "pr", "open", "feat/x", "main", "t", env=env)
    assert same.returncode == 0, same.stdout + same.stderr
    assert "--head feat/x" in argv_log(env, "gh")

    (Path(env["STUB_DIR"]) / "gh.argv").unlink()
    up = run(root, "pr", "open", "--upstream", "feat/x", "main", "t", env=env)
    assert up.returncode == 0, up.stdout + up.stderr
    log = argv_log(env, "gh")
    assert "--head acme:feat/x" in log, log
    assert "-R upstreamorg/vendored" in log, log


def test_upstream_is_refused_on_the_hub_arm(tmp_path):
    """The hub half has no parent-repo concept. Refusing names the gap instead of quietly
    aiming the PR at our own repo."""
    env = make_stubs(tmp_path, {"pr create": {"out": ""}})
    env["FORGE_REMOTE"] = "pushremote"
    root, _ = tree_with_bare_remote(tmp_path, {"hub": HUB_URL})
    r = run(root, "pr", "open", "--upstream", "feat/x", "main", "t", env=env)
    assert r.returncode == 1
    assert "no parent repo" in r.stderr


def test_pr_open_passes_a_draft_flag_to_the_hub_client_only_when_given(tmp_path):
    """The hub client drafts a PR that is not the front of the queue by itself, so the
    seam must stay silent unless the caller states it -- a flag added by default would override
    the client's decision for every caller. Stated, it must arrive intact: `--draft` is the burst
    case (several PRs meant to batch, opened on an empty queue)."""
    env = make_stubs(tmp_path, {"pr create": {"out": "PR #1 open\n"}})
    env["FORGE_REMOTE"] = "pushremote"
    root, _ = tree_with_bare_remote(tmp_path, {"hub": HUB_URL})
    argv = Path(env["STUB_DIR"]) / "hub-api.sh.argv"

    for flag in ("--draft", "--no-draft"):
        r = run(root, "pr", "open", flag, "feat/x", "main", "t", env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert argv_log(env, "hub-api.sh").split()[-1] == flag, argv_log(env, "hub-api.sh")
        argv.unlink()

    r = run(root, "pr", "open", "feat/x", "main", "t", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "draft" not in argv_log(env, "hub-api.sh"), "a flag reached the client nobody passed"


def test_pr_open_passes_serial_to_the_hub_client(tmp_path):
    """`--serial` keeps a PR out of every batch. The seam carries it to the client that
    applies the mark -- and adds nothing when not asked, the same as the draft flags."""
    env = make_stubs(tmp_path, {"pr create": {"out": "PR #1 open\n"}})
    env["FORGE_REMOTE"] = "pushremote"
    root, _ = tree_with_bare_remote(tmp_path, {"hub": HUB_URL})
    r = run(root, "pr", "open", "--serial", "feat/x", "main", "t", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--serial" in argv_log(env, "hub-api.sh").split(), argv_log(env, "hub-api.sh")
    (Path(env["STUB_DIR"]) / "hub-api.sh.argv").unlink()
    r = run(root, "pr", "open", "feat/x", "main", "t", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "serial" not in argv_log(env, "hub-api.sh"), argv_log(env, "hub-api.sh")


def test_pr_open_serial_is_refused_on_the_github_arm(tmp_path):
    """GitHub has no queue, so there is no batch to keep a PR out of. Refused by name rather than
    silently dropped -- a caller asking for serial and getting nothing would believe it held."""
    env = make_stubs(tmp_path, {"pr create": {"out": ""}})
    env["FORGE_REMOTE"] = "pushremote"
    root, _ = tree_with_bare_remote(tmp_path, {
        "origin": GH_URL, "upstream": "https://github.com/upstreamorg/vendored.git"})
    r = run(root, "pr", "open", "--serial", "feat/x", "main", "t", env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no queue" in r.stderr, r.stderr
    assert argv_log(env, "gh") == "", "gh was called for a flag it cannot honour"


def test_pr_open_draft_becomes_gh_draft_on_the_github_arm(tmp_path):
    """The same burst on a fork is drafted on GitHub too, or the forges differ in a way a caller
    discovers only by comparing PR states. `--no-draft` is gh's default and must add nothing."""
    env = make_stubs(tmp_path, {"pr create": {"out": ""}})
    env["FORGE_REMOTE"] = "pushremote"
    root, _ = tree_with_bare_remote(tmp_path, {
        "origin": GH_URL, "upstream": "https://github.com/upstreamorg/vendored.git"})
    argv = Path(env["STUB_DIR"]) / "gh.argv"

    r = run(root, "pr", "open", "--draft", "feat/x", "main", "t", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--draft" in argv_log(env, "gh").split(), argv_log(env, "gh")
    argv.unlink()

    r = run(root, "pr", "open", "--no-draft", "feat/x", "main", "t", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "draft" not in argv_log(env, "gh"), argv_log(env, "gh")


# ---------------------------------------------------------------------------------------
# Labels — the one verb where Forgejo's write silently no-ops.
# ---------------------------------------------------------------------------------------

def test_hub_label_rm_refuses_an_unknown_name(tmp_path):
    """Forgejo deletes a label by ID and answers 204 for an ID the issue never carried, so an
    unresolvable NAME would be reported as a removal that did not happen."""
    env = make_stubs(tmp_path, {
        "/labels?limit=100": {"out": json.dumps([{"id": 7, "name": "queue:needs-human-review"}])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "label", "rm", "42", "queue:no-such-label", env=env)
    assert r.returncode == 1
    assert "no label named" in r.stderr
    assert "-X DELETE" not in argv_log(env, "hub-api.sh")


def test_hub_label_add_reads_back_rather_than_trusting_the_write(tmp_path):
    """Forgejo drops a value it will not accept and still answers 2xx — the same shape as
    `hub-api.sh issue claim`, which is why that verb re-reads too."""
    env = make_stubs(tmp_path, {
        "issue tag": {"out": "#42 labels: queue:needs-human-review\n"},
        "/issues/42": {"out": json.dumps({"labels": [{"name": "queue:needs-human-review"}]})},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "label", "add", "42", "queue:needs-human-review", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#42 labels: queue:needs-human-review" in r.stdout
    assert "/issues/42" in argv_log(env, "hub-api.sh"), "the label was not read back"


def test_review_read_reports_the_native_reviews(tmp_path):
    """A native review is the AUTHORITY and the label is only the request. So this
    verb reads reviews and never infers approval from a label."""
    env = make_stubs(tmp_path, {
        "/pulls/42/reviews": {"out": json.dumps([
            {"user": {"login": "alice"}, "state": "APPROVED"},
            {"user": {"login": "claude"}, "state": "COMMENT"}])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "review", "read", "42", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "reviews: 2 approved=1" in r.stdout


# ---------------------------------------------------------------------------------------
# The seam's own claim: the two arms are indistinguishable from above.
# ---------------------------------------------------------------------------------------

def test_the_two_arms_print_the_same_verdict_lines(tmp_path):
    """If a caller can tell the forges apart from the output, the layer above will eventually
    branch on it, and the one-surface premise is lost one `case` statement at a time. Only the trailing
    `forge <kind> <repo>` provenance line may differ."""
    henv = make_stubs(tmp_path / "h", {
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "success"), ("lint", "skipped")])}})
    genv = make_stubs(tmp_path / "g", {
        f"/commits/{SHA}/check-runs": {"out": gh_runs([
            ("lint", "completed", "skipped", "2026-01-01T00:00:00Z"),
            ("pytest", "completed", "success", "2026-01-01T00:00:00Z")])}})

    h = run(make_tree(tmp_path, {"hub": HUB_URL}, "ht"), "pr", "gate", SHA, env=henv)
    g = run(make_tree(tmp_path, {"origin": GH_URL}, "gt"), "pr", "gate", SHA, env=genv)
    assert h.returncode == g.returncode == 0, h.stdout + h.stderr + g.stdout + g.stderr

    def verdict_lines(out):
        return [l for l in out.splitlines() if not l.startswith("forge ")]

    assert verdict_lines(h.stdout) == verdict_lines(g.stdout), (h.stdout, g.stdout)


# ---------------------------------------------------------------------------------------
# `pr gate --pr <n>` — the stale-head refusal.
#
# The failure this closes is NOT a wrong sha. A verdict about sha X is perfectly accurate about
# X; the defect is that it is READ as a verdict about the PR, and the two diverge silently the
# moment anything rebases. So the assertions below are as much about what is NOT printed, and
# about what is NOT requested, as about the exit code.
# ---------------------------------------------------------------------------------------

def pr_head(sha: str) -> str:
    """A `/pulls/<n>` body reduced to the field the seam reads. Both forges use `.head.sha`."""
    return json.dumps({"number": 7, "head": {"sha": sha}})


def test_gate_refuses_when_the_pr_head_has_moved(tmp_path):
    env = make_stubs(tmp_path, {
        "/pulls/7": {"out": pr_head(OTHER)},
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "success")])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, "--pr", "7", env=env)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "STALE HEAD" in r.stderr
    # Both shas, because a refusal naming only one leaves the reader to go and look up the other
    # — which is the extra request this ticket exists to remove.
    assert OTHER in r.stderr and SHA in r.stderr
    # And the fix, spelled out as a command rather than described.
    assert f"pr gate {OTHER} --pr 7" in r.stderr


def test_a_stale_refusal_prints_no_verdict_to_paste(tmp_path):
    """The core design claim. A warning printed above `gate: GREEN` is a warning that gets read
    past — it already was, four times in one night's drain. Nothing paste-able may exist."""
    env = make_stubs(tmp_path, {
        "/pulls/7": {"out": pr_head(OTHER)},
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "success")])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, "--pr", "7", env=env)
    both = r.stdout + r.stderr
    for forbidden in ("gate:", "GREEN", "measured "):
        assert forbidden not in both, f"{forbidden!r} reached the terminal on a stale refusal"


def test_the_head_is_checked_before_the_gate_is_read(tmp_path):
    """Structural, not cosmetic: the refusal must come first, so no verdict can be computed at
    all. Asserted on the REQUESTS made — the status endpoint must never be reached.

    This is what makes the test above more than a string check: an implementation that read the
    gate, formatted it, then suppressed the output would pass that one and fail this one."""
    env = make_stubs(tmp_path, {
        "/pulls/7": {"out": pr_head(OTHER)},
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "success")])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, "--pr", "7", env=env)
    assert r.returncode == 5
    log = argv_log(env, "hub-api.sh")
    assert "/pulls/7" in log, "the PR head was never read — this test would pass vacuously"
    assert f"/commits/{SHA}/status" not in log, "the gate was read despite the head being stale"


def test_matching_head_passes_through_and_is_transparent(tmp_path):
    """The control for every test above. Same sha, same stubs, head AGREES — must read the gate
    and return the ordinary verdict, byte-for-byte what the no-flag form returns."""
    responses = {
        "/pulls/7": {"out": pr_head(SHA)},
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "success"), ("lint", "skipped")])},
    }
    tree = make_tree(tmp_path, {"hub": HUB_URL})

    with_flag = run(tree, "pr", "gate", SHA, "--pr", "7", env=make_stubs(tmp_path / "a", responses))
    without = run(tree, "pr", "gate", SHA, env=make_stubs(tmp_path / "b", responses))

    assert with_flag.returncode == 0, with_flag.stdout + with_flag.stderr
    assert with_flag.returncode == without.returncode
    assert "gate: GREEN" in with_flag.stdout

    # The VERDICT must be identical — `--pr` may not change what the gate concluded. It is not
    # byte-identical, and that is deliberate: a `--pr` run adds one provenance line, or the
    # stronger check would leave no trace and a reader could not tell a confirmed head from one
    # nobody looked at.
    def verdict_lines(out):
        return [ln for ln in out.splitlines() if ln.startswith(("  ", "gate:", "measured "))]
    assert verdict_lines(with_flag.stdout) == verdict_lines(without.stdout)

    extra = set(with_flag.stdout.splitlines()) - set(without.stdout.splitlines())
    assert any("was #7 head at read time" in ln for ln in extra), extra
    # It must NOT claim currency. A "confirmed current" line is itself a measurement that goes
    # stale on the next rebase — this ticket's own bug, one level up.
    assert "is #7 head" not in with_flag.stdout
    assert "current" not in with_flag.stdout


def test_no_provenance_line_without_the_flag(tmp_path):
    """The line is evidence that a comparison happened. It must never appear when none did."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "success")])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "head at read time" not in r.stdout


def test_without_the_flag_no_pr_is_requested(tmp_path):
    """Backward compatibility, asserted on requests rather than on a passing exit code: every
    existing caller passes a bare sha and must not start paying an extra API call."""
    env = make_stubs(tmp_path, {
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "success")])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/pulls/" not in argv_log(env, "hub-api.sh")


def test_stale_head_works_on_the_github_arm_too(tmp_path):
    """`.head.sha` is the same path on both forges, so this needs no arm of its own — asserted
    rather than assumed, since 'needs no arm' is the claim the seam is built to keep honest."""
    env = make_stubs(tmp_path, {
        "/pulls/7": {"out": pr_head(OTHER)},
        f"/commits/{SHA}/check-runs": {"out": json.dumps({"total_count": 0, "check_runs": []})},
    })
    tree = make_tree(tmp_path, {"origin": GH_URL})
    r = run(tree, "pr", "gate", SHA, "--pr", "7", env=env)
    assert r.returncode == 5, r.stdout + r.stderr
    assert "STALE HEAD" in r.stderr


def test_a_pr_with_no_head_sha_refuses_rather_than_guessing(tmp_path):
    """An empty result is not an absence. If the forge cannot say what the head is, the seam
    cannot confirm the sha is current — and 'cannot confirm' must not read as 'confirmed'."""
    env = make_stubs(tmp_path, {
        "/pulls/7": {"out": json.dumps({"number": 7})},
        f"/commits/{SHA}/status": {"out": hub_status([("pytest", "success")])},
    })
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, "--pr", "7", env=env)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "no head sha" in r.stderr
    assert "gate:" not in r.stdout


@pytest.mark.parametrize("args, want", [
    (["--pr", "abc"], "wants a PR number"),
    (["--pr"], "needs a PR number"),
    (["--bogus", "1"], "unknown option"),
])
def test_gate_option_mistakes_die_rather_than_colliding_with_refused(tmp_path, args, want):
    """Exit 1, never 2 — `${2:?...}` would have exited 2, which this file reserves for REFUSED,
    and a usage typo that reads as a refusal is a driver branching on the wrong thing."""
    env = make_stubs(tmp_path, {f"/commits/{SHA}/status": {"out": hub_status([("t", "success")])}})
    tree = make_tree(tmp_path, {"hub": HUB_URL})
    r = run(tree, "pr", "gate", SHA, *args, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert want in r.stderr
