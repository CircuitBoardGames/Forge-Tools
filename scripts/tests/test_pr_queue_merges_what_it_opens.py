"""`pr-queue.sh` must merge the PRs it opens, and must STOP rather than report success.

Until 2026-08-21 the queue opened a PR and exited, so its runway could only be cleared from
outside: the next invocation polled 90x20s for "nothing open at all" and died with exit 2 unless
a human merged in between. One PR sat green while a second session burned its whole 30-minute wait.
The fix drains its own runway -- wait for the check-runs, merge, then admit the next branch --
which also means a PR that did NOT come through the queue no longer blocks it.

These run against a STUB `hub-api.sh` and a throwaway repo in tmp_path (PR_QUEUE_* env), so they
need no forge and can never merge anything real.

WHAT WOULD MAKE THIS VACUOUS. Three of the four defects being fixed are INVISIBLE to a happy-path
assertion, because each one's failure mode is "looks like success":

  * the subshell defect -- `echo "$QUEUE" | while` ran the loop in a subshell, so every `exit`
    inside it ended only that subshell and the script fell through to "queue drained" and
    returned 0. A test that only asserts the good path passes identically before and after.
    So `test_a_failing_step_stops_the_queue` asserts on the EXIT CODE and on the ABSENCE of the
    drained line, and its control asserts both appear when the fault is removed.
  * `pr merge` prints the HTTP code and exits 0 REGARDLESS, so a 405 refusal reads as a merge.
    `test_outdated_pr_is_updated_not_reported_merged` gives the stub a 405-then-200 script and
    asserts the update endpoint was actually called in between.
  * a missing `(#N)` is permanent on `main` (force push is refused) and nothing rejects it at
    merge time. `test_merge_subject_carries_the_number` reads the subject the stub received.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PR_QUEUE = REPO / "scripts/pr-queue.sh"

# A stub standing in for hub-api.sh. It logs every invocation to $STUB_LOG so a test can assert
# on what the queue ASKED FOR, not merely on what it printed, and replays canned responses.
STUB = r"""#!/bin/sh
echo "$@" >> "$STUB_LOG"
# The PR's head sha, as hub would report it. Defaults to a sha no repo has, which is what the
# pre-2026-08-21 tests relied on; the self-heal tests override it with a REAL sha so that git
# and the API agree, which is the only way to exercise a check that consults both.
_head="${STUB_HEAD_SHA:-$(printf 'b%.0s' $(seq 1 40))}"
# MODEL THE UPDATE ENDPOINT'S REAL EFFECT: it moves the PR head. A stub that reported the same
# sha after an update would make the repair loop look broken when it is the stub that cannot
# repair -- so once `/update` has been called, both the API AND `refs/pull/N/head` advance.
if [ -n "${STUB_AFTER_SHA:-}" ] && grep -q '/update' "$STUB_LOG"; then _head="$STUB_AFTER_SHA"; fi
# WHEN another run lands this PR. STUB_PR_MERGED=true means before this run asked at
# all; STUB_LANDS_ELSEWHERE_AFTER=N means once the log holds N `pr merge` POSTs -- N=1 is the
# landing happening IN THE GAP the first POST lost, which a static flag cannot express.
_merged="${STUB_PR_MERGED:-false}"
if [ -n "${STUB_LANDS_ELSEWHERE_AFTER:-}" ] && \
   [ "$(grep -c '^pr merge' "$STUB_LOG")" -ge "$STUB_LANDS_ELSEWHERE_AFTER" ]; then _merged=true; fi
case "$1 $2" in
"pr create")  [ -n "${STUB_DRAFT:-}" ] && echo "hub-api: opening as a DRAFT ('WIP: ' title, suite skipped) -- it is not at the front" >&2
              echo "PR #42 open mergeable=True" ;;
"pr checks")  echo "  Test / pytest (scripts/)   success"
              echo "total_count=7 state='success' skipped=3"
              echo "OK: 7 registered, 3 skipped, 4 actually ran"
              exit 0 ;;
"issue label-id")
              # resolve-or-refuse, mirroring the real verb -- an id and 0, or nothing
              # and 2. Never a diagnostic on stdout: the caller reads any non-empty stdout as a
              # resolved id, which is the fail-open this queue exists to refuse.
              if [ -n "${STUB_NO_LABEL:-}" ]; then exit 2; else echo 7; fi ;;
"pr merge")   n=$(grep -c '^pr merge' "$STUB_LOG")
              # a STALE client refuses on stderr and prints NO http= line at all, which
              # is what made the queue log `http=unreadable` and discard the cause.
              if [ -n "${STUB_STALE_CLIENT:-}" ]; then
                  echo "hub-api: REFUSING: this client is STALE -- scripts/hub-api.sh here is deadbeef" >&2
                  exit 1
              fi
              if [ "$n" -le "${STUB_405_TIMES:-0}" ]; then echo "merged: http=405"
              else echo "merged: http=200"; fi ;;
*)            case "$1" in
              # LABELS, WITH STATE. A stub that only logged the POST would let a broken
              # read-back pass: the queue verifies the label STUCK, so the stub has to be able
              # to say it did not. `$STUB_LOG.labels` existing IS the label being attached.
              # merging verbs read the wiki listing first; empty = no freeze stands.
              */wiki/pages) echo '[]' ;;
              */issues/*/labels/*)
                        case "$*" in *"-X DELETE"*) rm -f "$STUB_LOG.labels" ;; esac
                        echo "{}" ;;
              */issues/*/labels)
                        case "$*" in
                        # `${VAR-1}` NOT `${VAR:-1}`: the test sets STUB_LABEL_STICKS="" to mean
                        # "the write is accepted and ignored", and `:-` treats an empty value as
                        # UNSET and substitutes the default -- so the colon form made the
                        # does-not-stick test un-writable and it failed on first run.
                        *"-X POST"*) [ -z "${STUB_LABEL_STICKS-1}" ] || touch "$STUB_LOG.labels"
                                     echo "{}" ;;
                        *) if [ -f "$STUB_LOG.labels" ]; then
                               echo '[{"id": 7, "name": "queue:needs-human-review"}]'
                           else echo '[]'; fi ;;
                        esac ;;
              # the ticket a close keyword names. Unset = unreadable (JSON null).
              */issues/*) printf '%s\n' "${STUB_ISSUE:-null}" ;;
              */labels) if [ -n "${STUB_NO_LABEL:-}" ]; then echo '[]'
                        else echo '[{"id": 7, "name": "queue:needs-human-review"}]'; fi ;;
              # STUB_UPDATE_CODE makes the forge REFUSE the update, body then status
              # line, the shape `-w '\n%{http_code}'` produces. Unset keeps the old answer, whose
              # last line is `{}` -- not a number, so it reads as "no readable code" and falls back.
              */update|*/update\?*)
                        if [ -n "${STUB_UPDATE_CODE:-}" ]; then
                            echo '{"message":"merge conflict detected"}'; echo "$STUB_UPDATE_CODE"
                        else
                            [ -n "${STUB_AFTER_SHA:-}" ] && [ -n "${STUB_BARE:-}" ] && \
                                git -C "$STUB_BARE" update-ref refs/pull/42/head "$STUB_AFTER_SHA"
                            echo "{}"
                        fi ;;
              */pulls/*/commits) printf '%s\n' "${STUB_COMMITS:-[]}" ;;
              # the open-PR listing hand_on_to_drain reads after a merge. Default: none open.
              */pulls\?state=open*) printf '%s\n' "${STUB_OPEN_PULLS:-[]}" ;;
              # STUB_PR_DRAFT: the forge's own `draft` field, which the 405 arm reads.
              # Defaults false, so every test written before it behaves exactly as it did.
              */pulls/*) echo '{"title": "feat: a title", "body": "'"${STUB_PR_BODY:-}"'", "state": "'"${STUB_PR_STATE:-open}"'", "merged": '"$_merged"', "draft": '"${STUB_PR_DRAFT:-false}"', "head": {"sha": "'"$_head"'", "ref": "'"${STUB_HEAD_REF:-feat}"'"}}' ;;
              *) echo "{}" ;;
              esac ;;
esac
"""


@pytest.fixture()
def env(tmp_path: pathlib.Path):
    """A throwaway hub repo, a stub API, and the env that points pr-queue.sh at both."""
    hub = tmp_path / "hub"
    (hub / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(hub)], check=True)
    # AN IDENTITY IN THE REPO, not just `-c` on each commit. The queue REBASES, and a rebase that
    # must replay a commit needs a committer -- without this it dies with "unable to auto-detect
    # email address", which the script then reported as `REBASE CONFLICT`. Every test until
    # 2026-08-21 passed without it because none moved `main`, so every rebase was a no-op
    # fast-forward that created no commit and needed no identity. The first test to move `main`
    # was the first to exercise a real rebase at all.
    for kv in (["user.email", "t@t"], ["user.name", "t"]):
        subprocess.run(["git", "config", *kv], cwd=hub, check=True)
    (hub / "f.txt").write_text("base\n")
    for a in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", *a], cwd=hub, check=True)
    # A bare remote named `hub`, and a feature branch on it to be queued.
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "hub", str(bare)], cwd=hub, check=True)
    subprocess.run(["git", "push", "-q", "hub", "main"], cwd=hub, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "feat"], cwd=hub, check=True)
    (hub / "g.txt").write_text("feature\n")
    for a in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "feat"]):
        subprocess.run(["git", *a], cwd=hub, check=True)
    subprocess.run(["git", "push", "-q", "hub", "feat"], cwd=hub, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=hub, check=True)

    api = tmp_path / "stub-api.sh"
    api.write_text(STUB)
    api.chmod(0o755)
    log = tmp_path / "stub.log"
    log.write_text("")
    return {
        "hub": hub,
        "bare": bare,
        "log": log,
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
            "STUB_BARE": str(bare),
        },
    }


def _run(env, queue: str, script: pathlib.Path | None = None, args=(), **extra):
    return subprocess.run(
        ["sh", str(script or PR_QUEUE), *args],
        input=queue, capture_output=True, text=True,
        env={**env["env"], **extra},
    )


_WAYFINDER_OPEN = '{"number": 74, "state": "open", "labels": [{"name": "wayfinder:task"}]}'
_PLAIN_OPEN = '{"number": 74, "state": "open", "labels": []}'
_NEGATED = "Does not close #74."


def _approve(env, **extra):
    _run(env, "feat\tfeat: a title\treview\n")
    env["log"].write_text("")
    r = _run(env, "", args=("approve", "42"), **extra)
    return r, [l for l in env["log"].read_text().splitlines() if l.startswith("pr merge")]


def test_approve_refuses_a_body_that_would_close_an_open_wayfinder_ticket(env):
    """The forge closes a ticket for a close keyword beside its number in the PR body,
    negation included (measured: a PR body's `Does not close` closed its ticket), and a wayfinder ticket closed that way leaves
    the frontier with no resolution. The landing is the last point that can see it."""
    r, merge = _approve(env, STUB_PR_BODY=_NEGATED, STUB_ISSUE=_WAYFINDER_OPEN)
    assert not merge, f"merged a PR whose body closes an open wayfinder ticket:\n{r.stdout}"
    assert "REFUSING" in r.stdout and "#74 (wayfinder:task), named in the PR body" in r.stdout, r.stdout


def test_approve_refuses_a_landed_commit_that_would_close_one(env):
    """The same keyword in a commit message closes at push, so the PR's commits are read too."""
    r, merge = _approve(env, STUB_ISSUE=_WAYFINDER_OPEN,
                        STUB_COMMITS=r'[{"sha": "abc1234567", "commit": {"message": "feat: x\n\nCloses #74"}}]')
    assert not merge, f"merged a PR whose commit closes an open wayfinder ticket:\n{r.stdout}"
    assert "named in the commit abc1234567" in r.stdout, r.stdout


def test_a_close_of_a_ticket_outside_any_map_still_merges__control(env):
    """THE CONTROL. Refusing every close keyword would pass both tests above and block the
    close-trailer form a commit convention may ask for."""
    r, merge = _approve(env, STUB_PR_BODY=_NEGATED, STUB_ISSUE=_PLAIN_OPEN)
    assert merge and r.returncode == 0, r.stdout + r.stderr


def test_a_meant_close_merges_through_the_allow_list__control(env):
    r, merge = _approve(env, STUB_PR_BODY=_NEGATED, STUB_ISSUE=_WAYFINDER_OPEN,
                        PR_QUEUE_ALLOW_CLOSES="74")
    assert merge and r.returncode == 0, r.stdout + r.stderr


def test_an_unreadable_ticket_merges_and_says_the_check_did_not_run(env):
    """Fail-open is only acceptable when it is audible: silence here would read as 'closes
    nothing'."""
    r, merge = _approve(env, STUB_PR_BODY=_NEGATED)
    assert merge, r.stdout + r.stderr
    assert "close-keyword check NOT RUN for #42" in r.stdout, r.stdout


def test_the_queue_merges_the_pr_it_opened(env):
    """The happy path: open, wait for green, merge. Without this the rest prove nothing."""
    r = _run(env, "feat\tfeat: a title\n")
    calls = env["log"].read_text()
    assert "pr create" in calls, calls
    assert "pr merge" in calls, f"queue opened a PR and never merged it:\n{calls}"
    assert r.returncode == 0, r.stdout + r.stderr
    assert "queue drained" in r.stdout


def test_an_empty_queue_does_not_report_a_drain(env):
    """`queue drained -- every PR it opened is merged` was printed for a run that opened
    nothing. The sentence is true in the VACUOUS sense -- the set of PRs it opened is empty, so all
    of them are merged -- and it reads as a report that work completed.

    An empty heredoc is a legitimate no-op, so this is not an error and the exit stays 0. It just
    has to say which of the two happened.
    """
    r = _run(env, "")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "queue drained" not in r.stdout, f"claimed a drain of nothing:\n{r.stdout}"
    assert "nothing was opened and nothing was merged" in r.stdout, r.stdout
    assert "pr create" not in env["log"].read_text(), "an empty queue must touch no forge"


def test_a_non_empty_queue_still_reports_its_drain__control(env):
    """THE CONTROL. Suppressing the line unconditionally would also pass the test above, and would
    delete the report every real drain depends on."""
    r = _run(env, "feat\tfeat: a title\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "queue drained" in r.stdout, r.stdout
    assert "(1 queued)" in r.stdout, "the drain now names how many it drained"


def test_a_terminal_on_stdin_prints_usage_instead_of_blocking(env):
    """The empty-drain report's actual origin: it was reached by someone trying to READ THE USAGE.

    Run bare in a terminal, `QUEUE=$(cat)` BLOCKS, and after Ctrl-D the script printed a confident
    success. So the one invocation a reader naturally tries in order to discover the verbs was the
    one that hung and then claimed a drain.

    NEEDS A REAL PTY. `subprocess` gives the child a pipe, where `[ -t 0 ]` is false and this arm
    never runs -- the harness above cannot reach it, which is why the case survived.
    """
    import os, pty, select, signal, time
    pid, fd = pty.fork()
    if pid == 0:                                    # pragma: no cover - child execs away
        # AS A SESSION WOULD RUN IT. The detach arm keys on FORGE_TOOLS_WAKE_PID, and it ran BEFORE
        # this usage arm: from a session, the bare form did `cat > file` on the terminal and hung --
        # the same hang again, invisible to the forge because the runner has no FORGE_TOOLS_WAKE_PID. Passed on the
        # exec, not written to os.environ: this is the forked child, but the shared-state scanner
        # reads the source, not the process tree.
        child_env = {**os.environ, **env["env"], "FORGE_TOOLS_WAKE_PID": str(os.getppid())}
        os.execvpe("sh", ["sh", str(PR_QUEUE)], child_env)
    # A DEADLINE, because the failure mode here is a HANG, not a wrong string. Without the
    # `[ -t 0 ]` arm the script blocks in `cat` on the terminal for ever; measured while injecting
    # that exact regression -- this test sat for 145s and only "failed" once the process was killed
    # by hand. A test that hangs reads as a CI timeout, which presents as the
    # author's bug rather than as this assertion.
    out, deadline = b"", time.monotonic() + 20
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], 0.5)[0]:
            continue
        try:
            chunk = os.read(fd, 1024)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    else:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        raise AssertionError(
            "bare invocation never terminated in 20s -- it is blocking in `cat` on the terminal, "
            "which is the bare-invocation hang. Output so far:\n" + out.decode(errors="replace"))
    _, status = os.waitpid(pid, 0)
    text = out.decode(errors="replace")
    assert os.waitstatus_to_exitcode(status) == 2, text
    assert "no queue on stdin" in text, text
    assert "queue drained" not in text, text
    # The usage must be usable as typed: the first version rendered <<'"EOF"' through nested
    # shell quoting, which is not a heredoc anyone can paste.
    assert "<<'EOF'" in text, f"the printed usage is not valid shell:\n{text}"


def test_the_queue_passes_the_bare_title_and_never_hand_writes_the_number(env):
    """The client lands by fast-forward and writes no subject, and under the
    opt-in merge style it appends `(#N)` itself from argv. A hand-written number here was only
    ever a second copy of that rule, and under fast-forward it would be a lie in the log."""
    _run(env, "feat\tfeat: a title\n")
    merge = [l for l in env["log"].read_text().splitlines() if l.startswith("pr merge")]
    assert merge, "no merge call at all"
    assert "feat: a title" in merge[0] and "(#42)" not in merge[0], f"the queue still hand-writes the number: {merge[0]!r}"


def test_outdated_pr_is_updated_not_reported_merged(env):
    """405 is `block_on_outdated_branch` refusing. `pr merge` exits 0 on it regardless."""
    r = _run(env, "feat\tfeat: a title\n", STUB_405_TIMES="1")
    calls = env["log"].read_text()
    assert "/update" in calls, f"a 405 was treated as a merge -- no update attempted:\n{calls}"
    assert calls.count("pr merge") == 2, f"expected a retry after the update:\n{calls}"
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_405_on_an_ALREADY_MERGED_pr_is_not_repaired(env):
    """A 405 says "not allowed to merge" and NOT why. Two readings land here and they want
    opposite responses: `block_on_outdated_branch` firing (repair) and the PR already being
    merged (nothing to repair -- the branch is in the base already).

    MEASURED 2026-09-20. Two drains reached merge for the same green PR five seconds
    apart. The loser read its 405 as "main moved in the gap" and ran three `update_and_rewait`
    rounds against a PR already on main, stopping rc=8 about two minutes later, with its own log
    disproving the diagnosis every time:

        #49 head moved by THIS update step (405 repair): 38a8e2839f -> 38a8e2839f

    the same sha on both sides of the arrow, reported as a move.

    Its paired control is `test_outdated_pr_is_updated_not_reported_merged` directly above, which
    gives the identical 405 with `merged` absent and asserts the update DOES happen. Neither test
    means anything alone: together they say the update is driven by the answer to a question,
    not by the status code."""
    r = _run(env, "feat\tfeat: a title\n", STUB_405_TIMES="1", STUB_LANDS_ELSEWHERE_AFTER="1")
    calls = env["log"].read_text()
    assert "/update" not in calls, (
        "an ALREADY-MERGED pr was 'repaired' -- the 405 was read as an outdated base:\n" + calls)
    assert calls.count("pr merge") == 1, (
        f"the merge was retried against a merged PR:\n{calls}")
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_already_merged_arm_says_which_reading_of_the_405_it_took(env):
    """The log line is the only thing distinguishing "we merged it" from "it was already merged":
    both return 0 and the drain's tally cannot tell them apart. A silent return would make a lost
    race look identical to a won one in the record afterwards."""
    r = _run(env, "feat\tfeat: a title\n", STUB_405_TIMES="1", STUB_LANDS_ELSEWHERE_AFTER="1")
    out = r.stdout + r.stderr
    assert "ALREADY MERGED" in out, f"the arm took its branch silently:\n{out}"


def test_a_pr_that_landed_before_the_merge_was_attempted_spends_no_post(env):
    """The half the 405 arm cannot reach. A PR that landed while this run waited on its
    gate reads as `current` to the ancestry pre-check -- after a fast-forward main IS its head -- so
    nothing else stops a POST whose only possible answer is a 405. The two tests above now put the
    landing IN the gap (after one POST) for that reason: with it before, this path answers first."""
    r = _run(env, "feat\tfeat: a title\n", STUB_PR_MERGED="true")
    calls = env["log"].read_text()
    assert calls.count("pr merge") == 0, f"POSTed a merge for a PR that had landed:\n{calls}"
    assert "/update" not in calls, calls
    assert r.returncode == 0 and "ALREADY MERGED, and not by this run" in r.stdout, r.stdout + r.stderr
    assert "merged #42" not in r.stdout, f"claimed a merge this run did not make:\n{r.stdout}"


def test_the_update_asks_for_rebase_not_the_default_merge(env):
    """`style` DEFAULTS TO `merge` on this endpoint, so an update with no style injects a
    two-parent merge commit into the branch. `Do=squash` at merge time USED to flatten those
    away, which masked the defect rather than removing it.

    SINCE SQUASH WAS RETIRED, MERGES ARE `Do=merge` AND NOTHING FLATTENS ANYTHING. A stray two-parent commit
    from a bare update now lands on `main` verbatim and permanently, so this parameter is the
    only remaining protection and this assertion is the only automated guard on it. Assert the
    query parameter is actually sent.

    This asserts on the REQUEST, which is all a stub can see. It is therefore a check that the
    caller asked for a rebase, NOT a check that the resulting commit has one parent -- that is
    a forge behaviour and no stub can reach it. Do not let this test's passing be read as
    evidence about parent counts.

    THE OTHER HALF, MEASURED LIVE 2026-08-26 on a real PR and recorded here because this test
    cannot ever produce it:

        before   be7605e779
        POST /pulls/77/update?style=rebase  ->  http=200
        after    e79d19d120
        parent   4fa9b1db2a   PARENT COUNT 1, and that parent IS hub/main

    So the ask does produce the shape intended: a single-parent commit whose parent is `main`,
    no two-parent merge. The bare call this test guards against would have given 2 on the same
    PR. Both halves are needed and neither substitutes for the other -- the assertion below
    catches a caller that stops asking, the measurement above is why asking is worth anything."""
    _run(env, "feat\tfeat: a title\n", STUB_405_TIMES="1")
    calls = env["log"].read_text()
    upd = [l for l in calls.splitlines() if "/update" in l]
    assert upd, f"no update call to inspect:\n{calls}"
    assert all("style=rebase" in l for l in upd), (
        "the update endpoint was called without ?style=rebase, so it defaults to `merge` "
        "and injects a merge commit:\n" + "\n".join(upd)
    )


def test_a_failing_step_stops_the_queue(env, tmp_path):
    """THE SUBSHELL DEFECT. A pipe-fed loop makes every `exit` a subshell exit, so the script
    printed `queue drained` and returned 0 after a rebase conflict, a failed push or a red gate.
    Injected here by making the branch unpushable to a read-only remote."""
    hurt = tmp_path / "hurt.sh"
    # THE ANCHOR IS ASSERTED, NOT ASSUMED. This injection used to key on `log "$ref rebased onto`,
    # which a later change deleted -- the replacement then matched nothing, no fault was injected, and the
    # test failed as `exit 0 != 4`, which reads as the QUEUE regressing rather than the anchor
    # going stale. An anchor invented or inherited is not an anchor the file contains.
    src = PR_QUEUE.read_text()
    anchor = '    _post_rebase=$(cd "$WT"'
    assert anchor in src, (
        f"fault-injection anchor {anchor!r} is no longer in pr-queue.sh -- this test would inject "
        "nothing and report a queue defect instead of a stale anchor")
    hurt.write_text(src.replace(anchor, '    log "INJECTED"; exit 4\n' + anchor, 1))
    (tmp_path / "ft-config.sh").write_text((PR_QUEUE.parent / "ft-config.sh").read_text())   # its sibling
    r = _run(env, "feat\tfeat: a title\n", script=hurt)
    assert r.returncode == 4, f"failure did not propagate; exit={r.returncode}\n{r.stdout}"
    assert "queue drained" not in r.stdout, f"reported success after failing:\n{r.stdout}"

    # CONTROL: the same script without the fault reaches the drained line and exits 0. Without
    # this, a script that failed for an unrelated reason would pass the assertions above.
    ok = _run(env, "feat\tfeat: a title\n")
    assert ok.returncode == 0 and "queue drained" in ok.stdout, ok.stdout + ok.stderr


def test_a_foreign_open_pr_does_not_block_the_queue(env):
    """The hold-up being fixed: the old runway wait counted EVERY open PR regardless of origin,
    so one hand-opened PR stalled all queued work for 30 minutes and then failed."""
    r = _run(env, "feat\tfeat: a title\n")
    assert "runway never cleared" not in r.stdout, r.stdout
    assert r.returncode == 0, r.stdout + r.stderr
    # The queue must not be asking "is anything open?" BEFORE it merges -- that was the runway wait.
    # After a merge it does read the listing, once, to hand on to waiting drafts.
    before_merge = env["log"].read_text().split("pr merge", 1)[0]
    assert "state=open" not in before_merge, "queue still polls for foreign open PRs:\n" + before_merge


# ---------------------------------------------------------------------------------------------
# The `review` hold: the queue opens and proves a PR green, then stops for a human.
#
# WHAT THESE ARE GUARDING. Before the flag, wanting a human's eyes on a PR meant opening it by
# hand -- outside the rebase-before-open that gives a PR exactly one CI run. Measured 2026-08-21:
# two PRs were opened that way and the second paid a 405, a rebase and a full re-run. So the
# thing to test is not merely "it stops", but that it stops having ALREADY done the cheap part.
# ---------------------------------------------------------------------------------------------


def test_a_review_flagged_pr_is_opened_proved_green_and_not_merged(env):
    """The hold is AFTER the work, not instead of it: the PR exists and its checks were read."""
    r = _run(env, "feat\tfeat: a title\treview\n")
    calls = env["log"].read_text()
    assert "pr create" in calls, f"held without ever opening the PR:\n{calls}"
    assert "pr checks" in calls, f"held without proving it green -- a human gets an unchecked PR:\n{calls}"
    assert "pr merge" not in calls, f"REVIEW FLAG IGNORED -- the queue merged it anyway:\n{calls}"
    assert r.returncode == 9, f"a hold must be exit 9, not {r.returncode}:\n{r.stdout}{r.stderr}"
    assert "queue drained" not in r.stdout, "a hold is not a drained queue"
    assert "approve 42" in r.stdout, f"the hold must name the command that clears it:\n{r.stdout}"

    # CONTROL: the SAME line without the third field merges and drains. Without this, a queue
    # broken for any other reason would satisfy every assertion above.
    ok = _run(env, "feat\tfeat: a title\n")
    assert "pr merge" in env["log"].read_text(), "control: unflagged queue did not merge"
    assert ok.returncode == 0 and "queue drained" in ok.stdout, ok.stdout + ok.stderr


def test_a_held_pr_stops_the_queue_rather_than_admitting_the_next(env):
    """The invariant is one PR in flight. A held PR still occupies the runway, so admitting the
    next one would open it against an unmerged base -- the rebase churn the queue exists to avoid."""
    r = _run(env, "feat\tfeat: a title\treview\nfeat\tfeat: second\n")
    calls = env["log"].read_text()
    assert calls.count("pr create") == 1, f"a second PR was admitted behind a held one:\n{calls}"
    assert r.returncode == 9, r.stdout + r.stderr


def test_approve_merges_the_held_pr_with_its_number(env):
    """`approve` reuses merge_queued, so it inherits the bare-title call and the 405 repair.
    A human merging by hand in the web UI gets neither."""
    _run(env, "feat\tfeat: a title\treview\n")
    env["log"].write_text("")                     # only the approve run's calls
    r = _run(env, "", args=("approve", "42"))
    calls = env["log"].read_text()
    assert "pr checks" in calls, f"approve merged without re-reading the checks:\n{calls}"
    merge = [l for l in calls.splitlines() if l.startswith("pr merge")]
    assert merge, f"approve did not merge:\n{calls}"
    assert "feat: a title" in merge[0] and "(#42)" not in merge[0], f"approve hand-writes the number: {merge[0]!r}"
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_merge_names_the_sha_green_was_measured_on(env):
    """The queue's pre-check reads the head, compares it to the green sha, and THEN posts the
    merge -- a read-then-act with a window between the two. `head_commit_id` closes it by making
    the forge do the comparison as part of the write.

    This asserts the queue PASSES the sha it verified. That it is the same sha `pr checks` ran
    against is what makes the argument worth anything: a merge pinned to a head nobody gated
    would be a CAS that succeeds against an unmeasured tree.

    What this cannot reach is whether the forge honours the field -- that is forge behaviour and
    the stub cannot speak for it. See the header in test_hub_api.py.
    """
    _run(env, "feat\tfeat: a title\n")
    calls = env["log"].read_text()
    merge = [l for l in calls.splitlines() if l.startswith("pr merge")]
    assert merge, f"no merge call at all:\n{calls}"
    checked = [l for l in calls.splitlines() if l.startswith("pr checks")]
    assert checked, f"nothing was gated before the merge:\n{calls}"
    green_sha = checked[-1].split()[-1]
    assert merge[0].split()[-1] == green_sha, (
        f"the merge was not pinned to the sha green was measured on.\n"
        f"  gated:  {green_sha}\n  merged: {merge[0]!r}\n"
        f"An unpinned merge can land a head no check-run examined."
    )


def test_approve_refuses_a_pr_that_is_not_open(env):
    """A closed or already-merged PR must not be re-merged, and 'cannot read it' must not read
    as 'fine to merge' -- every failure in that lookup yields empty fields."""
    r = _run(env, "", args=("approve", "42"), STUB_PR_STATE="closed")
    assert "pr merge" not in env["log"].read_text(), "approve merged a closed PR"
    assert r.returncode != 0, r.stdout + r.stderr

    bad = _run(env, "", args=("approve", "not-a-number"))
    assert bad.returncode != 0 and "pr merge" not in env["log"].read_text()


def test_a_mistyped_verb_does_not_report_a_drained_queue(env):
    """OBSERVED on the pre-change script: `approve 42` fell through every exact-match verb to the
    stdin read, found an empty queue, and printed `queue drained -- every PR it opened is merged`
    with exit 0. A typo must not report the success of work it never did."""
    r = _run(env, "", args=("aprove", "42"))
    assert "queue drained" not in r.stdout, f"a typo reported a drained queue:\n{r.stdout}"
    assert r.returncode != 0, f"a typo exited 0:\n{r.stdout}"
    assert "pr create" not in env["log"].read_text()

    # CONTROL: the real verb is NOT caught by this guard.
    ok = _run(env, "", args=("approve", "42"))
    assert ok.returncode == 0, ok.stdout + ok.stderr


# ---------------------------------------------------------------------------------------------
# FAIL CLOSED. Both guards below exist because of a MEASURED merge past a review hold.
# ---------------------------------------------------------------------------------------------


def test_an_unknown_third_field_refuses_instead_of_merging(env):
    """`[ "$flag" = review ]` alone fails OPEN: a typo falls through to the merge and the only
    signal is a PR that quietly landed. The field decides whether a human sees the work."""
    r = _run(env, "feat\tfeat: a title\treveiw\n")
    calls = env["log"].read_text()
    assert "pr merge" not in calls, f"a typo'd flag MERGED the PR:\n{calls}"
    assert "pr create" not in calls, "refused too late -- a PR was already opened"
    assert r.returncode == 2, f"expected refusal exit 2, got {r.returncode}:\n{r.stdout}"

    # CONTROLS, both directions: the real flag holds, and no flag merges. Without these the
    # assertions above are satisfied by a queue that refuses everything.
    held = _run(env, "feat\tfeat: a title\treview\n")
    assert held.returncode == 9, held.stdout + held.stderr
    plain = _run(env, "feat\tfeat: a title\n")
    assert plain.returncode == 0 and "pr merge" in env["log"].read_text()


def test_a_checkout_behind_hub_main_refuses_to_run(env):
    """MEASURED 2026-08-21: $HUB was 4 commits behind, so the script being executed predated the
    `review` flag -- it folded the third field into the title and merged a PR past its hold.
    An old script cannot know a newer one exists; the checkout is the proxy it CAN check."""
    hub = env["hub"]
    # Move hub/main ahead of the local checkout, exactly as a merge by anyone else would.
    subprocess.run(["git", "checkout", "-q", "-b", "ahead"], cwd=hub, check=True)
    (hub / "later.txt").write_text("landed elsewhere\n")
    for a in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "later"]):
        subprocess.run(["git", *a], cwd=hub, check=True)
    subprocess.run(["git", "push", "-q", "hub", "ahead:main"], cwd=hub, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=hub, check=True)

    r = _run(env, "feat\tfeat: a title\n")
    assert r.returncode == 2, f"a stale checkout ran anyway; exit={r.returncode}\n{r.stdout}"
    assert "pr create" not in env["log"].read_text(), "opened a PR from a stale checkout"
    assert "behind hub/main" in r.stdout, r.stdout

    # CONTROL: catching up clears the refusal. Proves the guard reads staleness, not something
    # else about the fixture that would refuse forever.
    subprocess.run(["git", "merge", "-q", "--ff-only", "ahead"], cwd=hub, check=True)
    ok = _run(env, "feat\tfeat: a title\n")
    assert ok.returncode == 0 and "queue drained" in ok.stdout, ok.stdout + ok.stderr


def test_a_rebase_failure_quotes_git_rather_than_naming_a_cause(env, tmp_path):
    """MEASURED 2026-08-21: a rebase that died with `unable to auto-detect email address` was
    reported as `REBASE CONFLICT — needs a human`. A conflict is ONE cause among several, and the
    wrong word cost three wrong diagnoses. The verdict must quote git, not assert a cause."""
    hub = env["hub"]
    # A REAL conflict: main and feat both change the same file differently.
    subprocess.run(["git", "checkout", "-q", "-b", "clash"], cwd=hub, check=True)
    (hub / "g.txt").write_text("main's version\n")
    for a in (["add", "-A"], ["commit", "-qm", "clash"]):
        subprocess.run(["git", *a], cwd=hub, check=True)
    subprocess.run(["git", "push", "-q", "hub", "clash:main"], cwd=hub, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=hub, check=True)
    subprocess.run(["git", "merge", "-q", "--ff-only", "clash"], cwd=hub, check=True)

    r = _run(env, "feat\tfeat: a title\n")
    assert r.returncode == 4, f"a failed rebase must stop the queue; got {r.returncode}\n{r.stdout}"
    assert "REBASE FAILED" in r.stdout, r.stdout
    assert "CONFLICT" in r.stdout.upper(), f"git's own reason was not shown:\n{r.stdout}"
    assert "pr create" not in env["log"].read_text(), "opened a PR after a failed rebase"


def _make_stale(hub):
    """Move hub/main ahead of the local checkout, as any merge by anyone else does."""
    subprocess.run(["git", "checkout", "-q", "-b", "ahead2"], cwd=hub, check=True)
    (hub / "later2.txt").write_text("landed elsewhere\n")
    for a in (["add", "-A"], ["commit", "-qm", "later2"]):
        subprocess.run(["git", *a], cwd=hub, check=True)
    subprocess.run(["git", "push", "-q", "hub", "ahead2:main"], cwd=hub, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=hub, check=True)


def test_the_stale_guard_covers_the_verbs_not_only_the_queue(env, tmp_path):
    """MEASURED 2026-08-21, an hour after the guard shipped: it sat BELOW the verb dispatch, so
    `prune-merged` and `approve` exec'd from a stale tree without ever reaching it. prune-merged
    with PR_QUEUE_HUB unset ran the shared tree's pruner, ten commits behind, and printed the
    overclaim the pruner had since fixed -- the stale-tree failure, recurring on the command that
    reports it."""
    _make_stale(env["hub"])
    # A prune stub, so the verb would otherwise succeed loudly and we know the refusal is the guard.
    prune = env["hub"] / "scripts/prune-landed-branches-forgejo.sh"
    prune.parent.mkdir(parents=True, exist_ok=True)
    prune.write_text("#!/bin/sh\necho PRUNER RAN\n")
    prune.chmod(0o755)

    for argv in (("prune-merged",), ("approve", "42")):
        r = _run(env, "", args=argv)
        assert r.returncode == 2, f"{argv} ran from a stale checkout; exit={r.returncode}\n{r.stdout}"
        assert "behind hub/main" in r.stdout, f"{argv}: {r.stdout}"
        assert "PRUNER RAN" not in r.stdout, "the stale pruner was exec'd anyway"
        assert "pr merge" not in env["log"].read_text(), "approve merged from a stale checkout"

    # CONTROL: catching up lets both verbs through, so the guard reads staleness and not the verb.
    subprocess.run(["git", "merge", "-q", "--ff-only", "ahead2"], cwd=env["hub"], check=True)
    ok = _run(env, "", args=("prune-merged",))
    assert "PRUNER RAN" in ok.stdout, f"guard refused a current checkout:\n{ok.stdout}"


# =========================================================================================
# ONE OPERATOR (the flock), SELF-HEALING FRESHNESS, AND BRANCH RETIREMENT.
#
# Added 2026-08-21 after two collisions in one evening, both the same shape: a merge landed
# under an already-open PR and left it outdated. The queue's own header records the accepted
# cost -- it does not block on foreign PRs -- but nothing stopped two RUNS, and nothing checked
# freshness until a refused merge taught it.
#
# WHAT WOULD MAKE THESE VACUOUS, in the order they would bite:
#   * A freshness check whose sources disagree can never fire. `pr_head` reads git and the
#     is-ancestor test reads git, but the sha it compares against comes from the API -- so the
#     stub had to be taught to move BOTH on update, or the repair loop would look broken.
#   * A deletion guard that never sees a holder passes on every input. Each deletion test has
#     its opposite: one where the remote branch must go, one where it must survive.
# =========================================================================================


def _rev(repo, ref="HEAD"):
    return subprocess.run(["git", "rev-parse", ref], cwd=repo,
                          capture_output=True, text=True).stdout.strip()


def test_a_pr_opened_as_a_draft_is_left_to_drain_not_waited_on_or_merged(env):
    """The queue lock is retired (operator, 2026-09-16): admission no longer asserts it is the front
    with `--no-draft`, so `pr create` may draft the PR. A draft's suite is skipped, so waiting for
    green would wait on a run that never starts -- the run must hand it to `drain` and move on."""
    r = _run(env, "feat\tfeat: a title\n", STUB_DRAFT="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "opened as a DRAFT behind the front" in r.stdout, r.stdout
    calls = env["log"].read_text()
    assert "--no-draft" not in calls, "admission still forces the PR to be the front"
    assert "pr checks" not in calls and "pr merge" not in calls, calls


def test_a_run_that_merges_hands_on_to_drain_when_a_draft_waits_behind_it(env):
    """The run that clears the front lands what waits behind it. Measured 2026-09-16: a PR opened
    as a draft behind another, the run that merged the first exited, and nothing drained it until the
    operator asked. The drain it execs is observed by its own first line."""
    r = _run(env, "feat\tfeat: a title\n",
             STUB_OPEN_PULLS='[{"number": 43, "draft": true, "title": "WIP: behind"}]')
    assert "merged #42" in r.stdout or "pr merge" in env["log"].read_text(), r.stdout
    assert "hand-on: draft(s) waiting behind what just landed: #43 -- draining now" in r.stdout, r.stdout
    assert "drain" in r.stdout.split("draining now", 1)[1], "the hand-on printed but no drain ran"


def test_a_run_that_merges_with_no_draft_waiting_does_not_drain(env):
    """PASSES ON BASE: base never hands on; proven instead by removing the empty-drafts guard, which reds it."""
    r = _run(env, "feat\tfeat: a title\n", STUB_OPEN_PULLS="[]")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "hand-on" not in r.stdout, r.stdout


def test_a_run_whose_only_pr_was_drafted_does_not_claim_a_merge_or_drain(env):
    """8f's side finding: "every PR it opened is merged" was printed over a run that merged nothing."""
    r = _run(env, "feat\tfeat: a title\n", STUB_DRAFT="1",
             STUB_OPEN_PULLS='[{"number": 42, "draft": true, "title": "WIP: feat"}]')
    assert r.returncode == 0, r.stdout + r.stderr
    assert "every PR it opened is merged" not in r.stdout, r.stdout
    assert "0 merged, 1 opened as DRAFTS" in r.stdout, r.stdout
    assert "hand-on" not in r.stdout, "nothing merged, so nothing was cleared to hand on"


def test_approve_lands_only_what_it_was_given_and_names_the_drain_it_did_not_run(env):
    """Operator decision, 2026-09-22: `approve 42` handed on to a drain that landed every
    open draft, another owner's deploy included. It lands 42 alone, then names what still waits and
    the command that would land it -- without running it. Admission keeps its hand-on (undecided)."""
    r, merges = _approve(env, STUB_OPEN_PULLS='[{"number": 43, "draft": true, "title": "WIP: not mine", "head": {"ref": "other"}}]')
    assert "draining now" not in r.stdout, "approve handed on to a drain it was not asked for:\n" + r.stdout
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(merges) == 1, merges
    assert "#43 (other)" in r.stdout and "pr-queue drain" in r.stdout, r.stdout


def test_a_run_from_a_session_detaches_returns_at_once_and_messages_the_session_on_exit(env, tmp_path):
    """a landing run from a session held its turn for the whole gate. It must return at
    once, then land in the background and message the session. The stub peer-send records the frame."""
    import os, time
    got = tmp_path / "frame.txt"
    send = tmp_path / "peer-send.py"
    send.write_text("#!/usr/bin/env python3\nimport sys, pathlib\npathlib.Path(%r).write_text(sys.stdin.read() + ' ARGS ' + ' '.join(sys.argv[1:]))\n" % str(got))
    send.chmod(0o755)  # called by command name, as `session-notify` is
    t0 = time.time()
    r = _run(env, "feat\tfeat: a title\n", FORGE_TOOLS_WAKE_PID=str(os.getpid()),
             PR_QUEUE_LOG_DIR=str(tmp_path / "logs"), PR_QUEUE_PEER_SEND=str(send))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DETACHED" in r.stdout and "pr merge" not in r.stdout, r.stdout
    assert time.time() - t0 < 20, "the session was held for the run"
    deadline = time.time() + 60
    while time.time() < deadline and not got.exists():
        time.sleep(0.2)
    assert got.exists(), "the detached run never messaged the session"
    frame = got.read_text()
    assert "EXITED 0" in frame and f"--to claude-code:{os.getpid()}" in frame, frame
    assert "pr merge owner/repo 42" in env["log"].read_text(), "the detached run did not land the PR"


def test_a_missing_peer_send_command_refuses_to_detach_naming_it(env, tmp_path):
    """The detached run reports its end by `session-notify`, reached by command name. Missing, the
    session would wait for a message that never comes -- so the run refuses to detach, naming the
    command and the repo that provides it, and nothing is landed."""
    import os
    r = _run(env, "feat\tfeat: a title\n", FORGE_TOOLS_WAKE_PID=str(os.getpid()),
             PR_QUEUE_LOG_DIR=str(tmp_path / "logs"), PR_QUEUE_PEER_SEND="no-such-peer-send")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "MISSING COMMAND `no-such-peer-send` (from the Session-Notify repo)" in r.stdout, r.stdout
    assert "DETACHED" not in r.stdout and "pr merge" not in env["log"].read_text()


def test_a_session_can_force_the_foreground(env, tmp_path):
    """PASSES ON BASE: base never detaches; proven instead by removing the PR_QUEUE_FOREGROUND arm, which reds it."""
    import os
    r = _run(env, "feat\tfeat: a title\n", FORGE_TOOLS_WAKE_PID=str(os.getpid()), PR_QUEUE_FOREGROUND="1",
             PR_QUEUE_LOG_DIR=str(tmp_path / "logs"))
    assert r.returncode == 0 and "DETACHED" not in r.stdout, r.stdout
    assert "pr merge owner/repo 42" in env["log"].read_text()


def test_a_dead_runs_scratch_tree_is_reaped_and_a_live_ones_is_not(env, tmp_path):
    """One scratch tree per run replaced the lock's shared one, so nothing recycles a tree an early
    exit kept. The next run reaps it -- but only when its pid is gone."""
    import os
    base = tmp_path / "CC-merge"
    dead, live = f"{base}-999999999", f"{base}-{os.getpid()}"
    for p in (dead, live):
        subprocess.run(["git", "worktree", "add", "-q", "--detach", p], cwd=env["hub"], check=True)
    run_env = {k: v for k, v in env["env"].items() if k != "PR_QUEUE_WT"}
    # A real admission, so the reap runs where it is wired: just before this run adds its own tree.
    r = subprocess.run(["sh", str(PR_QUEUE)], input="feat\tfeat: a title\n", capture_output=True,
                       text=True, env={**run_env, "PR_QUEUE_WT_BASE": str(base)})
    assert f"reaped a dead run's scratch tree: {dead}" in r.stdout, r.stdout + r.stderr
    assert not pathlib.Path(dead).exists(), "a dead run's tree survived"
    assert pathlib.Path(live).exists(), "a LIVE run's tree was reaped"


def _behind_pr(env):
    """Put refs/pull/42/head on a commit that main has moved past, and return (behind, after)."""
    hub, bare = env["hub"], env["bare"]
    behind = _rev(hub, "feat")
    subprocess.run(["git", "update-ref", "refs/pull/42/head", behind], cwd=bare, check=True)
    # main moves on, exactly as another session's merge would move it.
    subprocess.run(["git", "checkout", "-q", "main"], cwd=hub, check=True)
    (hub / "h.txt").write_text("someone else\n")
    for a in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "other"]):
        subprocess.run(["git", *a], cwd=hub, check=True)
    subprocess.run(["git", "push", "-q", "hub", "main"], cwd=hub, check=True)
    # The head an update would produce: feat's work replayed on top of the new main.
    subprocess.run(["git", "checkout", "-q", "-B", "repaired", "main"], cwd=hub, check=True)
    (hub / "g.txt").write_text("feature\n")
    for a in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "feat2"]):
        subprocess.run(["git", *a], cwd=hub, check=True)
    after = _rev(hub, "repaired")
    subprocess.run(["git", "push", "-q", "hub", "repaired"], cwd=hub, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=hub, check=True)
    return behind, after


def test_an_outdated_pr_is_updated_BEFORE_a_merge_is_attempted(env):
    """The self-heal. `is-ancestor` answers for free what a 405 costs a round trip to learn, and
    per 2026-08-21 neither `mergeable` nor `base.sha` substitutes for it. The assertion is on
    ORDER: the update must precede the first merge attempt, not follow a refusal."""
    behind, after = _behind_pr(env)
    r = _run(env, "", args=("approve", "42"),
             STUB_HEAD_SHA=behind, STUB_AFTER_SHA=after)
    calls = env["log"].read_text().splitlines()
    upd = next((i for i, l in enumerate(calls) if "/update" in l), None)
    mrg = next((i for i, l in enumerate(calls) if l.startswith("pr merge")), None)
    assert upd is not None, f"no update was attempted on an outdated PR:\n{r.stdout}"
    assert mrg is not None, f"never merged after the repair:\n{r.stdout}"
    assert upd < mrg, (
        "the update came AFTER a merge attempt -- that is the old reactive 405 path, not a "
        f"pre-check:\n{chr(10).join(calls)}\n{r.stdout}")
    assert r.returncode == 0, f"self-heal did not end in a merge:\n{r.stdout}"


def test_a_current_pr_is_merged_without_an_update(env):
    """The control. Without it, a pre-check that fires on EVERYTHING would pass the test above
    while spending a pointless CI run on every merge."""
    head = _rev(env["hub"], "feat")
    subprocess.run(["git", "update-ref", "refs/pull/42/head", head], cwd=env["bare"], check=True)
    r = _run(env, "", args=("approve", "42"), STUB_HEAD_SHA=head)
    calls = env["log"].read_text()
    assert "pr merge" in calls, f"a current PR was not merged:\n{r.stdout}"
    assert "/update" not in calls, f"a CURRENT PR was updated anyway:\n{calls}"
    assert r.returncode == 0, r.stdout


def test_a_moved_head_stops_rather_than_merging(env):
    """Green measured on one head is not green on another. If hub's head is not the sha we
    waited on, the checks examined a tree that is no longer the one that would land."""
    subprocess.run(["git", "update-ref", "refs/pull/42/head", _rev(env["hub"], "feat")],
                   cwd=env["bare"], check=True)
    r = _run(env, "", args=("approve", "42"), STUB_HEAD_SHA=_rev(env["hub"], "main"))
    assert "pr merge" not in env["log"].read_text(), (
        f"merged a PR whose head had moved under the green run:\n{r.stdout}")
    assert r.returncode != 0, r.stdout
    assert "MOVED" in r.stdout, r.stdout


# Stands in for a concurrent `git fetch hub main` in the same checkout -- hub-api.sh's currency check
# runs one on every call: the real fetch finishes, then FETCH_HEAD is overwritten with another sha.
RACE_GIT = r"""#!/bin/sh
/usr/bin/git "$@"; rc=$?
if [ "$1" = fetch ] && [ "$rc" -eq 0 ]; then
    printf '%s\t\tbranch main of elsewhere\n' "$RACE_SHA" > "$(/usr/bin/git rev-parse --git-path FETCH_HEAD)"
fi
exit "$rc"
"""


def test_a_concurrent_fetch_cannot_make_a_current_head_read_as_moved(env, tmp_path):
    """Measured 2026-09-15 on a target repo: the queue fetched refs/pull/N/head and read FETCH_HEAD
    back, and a hub-api.sh call in the same checkout fetched hub/main in between -- so a green,
    current PR's head read as the consumer repo's main and the landing stopped with "head MOVED". The race
    is made deterministic: the wrapper overwrites FETCH_HEAD after every fetch, as that call did."""
    head = _rev(env["hub"], "feat")
    subprocess.run(["git", "update-ref", "refs/pull/42/head", head], cwd=env["bare"], check=True)
    bindir = tmp_path / "racebin"
    bindir.mkdir()
    (bindir / "git").write_text(RACE_GIT)
    (bindir / "git").chmod(0o755)
    r = _run(env, "", args=("approve", "42"), STUB_HEAD_SHA=head,
             PATH=f"{bindir}:{env['env']['PATH']}", RACE_SHA=_rev(env["hub"], "main"))
    assert "MOVED" not in r.stdout, f"a concurrent fetch made the queue misread the PR head:\n{r.stdout}"
    assert "pr merge" in env["log"].read_text(), f"a current PR was not merged:\n{r.stdout}"
    assert r.returncode == 0, r.stdout


def test_a_merged_branch_is_retired_from_the_remote(env):
    """"Branches should be deleted after they are merged." The queue is the only thing that
    knows the merge just succeeded, so it is the right place to do it."""
    r = _run(env, "feat\tfeat: a title\n")
    assert r.returncode == 0, r.stdout
    heads = subprocess.run(["git", "ls-remote", "--heads", str(env["bare"])],
                           capture_output=True, text=True).stdout
    assert "refs/heads/feat" not in heads, f"merged branch survived on the remote:\n{heads}"


def test_the_local_half_goes_through_the_pruner(env):
    """NO SECOND IMPLEMENTATION. Every ergonomic git question answers "not merged" about
    squash-merged work, which is why the pruner asks hub instead -- so the queue must call it
    rather than reimplement the judgement."""
    prune = env["hub"] / "scripts/prune-landed-branches-forgejo.sh"
    prune.parent.mkdir(parents=True, exist_ok=True)
    prune.write_text('#!/bin/sh\necho "PRUNER ARGS: $*"\n')
    prune.chmod(0o755)
    r = _run(env, "feat\tfeat: a title\n")
    assert "PRUNER ARGS: --delete feat" in r.stdout, (
        f"the queue did not hand the branch to the pruner:\n{r.stdout}")


def test_a_branch_a_live_worktree_holds_is_not_deleted_from_the_remote(env, tmp_path):
    """THE LANDMINE THE PRUNER DOCUMENTS AND DELIBERATELY DOES NOT STEP ON: git refuses to
    delete a branch checked out in any worktree, and there is NO equivalent protection on the
    remote side. A queue line is by definition another session's branch, so the guard the pruner
    says is missing has to exist here before the remote half is safe."""
    subprocess.run(["git", "worktree", "add", "-q", str(tmp_path / "live"), "feat"],
                   cwd=env["hub"], check=True)
    r = _run(env, "feat\tfeat: a title\n")
    heads = subprocess.run(["git", "ls-remote", "--heads", str(env["bare"])],
                           capture_output=True, text=True).stdout
    assert "refs/heads/feat" in heads, (
        "deleted the remote branch of a ref a live worktree still holds -- nothing on the "
        f"remote side would have refused it:\n{r.stdout}")
    assert "NOT deleting remote" in r.stdout, r.stdout


# =========================================================================================
# `queue:needs-human-review` -- the label that says a green PR is deliberately unmerged.
#
# WHAT WOULD MAKE THESE VACUOUS, and the stub is built around it: a test that asserts the queue
# SENT a POST proves nothing, because the API arm everyone reaches for first (`PATCH /issues/{n}`
# with "labels") is ACCEPTED AND IGNORED at HTTP 200. So the queue verifies by read-back, and the
# stub carries STATE so the read-back can fail: `$STUB_LOG.labels` existing is the label being on.
# `test_a_label_that_does_not_stick_is_reported` is the one that catches a missing read-back.
# =========================================================================================


def _labelled(env) -> bool:
    return pathlib.Path(str(env["log"]) + ".labels").exists()


def test_a_held_pr_is_labelled(env):
    """A held PR is green, open, and indistinguishable in a listing from one nobody has looked
    at. The hold lives in an exit code and a log line that scrolls away."""
    r = _run(env, "feat\tfeat: a title\treview\n")
    assert r.returncode == 9, r.stdout
    assert _labelled(env), f"held PR was not labelled:\n{r.stdout}"
    assert "labelled #42" in r.stdout, r.stdout


def test_an_unflagged_merge_is_not_labelled(env):
    """The control. A label attached to everything says nothing about anything."""
    r = _run(env, "feat\tfeat: a title\n")
    assert r.returncode == 0, r.stdout
    assert not _labelled(env), "an ordinary merged PR was labelled as held"


def test_the_label_is_cleared_when_the_held_pr_is_finally_merged(env):
    """THE HALF THAT MATTERS MORE. A merged PR still wearing `queue:needs-human-review` asserts a decision that
    has already been made -- confidently wrong, which is worse than unlabelled."""
    held = _run(env, "feat\tfeat: a title\treview\n")
    assert held.returncode == 9 and _labelled(env), held.stdout
    env["log"].write_text("")
    r = _run(env, "", args=("approve", "42"))
    assert r.returncode == 0, r.stdout
    assert not _labelled(env), f"merged PR kept its held label:\n{r.stdout}"
    assert "cleared" in r.stdout, r.stdout


def test_a_label_that_does_not_stick_is_reported(env):
    """The read-back, which exists because `PATCH /issues/{n}` with "labels" is accepted and
    IGNORED by this API -- a write that reports success having changed nothing. A queue that
    trusted the POST's status would log a label it never applied."""
    r = _run(env, "feat\tfeat: a title\treview\n", STUB_LABEL_STICKS="")
    assert r.returncode == 9, "labelling failure must not change the hold"
    assert "did NOT stick" in r.stdout, f"an unapplied label was reported as applied:\n{r.stdout}"


def test_a_missing_label_does_not_block_the_hold(env):
    """Fail soft. The hold is the safety property; the label is a convenience on top of it."""
    r = _run(env, "feat\tfeat: a title\treview\n", STUB_NO_LABEL="1")
    assert r.returncode == 9, f"a missing label broke the hold:\n{r.stdout}"
    assert "not found in" in r.stdout, r.stdout
    # THE SKIP MUST NAME ITS OWN FIX, or it is a line that recurs forever and is never acted on
    # because acting on it means going and finding out how.
    assert "/labels -X POST" in r.stdout, f"the skip did not say how to stop skipping:\n{r.stdout}"


def test_a_labelling_failure_does_not_block_a_merge(env):
    """Same property on the other call site: the detach must never cost a merge."""
    held = _run(env, "feat\tfeat: a title\treview\n")
    assert held.returncode == 9
    r = _run(env, "", args=("approve", "42"), STUB_NO_LABEL="1")
    assert r.returncode == 0, f"a labelling failure blocked the merge:\n{r.stdout}"
    assert "pr merge" in env["log"].read_text()


def test_the_label_is_resolved_by_name_not_by_a_hardcoded_id(env):
    """65 is true of the hub repo and of nothing else, including this fixture's repo. Resolving
    also means the script says `queue:needs-human-review` rather than a number a reader must look up."""
    r = _run(env, "feat\tfeat: a title\treview\n")
    calls = env["log"].read_text()
    assert "/labels" in calls, f"never asked the forge which id the label has:\n{calls}"
    # ASSERT ON THE PAYLOAD, NOT ON THE WHOLE LOG. The first version of this asserted `"65" not
    # in calls` and failed immediately: the log contains git shas, and one of them contained
    # "65". A substring search over a corpus full of hex is not a test of anything.
    body = [l for l in calls.splitlines() if "--data-binary" in l]
    assert body, f"no label POST body was sent:\n{calls}"
    assert '"labels":[7]' in body[0], (
        f"the POST did not carry the id resolved from the forge (7 in this fixture):\n{body[0]}")
    assert _labelled(env), r.stdout


def test_labelling_can_be_turned_off(env):
    r = _run(env, "feat\tfeat: a title\treview\n", PR_QUEUE_LABEL="0")
    assert r.returncode == 9, r.stdout
    assert not _labelled(env), "PR_QUEUE_LABEL=0 still labelled the hold"


# ---------------------------------------------------------------------------------------------
# the description slot belongs to the change, not to the queue.


def _amend_with_body(hub, body: str):
    """Give the queued commit a message body, which the fixture's `-qm` commits do not have."""
    subprocess.run(["git", "checkout", "-q", "feat"], cwd=hub, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--amend", "-m", "feat\n\n" + body], cwd=hub, check=True)
    subprocess.run(["git", "push", "-qf", "hub", "feat"], cwd=hub, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=hub, check=True)


def test_the_pr_body_is_the_commit_body_not_queue_telemetry(env):
    """A PR-authorship audit: 17 of 25 merged PRs carried one identical 122-char sentence
    about queue mechanics, displacing the description of the change.

    It is worse than an empty body because it LOOKS like one -- a reviewer sees a populated
    description and does not notice the description is missing. The commit body is already the
    description, so it is derived rather than retyped, the same argument `approve` makes for reading
    the title from the forge.
    """
    _amend_with_body(env["hub"], "WHY THIS EXISTS. A sentinel only this test would write.")
    _run(env, "feat\tfeat: a title\n")
    create = [l for l in env["log"].read_text().splitlines() if l.startswith("pr create")]
    assert create, "no pr create call at all"
    assert "A sentinel only this test would write." in create[0], (
        "the commit body did not reach the PR description: " + create[0])


def test_no_fixed_queue_sentence_is_written_into_any_pr_body(env):
    """Asserts the ABSENCE by its distinctive phrase rather than by the whole 122 chars.

    A whole-string assertion would pass the moment someone reworded the boilerplate, which is the
    failure mode being removed -- not one particular sentence.
    """
    _run(env, "feat\tfeat: a title\n")
    calls = env["log"].read_text()
    assert "Admitted by serial" not in calls, calls
    assert "born up to date" not in calls, calls


def test_a_commit_with_no_body_yields_an_empty_description_rather_than_boilerplate(env):
    """An empty body is the honest signal that the author owed a description and did not write one.

    Substituting boilerplate to fill the slot is exactly what produced the boilerplate bodies, so silence here is
    the point rather than a gap. The fixture's own commit has no body, so this is that case.
    """
    _run(env, "feat\tfeat: a title\n")
    create = [l for l in env["log"].read_text().splitlines() if l.startswith("pr create")]
    assert create, "no pr create call at all"
    assert "born up to date" not in create[0]


# ---------------------------------------------------------------------------------------------
# the queue must not leave its scratch worktree behind.
#
# Before this, the only `worktree remove` was on the `add` path immediately before `add` — so each
# run replaced the previous leftover and the last one survived indefinitely. Since the reaper learned
# ancestry it holds it ACCURATELY and for ever (a detached pre-merge sha is never an ancestor of `main`), which
# is the right verdict on a situation the queue created.
#
# The pair is removal vs retention, not removal vs nothing: a tree that may hold the only copy of
# something must still be kept, so a fix that removed unconditionally would be a regression.

def test_the_scratch_worktree_is_gone_after_a_successful_run(env):
    """The leak itself. The tree has yielded `_body` and `head_sha` and its branch is on hub, so
    from that line it is redundant — and nothing downstream reads it."""
    wt = pathlib.Path(env["env"]["PR_QUEUE_WT"])
    r = _run(env, "feat\tfeat: a title\n")
    assert r.returncode == 0, r.stdout
    assert not wt.exists(), (
        f"the queue left its scratch worktree at {wt}; the reaper will hold it for ever\n{r.stdout}")


def test_a_rebase_failure_keeps_the_scratch_worktree_as_evidence(env, tmp_path):
    """The control, and the reason the test above is not "the queue deletes its tree".

    A failed rebase is exactly when the tree must survive: it is what a human is being asked to
    look at. Identical fixture to the rebase-failure test above; only the assertion differs. An
    unconditional removal — the obvious way to fix the leftover — passes the test above and fails this.
    """
    hub = env["hub"]
    subprocess.run(["git", "checkout", "-q", "-b", "clash"], cwd=hub, check=True)
    (hub / "g.txt").write_text("main's version\n")
    for a in (["add", "-A"], ["commit", "-qm", "clash"]):
        subprocess.run(["git", *a], cwd=hub, check=True)
    subprocess.run(["git", "push", "-q", "hub", "clash:main"], cwd=hub, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=hub, check=True)
    subprocess.run(["git", "merge", "-q", "--ff-only", "clash"], cwd=hub, check=True)

    wt = pathlib.Path(env["env"]["PR_QUEUE_WT"])
    r = _run(env, "feat\tfeat: a title\n")
    assert r.returncode == 4, r.stdout
    assert wt.exists(), (
        f"a failed rebase's worktree was removed; that tree IS the diagnostic\n{r.stdout}")


def test_a_held_pr_does_not_leave_the_scratch_worktree_standing(env):
    """The review hold is the long-lived stop — the queue exits 9 and waits for a human, so this
    is the path that left a tree standing for days. It runs after the removal line, so the hold
    must not reintroduce the leak."""
    wt = pathlib.Path(env["env"]["PR_QUEUE_WT"])
    r = _run(env, "feat\tfeat: a title\treview\n")
    assert r.returncode == 9, r.stdout
    assert not wt.exists(), f"a held PR left the scratch worktree behind\n{r.stdout}"


def test_a_rebase_that_moved_nothing_does_not_say_it_rebased(env):
    """First of the two head-moving steps.

    Two steps in this queue move a PR head -- this rebase, and `update_and_rewait` before the
    merge -- and each logged one UNCONDITIONAL line. A head observed later is consistent with
    either having acted, so the record could not attribute and a reader guessed. Measured on a live
    run: the line read `rebased onto 2e525a3f57` when the rebase produced nothing, and the new
    sha in fact arrived from the update step four minutes later.

    In this fixture `feat` is already on top of `hub/main`, so the rebase is a no-op -- the exact
    shape. The line must say the head did not move, and must not assert a rebase that did nothing.
    """
    r = _run(env, "feat\tfeat: a title\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rebase moved nothing" in r.stdout, (
        "a rebase that produced no new commit still announced itself as a rebase, which is what "
        f"made the later head change unattributable:\n{r.stdout}")
    assert "rebased " not in r.stdout.split("opening PR")[0].replace("rebase moved nothing", ""), (
        f"the no-op path must not also claim it rebased:\n{r.stdout}")


def test_the_update_step_names_itself_as_the_mover(env):
    """The OTHER head-moving step -- the one that actually moved it in that run.

    A 405 is `block_on_outdated_branch` refusing, which sends the queue through
    `update_and_rewait`. That step genuinely changes the head, and the record must say so with
    both shas, so that a later reader attributes the change instead of inferring it from a head
    that is equally consistent with the rebase above.
    """
    r = _run(env, "feat\tfeat: a title\n", STUB_405_TIMES="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/update" in env["log"].read_text(), "fixture did not reach the update step"
    assert "head moved by THIS update step" in r.stdout, (
        "the step that moved the head did not name itself, so the change stays attributable only "
        f"by guessing which of the two steps acted:\n{r.stdout}")


def _sleep_stub(env, tmp_path):
    """A `sleep` on PATH that records its argument instead of sleeping. Asserting through it,
    rather than by timing the run, keeps this out of the timing-flake class."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    seen = tmp_path / "sleep.log"
    seen.write_text("")
    (bindir / "sleep").write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{seen}"\n')
    (bindir / "sleep").chmod(0o755)
    e = {k: v for k, v in env["env"].items()
         if k not in ("PR_QUEUE_REWAIT_SECS", "PR_QUEUE_REWAIT_POLL_SECS", "PR_QUEUE_POLL_SECS")}
    e["PATH"] = f"{bindir}:{e['PATH']}"
    e["STUB_405_TIMES"] = "1"  # the 405 repair path is the one that reaches the rewait
    return e, seen


def test_the_rewait_returns_the_moment_the_head_moves(env, tmp_path):
    """the wait after `update?style=rebase` is on the HEAD SHA CHANGING, not a clock.
    The stub advances `refs/pull/42/head` as soon as `/update` is called, so a correct rewait
    never sleeps at all.

    THE CEILING IS A DISTINCTIVE VALUE ON PURPOSE. Asserting the absence of the POLL interval
    here would pass against the old `sleep "$REWAIT_SECS"` too -- that code sleeps the ceiling,
    not the poll, so "no poll interval was slept" was true of the bug as well and the test
    measured nothing. Proven by running it against hub/main's copy: green. 97 is slept by the
    old code and by nothing else."""
    e, seen = _sleep_stub(env, tmp_path)
    e["PR_QUEUE_REWAIT_SECS"] = "97"
    subprocess.run(["sh", str(PR_QUEUE)], input="feat\tfeat: a title\n", capture_output=True, text=True, env=e)
    assert "/update" in env["log"].read_text(), "the 405 path was not taken, so no rewait happened"
    assert "97" not in seen.read_text().split(), f"the rewait slept the whole ceiling although the head had already moved: {seen.read_text().split()!r}"


def test_the_rewait_polls_up_to_the_ceiling_when_the_head_does_not_move(env, tmp_path):
    """The other arm: a forge that replays nothing. STUB_AFTER_SHA unset means `/update` moves
    nothing, so the loop must poll (`sleep 2`) until the ceiling and then proceed, not hang."""
    e, seen = _sleep_stub(env, tmp_path)
    e.pop("STUB_AFTER_SHA", None)
    e["PR_QUEUE_REWAIT_SECS"] = "4"
    r = subprocess.run(["sh", str(PR_QUEUE)], input="feat\tfeat: a title\n", capture_output=True, text=True, env=e)
    assert "/update" in env["log"].read_text()
    sleeps = [s for s in seen.read_text().split() if s == "2"]
    assert len(sleeps) == 2, f"expected two 2 s polls up to a 4 s ceiling, saw sleeps {seen.read_text().split()!r}"
    assert "did not move within 4s" in r.stdout + r.stderr


def test_the_rewait_poll_and_ceiling_are_read_from_the_environment(env, tmp_path):
    e, seen = _sleep_stub(env, tmp_path)
    e.pop("STUB_AFTER_SHA", None)
    e["PR_QUEUE_REWAIT_SECS"] = "6"
    e["PR_QUEUE_REWAIT_POLL_SECS"] = "3"
    subprocess.run(["sh", str(PR_QUEUE)], input="feat\tfeat: a title\n", capture_output=True, text=True, env=e)
    assert "/update" in env["log"].read_text()
    # BOTH knobs, and both are load-bearing: 6/3 means exactly two polls. Setting them to 0 and
    # asserting the absence of "2" (the previous shape) was green against the old bare
    # `sleep "$REWAIT_SECS"` as well, which sleeps 0 once and never 2 -- vacuous.
    assert [s for s in seen.read_text().split() if s == "3"] == ["3", "3"], \
        f"the poll interval and ceiling were not both read from the environment: {seen.read_text().split()!r}"


# ---------------------------------------------------------------------------------------------
# A REFUSED UPDATE IS READ, NOT RETRIED.
#
# MEASURED 2026-09-22 on a live drain: the forge could not rebase a CONFLICTING branch,
# refused the update, and the refusal went to /dev/null (`-o /dev/null >/dev/null 2>&1`). The loop
# ran three rounds ~35s apart and stopped rc 8 with the whole queue blocked behind it, logging
# "head moved by THIS update step: 28762170a4 -> 28762170a4" each time -- a move asserted with the
# same sha on both sides, contradicting its own "did not move" line from one line earlier.
def _update_posts(env):
    return [l for l in (env["log"].read_text().splitlines() if env["log"].exists() else [])
            if "/update" in l]


def test_a_REFUSED_update_is_READ_and_STOPS_on_the_first_attempt(env):
    """ONE post, and stopped FOR THAT REASON. The measured drain spent three rounds ~35s apart on
    a call the forge was never going to accept, and the queue behind it did not move for either
    owner.

    THE COUNT ALONE IS VACUOUS, which I found by injecting the fault rather than by reading it: a
    first draft asserted only `one post` and `rc != 0`, and it PASSED against the discarded
    response, because that build also posts once and then fails later for an unrelated reason. The
    refusal message is what separates "stopped because the forge said 409" from "stopped somehow"."""
    behind, after = _behind_pr(env)
    r = _run(env, "", args=("approve", "42"),
             STUB_HEAD_SHA=behind, STUB_AFTER_SHA=after, STUB_UPDATE_CODE="409")
    out = r.stdout + r.stderr
    assert "REFUSED to update" in out and "http=409" in out, out
    assert len(_update_posts(env)) == 1, _update_posts(env)
    assert r.returncode != 0


def test_an_UNREADABLE_update_response_is_not_a_refusal__control(env):
    """A read failure must not invent a verdict: with no readable status the behaviour stays the
    one that was here before the refusal path rather than becoming a new refusal -- the same choice
    _pr_is_merged makes for the same reason. The default stub answers `{}`, which is
    not a number. PASSES ON BASE by construction: base has no refusal path at all."""
    behind, after = _behind_pr(env)
    r = _run(env, "", args=("approve", "42"), STUB_HEAD_SHA=behind, STUB_AFTER_SHA=after)
    assert "REFUSED to update" not in r.stdout + r.stderr


# ---------------------------------------------------------------------------------------------
# A REFUSED MERGE PRINTS THE CLIENT'S OWN WORDS.
def test_a_refused_merge_PRINTS_the_clients_own_words(env):
    """`hub-api.sh` refuses a write from a client behind hub/main with `REFUSING: this client is
    STALE ...`, which names the cause AND the remedy: update the checkout and re-run. That line was
    captured in $_out and then dropped when logging, leaving only `http=unreadable` -- and the
    caller adds "main moved under us", which reads as a benign race to re-poll. Re-polling never
    refreshes a checkout, so the generic message sends you to the wrong remedy for ever."""
    r = _run(env, "feat\tfeat: a title\n", STUB_STALE_CLIENT="1")
    out = r.stdout + r.stderr
    assert "http=unreadable" in out, out
    assert "this client is STALE" in out, out


# ---------------------------------------------------------------------------------------------
# A 405 ON A DRAFT IS NOT AN OUTDATED BASE.
#
# MEASURED 2026-09-22 on a live PR, cause isolated to one variable: base sha == hub/main throughout and
# `merge-base --is-ancestor` true (so neither behind nor conflicting), `draft` True, `mergeable`
# False. Removing the `WIP: ` prefix and changing nothing else flipped mergeable to True. The drain
# spent all three repair attempts on a condition no update can change and then halted the queue with
# "main moved under us", which was measurably not what happened.


def test_a_405_on_a_DRAFT_pr_is_not_repaired_and_does_not_halt_the_queue(env):
    """THE POST COUNT IS THE ASSERTION, not the message. A build that printed the new words and
    still called `/update` would pass a message-only check while costing exactly what this fixes,
    so the strong claim is that the update endpoint is never asked at all."""
    r = _run(env, "", args=("approve", "42"), STUB_405_TIMES="9", STUB_PR_DRAFT="true")
    out = r.stdout + r.stderr
    assert "the forge says it is a DRAFT" in out, out
    assert not _update_posts(env), (
        "it tried to repair a draft; no update can make a draft mergeable:\n%s" % _update_posts(env))
    assert "main moved in the gap" not in out, (
        "it reported the one 405 cause that was measurably NOT the case:\n" + out)
    assert "DRAFT again by merge time" in out, (
        "the caller must say WHY it skipped rather than borrowing another cause's words:\n" + out)


def test_a_405_on_a_NON_draft_pr_still_repairs__control(env):
    """The control that stops the draft check swallowing the real staleness case, which is the
    common one and must still repair. `__control` because it PASSES ON BASE -- it asserts the
    behaviour the fix must leave alone, and without it a `_pr_is_draft` that answered `yes`
    unconditionally would pass the test above and break every genuine 405."""
    r = _run(env, "feat\tfeat: a title\n", STUB_405_TIMES="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _update_posts(env), "a genuine 405 must still reach the update step"
    assert "the forge says it is a DRAFT" not in r.stdout, r.stdout


def test_an_update_that_replays_nothing_says_UNCHANGED_not_moved(env):
    """The draft-405 fix's second half. Three sites printed `head moved by THIS update step: X -> Y`
    unconditionally, so an update that replayed nothing reported a move with the same sha on both
    sides of the arrow. This file already quotes two instances as evidence
    (`38a8e2839f -> 38a8e2839f` and `28762170a4 -> 28762170a4`, above), each
    diagnosed THROUGH the contradiction rather than from the log saying so.

    The fixture is the refused-update measurement's shape: the API agrees with git on the head, and no STUB_AFTER_SHA, so
    the update genuinely replays nothing and old == new."""
    behind, _after = _behind_pr(env)
    r = _run(env, "", args=("approve", "42"), STUB_HEAD_SHA=behind, STUB_405_TIMES="1")
    out = r.stdout + r.stderr
    assert "head UNCHANGED by the update step" in out, out
    assert "the update replayed nothing" in out, out
    assert "head moved by THIS update step" not in out, (
        "an unchanged head was still reported as moved:\n" + out)
