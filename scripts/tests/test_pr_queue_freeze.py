"""A queue freeze that every box honours, set by `freeze` and lifted by `thaw`.

The marker is the forge wiki page "Queue Freeze" (an operator ruling). Three
properties, each with the arm that proves it can fail:
- while the page exists, every entry that merges refuses and prints the page's text, and so does
  `merge_queued` just before it merges; `thaw` then lets the same `approve` merge;
- an unreadable wiki listing is CANNOT TELL and refuses, because an unread listing looks exactly
  like an empty one;
- `freeze` and `thaw` run above the per-box flock, because the run holding it is what a freeze
  exists to stop.

Driven against a stub `hub-api.sh` and a throwaway git repo with no `hub` remote, so the freshness
guard skips and the merge path is reached without a forge (the same seam as
test_pr_queue_merge_requested.py). The stub cannot tell us Forgejo's wiki behaviour; the routes it
serves are the ones scripts/handoff.sh already uses against the live wiki.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PR_QUEUE = REPO / "scripts/pr-queue.sh"

STUB = r'''#!/usr/bin/env python3
import base64, json, os, sys
args = sys.argv[1:]
state = os.environ["STUB_STATE"]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(" ".join(args) + "\n")
page = os.path.join(state, "freeze.txt")
a0 = args[0] if args else ""

def listed():
    counter = os.path.join(state, "listings")
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(n + 1))
    return os.path.exists(page) and n >= int(os.environ.get("STUB_HIDE_FIRST", "0"))

if a0 == "pr":
    if args[1] == "merge":
        print("merged: http=200")
    sys.exit(0)                                        # `pr checks`: green on the first poll
if a0 == "issue":
    print("65" if args[1] == "label-id" else "{}")
    sys.exit(0)
if a0.endswith("/wiki/pages"):
    if os.environ.get("STUB_WIKI_BROKEN"):
        sys.exit(1)
    if os.environ.get("STUB_WIKI_404"):                # hub-api.sh --fail-with-body: body on stdout, rc 22
        sys.stdout.write(json.dumps({"message": "The target couldn't be found.", "errors": [os.environ["STUB_WIKI_404"]]}))
        if "-w" in args:                               # curl's write-out, as the real client emits it
            sys.stdout.write("\n" + os.environ.get("STUB_WIKI_CODE", "404"))
        sys.exit(22)
    pages = [{"title": "Home", "sub_url": "Home"}]
    if listed():
        pages.append({"title": "Queue Freeze", "sub_url": "Queue-Freeze"})
    print(json.dumps(pages))
    sys.exit(0)
if a0.endswith("/wiki/new"):
    body = json.loads(sys.stdin.read())
    open(page, "w").write(base64.b64decode(body["content_base64"]).decode())
    print("{}")
    sys.exit(0)
if "/wiki/page/" in a0:
    if "DELETE" in args:
        if not os.environ.get("STUB_DELETE_IGNORED") and os.path.exists(page):
            os.remove(page)
        sys.exit(0)
    if not os.path.exists(page):
        print(json.dumps({"message": "The target could not be found."}))
        sys.exit(0)
    print(json.dumps({"title": "Queue Freeze",
                      "content_base64": base64.b64encode(open(page, "rb").read()).decode()}))
    sys.exit(0)
if "/labels" in a0:
    print("[]")
    sys.exit(0)
if "/pulls/" in a0:
    print(json.dumps({"title": "feat: a title", "head": {"sha": "a" * 40, "ref": "feat"}, "state": "open"}))
    sys.exit(0)
print("{}")
'''


@pytest.fixture
def env(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    subprocess.run(["git", "init", "-q", str(hub)], check=True)
    for kv in (["user.email", "t@t"], ["user.name", "t"]):
        subprocess.run(["git", "config", *kv], cwd=hub, check=True)
    (hub / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=hub, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=hub, check=True)

    api = tmp_path / "stub-api.sh"
    api.write_text(STUB)
    api.chmod(0o755)
    log = tmp_path / "stub.log"
    log.write_text("")
    state = tmp_path / "state"
    state.mkdir()
    return {
        "log": log, "state": state, "lock": tmp_path / "queue.lock",
        "env": {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "PR_QUEUE_HUB": str(hub),
            "PR_QUEUE_TOOLS_DIR": str(hub / "scripts"),  # siblings stay the fixture's
            "FORGE_TOOLS_REMOTE": "hub",  # the fixture's forge remote, the name pr-queue.sh defaulted to
            "PR_QUEUE_WT": str(tmp_path / "merge-wt"),
            "PR_QUEUE_REPO": "owner/repo",
            "PR_QUEUE_API": str(api),
                    "PR_QUEUE_REWAIT_SECS": "0",
            "PR_QUEUE_REWAIT_POLL_SECS": "0",
            "PR_QUEUE_POLL_SECS": "0",
            "STUB_LOG": str(log),
            "STUB_STATE": str(state),
        },
    }


def _q(env, *args, **extra):
    return subprocess.run(["sh", str(PR_QUEUE), *args], input="", capture_output=True, text=True,
                          env={**env["env"], **extra}, timeout=120)


def _merged(env):
    return "pr merge" in env["log"].read_text()


def test_freeze_then_approve_refuses_and_prints_why(env):
    f = _q(env, "freeze", "forge move to 203.0.113.7", "acceptance declared on #70")
    assert f.returncode == 0, f.stdout + f.stderr
    assert "FROZEN" in f.stdout and "acceptance declared on #70" in f.stdout

    r = _q(env, "approve", "5")
    assert r.returncode != 0
    assert "the queue is FROZEN" in r.stdout
    assert "forge move to 203.0.113.7" in r.stdout, "the refusal must print the page's text"
    assert not _merged(env)


def test_thaw_lifts_the_freeze_and_the_same_approve_merges(env):
    """The control for every refusal here: the same approve, with the page gone, merges."""
    assert _q(env, "freeze", "a reason").returncode == 0
    t = _q(env, "thaw")
    assert t.returncode == 0, t.stdout + t.stderr
    assert "THAWED" in t.stdout and "absent from owner/repo's wiki listing" in t.stdout

    r = _q(env, "approve", "5")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FROZEN" not in r.stdout
    assert _merged(env)


def test_an_unreadable_wiki_listing_refuses_as_cannot_tell(env):
    r = _q(env, "approve", "5", STUB_WIKI_BROKEN="1")
    assert r.returncode != 0
    assert "cannot tell whether the queue is frozen" in r.stdout
    assert not _merged(env)


def test_a_never_initialised_wiki_is_no_freeze_and_approve_merges(env):
    """A fresh fork's wiki listing answers 404 "no such file or directory". That is an
    empty wiki, and reading it as CANNOT TELL made every new repo unmergeable through the queue."""
    r = _q(env, "approve", "5", STUB_WIKI_404="no such file or directory")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cannot tell" not in r.stdout
    assert _merged(env)


@pytest.mark.parametrize("extra", [{"STUB_WIKI_404": "user does not exist"},
                                   {"STUB_WIKI_404": "no such file or directory", "STUB_WIKI_CODE": "500"}])
def test_a_different_not_found_still_refuses_as_cannot_tell__control(env, extra):
    r = _q(env, "approve", "5", **extra)
    assert r.returncode != 0
    assert "cannot tell whether the queue is frozen" in r.stdout
    assert not _merged(env)


def test_a_freeze_set_while_the_run_waited_stops_its_merge(env):
    """The page is hidden from the entry check and visible to the last-moment check in merge_queued."""
    (env["state"] / "freeze.txt").write_text("**The PR queue is FROZEN.**\n\n- Why: set mid-run\n")
    r = _q(env, "approve", "5", STUB_HIDE_FIRST="1")
    assert r.returncode != 0
    assert "REFUSING: merge #5 -- the queue is FROZEN" in r.stdout
    assert "set mid-run" in r.stdout
    assert not _merged(env)


@pytest.mark.parametrize("verb", [["drain"], ["merge-requested"]])
def test_every_merging_entry_refuses_while_frozen(env, verb):
    assert _q(env, "freeze", "a reason").returncode == 0
    r = _q(env, *verb)
    assert r.returncode != 0
    assert f"REFUSING: {verb[0]} -- the queue is FROZEN" in r.stdout
    assert "pulls?state=open" not in env["log"].read_text(), "the drain read its order before the freeze check"
    assert not _merged(env)


def test_freeze_never_overwrites_a_standing_freeze(env):
    assert _q(env, "freeze", "the first reason").returncode == 0
    second = _q(env, "freeze", "the second reason")
    assert second.returncode == 1
    assert "ALREADY FROZEN" in second.stdout and "the first reason" in second.stdout
    assert "the second reason" not in (env["state"] / "freeze.txt").read_text()


def test_thaw_believes_the_listing_not_the_delete(env):
    assert _q(env, "freeze", "a reason").returncode == 0
    t = _q(env, "thaw", STUB_DELETE_IGNORED="1")
    assert t.returncode == 1
    assert "STILL LISTED" in t.stdout
