"""`drain --batch`: ready PRs assembled into ONE integration PR, landed by
fast-forward, members marked manually-merged at their replayed tips, halved on red.

The forge is a stub that does the two things a batch depends on FOR REAL: `pr merge` moves the
bare remote's `main` to the sha it was asked to land (as a fast-forward would), and `pr checks`
answers by CONTENT -- red when the commit range behind the sha carries a subject named in
STUB_RED_SUBJECT -- so a bisection can be exercised without knowing integration shas in advance.
Everything else (the open-PR listing, PR objects, labels, blockers, marks) is served from files and
logged, so a test asserts on what the queue ASKED FOR.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE = REPO_ROOT / "scripts" / "pr-queue.sh"

STUB = r'''#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG"
d="$STUB_DIR"
# The queue IS the open PRs, oldest first: queueing members 41-43 opens them and closes
# the rest -- what STUB_ORDER_AFTER_REOPEN does mid-run, and `queue()` below does before it.
requeue() { python3 -c '
import json, sys
for n in ("41", "42", "43"):
    f = "%s/pr.%s.json" % (sys.argv[1], n); p = json.load(open(f))
    p["state"] = "open" if n in sys.argv[2:] else "closed"; json.dump(p, open(f, "w"))' "$d" "$@"; }
case "$1" in
  # `--repo` reads the target's default branch; STUB_DEFAULT_BRANCH models a fork whose
  # patches live on a branch that is not `main`. The merge and update arms below land on it too.
  */v1/repos/o/target) printf '{"default_branch":"%s"}\n' "${STUB_DEFAULT_BRANCH:-main}" ;;
  */wiki/pages)   # merging verbs read the wiki listing first; empty = no freeze.
      # STUB_FREEZE_FROM=<n> lists the freeze from the n-th listing on, so it can appear mid-run.
      c=$(( $(cat "$d/listings" 2>/dev/null || echo 0) + 1 )); echo "$c" > "$d/listings"
      if [ -n "${STUB_FREEZE_FROM:-}" ] && [ "$c" -ge "$STUB_FREEZE_FROM" ]; then echo '[{"title":"Queue Freeze","sub_url":"Queue-Freeze"}]'
      else echo '[]'; fi ;;
  */wiki/page/*) printf '{"content_base64":"%s"}\n' "$(printf 'set mid-gate' | base64)" ;;
  */pulls/*/merge)
      n=${1##*/pulls/}; n=${n%%/*}
      printf '%s\n' "$n $*" >> "$d/marks.log"
      # STUB_MANUAL_DEAD_CLIENT: the client dies before any HTTP, on stderr, with no http= line
      # (2026-09-26: a credential helper naming a deleted script).
      if [ -n "${STUB_MANUAL_DEAD_CLIENT:-}" ]; then echo "git: 'credential-/gone/hub-api.sh' is not a git command" >&2; exit 1; fi
      if [ -n "${STUB_MANUAL_405:-}" ]; then printf '{"message":"manually-merged is not an allowed merge style for this repository"}\nmarked: http=405\n'
      else printf '{}\nmarked: http=200\n'; fi ;;
  */pulls\?state=open*)
      python3 -c '
import json, glob, sys, subprocess
out = []
for f in sorted(glob.glob(sys.argv[1] + "/pr.*.json")):
    n = f.rsplit(".", 2)[1]
    # the single-PR arm substitutes the head sha; the listing must too, or a standing PR reads HEADSHA
    sha = subprocess.run(["git", "-C", sys.argv[2], "rev-parse", "refs/pull/%s/head" % n], capture_output=True, text=True).stdout.strip()
    d = json.loads(open(f).read().replace("HEADSHA", sha or "HEADSHA"))
    if d.get("state") == "open": out.append(d)
print(json.dumps(out))' "$d" "$STUB_BARE" ;;
  */pulls/*/commits) printf '%s\n' "${STUB_COMMITS:-[]}" ;;   # the close-keyword read
  */pulls/*/update*)
      # A forge rebase, modelled for real: replay the member onto current main and move BOTH the
      # branch and refs/pull/N/head, so the head actually MOVES. Without this the queue's ordinary
      # path sees "head did not move within 0s", retries three times and STOPS -- which is a
      # property of the stub, not of the queue.
      n=${1##*/pulls/}; n=${n%%/update*}
      # the forge's real refusal of a conflicting rebase -- body, then the code curl's -w adds.
      if [ "${STUB_UPDATE_409:-}" = "$n" ]; then
        echo '{"message":"rebase failed because of conflict"}'; echo 409; exit 22
      fi
      br=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["head"]["ref"])' "$d/pr.$n.json")
      wt="$d/upd.$n"; rm -rf "$wt"
      if git clone -q "$STUB_BARE" "$wt" 2>/dev/null; then
        git -C "$wt" checkout -q -B "$br" "origin/$br" 2>/dev/null
        if git -C "$wt" -c user.email=t@t -c user.name=t rebase -q "origin/${STUB_DEFAULT_BRANCH:-main}" >/dev/null 2>&1; then
          git -C "$wt" push -q -f origin "$br" 2>/dev/null
          git -C "$STUB_BARE" update-ref "refs/pull/$n/head" "refs/heads/$br"
          # a PR brought current BEFORE its gate first runs its suite on this push, not on a
          # reopen -- so the "after its suite first runs" hooks fire here too.
          echo "$n updated" >> "$d/updated.log"
          [ -z "${STUB_ORDER_AFTER_REOPEN:-}" ] || requeue $STUB_ORDER_AFTER_REOPEN
        fi
      fi
      echo '{}'; exit 0 ;;
  */pulls/*)
      n=${1##*/pulls/}; n=${n%%\?*}
      case "$*" in
        *'"title"'*)
          python3 -c '
import json, sys
f = sys.argv[1]; d = json.load(open(f)); t = json.loads(sys.argv[2])["title"]; d["title"] = t
json.dump(d, open(f, "w")); open(sys.argv[3], "a").write("%s title %s\n" % (sys.argv[4], t))' "$d/pr.$n.json" "$(printf '%s\n' "$@" | sed -n '/^-d$/{n;p;}')" "$d/titles.log" "$n"
          echo '{}'; exit 0 ;;
        *'"state":"open"'*) echo "$n reopened" >> "$d/reopened.log"
            # A reopen OPENS it, as the forge does -- the stub used to leave the close standing, so a
            # reopened PR read as closed to everything after (found by replaying a live two-drain overlap).
            python3 -c 'import json,sys; f=sys.argv[1]; d=json.load(open(f)); d["state"]="open"; json.dump(d, open(f,"w"))' "$d/pr.$n.json"
            # the queue gains a PR while the run is on its way, as a real PR arrived mid-drain.
            [ -z "${STUB_ORDER_AFTER_REOPEN:-}" ] || requeue $STUB_ORDER_AFTER_REOPEN
            echo '{}'; exit 0 ;;
        *"-X PATCH"*) echo "$n closed" >> "$d/closed.log"; python3 -c '
import json, sys
f = sys.argv[1]; d = json.load(open(f)); d["state"] = "closed"; json.dump(d, open(f, "w"))' "$d/pr.$n.json" 2>/dev/null; echo '{}'; exit 0 ;;
      esac
      if [ -f "$d/pr.$n.json" ]; then
        sha=$(git -C "$STUB_BARE" rev-parse "refs/pull/$n/head" 2>/dev/null)
        sed "s/HEADSHA/$sha/" "$d/pr.$n.json"
      else echo '{"message":"Not Found"}'; fi ;;
  */issues/*/labels*)   # a PR's labels come from its pr.N.json, so a test can mark one
      n=${1#*/issues/}; n=${n%%/*}
      if [ -f "$d/pr.$n.json" ]; then python3 -c '
import json, sys
print(json.dumps([{"name": l} for l in json.load(open(sys.argv[1])).get("labels", [])]))' "$d/pr.$n.json"
      else echo '[]'; fi ;;
  */issues/*)   # unset = unreadable.
      # with STUB_TICKET_REPO set, only THAT repo answers the ticket; any other repo answers
      # the forge's real not-found payload -- a body with no `state` key, which is what
      # a repo with no tickets returned when the check looked the number up in the wrong repo.
      case "${STUB_TICKET_REPO:-}" in
      "") printf '%s\n' "${STUB_ISSUE:-null}" ;;
      *)  case "$1" in
          *"/repos/$STUB_TICKET_REPO/issues/"*) printf '%s\n' "${STUB_TICKET:-null}" ;;
          *) echo '{"message":"The target could not be found.","errors":[]}' ;;
          esac ;;
      esac ;;
  issue) case "$2" in blockers) echo "#$4 open_blockers=0" ;; *) echo 7 ;; esac ;;
  pr) case "$2" in
        # the red's own report, which the drain reads to blame a member before halving.
        why-red) printf '%s\n' "${STUB_WHY_RED:-}"; exit 0 ;;
        checks)
          sha=$4
          # the owner pushes while the run gates -- the first checks read moves refs/pull/N/head.
          # STUB_MOVE_AFTER_CREATE: only once an integration PR exists -- a push while the BATCH gates.
          if [ -n "${STUB_MOVE_PR:-}" ] && [ ! -f "$d/moved" ] && { [ -z "${STUB_MOVE_AFTER_CREATE:-}" ] || [ -f "$d/created.log" ]; }; then
            git -C "$STUB_BARE" update-ref "refs/pull/$STUB_MOVE_PR/head" "refs/heads/$STUB_MOVE_TO"; : > "$d/moved"
          fi
          for ps in ${STUB_PENDING_SHAS:-}; do [ "$ps" = "$sha" ] && { echo "total_count=1 state='pending'"; echo "REFUSING: state is 'pending'"; exit 1; }; done
          if [ -n "${STUB_PENDING_SUBJECTS:-}" ]; then
            for s in $STUB_PENDING_SUBJECTS; do git -C "$STUB_BARE" log --format=%s "$sha" 2>/dev/null | grep -q "$s" && { echo "total_count=1 state='pending'"; echo "REFUSING: state is 'pending'"; exit 1; }; done
          fi
          # STUB_NORUNS_SUBJECTS: the shape hub-api.sh refuses as "exists but has NO check-runs
          # registered -- not a pass", which `_gate_state` reads as `none`. Distinct from red and
          # from pending, and previously unreachable in this harness.
          if [ -n "${STUB_NORUNS_SUBJECTS:-}" ]; then
            for s in $STUB_NORUNS_SUBJECTS; do git -C "$STUB_BARE" log --format=%s "$sha" 2>/dev/null | grep -q "$s" && { echo "total_count=0 state='' skipped=0"; echo "REFUSING: $sha exists but has NO check-runs registered -- not a pass."; exit 2; }; done
          fi
          # STUB_RED_AFTER_REOPEN: red only once a suite has run -- a draft's skipped suite hid it.
          if [ -n "${STUB_RED_SUBJECTS:-}" ] && { [ -z "${STUB_RED_AFTER_REOPEN:-}" ] || [ -f "$d/reopened.log" ] || [ -f "$d/updated.log" ]; }; then
            red=1
            for s in $STUB_RED_SUBJECTS; do git -C "$STUB_BARE" log --format=%s "$sha" 2>/dev/null | grep -q "$s" || red=0; done
            if [ "$red" = 1 ]; then echo "total_count=1 state='failure'"; echo "REFUSING: state is 'failure'"; exit 1; fi
          fi
          # STUB_SKIPPED_SUITE_SHAS: green with `Test / pytest` SKIPPED, the shape a draft produces
          # until the PR is reopened, unless STUB_REOPEN_NO_RUN says no run ever came.
          for ss in ${STUB_SKIPPED_SUITE_SHAS:-}; do
            if [ "$ss" = "$sha" ] && { [ ! -f "$d/reopened.log" ] || [ -n "${STUB_REOPEN_NO_RUN:-}" ]; }; then
              echo "  Lint / eslint (first-party JS/TS) (pull_request)       success"
              echo "  Test / pytest (scripts/) (pull_request)                skipped"
              echo "total_count=2 state='success' skipped=1"; echo "OK: 2 registered, 1 skipped"; exit 0
            fi
          done
          for ss in ${STUB_SKIPPED_OTHER_SHAS:-}; do
            if [ "$ss" = "$sha" ]; then
              echo "  Fallow / audit (repo root, EXCLUDING dot-directories) (pull_request) skipped"
              echo "  Test / pytest (scripts/) (pull_request)                success"
              echo "total_count=2 state='success' skipped=1"; echo "OK: 2 registered, 1 skipped"; exit 0
            fi
          done
          echo "OK: 1 registered, 0 skipped, 1 actually ran"; exit 0 ;;
        create)
          # The real client refuses without a local ref or HUB_API_EXPECT_HEAD (hub-api.sh pr create);
          # the batch branch is never a local ref of $HUB, so the stub holds the same line.
          want=$(git -C "$STUB_BARE" rev-parse "refs/heads/$4")
          [ "${HUB_API_EXPECT_HEAD:-}" = "$want" ] || { echo "pr create: no local ref refs/heads/$4 and no HUB_API_EXPECT_HEAD, so the head cannot be verified against what you have."; exit 1; }
          c=$(cat "$d/prcount" 2>/dev/null || echo 76); c=$((c + 1)); echo "$c" > "$d/prcount"
          echo "$c $4" >> "$d/created.log"
          echo "$c $5" >> "$d/bases.log"   # the base each PR was opened against
          git -C "$STUB_BARE" update-ref "refs/pull/$c/head" "refs/heads/$4"
          python3 -c '
import json, sys
json.dump({"number": int(sys.argv[1]), "state": "open", "merged": False, "head": {"ref": sys.argv[2], "sha": "HEADSHA"},
           "labels": [], "title": sys.argv[3], "body": sys.argv[4]}, open(sys.argv[5], "w"))' "$c" "$4" "$6" "$7" "$d/pr.$c.json"
          echo "PR #$c open mergeable=True"; exit 0 ;;
        merge)
          # a STALE client refuses on stderr and prints NO http= line, which is what
          # made the batch landing log `http=unreadable` and discard the cause. The batch site
          # piped this straight into sed, so the words were consumed by the pipe.
          if [ -n "${STUB_STALE_CLIENT:-}" ]; then
            echo "hub-api: REFUSING: this client is STALE -- scripts/hub-api.sh here is deadbeef" >&2
            exit 1
          fi
          if git -C "$STUB_BARE" merge-base --is-ancestor "refs/heads/${STUB_DEFAULT_BRANCH:-main}" "$6" 2>/dev/null; then
            # STUB_DIVERT_LANDING: report the merge as done while leaving `main` where it was.
            # That is the shape the landed-head guard exists for -- the queue believes the head it gated
            # became `main`, and the forge is content either way because `manually-merged` does not
            # consult protection. Without a knob there is no way to reach that branch in a test.
            if [ -n "${STUB_DIVERT_LANDING:-}" ]; then
              echo "merged: http=200"; echo "merge-path: pr-queue (label on #$4)"; exit 0
            fi
            git -C "$STUB_BARE" update-ref "refs/heads/${STUB_DEFAULT_BRANCH:-main}" "$6"
            echo "merged: http=200"; echo "merge-path: pr-queue (label on #$4)"; exit 0
          fi
          echo "merged: http=405"; echo "reason: Branch is outdated"; exit 0 ;;
      esac ;;
esac
exit 0
'''


def git(cwd, *a, **kw):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True, **kw).stdout.strip()


def queue(w, *nums):
    """Queue exactly these members. The queue IS the open PRs, oldest first (the hand-kept
    order fence is gone), so the members not named are closed."""
    for n in (41, 42, 43):
        f = w["d"] / ("pr.%d.json" % n); d = json.loads(f.read_text())
        d["state"] = "open" if n in nums else "closed"; f.write_text(json.dumps(d))


@pytest.fixture
def world(tmp_path):
    """A bare `hub` with main and three feature branches (one commit each, distinct files), each
    exposed as refs/pull/N/head; a level clone; a stub forge; the env the queue reads."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    hub = tmp_path / "hub"; (hub / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True)
    git(hub, "config", "user.email", "t@t"); git(hub, "config", "user.name", "t")
    (hub / "f.txt").write_text("base\n"); git(hub, "add", "-A"); git(hub, "commit", "-qm", "base")
    git(hub, "remote", "add", "hub", str(bare)); git(hub, "push", "-q", "hub", "main")
    d = tmp_path / "stub"; d.mkdir()
    for n, name in ((41, "feat1"), (42, "feat2"), (43, "feat3")):
        git(hub, "checkout", "-q", "-b", name, "main")
        (hub / (name + ".txt")).write_text(name + "\n"); git(hub, "add", "-A"); git(hub, "commit", "-qm", name)
        git(hub, "push", "-q", "hub", name)
        git(bare, "update-ref", "refs/pull/%d/head" % n, "refs/heads/" + name)
        (d / ("pr.%d.json" % n)).write_text(json.dumps({"number": n, "state": "open", "merged": False, "title": name,
                                                         "head": {"ref": name, "sha": "HEADSHA"}, "labels": []}))
    git(hub, "checkout", "-q", "main"); git(hub, "fetch", "-q", "hub")
    api = tmp_path / "stub-api.sh"; api.write_text(STUB); api.chmod(0o755)
    env = dict(os.environ, PR_QUEUE_FOREGROUND="1", PR_QUEUE_API=str(api), PR_QUEUE_HUB=str(hub), PR_QUEUE_WT=str(tmp_path / "merge-wt"),
               PR_QUEUE_TOOLS_DIR=str(hub / "scripts"),  # siblings stay the fixture's
               PR_QUEUE_REWAIT_SECS="0", PR_QUEUE_REWAIT_POLL_SECS="0",
               PR_QUEUE_POLL_SECS="0", PR_QUEUE_REPO="o/r", PR_QUEUE_LABEL="0",
               FORGE_TOOLS_REMOTE="hub",  # the fixture's forge remote, the name pr-queue.sh defaulted to
               STUB_DIR=str(d), STUB_LOG=str(d / "stub.log"), STUB_BARE=str(bare))
    return {"bare": bare, "hub": hub, "d": d, "env": env}


def drain(w, *args, env=None):
    return subprocess.run(["sh", str(QUEUE), "drain", *args], capture_output=True, text=True,
                          env={**w["env"], **(env or {})}, timeout=180)


def subjects_on_main(w):
    return git(w["bare"], "log", "--format=%s", "main").splitlines()


def created(w):
    p = w["d"] / "created.log"
    return p.read_text().splitlines() if p.exists() else []


def marks(w):
    p = w["d"] / "marks.log"
    return p.read_text().splitlines() if p.exists() else []


def test_a_batch_member_whose_body_would_close_an_open_wayfinder_ticket_is_not_landed(world):
    """The close-keyword check on the batch path. The integration PR's own body is generated, but every member's
    commits land with it, so each member is read before the landing -- and refused there."""
    p = world["d"] / "pr.42.json"
    pr = json.loads(p.read_text()); pr["body"] = "Does not close #74."; p.write_text(json.dumps(pr))
    world["env"]["STUB_ISSUE"] = '{"number": 74, "state": "open", "labels": [{"name": "wayfinder:task"}]}'
    r = drain(world, "--batch")
    assert "REFUSING to merge #42" in r.stdout, r.stdout + r.stderr
    assert "feat2" not in subjects_on_main(world), f"member 42 landed anyway:\n{r.stdout}"


def test_a_member_that_would_close_a_ticket_is_DROPPED_and_the_rest_still_land(world):
    """The test above asserts the offender does not land, which was already true -- and
    passed while the drain STOPPED and took every innocent member with it. Measured 2026-09-22: a
    GREEN batch of two was abandoned because ONE body named a ticket, and the other PR, which had
    nothing wrong with it, did not land.

    A close keyword is per-member and the author's to fix, so it means "this PR cannot land right
    now", never "this run cannot continue" -- the same distinction the one-member batch and the draft 405
    each drew once before. It is dropped exactly as a CONFLICTING member is, position kept."""
    p = world["d"] / "pr.42.json"
    pr = json.loads(p.read_text()); pr["body"] = "Closes #74."; p.write_text(json.dumps(pr))
    world["env"]["STUB_ISSUE"] = '{"number": 74, "state": "open", "labels": [{"name": "wayfinder:task"}]}'
    r = drain(world, "--batch")
    assert r.returncode == 0, "the drain stopped instead of dropping one member:\n" + r.stdout + r.stderr
    assert "#42 would CLOSE an open wayfinder ticket" in r.stdout and "DROPPED" in r.stdout, r.stdout
    on_main = subjects_on_main(world)
    assert "feat2" not in on_main, f"the offending member landed anyway:\n{r.stdout}"
    assert "feat1" in on_main and "feat3" in on_main, (
        f"innocent members were stranded by another PR's body:\n{r.stdout}")
    assert "drain: merged 2, skipped 1, of 3 queued" in r.stdout, r.stdout


def test_three_green_members_land_as_one_batch_with_one_gate_run(world):
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ADMITTED -- success" in r.stdout and "3 ready" in r.stdout   # admission wording
    assert len(created(world)) == 1, "one integration PR, not one per member: " + str(created(world))
    assert "LANDED -- main is now" in r.stdout and "3 PR(s), one gate run" in r.stdout, r.stdout
    assert subjects_on_main(world) == ["feat3", "feat2", "feat1", "base"], subjects_on_main(world)
    # every member marked manually-merged at the replayed commit that is now on main
    tips = {git(world["bare"], "rev-parse", "main~2"): "41", git(world["bare"], "rev-parse", "main~1"): "42", git(world["bare"], "rev-parse", "main"): "43"}
    got = {}
    for line in marks(world):
        n = line.split()[0]
        sha = json.loads([a for a in line.split(" -d ")[1:]][0].split(" -w")[0])["MergeCommitID"]
        got[sha] = n
    assert got == tips, (got, tips)
    assert "drain: merged 3, skipped 0, of 3 queued" in r.stdout
    # the integration branch is retired afterwards
    assert not [b for b in git(world["bare"], "for-each-ref", "--format=%(refname)").splitlines() if "queue/batch" in b]


def test_a_batch_that_lands_a_client_change_still_marks_its_members(world):
    """`hub_api_require_current` refuses every WRITE from a client that is a former version
    of `scripts/hub-api.sh` on main. A batch that LANDS a change to that file makes its own
    client a former version the instant main moves, so every post-landing write is refused --
    measured on a live drain that landed two members and left both OPEN, still carrying
    the `WIP: ` prefix, with their code already on main.

    Here the client IS tracked in the fixture's checkout and one member edits it, which is the shape
    that trips it. The fix stands up a throwaway detached worktree at the new main and runs the
    bookkeeping from that, so the guard passes on its own terms rather than being exempted."""
    hub, d = world["hub"], world["d"]
    tracked = hub / "scripts"
    tracked.mkdir(exist_ok=True)
    api = tracked / "hub-api.sh"
    api.write_text((d.parent / "stub-api.sh").read_text())
    api.chmod(0o755)
    git(hub, "checkout", "-q", "main")
    git(hub, "add", "-A"); git(hub, "commit", "-qm", "client")
    git(hub, "push", "-q", "hub", "main")
    # A member that edits the client -- this is what makes the queue's own copy stale on landing.
    # REBUILD feat3 ON TOP OF THE CLIENT COMMIT. Its edit has to reach the replay, and the replay
    # is a CHERRY-PICK, which constrains the shape of `main..feat3` twice over:
    #
    #   `git merge main` -- what this did first -- puts a MERGE COMMIT in the range, and a
    #   cherry-pick cannot take a merge without `-m`. The drain therefore replayed the PRE-EDIT
    #   branch and main never changed the client.
    #
    #   `git checkout main -- scripts/hub-api.sh` removes the merge, but then feat3's commit ADDS a
    #   file main already has, so the replay hits an add/add conflict and the member is DROPPED.
    #   Measured on CI 2026-09-09: "batch: #43 CONFLICTS when replayed after #41 #42".
    #
    # Branching feat3 from main AFTER the client commit makes its edit an ordinary MODIFICATION of
    # a file the base already tracks: no merge to pick, and nothing to collide with. Both earlier
    # shapes present identically -- the guard below fires because main never changed the client --
    # so the property is asserted where it is created rather than diagnosed from the drain a third
    # time.
    git(hub, "branch", "-qD", "feat3")
    git(hub, "checkout", "-q", "-b", "feat3", "main")
    (hub / "feat3.txt").write_text("feat3\n")
    api2 = hub / "scripts" / "hub-api.sh"
    api2.write_text(api2.read_text() + "\n# landed change to the client\n")
    git(hub, "add", "-A"); git(hub, "commit", "-qm", "feat3 edits the client")
    git(hub, "push", "-q", "-f", "hub", "feat3")
    assert git(hub, "rev-list", "--merges", "main..feat3") == "", (
        "a merge commit in main..feat3 is not cherry-pickable, so the client edit would never "
        "reach the replay and this test would pass through the code it exists to exercise")
    # THE PR REF IS PINNED AT FIXTURE TIME and does not follow a later push. Without this the drain
    # batches the PRE-EDIT feat3, the client change never lands, nothing is stale, and the test
    # passes through the code it exists to exercise -- which is what it reported on the first run.
    git(world["bare"], "update-ref", "refs/pull/43/head", "refs/heads/feat3")
    git(hub, "checkout", "-q", "main"); git(hub, "fetch", "-q", "hub")

    world["env"] = dict(world["env"], PR_QUEUE_API=str(api))
    r = drain(world, "--batch")

    assert "LANDED -- main is now" in r.stdout, r.stdout + r.stderr
    assert "the queue's own client is now a" in r.stdout, (
        "the staleness was not detected, so this test is not exercising the stale-client path: " + r.stdout)
    assert "throwaway checkout" in r.stdout, r.stdout
    assert "landed but are NOT marked merged" not in r.stdout, (
        "members landed unmarked -- this is the failure this test guards: " + r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr
    # and the throwaway checkout is not left behind
    left = [l for l in git(hub, "worktree", "list").splitlines() if "pr-queue-client" in l]
    assert not left, "the throwaway client worktree leaked: %r" % left


def test_an_ordinary_batch_does_not_stand_up_a_client_worktree__control(world):
    """THE CONTROL. The refresh must engage ONLY when staleness is positively established --
    otherwise it is doing work, and standing up a worktree, on every landing.

    The default fixture's stub API is not tracked in the checkout, so `_client_is_stale` cannot read
    a blob for it and answers NO. Without this, the test above passes on a refresh that fires
    unconditionally."""
    r = drain(world, "--batch")
    assert "LANDED -- main is now" in r.stdout, r.stdout + r.stderr
    assert "the queue's own client is now a" not in r.stdout, (
        "the refresh fired on an ordinary batch that changed no client: " + r.stdout)
    assert "throwaway checkout" not in r.stdout, r.stdout


def test_a_member_red_on_its_own_head_is_skipped_before_any_assembly(world):
    """Ready means green on its own head: a red member never enters a batch, so the batch is not
    what discovers it and no gate run is spent finding it."""
    world["env"]["STUB_RED_SUBJECTS"] = "feat2"
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#42 SKIP -- RED on its own head" in r.stdout, r.stdout
    assert len(created(world)) == 1 and "batch: RED" not in r.stdout   # the batch itself found no red
    assert subjects_on_main(world) == ["feat3", "feat1", "base"], subjects_on_main(world)
    assert "drain: merged 2, skipped 1, of 3 queued" in r.stdout, r.stdout


def test_an_interaction_red_is_found_by_halving_and_the_halves_land(world):
    """Each member is green alone; the batch is red because feat2 and feat3 together break the
    gate. Halving lands [41 42] as one batch, then 43 ALONE through the ordinary path -- one
    member is never a batch -- where its branch is updated onto the main that landing
    produced and its own gate then shows the interaction. A red that no per-PR gate could have
    shown before the update, found in O(log n) runs."""
    world["env"]["STUB_RED_SUBJECTS"] = "feat2 feat3"
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "batch: RED" in r.stdout and "splitting #41 #42 #43" in r.stdout, r.stdout
    # TWO integration attempts, not three: [41 42 43] (red) and [41 42] (green). The lone #43
    # builds no `queue/batch-*` branch and no integration PR -- it is updated onto main and
    # gated on its OWN head, which is the head the watch and the merge then agree about.
    assert len(created(world)) == 2, created(world)
    closed = (world["d"] / "closed.log").read_text().splitlines()
    assert len(closed) == 1, "only the red integration PR is closed: " + str(closed)
    # The red arrives as rc 6 from `wait_for_green` through `update_and_rewait` and `merge_queued`,
    # which `approve_one` reports as 7 -- skipped with its position kept, NOT a halted drain.
    assert "batch: #43 SKIP (rc=7) -- position kept" in r.stdout, r.stdout
    assert "STANDING" not in r.stdout, "a red lone member must not halt the drain:\n" + r.stdout
    assert subjects_on_main(world) == ["feat2", "feat1", "base"], subjects_on_main(world)
    assert "drain: merged 2, skipped 1, of 3 queued" in r.stdout, r.stdout


def _scope_map(w, tests):
    """Commit a `.ci-scope.json` and impact map onto main AFTER the feature branches were cut, as
    the real map lands between PRs; the drain reads both at the assembly base."""
    hub = w["hub"]
    (hub / "scripts" / "tests").mkdir(parents=True, exist_ok=True)
    (hub / ".ci-scope.json").write_text(json.dumps({"tests_dir": "scripts/tests", "map": "scripts/tests/impact-map.json",
                                                     "machinery": ["scripts/tests/conftest.py"]}))
    (hub / "scripts" / "tests" / "impact-map.json").write_text(json.dumps({"tests": tests}))
    (hub / "scripts" / "batch_blame.py").write_text((REPO_ROOT / "scripts" / "batch_blame.py").read_text())
    git(hub, "add", "-A"); git(hub, "commit", "-qm", "scopemap"); git(hub, "push", "-q", "hub", "main")


def test_a_red_that_names_one_members_test_ejects_it_and_lands_the_rest_in_two_runs(world):
    """The red names test_feat2.py, which the map records reading feat2.txt -- only #42's
    diff reaches it. So #42 is ejected (position kept) and #41 #43 are re-gated ONCE: two
    integration runs, no halving. Halving took three (#41 #42 #43, #41 #42, then #43 alone)."""
    _scope_map(world, {"scripts/tests/test_feat2.py": ["feat2.txt"], "scripts/tests/test_rest.py": ["feat1.txt", "feat3.txt"]})
    world["env"]["STUB_RED_SUBJECTS"] = "feat2 scopemap"   # red only on the new main: #42 is green alone
    world["env"]["STUB_WHY_RED"] = "  FAILED scripts/tests/test_feat2.py::test_it - AssertionError: boom"
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "EJECTING #42 (position kept), re-gating #41 #43 once" in r.stdout, r.stdout
    assert "splitting" not in r.stdout, r.stdout
    assert len(created(world)) == 2, created(world)
    assert subjects_on_main(world) == ["feat3", "feat1", "scopemap", "base"], subjects_on_main(world)
    assert "drain: merged 2, skipped 1, of 3 queued" in r.stdout, r.stdout


def test_a_red_whose_only_hits_are_test_NAMES_halves_and_says_why(world):
    """The negative half of the test above: blame must not read these shapes as failures. A
    passing test NAMED with `assertion`, and the differential's indented `FAILED ... [proven:
    FAILED on base]` (a PASS of that gate) both name test_feat2.py; neither is a failure, so
    nothing is ejected and the drain says why it halves."""
    _scope_map(world, {"scripts/tests/test_feat2.py": ["feat2.txt"], "scripts/tests/test_rest.py": ["feat1.txt", "feat3.txt"]})
    world["env"]["STUB_RED_SUBJECTS"] = "feat2 scopemap"
    world["env"]["STUB_WHY_RED"] = ("  3.05s call     scripts/tests/test_feat2.py::test_the_assertion_can_fail\n"
                                    "      FAILED        scripts/tests/test_feat2.py::test_new  [proven: FAILED on base]")
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "EJECTING" not in r.stdout and "halve: the red names no failing test file" in r.stdout, r.stdout
    assert "splitting #41 #42 #43" in r.stdout, r.stdout


def test_a_red_batch_that_will_be_SPLIT_leaves_its_members_drafted(world):
    """A red batch of two or more is HALVED, and each half is gated on its own integration run --
    so un-drafting every member at the red pre-empts the design and buys nothing.

    Measured 2026-09-21 on the live queue: a red batch of two un-drafted BOTH two seconds
    before `splitting`. `undraft_member` retitles the PR, which the forge runs a suite for, and
    `_watch_undrafted` arms a watch on each -- so the drain started two simultaneous full gates
    where it should have run one. The next `try_batch` re-drafts the second member moments later,
    so the STATE settles correctly and the wasted run does not: it is already queued.
    """
    world["env"]["STUB_RED_SUBJECTS"] = "feat2 feat3"
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "batch: RED" in r.stdout and "splitting" in r.stdout, r.stdout
    assert "batch: members stay DRAFTS through the split" in r.stdout, r.stdout
    # THE ASSERTION IS ABOUT THE WINDOW, not the end state: the end state was always right, because
    # the next `try_batch` re-drafts. What the defect produced was un-draft lines BEFORE the split.
    head, _sep, _rest = r.stdout.partition("splitting")
    _red = head[head.index("batch: RED"):]
    assert "un-drafted" not in _red, (
        "members were un-drafted between the red and the split, which starts their suites:\n" + _red)


def test_a_conflicting_member_is_dropped_and_the_rest_land_as_a_batch(world):
    hub = world["hub"]
    for name, n in (("feat1", 41), ("feat2", 42)):
        git(hub, "checkout", "-q", name)
        (hub / "f.txt").write_text(name + " edits the base file\n"); git(hub, "add", "-A"); git(hub, "commit", "-qm", name + " conflict")
        git(hub, "push", "-q", "-f", "hub", name); git(world["bare"], "update-ref", "refs/pull/%d/head" % n, "refs/heads/" + name)
    git(hub, "checkout", "-q", "main")
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#42 CONFLICTS" in r.stdout and "DROPPED" in r.stdout, r.stdout
    assert "feat2 conflict" not in subjects_on_main(world) and "feat3" in subjects_on_main(world) and "feat1 conflict" in subjects_on_main(world)
    assert "drain: merged 2, skipped 1, of 3 queued" in r.stdout, r.stdout


def test_a_refused_manual_mark_leaves_the_landing_and_says_which_setting(world):
    world["env"]["STUB_MANUAL_405"] = "1"
    r = drain(world, "--batch")
    assert r.returncode != 0
    assert subjects_on_main(world) == ["feat3", "feat2", "feat1", "base"], "the landing happened before the marks"
    assert "allow_manual_merge" in r.stdout and "NOT marked merged" in r.stdout, r.stdout
    assert not [l for l in (world["d"] / "stub.log").read_text().splitlines() if "--delete" in l], "unmarked members' branches must not be deleted"


def test_a_mark_that_dies_before_http_prints_the_clients_own_words(world):
    """The third `_refusal_detail` site. `mark_manually_merged` sent stderr to /dev/null, so a client
    that failed before any HTTP logged `http=unreadable:` and nothing after the colon."""
    world["env"]["STUB_MANUAL_DEAD_CLIENT"] = "1"
    r = drain(world, "--batch")
    assert r.returncode != 0
    assert "http=unreadable" in r.stdout and "NOT marked merged" in r.stdout, r.stdout
    assert "credential-/gone/hub-api.sh" in r.stdout, r.stdout


def test_dry_run_batch_reports_the_batch_and_pushes_nothing(world):
    r = drain(world, "--dry-run", "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "as ONE batch of 3" in r.stdout, r.stdout
    assert not created(world) and subjects_on_main(world) == ["base"]


def test_a_single_ready_member_takes_the_ordinary_path(world):
    queue(world, 41)
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not created(world), "one member needs no integration PR"
    assert subjects_on_main(world) == ["feat1", "base"]


def test_serial_drain_is_unchanged_without_the_flag__control(world):
    """One queued PR, no flag: the serial path lands it and no integration PR exists. (The stub
    forge cannot rebase on `/update`, so a multi-PR serial drain is test_pr_queue_drain.py's.)"""
    queue(world, 41)
    r = drain(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not created(world) and "batch" not in r.stdout
    assert subjects_on_main(world) == ["feat1", "base"]


# ------------------------------------------------ the draft-first queue
#
# Operator, 2026-09-15/16: a PR that is not at the front of the queue waits as a DRAFT (`pr create`
# drafts it). When the front of the queue is a draft, `drain` -- with no flag -- batches
# every waiting draft into one integration PR; a draft that reaches the front ALONE is un-drafted
# and lands on its own run. A ready PR behind drafts keeps its position for the next drain. The
# queue IS the open PRs, oldest first.


def as_draft(w, *nums):
    for n in nums:
        f = w["d"] / ("pr.%d.json" % n)
        d = json.loads(f.read_text())
        d["title"] = "WIP: " + d["title"]
        f.write_text(json.dumps(d))


def titles(w):
    p = w["d"] / "titles.log"
    return p.read_text().splitlines() if p.exists() else []


def test_two_drafts_at_the_front_land_as_one_batch_without_the_flag(world):
    queue(world, 41, 42)
    as_draft(world, 41, 42)
    r = drain(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(created(world)) == 1, "two waiting drafts must gate as ONE integration PR: %s" % created(world)
    assert {"feat1", "feat2"} <= set(subjects_on_main(world)), subjects_on_main(world)


def test_a_lone_draft_at_the_front_is_undrafted_and_lands_on_its_own(world):
    """One draft is not a batch: it is un-drafted so its suite runs, and lands through the ordinary
    path. The un-draft is the assertion that can fail -- this stub reports the suite as run whatever
    the title says, so a queue that merged the draft as-is would still put feat1 on main."""
    queue(world, 41)
    as_draft(world, 41)
    r = drain(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not created(world), "one draft needs no integration PR"
    assert "41 title feat1" in titles(world), "landed without being un-drafted: %s" % titles(world)
    assert subjects_on_main(world) == ["feat1", "base"]


def test_a_batch_that_assembly_leaves_with_ONE_member_opens_no_integration_pr(world):
    """One member is never a batch, at the second count. `batch_run` refuses to batch one member it is HANDED, but
    assembly drops members, and the survivor of a dropped batch went on to its own integration PR.
    Measured 2026-09-22: three drafts in, two dropped, and a "queue: batch of 1"
    integration PR opened. Here two waiting drafts go in and #42 is dropped for naming a ticket, so #41
    must take the one-member path: un-drafted, gated on its own head, no queue/batch-* PR."""
    queue(world, 41, 42)
    as_draft(world, 41, 42)
    p = world["d"] / "pr.42.json"
    pr = json.loads(p.read_text()); pr["body"] = "Closes #74."; p.write_text(json.dumps(pr))
    world["env"]["STUB_ISSUE"] = '{"number": 74, "state": "open", "labels": [{"name": "wayfinder:task"}]}'
    r = drain(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#42 would CLOSE an open wayfinder ticket" in r.stdout, r.stdout
    assert not created(world), "a batch reduced to one member opened an integration PR: %s\n%s" % (created(world), r.stdout)
    assert "41 title feat1" in titles(world), "the survivor landed without being un-drafted: %s" % titles(world)
    assert "feat1" in subjects_on_main(world) and "feat2" not in subjects_on_main(world), subjects_on_main(world)


def test_a_ready_pr_behind_drafts_keeps_its_position_for_the_next_drain(world):
    """A ready PR behind waiting drafts is not at the front. Landing it first would jump the queue,
    so it is skipped with its position kept, and the drafts ahead of it land as their batch."""
    as_draft(world, 41, 42)          # 43 stays ready, behind them
    r = drain(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(created(world)) == 1, created(world)
    assert "feat3" not in subjects_on_main(world), "the ready PR jumped the waiting drafts"
    assert "behind" in r.stdout and "#43" in r.stdout, r.stdout


def test_drafts_behind_an_open_RED_pr_wait_instead_of_gating_beside_it(world):
    """Measured 2026-09-24 16:39:47: a drain skipped a red PR -- still open, its owner messaged to
    fix it -- then called the next one "the only waiting draft ... reached the front" and un-drafted it. The
    fix was pushed 23 seconds later and two PRs gated at once. The front is the oldest open PR, and
    a red one is still open: the drafts behind it wait, positions kept, and nothing is un-drafted."""
    as_draft(world, 42, 43)
    world["env"]["STUB_RED_SUBJECTS"] = "feat1"     # #41 is ready and red on its own head
    r = drain(world)
    out = r.stdout + r.stderr
    assert "#41 SKIP -- checks are not green" in out, "the red PR was never reached, so this measured nothing:\n" + out
    assert "#42 SKIP -- a draft behind #41" in out and "#43 SKIP -- a draft behind #41" in out, out
    assert titles(world) == [], "a draft behind a red PR was un-drafted, starting a second gate: %s\n%s" % (titles(world), out)
    assert created(world) == [], "a batch gated beside the red PR:\n" + out
    assert subjects_on_main(world) == ["base"], out


def test_drafts_behind_a_pr_that_LANDED_still_batch__control(world):
    """THE CONTROL: the wait is for a PR ahead that is still open, not for any PR ahead. Same world
    with #41 green -- it lands, and the two drafts behind it gate as ONE batch in the same drain."""
    as_draft(world, 42, 43)
    r = drain(world)
    out = r.stdout + r.stderr
    assert "a draft behind" not in out, out
    assert len(created(world)) == 1, "the drafts behind a landed PR did not batch: %s\n%s" % (created(world), out)
    assert {"feat1", "feat2", "feat3"} <= set(subjects_on_main(world)), out


def updated(w):
    p = w["d"] / "updated.log"
    return p.read_text().splitlines() if p.exists() else []


def test_a_ready_pr_behind_an_open_RED_pr_is_still_updated_and_lands(world):
    """Operator, 2026-09-24: a red PR holds only the DRAFTS behind it, never a ready PR. #41 lands and
    moves main, so #42 and #43 are both behind it; #42 is updated, gated and RED, and #43 is still
    updated, gated and landed in the same drain -- a red PR's fix can take hours, and the drain exists
    to route around a stuck head."""
    world["env"]["STUB_RED_SUBJECTS"] = "feat2"
    r = drain(world)
    out = r.stdout + r.stderr
    assert "feat1" in subjects_on_main(world), "nothing landed first, so nothing was behind:\n" + out
    assert "42 updated" in updated(world), "#42 was never gated, so this measured nothing:\n" + out
    assert "feat2" not in subjects_on_main(world), "red #42 landed:\n" + out
    assert "43 updated" in updated(world), "#43 was held behind red #42:\n" + out
    assert "feat3" in subjects_on_main(world), out


def as_serial(w, *nums):
    for n in nums:
        f = w["d"] / ("pr.%d.json" % n)
        d = json.loads(f.read_text())
        d["labels"] = d.get("labels", []) + ["queue:serial"]
        f.write_text(json.dumps(d))


def test_a_serial_draft_at_the_front_lands_alone_and_the_drafts_behind_it_batch(world):
    """Since 2026-09-16, `queue:serial` keeps a PR out of every batch. At the front it
    lands on its own -- un-drafted, its own run -- and the drafts behind it are still a batch."""
    as_draft(world, 41, 42, 43)
    as_serial(world, 41)
    r = drain(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "41 title feat1" in titles(world), "the serial draft was not un-drafted: %s" % titles(world)
    assert len(created(world)) == 1, "one batch, for the two behind it: %s" % created(world)
    # The derived-order line lists every open PR, so it is not evidence about the batch.
    walk = "\n".join(l for l in r.stdout.splitlines() if "queue order DERIVED" not in l)
    assert "#42 #43" in walk and "#41 #42" not in walk, "the serial PR joined the batch: " + r.stdout
    assert {"feat1", "feat2", "feat3"} <= set(subjects_on_main(world)), subjects_on_main(world)


def test_a_serial_draft_behind_a_draft_closes_its_batch(world):
    """Behind the front, a serial draft ends the batch where it stands: the draft ahead of it is
    alone, so it lands alone; the serial one and everything behind it wait their turn."""
    as_draft(world, 41, 42, 43)
    as_serial(world, 42)
    r = drain(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not created(world), "no batch can form around a serial PR: %s" % created(world)
    assert subjects_on_main(world) == ["feat1", "base"], subjects_on_main(world)
    assert "SERIAL" in r.stdout, r.stdout


def test_batch_flag_keeps_a_serial_pr_out_of_the_batch(world):
    as_serial(world, 43)            # all three ready; the flag batches every ready PR but this one
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(created(world)) == 1, created(world)
    assert "feat3" not in subjects_on_main(world), "a serial PR landed inside a batch"
    assert {"feat1", "feat2"} <= set(subjects_on_main(world)), subjects_on_main(world)


def test_the_queue_is_derived_oldest_first_from_open_prs(world):
    """PASSES ON BASE: a rename of the unset-order-issue test. Base derived the same order whenever
    no fence was set; removing the fence made that the only order, so there is no new behaviour to fail on."""
    as_draft(world, 41, 42, 43)
    r = drain(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DERIVED" in r.stdout, r.stdout
    assert "#41 #42 #43" in r.stdout, "not oldest first: " + r.stdout
    assert len(created(world)) == 1, created(world)
    assert {"feat1", "feat2", "feat3"} <= set(subjects_on_main(world)), subjects_on_main(world)


def test_a_gate_that_never_settles_is_not_a_red_and_is_not_halved(world):
    """2026-09-08: an integration PR sat 14 minutes with every check 'Waiting to run' (runner capacity
    1, other PRs ahead of it), the wait expired, the batch read that as RED and halved --
    two more integration PRs gating a tree nothing had measured. Expiry is a STOP with positions kept."""
    before = subjects_on_main(world)
    # Only the integration head carries both subjects; each member's own head carries one, so the
    # members read green on their own heads and the batch's gate reads pending forever.
    world["env"]["STUB_PENDING_SUBJECTS"] = "feat1 feat3"
    world["env"]["PR_QUEUE_WAIT_POLLS"] = "2"
    r = drain(world, "--batch")
    assert "NEVER SETTLED" in r.stdout, r.stdout + r.stderr
    assert "splitting" not in r.stdout, r.stdout
    assert len(created(world)) == 1, created(world)   # one integration PR, then stop -- no halves
    assert subjects_on_main(world) == before


def test_approve_with_repo_lands_and_retires_on_the_TARGET_and_leaves_the_hub_alone(world, tmp_path):
    """`--repo <owner/repo>` drains a target. The code is this checkout's; the merge, the
    branch retirement and every fetch name the target. A second bare repo stands in for it."""
    target = tmp_path / "target.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(world["bare"]), str(target)], check=True)
    # `clone --bare` copies branches, not the forge's refs/pull/*; the target serves those too.
    subprocess.run(["git", "-C", str(target), "fetch", "-q", str(world["bare"]), "refs/pull/*:refs/pull/*"], check=True)
    env = dict(world["env"], STUB_BARE=str(target), PR_QUEUE_REMOTE=str(target))
    r = subprocess.run(["sh", str(QUEUE), "--repo", "o/target", "approve", "41"],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "feat1" in git(target, "log", "--format=%s", "main"), "landed on the target's main"
    assert subprocess.run(["git", "-C", str(target), "rev-parse", "-q", "--verify", "refs/heads/feat1"],
                          capture_output=True).returncode != 0, "branch retired on the TARGET"
    assert "feat1" not in git(world["bare"], "log", "--format=%s", "main"), "the hub's main did not move"
    git(world["bare"], "rev-parse", "--verify", "refs/heads/feat1")   # the hub's branch is untouched


def _bare_copy(src, dest):
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "fetch", "-q", str(src), "refs/pull/*:refs/pull/*"], check=True)


def test_approve_with_repo_compares_against_the_TARGETS_main_not_the_hubs(world, tmp_path):
    """Measured 2026-09-15 on a target repo: the merge pre-check asked whether `hub/main` was an ancestor
    of the target's PR head. A target is another repo, so the answer was always no: "behind main",
    an update that moved nothing, three tries, STOPPING. The test above could not see it because its
    target is a clone of the hub. Here the hub moves on and the target does not."""
    target = tmp_path / "target.git"
    _bare_copy(world["bare"], target)
    (world["hub"] / "hub-only.txt").write_text("hub only\n")
    git(world["hub"], "add", "-A"); git(world["hub"], "commit", "-qm", "hub-only")
    git(world["hub"], "push", "-q", "hub", "main"); git(world["hub"], "fetch", "-q", "hub")
    env = dict(world["env"], STUB_BARE=str(target), PR_QUEUE_REMOTE=str(target))
    r = subprocess.run(["sh", str(QUEUE), "--repo", "o/target", "approve", "41"],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "is behind main" not in r.stdout, "judged against the hub's main:\n" + r.stdout
    target_log = git(target, "log", "--format=%s", "main")
    assert "feat1" in target_log, "landed on the target's main"
    assert "hub-only" not in target_log, "the hub's commit must not reach the target"


def test_approve_with_repo_lands_on_a_fork_whose_default_branch_is_not_main(world, tmp_path):
    """A vendored fork carries its patches on `fix/quiet-...`, with plain upstream
    on `main`. The queue hardcoded `main`, so it judged freshness against upstream and asked the
    forge to land on a line that is not ours. The target here has `patch` = main + one commit, as
    its default: #41 is behind it, must be updated onto it, and must land there -- main untouched."""
    target = _bare_copy_with_patch_default(world, tmp_path)
    env = dict(world["env"], STUB_BARE=str(target), PR_QUEUE_REMOTE=str(target), STUB_DEFAULT_BRANCH="patch")
    r = subprocess.run(["sh", str(QUEUE), "--repo", "o/target", "approve", "41"],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    patch_log = git(target, "log", "--format=%s", "patch")
    assert "feat1" in patch_log and "patch-only" in patch_log, "landed on the default branch:\n" + r.stdout
    assert "feat1" not in git(target, "log", "--format=%s", "main"), "upstream's main must not move"
    # The unfixed queue ALSO lands here, via the forge's 405 update -- which rebases onto the PR's
    # own base. What it got wrong is the verdict before that: "current" against upstream's main.
    assert "is behind patch" in r.stdout and "outdated" not in r.stdout, r.stdout


def test_a_batch_on_a_fork_builds_on_and_targets_its_default_branch(world, tmp_path):
    """Where hardcoding `main` does real damage: the integration branch was cut from upstream's
    `main` and its PR opened AGAINST `main`, so a batch would propose our patches to upstream's line."""
    target = _bare_copy_with_patch_default(world, tmp_path)
    env = dict(world["env"], STUB_BARE=str(target), PR_QUEUE_REMOTE=str(target), STUB_DEFAULT_BRANCH="patch")
    r = subprocess.run(["sh", str(QUEUE), "--repo", "o/target", "drain", "--batch"],
                       capture_output=True, text=True, env=env, timeout=300)
    bases = (world["d"] / "bases.log").read_text().split()[1::2] if (world["d"] / "bases.log").exists() else []
    assert bases and set(bases) == {"patch"}, "integration PR base(s) %r\n%s" % (bases, r.stdout + r.stderr)
    assert "patch-only" in git(target, "log", "--format=%s", "patch")
    assert "feat1" not in git(target, "log", "--format=%s", "main"), "upstream's main must not move"


def test_approve_with_repo_REFUSES_when_the_default_branch_is_unreadable(world, tmp_path):
    """The control's other half: an unreadable answer must not fall back to `main`, which is the
    very assumption reading the default branch removes. `o/elsewhere` is a repo the stub does not know."""
    env = dict(world["env"], PR_QUEUE_REMOTE=str(world["bare"]))
    r = subprocess.run(["sh", str(QUEUE), "--repo", "o/elsewhere", "approve", "41"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2 and "cannot read its default branch" in r.stderr, r.stdout + r.stderr


def _bare_copy_with_patch_default(world, tmp_path):
    target = tmp_path / "fork.git"
    _bare_copy(world["bare"], target)
    wt = tmp_path / "fork-wt"
    subprocess.run(["git", "clone", "-q", str(target), str(wt)], check=True)
    subprocess.run(["git", "-C", str(wt), "checkout", "-q", "-b", "patch"], check=True)
    (wt / "patch.txt").write_text("our patch\n")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "patch-only"], check=True)
    subprocess.run(["git", "-C", str(wt), "push", "-q", "origin", "patch"], check=True)
    return target


def test_approve_with_repo_DERIVES_the_target_remote_from_the_hub_remote(world, tmp_path):
    """No PR_QUEUE_REMOTE: the target is this checkout's `hub` URL with <owner/repo> swapped. The
    default was a hardcoded ssh alias that a new host lacked, and the branch retirement
    below pushes to it with stderr discarded -- so the PR landed and its branch silently stayed."""
    forge = tmp_path / "forge" / "o"; forge.mkdir(parents=True)
    hub_bare, target = forge / "r.git", forge / "target.git"
    _bare_copy(world["bare"], hub_bare)
    _bare_copy(world["bare"], target)
    git(world["hub"], "remote", "set-url", "hub", str(hub_bare))
    env = dict(world["env"], STUB_BARE=str(target))
    env.pop("PR_QUEUE_REMOTE", None)
    r = subprocess.run(["sh", str(QUEUE), "--repo", "o/target", "approve", "41"],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "feat1" in git(target, "log", "--format=%s", "main"), "landed on the target's main"
    assert subprocess.run(["git", "-C", str(target), "rev-parse", "-q", "--verify", "refs/heads/feat1"],
                          capture_output=True).returncode != 0, "branch NOT retired: the derived remote was not the target"
    git(hub_bare, "rev-parse", "--verify", "refs/heads/feat1")   # the hub's branch is untouched


def test_approve_with_repo_and_no_hub_remote_REFUSES_rather_than_guessing(world):
    git(world["hub"], "remote", "remove", "hub")
    env = dict(world["env"]); env.pop("PR_QUEUE_REMOTE", None)
    r = subprocess.run(["sh", str(QUEUE), "--repo", "o/target", "approve", "41"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "PR_QUEUE_REMOTE" in r.stderr, r.stderr

# ---- drafts while batched, admission on not-red, a standing integration PR ------------



def test_members_are_drafts_while_batched_and_restored_before_the_mark(world):
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    t = titles(world)
    assert t[:3] == ["41 title WIP: feat1", "42 title WIP: feat2", "43 title WIP: feat3"], t
    assert t[3:] == ["41 title feat1", "42 title feat2", "43 title feat3"], t
    log = (world["d"] / "stub.log").read_text()
    assert 0 < log.find('"title": "feat1"') < log.find("/pulls/41/merge"), "restored BEFORE the mark"
    assert json.loads((world["d"] / "pr.41.json").read_text())["title"] == "feat1"


def test_a_member_pending_on_its_own_head_is_admitted_and_gated_by_the_integration_run(world):
    """Admission is 'not red': the integration PR gates the same bytes, so a member need not
    wait for its own run to finish before joining."""
    world["env"]["STUB_PENDING_SHAS"] = git(world["bare"], "rev-parse", "refs/pull/42/head")
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#42 ADMITTED -- pending on" in r.stdout, r.stdout
    assert "3 PR(s), one gate run" in r.stdout and subjects_on_main(world)[:3] == ["feat3", "feat2", "feat1"]


def test_a_gate_that_does_not_settle_leaves_the_integration_pr_standing_and_the_next_drain_resumes_it(world):
    world["env"]["STUB_PENDING_SUBJECTS"] = "feat3"        # every sha carrying feat3 -- the integration head too
    r = drain(world, "--batch")
    assert r.returncode == 8, r.stdout + r.stderr
    assert "left STANDING" in r.stdout and "re-run drain to resume" in r.stdout, r.stdout
    assert len(created(world)) == 1, "one integration PR"
    assert not (world["d"] / "closed.log").exists(), "the standing PR must not be closed"
    assert [b for b in git(world["bare"], "for-each-ref", "--format=%(refname)").splitlines() if "queue/batch" in b], "its branch must survive"
    assert titles(world)[-1].endswith("WIP: feat3"), "members stay drafts while it stands"
    assert subjects_on_main(world) == ["base"], "nothing landed"
    del world["env"]["STUB_PENDING_SUBJECTS"]
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESUMING standing integration PR" in r.stdout, r.stdout
    assert len(created(world)) == 1, "resumed, not rebuilt: still one integration PR"
    assert subjects_on_main(world)[:3] == ["feat3", "feat2", "feat1"]
    assert titles(world)[-3:] == ["41 title feat1", "42 title feat2", "43 title feat3"]


def test_a_freeze_set_during_the_integration_gate_stops_that_landing_and_the_next_drain_resumes_it(world):
    """The drain's entry check reads the listing and sees no freeze; the page appears before
    try_batch's last-moment check. That landing must refuse and leave the integration PR STANDING with
    its members drafted, and once the freeze lifts the next drain must RESUME it, not rebuild it."""
    world["env"]["STUB_FREEZE_FROM"] = "2"
    r = drain(world, "--batch")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSING: batch merge #" in r.stdout and "the queue is FROZEN" in r.stdout, r.stdout
    assert "set mid-gate" in r.stdout, "the refusal must print the page's text"
    assert subjects_on_main(world) == ["base"], "nothing lands under the freeze"
    assert len(created(world)) == 1, "one integration PR"
    assert not (world["d"] / "closed.log").exists(), "the refused integration PR must stand"
    assert titles(world)[-1].endswith("WIP: feat3"), "members stay drafts while it stands"
    del world["env"]["STUB_FREEZE_FROM"]
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESUMING standing integration PR" in r.stdout, r.stdout
    assert len(created(world)) == 1, "resumed, not rebuilt"
    assert subjects_on_main(world)[:3] == ["feat3", "feat2", "feat1"]
    assert titles(world)[-3:] == ["41 title feat1", "42 title feat2", "43 title feat3"]


def test_a_standing_pr_whose_member_moved_is_closed_and_rebuilt(world):
    world["env"]["STUB_PENDING_SUBJECTS"] = "feat3"
    assert drain(world, "--batch").returncode == 8
    hub = world["hub"]
    git(hub, "checkout", "-q", "feat2"); (hub / "feat2.txt").write_text("feat2 v2\n")
    git(hub, "add", "-A"); git(hub, "commit", "-qm", "feat2 v2"); git(hub, "push", "-q", "hub", "feat2")
    git(world["bare"], "update-ref", "refs/pull/42/head", "refs/heads/feat2"); git(hub, "checkout", "-q", "main")
    del world["env"]["STUB_PENDING_SUBJECTS"]
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "is STALE (a member's head moved" in r.stdout, r.stdout
    assert len(created(world)) == 2, "closed and rebuilt: a second integration PR"
    assert "feat2 v2" in subjects_on_main(world)


def test_a_landing_that_did_not_reach_main_refuses_the_bookkeeping(world):
    """The mark path does not enforce branch protection -- `Do: manually-merged` returns
    200 with a required context matching nothing and the head still pending -- so nothing outside
    this check stops the queue marking members merged against a tip nobody gated.

    The log line has always SAID "main is now <head>". This asserts it.
    """
    world["env"]["STUB_DIVERT_LANDING"] = "1"
    r = drain(world, "--batch")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "THE MEASURED HEAD DID NOT REACH MAIN" in r.stdout, r.stdout
    # FAILS CLOSED AND RECOVERABLY: members must be left drafted and unmarked, which is the state
    # that has a documented repair. Marking them is the expensive direction.
    assert "marked: http=200" not in r.stdout, (
        "members were marked merged against a tip that never landed\n" + r.stdout)


def test_a_landing_that_did_reach_main_still_marks__control(world):
    """THE CONTROL, and it PASSES ON BASE deliberately -- base has no such check to fire.

    It is here because a guard that refused every batch would satisfy the test above. Same drain,
    same fixture, knob off: the bookkeeping must still run.
    """
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "THE MEASURED HEAD DID NOT REACH MAIN" not in r.stdout, r.stdout
    assert "batch: LANDED" in r.stdout, r.stdout


def test_a_member_with_no_check_runs_is_skipped_not_admitted(world):
    """`_gate_state` collapses "no check-runs registered" to `none`, which fell into the
    same arm as `pending` and `success` and was ADMITTED.

    `hub-api.sh pr checks` already refuses that state in its own words -- "not a pass. If a workflow
    should have fired, it did not" -- so the queue was discarding a refusal the client had made and
    admitting a member nothing had measured, while logging ADMITTED as though it had been.
    """
    world["env"]["STUB_NORUNS_SUBJECTS"] = "feat2"
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NO check-runs registered" in r.stdout, r.stdout
    assert "#42 SKIP" in r.stdout, r.stdout
    # It must be SKIPPED, not admitted-and-gated-later: the point is that nothing measured it.
    assert "#42 ADMITTED" not in r.stdout, (
        "a member with no runs was admitted anyway\n" + r.stdout)
    assert "42 title WIP" not in "\n".join(titles(world)), "a skipped member is never drafted"


def test_a_member_with_runs_is_still_admitted__control(world):
    """THE CONTROL, and it PASSES ON BASE deliberately -- base admits everything non-red.

    Without it, a change that skipped every member would satisfy the test above completely. Same
    drain, knob off: the members must still be admitted and the batch must still form.
    """
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ADMITTED" in r.stdout, r.stdout
    assert "NO check-runs registered" not in r.stdout, r.stdout


def test_a_red_member_is_still_skipped_before_assembly__control(world):
    world["env"]["STUB_RED_SUBJECTS"] = "feat2"
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#42 SKIP -- RED on its own head" in r.stdout, r.stdout
    assert "42 title WIP" not in "\n".join(titles(world)), "a red member is never drafted"


# --- a run stops when the pr-queue.sh it is executing is superseded on main ------------

def _queue_on_main_and_41_edits_it(w, edit):
    """main carries THIS pr-queue.sh, so the run starts current; #41 lands a change to it (or, for
    the control, an unrelated file), and #42 is an ordinary PR queued behind it."""
    hub, bare = w["hub"], w["bare"]
    git(hub, "checkout", "-q", "main")
    (hub / "scripts" / "pr-queue.sh").write_bytes(QUEUE.read_bytes())
    git(hub, "add", "-A"); git(hub, "commit", "-qm", "queue script"); git(hub, "push", "-q", "hub", "main")
    for n, name in ((41, "feat1"), (42, "feat2")):
        git(hub, "checkout", "-q", "-B", name, "main")
        (hub / (name + ".txt")).write_text(name + "\n")
        if n == 41 and edit:
            with open(hub / "scripts" / "pr-queue.sh", "a") as f:
                f.write("# a newer queue\n")
        git(hub, "add", "-A"); git(hub, "commit", "-qm", name)
        git(hub, "push", "-q", "-f", "hub", name)
        git(bare, "update-ref", "refs/pull/%d/head" % n, "refs/heads/" + name)
    git(hub, "checkout", "-q", "main"); git(hub, "fetch", "-q", "hub")
    queue(w, 41, 42)


def test_a_drain_STOPS_once_main_carries_a_newer_pr_queue_sh(world):
    """Drain 1797953 started at 21:17 and was still landing at 21:41 under rules a newer pr-queue.sh had replaced.
    Once #41 puts a different pr-queue.sh on main, the run lands nothing more: #42 waits for a
    drain running the new code."""
    _queue_on_main_and_41_edits_it(world, edit=True)
    r = drain(world)
    out = r.stdout + r.stderr
    assert "feat1" in subjects_on_main(world), out
    assert "feat2" not in subjects_on_main(world), "landed #42 under superseded code:\n" + out
    assert "changed since this run started" in out, out
    assert r.returncode == 2, out


def test_a_drain_whose_pr_queue_sh_is_unchanged_lands_both__control(world):
    """THE CONTROL, and it PASSES ON BASE deliberately: with main's pr-queue.sh unchanged by #41, the
    same drain lands both. Without it, a check that stopped every run would satisfy the test above."""
    _queue_on_main_and_41_edits_it(world, edit=False)
    r = drain(world)
    out = r.stdout + r.stderr
    assert subjects_on_main(world)[:2] == ["feat2", "feat1"], out
    assert "changed since this run started" not in out, out


def _installed_tools(tmp_path, moved):
    """Forge-Tools as an INSTALLED checkout: a clone of a bare upstream holding this scripts/ tree.
    `moved` pushes a newer pr-queue.sh to that upstream from another clone, as a merge would."""
    import shutil
    up = tmp_path / "tools.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(up)], check=True)
    seed = tmp_path / "tools-seed"
    shutil.copytree(REPO_ROOT / "scripts", seed / "scripts", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    git(seed, "add", "-A"); git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "tools")
    git(seed, "push", "-q", str(up), "main")
    tools = tmp_path / "tools"
    subprocess.run(["git", "clone", "-q", str(up), str(tools)], check=True)
    if moved:
        with open(seed / "scripts" / "pr-queue.sh", "a") as f:
            f.write("# a newer queue\n")
        git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "newer")
        git(seed, "push", "-q", str(up), "main")
    return tools / "scripts" / "pr-queue.sh"


@pytest.mark.parametrize("moved", [True, False])
def test_an_installed_queue_stops_when_its_own_checkouts_upstream_moved(world, tmp_path, moved):
    """The consumer carries no pr-queue.sh (the installed layout), so main's copy cannot be the
    reference; the run's own checkout's upstream is. Moved: it lands nothing and says why. The
    control (not moved) lands both, or a check that stopped every run would pass the first arm."""
    queue(world, 41, 42)
    q = _installed_tools(tmp_path, moved)
    r = subprocess.run(["sh", str(q), "drain"], capture_output=True, text=True, env=world["env"], timeout=180)
    out = r.stdout + r.stderr
    if moved:
        assert "changed since this run started" in out, out
        assert r.returncode == 2 and "feat1" not in subjects_on_main(world), out
    else:
        assert "changed since this run started" not in out, out
        assert subjects_on_main(world)[:2] == ["feat2", "feat1"], out


# --- a refused update-before-gate skips its PR and the drain carries on ----------------

def test_a_PR_the_forge_refuses_to_rebase_is_SKIPPED_and_the_queue_behind_it_still_lands(world):
    """Measured 2026-09-23 00:33: the forge answered 409 to its update and the drain STOPPED --
    `0 merged, 0 skipped before it` -- with every PR behind it waiting on one owner's rebase."""
    hub = world["hub"]
    git(hub, "checkout", "-q", "main")
    (hub / "moved.txt").write_text("main moved\n"); git(hub, "add", "-A"); git(hub, "commit", "-qm", "main moves")
    git(hub, "push", "-q", "hub", "main")
    queue(world, 41, 42)
    r = drain(world, env={"STUB_UPDATE_409": "41"})
    out = r.stdout + r.stderr
    assert "feat2" in subjects_on_main(world), "one conflicting PR at the front halted the queue:\n" + out
    assert "feat1" not in subjects_on_main(world), out
    assert "#41 could not be brought current" in out and "position kept" in out, out


# --- a draft skips the suite, and a skipped suite is never a pass for a serial landing ---

def approve(w, n):
    return subprocess.run(["sh", str(QUEUE), "approve", str(n)], capture_output=True, text=True, env=w["env"], timeout=180)


def _skip_suite_on_42(w, title):
    head = git(w["bare"], "rev-parse", "refs/pull/42/head")
    w["env"]["STUB_SKIPPED_SUITE_SHAS"] = head
    w["env"]["PR_QUEUE_WAIT_POLLS"] = "3"
    pr = w["d"] / "pr.42.json"
    d = json.loads(pr.read_text()); d["title"] = title; pr.write_text(json.dumps(d))
    return head


def test_approve_refuses_a_draft_whose_suite_was_skipped(world):
    """A draft's `Test / pytest` context is `skipped`, and `pr checks` still says OK. Landing it
    alone would merge a head no test ran on. Reopening would not help while it is still a draft."""
    _skip_suite_on_42(world, "WIP: feat2")
    r = approve(world, 42)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "is a DRAFT" in r.stdout, r.stdout
    assert "feat2" not in subjects_on_main(world), "a draft landed with its suite skipped"
    assert not (world["d"] / "reopened.log").exists(), "a draft must not be reopened: its run would skip again"


def _set_base(w, n, ref):
    pr = w["d"] / ("pr.%d.json" % n)
    d = json.loads(pr.read_text()); d["base"] = {"ref": ref}; pr.write_text(json.dumps(d))


def test_approve_REFUSES_a_pr_into_another_branch_and_never_updates_it(world):
    """Measured on a vendored fork: a PR targeted `hub` while the fork's `main` tracks upstream; the queue said
    "behind main" and was about to update the PR FROM main, carrying declined drift into `hub`.
    A PR whose base is not the landing branch is refused before any update or merge."""
    _set_base(world, 42, "hub")
    r = approve(world, 42)
    assert r.returncode == 7, r.stdout + r.stderr
    assert "targets 'hub'" in r.stdout and "lands against 'main' only" in r.stdout, r.stdout
    assert not (world["d"] / "updated.log").exists(), "it must not be brought current from main"
    assert "feat2" not in subjects_on_main(world), "a PR into another branch landed on main"


def test_a_pr_whose_base_IS_the_landing_branch_still_lands__control(world):
    """The field must not break the ordinary path: base == main lands exactly as before."""
    _set_base(world, 42, "main")
    r = approve(world, 42)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "feat2" in subjects_on_main(world), r.stdout


def test_approve_reopens_an_undrafted_pr_whose_suite_skipped_and_lands_after_the_run(world):
    """A member that left its batch has had its prefix restored, but its head still carries the
    skipped suite. It gets its own run -- by reopening, which keeps full PR context -- before landing."""
    _skip_suite_on_42(world, "feat2")
    r = approve(world, 42)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "reopening it to run the suite" in r.stdout, r.stdout
    log = (world["d"] / "stub.log").read_text()
    reopen, merge = log.find('"state":"open"'), log.find("pr merge o/r 42")
    assert 0 <= reopen < merge, "the reopen must come BEFORE the merge\n" + log
    assert "feat2" in subjects_on_main(world)


def test_a_reopen_that_never_runs_the_suite_stops_and_lands_nothing(world):
    _skip_suite_on_42(world, "feat2")
    world["env"]["STUB_REOPEN_NO_RUN"] = "1"
    r = approve(world, 42)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "did not run and pass after the reopen" in r.stdout, r.stdout
    assert "feat2" not in subjects_on_main(world), "landed although the suite never ran"


def test_a_skipped_job_that_is_not_the_suite_is_still_green__control(world):
    """THE CONTROL, and it PASSES ON BASE deliberately. Fallow skips jobs on most PRs; a guard that
    refused every skipped context would satisfy the three tests above and stop the queue."""
    world["env"]["STUB_SKIPPED_OTHER_SHAS"] = git(world["bare"], "rev-parse", "refs/pull/42/head")
    r = approve(world, 42)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "feat2" in subjects_on_main(world)
    assert not (world["d"] / "reopened.log").exists()


def test_a_drafted_member_whose_suite_skipped_is_admitted_to_a_batch__control(world):
    """THE CONTROL for the batch path, which must NOT inherit the serial guard: the integration PR
    runs the suite over the same bytes, so a skipped suite on a member's own head is expected."""
    world["env"]["STUB_SKIPPED_SUITE_SHAS"] = git(world["bare"], "rev-parse", "refs/pull/42/head")
    r = drain(world, "--batch")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#42 ADMITTED" in r.stdout, r.stdout
    assert subjects_on_main(world)[:3] == ["feat3", "feat2", "feat1"]
    assert not (world["d"] / "reopened.log").exists()


# ---- an un-drafted PR gets a gate-watch, and a vanished API stops the wait ----------

def test_an_undrafted_pr_is_gate_watched_for_the_session(world, tmp_path):
    """`pr create` registers a gate-watch for NON-draft heads only, so a PR drafted at
    creation had none -- and when the queue un-drafted it and its suite went red, nothing told the
    session. The un-draft is where the PR first runs a suite, so it is where the watch is owed."""
    (world["hub"] / "scripts" / "gate-watch.py").write_text((REPO_ROOT / "scripts" / "gate-watch.py").read_text())
    (world["hub"] / "scripts" / "agent_comms.py").write_text((REPO_ROOT / "scripts" / "agent_comms.py").read_text())
    (world["hub"] / "scripts" / "ft_config.py").write_text((REPO_ROOT / "scripts" / "ft_config.py").read_text())
    # gate-watch refuses anything but a full sha, and the queue re-reads the head after green, so
    # the stub's head must be the REAL tip of refs/pull/41/head -- not the fixture's "HEADSHA".
    sha = git(world["bare"], "rev-parse", "refs/pull/41/head").strip()
    f = world["d"] / "pr.41.json"; d = json.loads(f.read_text()); d["head"]["sha"] = sha; f.write_text(json.dumps(d))
    queue(world, 41)
    as_draft(world, 41)
    registry = tmp_path / "gate-watch.jsonl"
    world["env"] = dict(world["env"], FORGE_TOOLS_WAKE_PID=str(os.getpid()), GATE_WATCH_REGISTRY=str(registry))
    r = drain(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#41 gate-watched for pid %d" % os.getpid() in r.stdout, r.stdout + r.stderr
    events = [json.loads(l) for l in registry.read_text().splitlines()]
    assert [(e["event"], e["sha"], e["pid"]) for e in events] == [("register", sha, os.getpid())], events


def test_a_drain_with_no_session_to_wake_registers_nothing__control(world, tmp_path):
    """PASSES ON BASE: a cron or bare-shell drain has no FORGE_TOOLS_WAKE_PID, and the un-draft must not die
    on gate-watch's own refusal to register for nobody."""
    (world["hub"] / "scripts" / "gate-watch.py").write_text((REPO_ROOT / "scripts" / "gate-watch.py").read_text())
    (world["hub"] / "scripts" / "agent_comms.py").write_text((REPO_ROOT / "scripts" / "agent_comms.py").read_text())
    (world["hub"] / "scripts" / "ft_config.py").write_text((REPO_ROOT / "scripts" / "ft_config.py").read_text())
    queue(world, 41)
    as_draft(world, 41)
    registry = tmp_path / "gate-watch.jsonl"
    env = dict(world["env"], GATE_WATCH_REGISTRY=str(registry)); env.pop("FORGE_TOOLS_WAKE_PID", None); env.pop("PR_QUEUE_DETACHED", None)
    world["env"] = env
    r = drain(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "41 title feat1" in titles(world)
    assert not registry.exists(), registry.read_text()


def _no_ci_world(world, tmp_path, ci_rc):
    """A forge that registers NO checks for this head, and answers `repo ci` with `ci_rc`."""
    real = world["env"]["PR_QUEUE_API"]
    wrapper = tmp_path / "no-ci-api.sh"
    wrapper.write_text("#!/bin/sh\n"
                       "if [ \"$1 $2\" = 'repo ci' ]; then echo probe; exit %d; fi\n"
                       "if [ \"$1 $2\" = 'pr checks' ]; then\n"
                       "  echo \"total_count=0 state='' skipped=0\"; echo 'REFUSING: NO check-runs registered'; exit 2\n"
                       "fi\n"
                       "exec sh '%s' \"$@\"\n" % (ci_rc, real))
    wrapper.chmod(0o755)
    queue(world, 41)
    world["env"] = dict(world["env"], PR_QUEUE_API=str(wrapper), PR_QUEUE_WAIT_POLLS="3")
    r = drain(world)
    return r.stdout + r.stderr


def test_a_repo_with_no_ci_is_refused_at_once_not_waited_on(world, tmp_path):
    """On a repo provisioned `--no-ci`, approve polled WAIT_POLLS (30 min) for checks no
    workflow would ever register, then gave up. `repo ci` answering `none` refuses it by name now."""
    out = _no_ci_world(world, tmp_path, 4)
    assert "runs NO CI" in out and "so no check will ever register" in out, out
    assert "NEVER SETTLED" not in out, "it waited out the polls instead of refusing:\n" + out
    assert subjects_on_main(world) == ["base"], "merged a PR no gate ever measured"


def test_an_unreadable_ci_listing_keeps_the_bounded_wait__control(world, tmp_path):
    """Only a MEASURED `none` refuses. An unread listing (3) is not evidence of no CI."""
    out = _no_ci_world(world, tmp_path, 3)
    assert "runs NO CI" not in out, out
    assert subjects_on_main(world) == ["base"], out


def test_an_api_that_vanishes_mid_wait_stops_instead_of_polling_out(world, tmp_path):
    """The compounding fault: a detached drain's worktree was deleted under it, so every
    `pr checks` returned 127 with no state= in the output, which `wait_for_green` read as pending
    and slept through WAIT_POLLS. 127 is not a check state; it is the instrument gone."""
    real = world["env"]["PR_QUEUE_API"]
    wrapper = tmp_path / "vanishing-api.sh"; n = tmp_path / "checks.n"
    wrapper.write_text("#!/bin/sh\n"
                       "if [ \"$1 $2\" = 'pr checks' ]; then\n"
                       "  c=$(cat '%s' 2>/dev/null || echo 0); c=$((c+1)); echo $c > '%s'\n"
                       "  [ $c -ge 1 ] && { rm -f \"$0\"; exit 127; }\n"
                       "fi\n"
                       "exec sh '%s' \"$@\"\n" % (n, n, real))
    wrapper.chmod(0o755)
    queue(world, 41)
    world["env"] = dict(world["env"], PR_QUEUE_API=str(wrapper), PR_QUEUE_WAIT_POLLS="3")
    r = drain(world)
    out = r.stdout + r.stderr
    assert "cannot be run (rc 127)" in out, out
    assert "NEVER SETTLED" not in out, out
    assert subjects_on_main(world) == ["base"], "merged on a wait that measured nothing"


# ------------------------------------------------ the watch a landed batch must not own
#
# `_watch_undrafted` registers a gate-watch on the member's OWN head whenever the queue un-drafts
# it. That is owed where the PR is LEFT OPEN and will run its own suite. On the LANDED
# path the un-draft is post-merge bookkeeping: the PR is closed, its head never runs a suite, and
# the watch polls a permanently `skipped` pytest until it gives up at 7200s -- then reports "NOT a
# verdict ... do not read this as green" about a change that is already on main. Measured on two
# landed PRs, 2026-09-20; `1851f123ab` is not even an ancestor of the main it landed on.


def _with_session(w):
    """A watch is only attempted when the run has a session to wake."""
    w["env"] = dict(w["env"], FORGE_TOOLS_WAKE_PID="424242")
    return w


def test_a_landed_batch_registers_no_watch_on_a_member_head(world):
    w = _with_session(world)
    queue(w, 41, 42)
    as_draft(w, 41, 42)
    r = drain(w)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "LANDED -- main is now" in r.stdout, r.stdout
    assert "un-drafted" in r.stdout, "the members must still be un-drafted: " + r.stdout
    # "NOT gate-watched" also contains the substring, so this catches a failed register too.
    assert "gate-watched" not in r.stdout, (
        "a merged PR's head never runs a suite; a watch on it can only expire at 7200s:\n" + r.stdout)


def test_a_red_batch_still_watches_the_members_it_leaves_open__control(world):
    """The control that makes the assertion above mean something: on the RED path the members are
    left OPEN and each does run its own suite, so the watch is owed and must still be registered."""
    w = _with_session(world)
    queue(w, 41, 42)
    as_draft(w, 41, 42)
    w["env"]["STUB_RED_SUBJECTS"] = "feat1 feat2"   # green alone, red together
    r = drain(w)
    assert "batch: RED" in r.stdout, r.stdout
    assert "gate-watched" in r.stdout, (
        "a member left open by a red batch runs its own suite and is owed a watch:\n" + r.stdout)


# ---- a drain leaves what it did not land in the draft state it found it in ---------------------
#
# Measured 2026-09-21: four PRs, every one OPENED as a draft, all un-drafted by drains that then did
# not land them -- a red suite, a head that moved, a drain that was killed. `undraft_member` had
# callers on every such path and `draft_member` had none, so un-drafting was one-way: each PR ran
# the full suite on every push from then on, and each landing invalidated the other three.


def _last_title(w, n):
    mine = [t for t in titles(w) if t.startswith("%d title " % n)]
    return mine[-1] if mine else None


def test_a_draft_the_drain_undrafted_and_did_not_land_is_a_draft_again(world):
    """Green alone, red together: the batch goes red, both members are un-drafted so the halves can
    be landed on their own runs, #41 lands and #42 is red on the main that produced. #42 was a draft
    when this drain found it, this drain did not land it, so it is a draft when this drain ends."""
    queue(world, 41, 42)
    as_draft(world, 41, 42)
    world["env"]["STUB_RED_SUBJECTS"] = "feat1 feat2"
    r = drain(world)
    assert "batch: RED" in r.stdout, r.stdout
    assert "feat1" in subjects_on_main(world) and "feat2" not in subjects_on_main(world), subjects_on_main(world)
    assert "42 title feat2" in titles(world), "the scenario needs #42 un-drafted at some point: %s" % titles(world)
    assert _last_title(world, 42) == "42 title WIP: feat2", (
        "#42 was found a draft, un-drafted, not landed, and LEFT un-drafted: %s" % titles(world))


def test_a_landed_pr_is_never_redrafted__control(world):
    """The other direction, and the one that would be worse: a measured merged PR was still
    carrying `WIP: `. Same run as above -- #41 landed, so its last title has no prefix."""
    queue(world, 41, 42)
    as_draft(world, 41, 42)
    world["env"]["STUB_RED_SUBJECTS"] = "feat1 feat2"
    drain(world)
    assert _last_title(world, 41) == "41 title feat1", titles(world)


def test_a_pr_found_ready_is_not_drafted_by_a_red_batch__control(world):
    """`--batch` drafts READY members while they are batched and un-drafts them on red. Those were
    never drafts, so restoring "the state it was found in" leaves them ready."""
    world["env"]["STUB_RED_SUBJECTS"] = "feat1 feat2"
    queue(world, 41, 42)
    r = drain(world, "--batch")
    assert "batch: RED" in r.stdout, r.stdout
    assert _last_title(world, 42) == "42 title feat2", titles(world)


def test_a_detached_drain_restores_too(world, tmp_path):
    """EVERY REAL DRAIN IS DETACHED, and a detached run arms its own EXIT trap for the exit nudge.
    `sh` keeps one handler per signal, so that trap REPLACES the restore -- which would then pass
    every test above and never run in production. Same scenario, under the detached marker."""
    queue(world, 41, 42)
    as_draft(world, 41, 42)
    world["env"]["STUB_RED_SUBJECTS"] = "feat1 feat2"
    world["env"]["PR_QUEUE_DETACHED"] = "999999"            # no such session: the nudge goes nowhere
    world["env"]["PR_QUEUE_DETACHED_LOG"] = str(tmp_path / "detached.log")
    r = drain(world)
    assert "batch: RED" in r.stdout, r.stdout
    assert _last_title(world, 42) == "42 title WIP: feat2", titles(world)


def test_a_refused_BATCH_landing_prints_the_clients_own_words(world):
    """The refused-merge-prints-its-words rule at the site first missed -- the one actually measured,
    on a batch PR. `merge_queued` at least CAPTURED the client's stderr before dropping it; the
    batch landing piped the client straight into `sed`, so no variable ever held the words.

    A stale client refuses on stderr with no `http=` line, which is exactly what produced
    `http=unreadable`. The generic line stays -- what this adds is the cause beneath it, because
    "main moved under us" tells you to re-poll and re-polling never refreshes a checkout."""
    r = drain(world, "--batch", env={"STUB_STALE_CLIENT": "1"})
    out = r.stdout + r.stderr
    assert "http=unreadable" in out, out
    assert "this client is STALE" in out, out


# ---------------------------------------------------------------------------------------------
# TICKET NUMBERS RESOLVE AGAINST THE HUB, NOT THE PR'S REPO.
#
# Found by a peer on the first live cross-repo run, draining a PR on a sibling repo:
# `open-task check NOT RUN for #13: ticket #91 unreadable: 'state'`. `REPO` is the
# TARGET under `--repo`, and both wayfinder scans read `/repos/$REPO/issues/<n>`. Measured
# 2026-09-22: the target's `issues?state=all` returns five items and every one is a pull_request, so
# that repo has no tickets at all, its `issues/91` answers "The target couldn't be found"
# (no `state` key), and ticket 91 on the ticket repo exists and is CLOSED -- a correct lookup prints nothing.

OPEN_TASK = json.dumps({"number": 37, "state": "open", "title": "the hub ticket",
                        "labels": [{"name": "wayfinder"}, {"name": "wayfinder:bug"},
                                   {"name": "wayfinder:map-68"}]})


def test_approve_with_repo_reads_TICKETS_from_the_hub_not_the_target(world, tmp_path):
    """THE FIXTURE MAKES THE WRONG REPO ANSWER THE REAL ERROR. `o/r` (the hub, which `--repo` does
    not change) serves the ticket; `o/target` answers the forge's not-found payload. A build that
    reads tickets out of `REPO` therefore reproduces the peer's NOT RUN line rather than failing
    some invented way."""
    target = tmp_path / "target.git"
    _bare_copy(world["bare"], target)
    (world["d"] / "pr.41.json").write_text(json.dumps(
        {"number": 41, "state": "open", "merged": False, "title": "feat1",
         "body": "Part of #68. Implements #37.",
         "head": {"ref": "feat1", "sha": "HEADSHA"}, "labels": []}))
    env = dict(world["env"], STUB_BARE=str(target), PR_QUEUE_REMOTE=str(target),
               STUB_TICKET_REPO="o/r", STUB_TICKET=OPEN_TASK)
    r = subprocess.run(["sh", str(QUEUE), "--repo", "o/target", "approve", "41"],
                       capture_output=True, text=True, env=env, timeout=180)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "#37  the hub ticket" in out, out
    assert "issue resolve o/r 37 68" in out, (
        "the printed command must name the repo the ticket is actually on:\n" + out)
    assert "unreadable" not in out, (
        "it looked the ticket up in the target repo, which has no tickets:\n" + out)


def test_a_bare_run_still_resolves_tickets_where_it_resolves_everything_else__control(world):
    """The no-flag seam: `PR_QUEUE_REPO` alone means "this IS the hub for this run", which every
    stub in this suite relies on. `__control` -- it PASSES ON BASE, because base reads tickets out
    of REPO and REPO is the same repo here. Without it, a TICKET_REPO hardcoded to one real
    deployment's repo would pass the test above and break every other test in the file.
    """
    (world["d"] / "pr.41.json").write_text(json.dumps(
        {"number": 41, "state": "open", "merged": False, "title": "feat1",
         "body": "Implements #37.", "head": {"ref": "feat1", "sha": "HEADSHA"}, "labels": []}))
    r = subprocess.run(["sh", str(QUEUE), "approve", "41"], capture_output=True, text=True,
                       env=dict(world["env"], STUB_TICKET_REPO="o/r", STUB_TICKET=OPEN_TASK),
                       timeout=180)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "issue resolve o/r 37 68" in out, out


def test_the_member_whose_head_moved_is_TOLD_its_force_push_rebuilt_the_batch(world):
    """The rebuild above is correct and cheap; what was missing is that it is reported only
    to the DRAIN's log, and the drain usually belongs to a third party. Measured 2026-09-22: a
    force-push onto a PR while it was a member of a batch invalidated that batch, and the
    member's owner learned it from someone else's log.

    ONLY THE MOVED MEMBER IS NAMED. A message to the two members that did nothing would be the
    over-report that teaches its readers to skim -- the same reasoning behind every quiet default here.
    """
    world["env"]["STUB_PENDING_SUBJECTS"] = "feat3"
    assert drain(world, "--batch").returncode == 8
    hub = world["hub"]
    git(hub, "checkout", "-q", "feat2"); (hub / "feat2.txt").write_text("feat2 v2\n")
    git(hub, "add", "-A"); git(hub, "commit", "-qm", "feat2 v2"); git(hub, "push", "-q", "hub", "feat2")
    git(world["bare"], "update-ref", "refs/pull/42/head", "refs/heads/feat2"); git(hub, "checkout", "-q", "main")
    del world["env"]["STUB_PENDING_SUBJECTS"]
    out = drain(world, "--batch").stdout

    assert "#42's head moved while it was a member of batch" in out, out
    assert "CLOSED AND REBUILT" in out, out
    assert "looks like an idle draft" in out, "the message must say WHY it happened:\n" + out
    # No owner resolves under the fixture, so the notifier takes its "nobody was messaged" arm --
    # which still proves the call was made FOR #42 and for nothing else.
    assert "feat1" not in out.split("head moved while it was a member")[1][:400], (
        "an unmoved member was told about a rebuild it did not cause:\n" + out)


def test_a_standing_batch_with_no_recorded_members_at_tells_nobody__control(world):
    """A body written before `members-at` existed has none, and then EVERY member reads as moved.
    Messaging all of them about something they did not do is worse than saying nothing.

    `__control` -- it PASSES ON BASE, where no notification exists at all. It pins the guard that
    makes the feature safe rather than the feature."""
    world["env"]["STUB_PENDING_SUBJECTS"] = "feat3"
    assert drain(world, "--batch").returncode == 8
    mutated = 0
    for f in (world["d"]).glob("pr.*.json"):
        t = f.read_text()
        if "members-at:" in t:
            f.write_text(t.replace("members-at:", "members-were:")); mutated += 1
    # THE CONTROL MUST PROVE ITS OWN PREMISE. Without this the test passes when the fixture stores
    # no body at all -- a control that cannot fail, which is the class this repo keeps paying for.
    assert mutated == 1, "expected exactly one stored integration body to strip, stripped %d" % mutated
    del world["env"]["STUB_PENDING_SUBJECTS"]
    out = drain(world, "--batch").stdout
    assert "head moved while it was a member" not in out, (
        "with no recorded members-at it must tell nobody:\n" + out)


# --- one queue operator. Two live drains were two fronts: two batches claimed the same
# drafts, then each split its red batch and un-drafted a lone member -- two non-drafts at once.

def _claim_as(w, tmp, pid):
    """Write the operator claim `_claim_queue` reads, for this world's queue, naming `pid`."""
    import hashlib
    key = hashlib.sha256(("%s %s" % (w["bare"], "o/r")).encode()).hexdigest()[:16]
    (w["hub"] / ".git" / ("pr-queue-operator-" + key)).write_text("%d\n" % pid)


def test_a_second_drain_DEFERS_to_a_live_operator_and_touches_nothing(world, tmp_path):
    queue(world, 41, 42)
    as_draft(world, 41, 42)
    # argv must KEEP naming pr-queue.sh for its lifetime: `exec` in a `sh -c` replaces it, which is
    # how the first version of this test measured a drain that did not look like one.
    live = subprocess.Popen(["python3", "-c", "import signal; signal.pause()", "pr-queue.sh"])
    try:
        _claim_as(world, tmp_path, live.pid)
        r = drain(world)
    finally:
        live.kill(); live.wait()
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "DEFERRING" in out and str(live.pid) in out, out
    assert created(world) == [] and titles(world) == [], "a deferring drain changed the queue:\n" + out
    assert not {"feat1", "feat2"} & set(subjects_on_main(world))


def test_a_claim_whose_drain_is_GONE_is_free__control(world, tmp_path):
    """The control: a dead operator's claim must not defer anyone, or one crash stalls the queue."""
    queue(world, 41, 42)
    as_draft(world, 41, 42)
    dead = subprocess.Popen(["true"]); dead.wait()
    _claim_as(world, tmp_path, dead.pid)
    r = drain(world)
    assert "DEFERRING" not in r.stdout + r.stderr, r.stdout + r.stderr
    assert {"feat1", "feat2"} <= set(subjects_on_main(world)), r.stdout + r.stderr


def test_a_drain_never_has_two_unlanded_PRs_undrafted_at_once(world):
    """The half that one-operator-per-queue does not reach -- replayed from 2026-09-22.

    ONE drain un-drafted a PR (call it A) as the lone draft at the front; its suite had been SKIPPED as a draft,
    so it was red only once it ran. The drain left it open, carried on (the drain's repeat pass), found another (B)
    newly at the front and un-drafted that too: two non-drafts from one run, because re-drafting
    happened only at EXIT. Here: 42 is that red lone draft, and 43 joins the queue at 42's reopen.
    Replayed from the title log IN ORDER -- the end state was always right; the defect is the window.

    Since 2026-09-24 the stricter rule holds: B is not un-drafted AT ALL while A is open and
    red, since A's fix re-gates it. 43 waits behind 42, still a draft.
    """
    as_draft(world, 42, 43)
    # 41 is READY and green, so pass 1 lands something and the drain repeats -- the live run
    # had landed before it reached A. 42 plays A (the red lone draft), 43 plays B.
    queue(world, 41, 42)
    heads = " ".join(git(world["bare"], "rev-parse", "refs/pull/%d/head" % n) for n in (42, 43))
    world["env"].update(STUB_RED_SUBJECTS="feat2", STUB_RED_AFTER_REOPEN="1",
                        STUB_SKIPPED_SUITE_SHAS=heads, STUB_ORDER_AFTER_REOPEN="42 43")
    r = drain(world)
    out = r.stdout + r.stderr
    log = titles(world)
    assert any(l == "42 title feat2" for l in log), "42 was never un-drafted, so this measured nothing:\n%s\n%s" % ("\n".join(log), out)
    assert "#43 SKIP -- a draft behind #42" in out, "43 never reached the front behind red 42, so this measured nothing:\n" + out
    assert "43 title feat3" not in log, "43 was un-drafted while red 42 was open:\n%s\n%s" % ("\n".join(log), out)
    undrafted = set()
    for line in log:
        n, _, title = line.split(" ", 2)
        (undrafted.discard if title.startswith("WIP: ") else undrafted.add)(n)
        assert len(undrafted - {"43"} if "43 title feat3" not in log[:log.index(line) + 1] else undrafted) < 2, \
            "two PRs un-drafted at once, #41 red and unlanded: %s\n%s\n%s" % (sorted(undrafted), "\n".join(log), out)



def test_a_head_that_moves_while_the_run_gates_it_is_regated_and_lands_in_the_same_run(world):
    """A push onto the PR at the front while it gates is not a failure: it is still the
    front, and its new head is what should land. Measured 2026-09-22 on a live PR: the merge-time
    "head MOVED" became a bare rc 1 -> 8 and STOPPED the whole drain as "main moved under us", and the
    PR was re-drafted with no drain left to take it."""
    hub = world["hub"]
    git(hub, "checkout", "-q", "-b", "feat1-v2", "feat1")
    (hub / "feat1.txt").write_text("feat1 v2\n"); git(hub, "commit", "-qam", "feat1 v2")
    git(hub, "push", "-q", "hub", "feat1-v2"); git(hub, "checkout", "-q", "main")
    queue(world, 41)
    world["env"].update(STUB_MOVE_PR="41", STUB_MOVE_TO="feat1-v2")
    r = drain(world)
    out = r.stdout + r.stderr
    assert (world["d"] / "moved").exists(), "the head never moved, so this measured nothing:\n" + out
    assert "STOPPING" not in out, "a moved head stopped the drain:\n" + out
    assert "feat1 v2" in subjects_on_main(world), "the new head did not land in this run:\n" + out


def test_a_member_pushed_while_its_batch_gates_is_rebuilt_not_landed_at_its_old_head(world):
    """Measured 2026-09-24: a PR was pushed while its batch -- assembled on its previous head --
    gated. Green, the batch would have landed that old head and marked the PR merged: its new commit
    silently off main, the PR reading as landed. The live run now re-reads the members' heads before
    the landing POST, and a moved one rebuilds the batch on the new head instead."""
    hub = world["hub"]
    git(hub, "checkout", "-q", "-b", "feat1-v2", "feat1")
    (hub / "feat1.txt").write_text("feat1 v2\n"); git(hub, "commit", "-qam", "feat1 v2")
    git(hub, "push", "-q", "hub", "feat1-v2"); git(hub, "checkout", "-q", "main")
    queue(world, 41, 42)
    as_draft(world, 41, 42)
    world["env"].update(STUB_MOVE_PR="41", STUB_MOVE_TO="feat1-v2", STUB_MOVE_AFTER_CREATE="1")
    r = drain(world)
    out = r.stdout + r.stderr
    assert (world["d"] / "moved").exists(), "the head never moved, so this measured nothing:\n" + out
    assert "head MOVED while the gate ran" in out, out
    assert len(created(world)) == 2, "the stale batch was not rebuilt: %s\n%s" % (created(world), out)
    assert "feat1 v2" in subjects_on_main(world), "the member's new head did not land:\n" + out


def test_two_drains_started_in_the_same_instant_open_ONE_batch(world):
    """Measured 2026-09-22 20:33:40: two drains started within one second, each read "no
    live drain" and each opened an identical batch. The guard was a read-then-act;
    the claim is a check-and-write under a lock, so one of two simultaneous drains must defer."""
    queue(world, 41, 42)
    as_draft(world, 41, 42)
    # Each its OWN scratch tree, as real runs have (`CC-merge-<pid>`): a shared PR_QUEUE_WT makes the
    # second run fail for an unrelated reason, which passed this test on the unfixed script.
    procs = [subprocess.Popen(["sh", str(QUEUE), "drain"], env={**world["env"], "PR_QUEUE_WT": str(world["d"] / ("wt%d" % i))},
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for i in range(2)]
    outs = [p.communicate(timeout=180)[0] for p in procs]
    assert len(created(world)) == 1, "two drains opened %d batches:\n%s" % (len(created(world)), "\n=====\n".join(outs))


def test_a_pr_behind_main_is_updated_BEFORE_its_gate_so_the_stale_head_is_never_gated(world):
    """Measured 2026-09-22 on a live PR: un-drafted 5 commits behind main and gated on that head, with a
    merge-time rebase -- and a second full suite -- still to come. The merge refuses a behind PR, so
    a gate on the stale head measured code that could never land."""
    hub, bare = world["hub"], world["bare"]
    stale = git(bare, "rev-parse", "refs/pull/41/head")
    # main moves on after #41 branched, so #41 is behind it.
    git(hub, "checkout", "-q", "main")
    (hub / "other.txt").write_text("moved\n"); git(hub, "add", "-A"); git(hub, "commit", "-qm", "main moves")
    git(hub, "push", "-q", "hub", "main")
    queue(world, 41)
    r = drain(world)
    out = r.stdout + r.stderr
    gated = [l for l in (world["d"] / "stub.log").read_text().splitlines() if l.startswith("pr checks") or " checks " in l]
    assert gated, "no gate was read at all, so this measured nothing:\n" + out
    assert not any(stale in l for l in gated), "the stale head %s was gated:\n%s\n%s" % (stale, "\n".join(gated), out)
    assert "updating it BEFORE the gate" in out, out
    assert "feat1" in subjects_on_main(world), "the updated PR did not land:\n" + out


def test_a_batch_members_owner_is_gate_watched_on_the_batch_head(world, tmp_path):
    """`pr create` arms the integration PR's watch for the drain's starter only; a batch
    went red and closed with a member's owner never told. Each member's owner -- the agent session
    working in that branch's worktree -- is now registered on the batch head too."""
    hub = world["hub"]
    (hub / "scripts" / "agent_comms.py").write_text((REPO_ROOT / "scripts/agent_comms.py").read_text())
    reg = tmp_path / "registrations.log"
    (hub / "scripts" / "gate-watch.py").write_text(
        "import os, sys\nopen(%r, 'a').write('%%s %%s\\n' %% (os.environ.get('FORGE_TOOLS_WAKE_PID', ''), ' '.join(sys.argv[1:])))\n" % str(reg))
    wt = tmp_path / "wt-feat2"
    git(hub, "worktree", "add", "-q", str(wt), "feat2")
    proc = tmp_path / "proc"
    d = proc / "5150"; d.mkdir(parents=True)
    (d / "comm").write_text("claude\n"); (d / "cwd").symlink_to(wt)
    queue(world, 41, 42)
    as_draft(world, 41, 42)
    r = drain(world, env={"PR_QUEUE_PROC": str(proc), "FORGE_TOOLS_WAKE_PID": ""})
    out = r.stdout + r.stderr
    lines = reg.read_text().splitlines() if reg.exists() else []
    assert any(l.startswith("5150 register o/r ") and "holds your PR #42" in l for l in lines), \
        "the owner of member #42 was not registered on the batch:\n%s\n%s" % ("\n".join(lines), out)
    assert "#41's owner does not resolve" in out, "an unresolved owner must be named, not skipped:\n" + out

# --- approve and merge-requested move main, so they take the operator claim too ------

def _approve(w, *nums, env=None):
    p = subprocess.Popen(["sh", str(QUEUE), "approve", *[str(n) for n in nums]], env={**w["env"], **(env or {})},
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = p.communicate(timeout=180)[0]
    return p.returncode, out, p.pid


def _claim_file(w):
    import hashlib
    key = hashlib.sha256(("%s %s" % (w["bare"], "o/r")).encode()).hexdigest()[:16]
    return w["hub"] / ".git" / ("pr-queue-operator-" + key)


def test_approve_takes_the_queue_claim_under_its_own_pid(world):
    """A drain started beside an approve must see a live operator -- which it can only if approve
    writes the claim `_claim_queue` reads. The claim outlives the run (a dead pid is a free claim)."""
    rc, out, pid = _approve(world, 41)
    assert rc == 0, out
    assert _claim_file(world).exists(), "approve never wrote the queue's operator claim:\n" + out
    assert _claim_file(world).read_text().split()[0] == str(pid), (_claim_file(world).read_text(), pid)


def test_approve_REFUSES_while_a_live_drain_holds_the_queue(world):
    live = subprocess.Popen(["python3", "-c", "import signal; signal.pause()", "pr-queue.sh"])
    try:
        _claim_file(world).write_text("%d\n" % live.pid)
        rc, out, _ = _approve(world, 41)
    finally:
        live.kill(); live.wait()
    assert rc == 3 and "REFUSING" in out and str(live.pid) in out, out
    assert "feat1" not in subjects_on_main(world), "approve landed beside a live drain:\n" + out


def test_approve_lands_a_sequence_in_order_under_one_claim(world):
    """`approve 1398 1396` as one run: no gap between two approvals for a drain to start in."""
    rc, out, _ = _approve(world, 41, 42)
    assert rc == 0, out
    subj = subjects_on_main(world)
    assert "feat1" in subj and "feat2" in subj, out
    assert subj.index("feat2") < subj.index("feat1"), "landed out of order: %s" % subj   # log is newest first
