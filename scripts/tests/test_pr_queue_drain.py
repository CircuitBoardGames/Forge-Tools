"""The drain lands every READY PR from the head of the queue's order downward.

WHAT THIS FILE IS ACTUALLY ABOUT. The drain's design is one decision: which answers SKIP a PR and
keep going, and which STOP the run. `merge-requested` stops on any non-zero and says why -- the
assumption the next merge rests on is gone. The drain cannot use that rule, because the case it
exists for (a PR at the head nobody can land right now) is exactly the case that rule halts on. So
every test here is one cell of that matrix, and the two halves must be proven separately: a file
that only tested skips would pass against a drain that never stops, and vice versa.

The stub is the forge. It cannot tell us Forgejo's real behaviour -- see the ceiling note on
test_pr_queue_merges_what_it_opens.py, which applies here unchanged. The stub, the throwaway
checkout and the runner live in pr_queue_drain_helpers.py, shared with the other drain tests.
"""

import json
import subprocess
import time
from pathlib import Path

from pr_queue_drain_helpers import (
    QUEUE,
    _listing,
    _open_pr,
    _run,
    _stub,
)


# ------------------------------------------------------------------ refusals before any merge


def test_an_unreadable_listing_REFUSES_rather_than_draining_nothing(tmp_path):
    """The queue IS the open PRs, oldest first -- a hand-kept order went stale (every entry merged)
    and is gone with its override. An unreadable listing must never arrive here as 'nothing to
    merge': those produce the same silence and the same exit 0.

    The refusal predates the listing becoming the only order -- the hand-kept order's absence was
    refused the same way. This keeps it pinned now that the listing is the only order."""
    api, log = _stub(tmp_path, order=None, pulls=_open_pr(41), labels="", blockers="")
    r = _run(tmp_path, api)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "open PR listing" in r.stdout and "REFUSING" in r.stdout, r.stdout
    assert "pulls?state=open" in log.read_text(), "no listing was ever requested"
    assert "pr merge" not in log.read_text(), "an unreadable queue merged something"


def test_an_EMPTY_queue_says_so_in_its_own_words(tmp_path):
    """Distinct from a populated drain that merged nothing, and from the refusal above. Same exit
    code as the former, different sentence."""
    api, log = _stub(tmp_path, order=(), pulls="", labels="", blockers="")
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "EMPTY" in r.stdout and "no open PRs" in r.stdout, r.stdout
    assert "pr merge" not in log.read_text()


def test_a_hand_typed_drain_names_a_live_run_and_still_drains(tmp_path):
    """Two hand-typed drains collided and the second stopped on "main moved
    under us" with nothing naming the run that moved it. Only `drain-duty.sh` looked first. The
    drain now says who else is live -- and does not refuse, since concurrent drains are legal. The
    control is the same run with the log closed: a finished run's log must not read as live."""
    api, _ = _stub(tmp_path, order=(),
                   pulls="", labels="", blockers="")
    logs = tmp_path / "logs"
    logs.mkdir()
    held = logs / "20260922T161842Z-3011766-drain.log"
    with open(held, "w"):
        r = _run(tmp_path, api)
    assert r.returncode == 0 and "EMPTY" in r.stdout, "the notice must not stop the drain: " + r.stdout
    assert "ANOTHER pr-queue RUN IS LIVE" in r.stdout and str(held) in r.stdout, r.stdout


def test_a_finished_runs_log_is_not_named_as_live__control(tmp_path):
    """Logs are never deleted, so a check that fired on one merely existing would pass the test
    above and cry wolf on every drain after the first."""
    api, _ = _stub(tmp_path, order=(),
                   pulls="", labels="", blockers="")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "20260922T161842Z-3011766-drain.log").write_text("drain: merged 1\n")
    r = _run(tmp_path, api)
    assert r.returncode == 0 and "EMPTY" in r.stdout, r.stdout
    assert "ANOTHER pr-queue RUN IS LIVE" not in r.stdout, "a closed log read as a live run: " + r.stdout


# ------------------------------------------------------------------ SKIP, and keep draining


def test_a_held_pr_is_passed_over_and_the_drain_CONTINUES(tmp_path):
    """The property that makes this a drain rather than a queue head.

    Premise 6: passing over must not reorder it. Nothing here writes the list, so its position
    stands -- and the PR below it still lands, which is the whole point of a drain.
    """
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels='41) echo \'[{"name":"queue:needs-human-review"}]\' ;;',
        blockers="")
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#41 SKIP" in r.stdout and "human" in r.stdout
    calls = log.read_text()
    assert "pr merge o/r 42" in calls, f"the drain stopped at the held PR instead of passing it:\n{calls}"
    assert "pr merge o/r 41" not in calls, "the held PR was merged"


def test_a_label_read_that_answers_an_error_object_STOPS_and_merges_nothing(tmp_path):
    """An unreadable label set is not "no hold". The forge can answer valid JSON that is not a
    label list -- an error object -- and the one-label-at-a-time read iterated it as strings,
    raised, exited 1, and read that as "not labelled": a held PR would merge. The three labels are
    now read at once, and anything but a list of labels stops the drain."""
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels='41) echo \'{"message":"token does not have at least one of required scope(s)"}\' ;;',
        blockers="")
    _run(tmp_path, api)
    calls = log.read_text()
    assert "pr merge o/r 41" not in calls, f"an unreadable label set merged the PR it could not read:\n{calls}"


def test_a_hold_that_becomes_UNREADABLE_while_checks_ran_is_not_merged(tmp_path):
    """The hold is read again just before the merge POST. That read goes through the same check:
    a label list at the drain's first read, then an error object, must not merge."""
    seen = tmp_path / "labels-read-once"
    api, log = _stub(
        tmp_path, order=(41, 42), pulls=_open_pr(41),
        labels=f"41) if [ -e {seen} ]; then echo '{{\"message\":\"forbidden\"}}'; "
               f"else : > {seen}; echo '[]'; fi ;;",
        blockers="")
    _run(tmp_path, api)
    calls = log.read_text()
    assert seen.exists(), f"the stub's first read never happened, so this measured nothing:\n{calls}"
    assert "pr merge o/r 41" not in calls, f"a hold read that answered an error object merged:\n{calls}"


def test_a_gate_suppressed_pr_is_skipped_for_its_OWN_reason(tmp_path):
    """Premise 4: the two holds must never share a label, or 'passed, waiting for a human' and
    'never ran' become the same reading. Assert the log distinguishes them."""
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels='41) echo \'[{"name":"queue:not-admitted"}]\' ;;',
        blockers="")
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "queue:not-admitted" in r.stdout
    assert "human" not in r.stdout.split("#41")[1].split("#42")[0], (
        "a suppressed PR was reported as a human hold -- premise 4's exact conflation"
    )
    assert "pr merge o/r 42" in log.read_text()


def test_a_dependency_blocked_pr_is_skipped(tmp_path):
    """Forgejo enforces edges -- a dependency-blocked merge answers HTTP 500. Skipping
    is not politeness; forcing it would fail anyway."""
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels="", blockers='41) echo "#41 open_blockers=1 #40" ;;')
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dependency" in r.stdout
    assert "pr merge o/r 42" in log.read_text()


def test_a_closed_queue_entry_is_data_not_damage(tmp_path):
    """The listing lags: a PR it named open can have landed by the time the drain reads it, so one
    stale line must not stop the queue."""
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join(
            ['41) echo \'{"state":"closed","title":"t","head":{"sha":"a","ref":"b"}}\' ;;',
             _open_pr(42)]),
        labels="", blockers="")
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not open" in r.stdout
    assert "pr merge o/r 42" in log.read_text()


# ------------------------------------------------------------------ STOP the whole run


def test_an_unreadable_pr_STOPS_rather_than_being_skipped(tmp_path):
    """The other half of the matrix, and the reason the skip tests above are not vacuous.

    An unreadable PR is an answer about the FORGE, not about this PR: every later verdict would
    rest on a forge we just failed to reach. If this were also a skip, the drain would merge on
    through an outage.
    """
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls=_open_pr(42),          # #41 falls through to `exit 1` -- unreadable
        labels="", blockers="")
    r = _run(tmp_path, api)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "cannot read #41" in r.stdout and "STOPPING" in r.stdout
    assert "pr merge o/r 42" not in log.read_text(), (
        "the drain continued past an unreadable PR -- it would merge through a forge outage"
    )


def test_a_refused_merge_STOPS_the_run(tmp_path):
    """We hold the flock, so main moving under us means a merge happened OUTSIDE this box
    (a PR once landed from the web UI with no session in the loop). The next PR's
    freshness is no longer established."""
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels="", blockers="", merge_code="409")
    r = _run(tmp_path, api)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "STOPPING" in r.stdout
    assert "pr merge o/r 42" not in log.read_text(), "the drain merged on after a refusal"


# ------------------------------------------------------------------ the control


def test_the_happy_path_merges_every_ready_pr_in_order(tmp_path):
    """Without this, every assertion above could be satisfied by a drain that merges nothing."""
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels="", blockers="")
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text()
    assert "pr merge o/r 41" in calls and "pr merge o/r 42" in calls, calls
    assert calls.index("pr merge o/r 41") < calls.index("pr merge o/r 42"), (
        "the drain merged out of queue order"
    )
    assert "merged 2" in r.stdout


def test_a_pr_another_run_landed_is_not_counted_as_this_drains_merge(tmp_path):
    """There is no queue lock, so a second drain reaching a PR the first already merged is
    expected; it must neither POST a merge nor claim one. The stub is static, so the landing is
    modelled as a PR still listed `open` that the forge also reports `merged` -- the read the drain
    makes at the top of its merge attempt. The summary is the assertion that matters: the count is
    read by whoever reports the drain, and "merged 2" alone would hand them another run's merge."""
    landed = ('41) echo \'{"state":"open","merged":true,"merged_at":"2026-09-20T15:50:21Z",'
              '"merged_by":{"login":"another-run"},"title":"pr 41",'
              '"head":{"sha":"%s","ref":"b41"}}\' ;;' % ("41" * 20))
    api, log = _stub(tmp_path, order=(41, 42),
                     pulls="\n            ".join([landed, _open_pr(42)]), labels="", blockers="")
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text()
    assert "pr merge o/r 41" not in calls, f"POSTed a merge for a PR that had landed:\n{calls}"
    assert "pr merge o/r 42" in calls, "the drain did not carry on to the PR behind it"
    assert "#41 is ALREADY MERGED, and not by this run" in r.stdout, r.stdout
    assert "1 of those merged had ALREADY LANDED by another run" in r.stdout, r.stdout


def test_dry_run_merges_nothing(tmp_path):
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels="", blockers="")
    r = _run(tmp_path, api, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WOULD MERGE" in r.stdout
    assert "pr merge" not in log.read_text(), "--dry-run merged something"


def test_a_pr_whose_CHECKS_ARE_RED_is_skipped_and_the_drain_CONTINUES(tmp_path):
    """The fifth ready-predicate arm, and the one no test reached.

    The other four skip a PR that *should not* merge for a reason visible without asking CI. This
    one stops a PR whose gates went RED, and the drain's entire safety claim is that it merges only
    what CI actually passed -- so if it ever regresses it fails in the MERGING direction.

    It was unreachable because the stub answered every checks query `exit 0`. That is the natural
    stub to write, it makes the other eleven tests pass, and nothing signals the omission: grepping
    this file for `red` returned only prose inside docstrings, so the word was present and the
    behaviour was not.

    The red is a genuine `state='failure'`, not a bare non-zero: `wait_for_green` reads the printed
    STATE precisely because `pr checks` exits 1 for pending and 1 for failure alike, so a stub that
    only failed would exercise the polling arm instead of this one. `was_cancelled` answers "not
    cancelled" here (the stub serves no `actions/runs`, which its own reader treats as NOT-cancelled
    rather than as absence), which is what makes the red terminal rather than another wait.
    """
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels="", blockers="",
        red="%s) echo \"REFUSING: state='failure'\"; exit 1 ;;" % ("41" * 20))
    r = _run(tmp_path, api)
    assert r.returncode == 0, f"a red PR must be SKIPPED, not stop the run:\n{r.stdout}{r.stderr}"
    assert "#41 SKIP" in r.stdout, r.stdout
    assert "checks are not green" in r.stdout, (
        f"the skip did not name its own reason -- premise 4's conflation one arm over:\n{r.stdout}")

    calls = log.read_text()
    assert "pr merge o/r 42" in calls, (
        f"the drain stopped at the red PR instead of passing over it:\n{calls}")
    assert "pr merge o/r 41" not in calls, (
        f"A PR WITH RED CHECKS WAS MERGED. This is the failure the arm exists to prevent:\n{calls}")
    # Premise 6, on this arm: passing over must not reorder it. The held-PR test asserts the same
    # property one arm over; nothing in the drain writes the list, and this pins that it stays so.
    assert "body-edit" not in calls and "PATCH" not in calls, (
        f"the drain rewrote the queue order while skipping a red PR:\n{calls}")


def test_the_red_arm_is_reached_only_when_the_stub_says_red(tmp_path):
    """The control for the fixture, not for the drain.

    Without it, a `red=` argument that silently failed to interpolate would leave the test above
    asserting a skip that never happened for the reason claimed -- #41 would merge, `pr merge o/r
    41` would appear, and the failure would look like a drain bug rather than a broken fixture.
    Same shas, same order, red disarmed: #41 must now merge.
    """
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels="", blockers="", red="")
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text()
    assert "pr merge o/r 41" in calls, (
        f"with no red configured #41 must merge; if it does not, the arm above proves nothing:\n{calls}")
    assert "checks are not green" not in r.stdout, r.stdout


# ------------------------------------------------------------------ --json
# One JSON record per line on stdout -- verdict, refusal, summary -- and the human log on stderr, so
# a caller branches on a record rather than on prose. Exit codes are unchanged.


def _records(stdout: str) -> list[dict]:
    """Every stdout line under --json is JSON; a stray log line fails here, which is the point."""
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_json_happy_path_is_one_verdict_per_pr_and_a_summary(tmp_path):
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels="", blockers="")
    r = _run(tmp_path, api, "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    recs = _records(r.stdout)
    assert [(x["pr"], x["decision"]) for x in recs if x["kind"] == "verdict"] == [(41, "merged"), (42, "merged")], recs
    summary = [x for x in recs if x["kind"] == "summary"]
    assert summary == [{"kind": "summary", "merged": 2, "skipped": 0, "queued": 2,
                        "dry_run": False, "batch": False}], recs
    assert "merged 2" in r.stderr, "the human log moves to stderr, it does not disappear"


def test_json_a_held_pr_is_a_skip_record_and_the_drain_continues(tmp_path):
    api, log = _stub(
        tmp_path, order=(41, 42),
        pulls="\n            ".join([_open_pr(41), _open_pr(42)]),
        labels='41) echo \'[{"name":"queue:needs-human-review"}]\' ;;',
        blockers="")
    r = _run(tmp_path, api, "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    verdicts = [x for x in _records(r.stdout) if x["kind"] == "verdict"]
    assert [(x["pr"], x["decision"], x["reason"]) for x in verdicts] == [
        (41, "skip", "held"), (42, "merged", "merged")], verdicts
    assert "pr merge o/r 42" in log.read_text()


def test_json_an_unreadable_pr_is_a_stop_record_never_a_skip(tmp_path):
    api, log = _stub(tmp_path, order=(41, 42), pulls=_open_pr(42), labels="", blockers="")
    r = _run(tmp_path, api, "--json")
    assert r.returncode == 1, r.stdout + r.stderr
    verdicts = [x for x in _records(r.stdout) if x["kind"] == "verdict"]
    assert [(x["pr"], x["decision"], x["reason"]) for x in verdicts] == [(41, "stop", "unreadable_pr")], verdicts
    assert "pr merge o/r 42" not in log.read_text()


def test_json_an_unreadable_derived_order_is_a_refusal_record(tmp_path):
    """An unreadable listing is a queue that could not be read: one refusal code, never a summary."""
    api, _ = _stub(tmp_path, order=None, pulls=_open_pr(41), labels="", blockers="")
    r = _run(tmp_path, api, "--json")
    assert r.returncode == 2, r.stdout + r.stderr
    assert _records(r.stdout) == [{"kind": "refusal", "reason": "order_unreadable"}], r.stdout


# --- one drain empties the queue, instead of one snapshot of it ---------------------------------


def _stateful_stub(tmp_path, orders, pulls):
    """A stub whose QUEUE ORDER changes per read, so the passes see different queues.

    That is not artificial: the draft-first rules END a pass with work still
    queued by design -- "a READY PR behind waiting drafts keeps its position for the NEXT drain".
    A static stub cannot express that, and a drain tested only against one would look complete.
    Only PAGE 1 advances the read: later pages answer empty, so each pass is one derived order.
    """
    api = tmp_path / "stateful-api.sh"
    counter = tmp_path / "reads"
    arms = "\n".join(
        "%d) echo '%s' ;;" % (i + 1, _listing(o))
        for i, o in enumerate(orders))
    api.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '"/api/v1/repos/"*"/wiki/pages") echo "[]" ;;\n'
        '"/api/v1/repos/"*"/pulls?state=open"*"&page=1")\n'
        'n=$(cat "%s" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "%s"\n'
        '[ "$n" -le %d ] || n=%d\n'
        'case "$n" in\n%s\nesac ;;\n'
        '"/api/v1/repos/"*"/pulls?state=open"*) echo "[]" ;;\n'
        '"/api/v1/repos/"*"/pulls/"*)\n'
        'p=${1##*/}\ncase "$p" in\n%s\n*) exit 1 ;;\nesac ;;\n'
        '"/api/v1/repos/"*"/issues/"*"/labels") echo "[]" ;;\n'
        'issue) echo "#$4 open_blockers=0" ;;\n'
        'pr)\ncase "$2" in\n'
        'checks) echo "OK: 1 registered, 0 skipped, 1 actually ran"; exit 0 ;;\n'
        'merge) echo "merged: http=200"; exit 0 ;;\n'
        "esac ;;\n"
        "esac\nexit 0\n"
        % (counter, counter, len(orders), len(orders), arms, pulls))
    api.chmod(0o755)
    return api


def test_one_drain_keeps_going_until_the_queue_is_EMPTY(tmp_path):
    """THE POINT. Pass 1 sees #41, pass 2 sees #42, pass 3 sees nothing.

    On the unfixed script the drain reads the order ONCE: it merges #41 and exits reporting
    "merged 1, of 1 queued", leaving #42 for whenever something next calls drain -- which in
    practice is `drain-duty.sh` behind a 15-minute cooldown.
    """
    api = _stateful_stub(tmp_path, [(41,), (42,), ()],
                         pulls=_open_pr(41) + "\n" + _open_pr(42))
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "merged $_merged" not in out
    assert "re-deriving the queue and continuing" in out, "it never started a second pass:\n" + out
    assert "the queue is EMPTY" in out, "it stopped without emptying the queue:\n" + out
    assert "drain: merged 2," in out, "both PRs must be counted in ONE drain's total:\n" + out


def test_a_pass_that_merges_NOTHING_does_not_loop__control(tmp_path):
    """The termination guard. Every extra pass must be bought by a merge, or a queue holding one
    permanently-skipped PR would spin forever. Here the only PR is closed, so nothing merges."""
    api = _stateful_stub(tmp_path, [(41,), (41,)],
                         pulls='41) echo \'{"state":"closed","title":"x","head":{"sha":"%s","ref":"b41"}}\' ;;'
                               % ("4" * 40))
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "re-deriving the queue and continuing" not in out, "it looped on a pass that merged nothing"
    assert "drain: merged 0," in out, out


def test_dry_run_is_always_exactly_one_pass__control(tmp_path):
    """A dry run merges nothing, so it must not re-derive -- it would print a queue that a real
    drain would have changed, which is a report of a world that does not exist."""
    api = _stateful_stub(tmp_path, [(41,), (42,), ()],
                         pulls=_open_pr(41) + "\n" + _open_pr(42))
    r = _run(tmp_path, api, "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "re-deriving the queue and continuing" not in out, out
    assert "would merge" in out, out


def test_an_UNCHANGED_queue_stops_the_loop_even_while_merges_are_reported(tmp_path):
    """THE TERMINATION CONDITION THAT ACTUALLY HOLDS.

    "A pass that merged something may have more to do" is true and is NOT sufficient: on the
    `--batch` path the summary ASSIGNS `_merged=$BATCH_LANDED` rather than accumulating, so a landed
    batch reports merges on every pass and the merge count never falls to zero. The first version of
    this loop reasoned from that count and spun to the pass cap -- 14 failures across
    test_pr_queue_batch.py, every one a drain re-walking a queue it had already served.

    Here the stub serves the SAME queue forever and always reports #41 open and merging, which is
    the shape that defeats a merge-count argument. The queue not changing is what must stop it.
    """
    api = _stateful_stub(tmp_path, [(41,)] * 6, pulls=_open_pr(41))
    r = _run(tmp_path, api)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "the queue did not change" in out, "it did not stop on an unchanged queue:\n" + out
    assert "the cap (PR_QUEUE_DRAIN_PASSES" not in out, "it spun to the pass cap instead:\n" + out
    # ONE extra derive, not twenty: the check fires at the top of pass 2, before it does work again.
    assert out.count("drain: merging #41") == 1, (
        "#41 was merged more than once -- the loop re-served a queue it had already served:\n" + out)


# ------------------------------------------------------- a cancellation on a stale head

SHA_A = "a" * 40          # the head the wait starts on
SHA_B = "b" * 40          # what a force-push moves it to

CANCELLED_RUN = '{"workflow_runs":[{"id":1,"status":"cancelled"}]}'


def _moving_head_stub(tmp_path, *, moves):
    """A PR whose head changes WHEN the cancellation is observed, which is the real causal order:
    the force-push is what cancelled the run. Keyed on a marker the `/actions/runs` route drops
    rather than on a call count, so the test does not depend on how many times the drain happens
    to read the PR before it starts waiting."""
    marker = tmp_path / "pushed"
    moved = ('if [ -f %s ]; then echo \'{"state":"open","title":"pr 41","head":{"sha":"%s","ref":"b41"}}\';'
             ' else echo \'{"state":"open","title":"pr 41","head":{"sha":"%s","ref":"b41"}}\'; fi ;;'
             % (marker, SHA_B if moves else SHA_A, SHA_A))
    return _stub(
        tmp_path,
        order=(41,),
        pulls="41)\n" + moved,
        labels="",
        blockers="",
        # The run for SHA_A comes back cancelled, and observing that is what "publishes" the push.
        runs='*head_sha=%s*) touch %s; echo \'%s\' ;;' % (SHA_A, marker, CANCELLED_RUN),
        red="%s) echo \"total_count=1 state='failure' skipped=0\"; exit 1 ;;" % SHA_A,
    )


def test_a_cancellation_on_a_sha_that_is_no_longer_the_head_STOPS_instead_of_waiting_out(tmp_path):
    """A cancelled run has two causes wanting opposite responses: a re-queue (wait) or a
    force-push that superseded the head (no run will ever settle on this sha, so waiting can only
    end in timeout). Measured 2026-09-20: 168 polls over 3m43s on a sha that had not been the head
    since before the first of them, ending "not red: nothing was measured".

    The assertion is that it STOPS, not merely that it logs -- a version that printed the diagnosis
    and kept polling would satisfy a log-only check while costing the same four minutes."""
    api, _log = _moving_head_stub(tmp_path, moves=True)
    r = _run(tmp_path, api, PR_QUEUE_WAIT_POLLS="60")
    out = r.stdout + r.stderr
    assert "head has since moved to %s" % SHA_B in out, out
    assert "no run will settle on %s again" % SHA_A in out, out
    # It must not have burned the poll budget: the give-up line is the failure this replaces.
    assert "NEVER SETTLED" not in out, "it waited the sha out anyway:\n" + out
    assert "#41 moved under this wait" in out, out


def test_a_cancellation_on_the_CURRENT_head_still_waits__control(tmp_path):
    """THE CONTROL THAT MATTERS. Without it, a `was_cancelled` arm that returned 8 unconditionally
    would pass the test above and convert every genuine re-queue into an abandoned PR -- turning a
    correct wait into a skip, silently."""
    api, _log = _moving_head_stub(tmp_path, moves=False)
    r = _run(tmp_path, api, PR_QUEUE_WAIT_POLLS="2")
    out = r.stdout + r.stderr
    assert "a run was CANCELLED, not failed -- still waiting" in out, out
    assert "head has since moved" not in out, "it called an unmoved head moved:\n" + out


def test_an_UNREADABLE_head_is_not_a_moved_head__control(tmp_path):
    """FAIL CLOSED. `_head_of` prints nothing when the forge does not answer. Reading that as
    "moved" would abandon a PR on a transport blip, so empty must fall through to the waiting
    behaviour that existed before this check."""
    marker = tmp_path / "pushed"
    api, _log = _stub(
        tmp_path,
        order=(41,),
        # Readable once so the drain can start; unreadable on the re-read.
        pulls=('41)\nif [ -f %s ]; then exit 1;'
               ' else echo \'{"state":"open","title":"pr 41","head":{"sha":"%s","ref":"b41"}}\'; fi ;;'
               % (marker, SHA_A)),
        labels="",
        blockers="",
        runs='*head_sha=%s*) touch %s; echo \'%s\' ;;' % (SHA_A, marker, CANCELLED_RUN),
        red="%s) echo \"total_count=1 state='failure' skipped=0\"; exit 1 ;;" % SHA_A,
    )
    r = _run(tmp_path, api, PR_QUEUE_WAIT_POLLS="2")
    out = r.stdout + r.stderr
    assert "head has since moved" not in out, "an unreadable head was reported as moved:\n" + out

# --- a cancellation on a head whose runs have ALL FINISHED is not "keep waiting" ----------------


def _runs_stub(tmp_path, statuses):
    """A hub-api.sh stub whose /actions/runs answer we control, plus a red `pr checks`."""
    import json as _json
    d = tmp_path / "scripts"
    d.mkdir(exist_ok=True)
    (d / "runs.json").write_text(_json.dumps(
        {"workflow_runs": [{"status": s} for s in statuses]}))
    api = d / "stub.sh"
    api.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  */actions/runs\\?head_sha=*) cat "$(dirname "$0")/runs.json" ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n")
    api.chmod(0o755)
    return api


def _was_cancelled(tmp_path, statuses):
    """Run the SHIPPED function against those run statuses, and return its exit code."""
    body = subprocess.run(["sed", "-n", "/^was_cancelled() {/,/^}/p", str(QUEUE)],
                          capture_output=True, text=True, check=True).stdout
    assert "was_cancelled()" in body, "the helper was renamed; this test would measure nothing"
    chk = subprocess.run(["sh", "-n", "-c", body], capture_output=True, text=True)
    assert chk.returncode == 0, "the extracted function does not parse:\n" + chk.stderr
    api = _runs_stub(tmp_path, statuses)
    # The shipped function delegates to forge.sh beside the script, found through $SELF_DIR.
    script = 'API=%s\nREPO=o/r\nSELF_DIR=%s\nlog() { :; }\n%s\nwas_cancelled %s\n' % (
        api, QUEUE.parent, body, "a" * 40)
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True).returncode


def test_the_MEASURED_case_still_answers_rc_0_as_it_always_did__control(tmp_path):
    """THE MEASURED CASE. 903be0a243 had four runs -- one cancelled, three success, all terminal --
    and `any(cancelled)` stayed true for the full WAIT_POLLS=180, a 30-minute spin.

    A CONTROL, and it earned the name the hard way. It read `== 3` while this branch repurposed
    rc 0, and passed -- which is exactly what a broken contract looks like from inside the branch
    that broke it. rc 0 for a settled cancellation is the answer every caller written before the
    terminal stop depends on, so it must NOT move; this pins that it did not.

    Which means it cannot also prove the fix, and it no longer claims to. The behavioural half --
    that the wait STOPS on a finished head instead of spinning -- is
    `test_the_terminal_stop_is_NOT_rc_6_which_means_RED`, which asserts rc 10 out of
    `wait_for_green` and does fail on the base."""
    assert _was_cancelled(tmp_path, ["cancelled", "success", "success", "success"]) == 0


def test_a_cancellation_with_a_run_STILL_IN_FLIGHT_keeps_waiting__control(tmp_path):
    """The half that must NOT change: this is the force-push case the cancellation check exists
    for, and stopping here would halt the queue on a red it caused itself."""
    assert _was_cancelled(tmp_path, ["cancelled", "running"]) == 3


def test_an_UNKNOWN_status_counts_as_IN_FLIGHT_not_terminal(tmp_path):
    """THE DESIGN DECISION, pinned. Forgejo has an open status vocabulary. Testing for terminality
    negatively (`not running`) would make any unseen state read as terminal and stop a wait that
    should continue -- failing toward the silence this ticket is about. So TERMINAL is an explicit
    allow-list and anything else keeps waiting, which is the pre-change behaviour."""
    assert _was_cancelled(tmp_path, ["cancelled", "some_state_forgejo_adds_later"]) == 3


def test_no_cancellation_is_still_a_real_verdict__control(tmp_path):
    assert _was_cancelled(tmp_path, ["failure", "success"]) == 1


def test_the_terminal_stop_is_NOT_rc_6_which_means_RED(tmp_path):
    """rc 6 is RED SPECIFICALLY -- "this PR's tests failed". The first version of the terminal stop
    returned it, which would assert the very verdict the cancellation destroyed. Caught while
    resolving against the superseded-head check, by reading the code around the conflict rather
    than only the conflict."""
    body = Path(QUEUE).read_text()
    i = body.index("no run will ever settle here. STOPPING this wait")
    after = body[i:i + 400]
    assert "return 10" in after, "the terminal stop must not reuse a code that means something else"
    assert "return 6" not in after, "rc 6 means RED; nothing measured this head"
    assert '"$_wrc" = 10' in body, "approve_one does not translate rc 10, so the drain would STOP on it"


def test_the_SUPERSEDED_check_runs_before_the_terminal_one(tmp_path):
    """ORDER IS A DECISION, pinned. When a head is both superseded AND finished, the superseded answer is
    strictly more useful: there is a newer head to measure, so "skip and read the new one" beats
    "nothing measured this one". Reversing them would lose that, silently and plausibly."""
    body = Path(QUEUE).read_text()
    moved = body.index("no run will settle on $_sha again")
    finished = body.index("every run on it has FINISHED")
    assert moved < finished, (
        "the terminal check precedes the superseded one; a moved-and-finished head would report "
        "'nothing measured' when a newer head was available to measure")


# ---------------------------------------------------------------------------------------------
# ONE BOUNDED RETRY ON THE ORDER READ.
#
# A hand-on fires seconds after a merge, when the forge is busiest, and a single failed listing
# read aborted the whole drain: two PRs were named as waiting drafts and then abandoned
# two seconds later. The REFUSAL is correct and is not weakened here -- what these pin is that it
# now happens after N reads instead of one, and still happens.
def _flaky_listing(tmp_path, fail_times):
    """A client whose OPEN-PR listing fails its first `fail_times` reads, then answers with an
    empty listing. `fail_times < 0` fails for ever. Every read is counted, because "it retried"
    and "it refused later" are different claims and only the count separates them."""
    count = tmp_path / "listing-calls"
    count.write_text("")
    api = tmp_path / "flaky-api.sh"
    api.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '"/api/v1/repos/"*"/wiki/pages") echo "[]" ;;\n'
        '"/api/v1/repos/"*"/pulls?state=open"*)\n'
        '  echo x >> "' + str(count) + '"\n'
        '  n=$(wc -l < "' + str(count) + '" | tr -d " ")\n'
        '  if [ ' + str(fail_times) + ' -lt 0 ] || [ "$n" -le ' + str(fail_times) + ' ]\n'
        '  then echo "not json at all"\n'
        '  else echo "[]"\n'
        '  fi ;;\n'
        "*) : ;;\n"
        "esac\n"
    )
    api.chmod(0o755)
    return api, count


def _calls(count):
    return len([l for l in count.read_text().splitlines() if l])


def test_an_unreadable_listing_STILL_REFUSES_after_the_retries_are_spent(tmp_path):
    """THE CONTROL THE TICKET ASKS FOR, and the one a wrong fix would fail. Retrying is the easy
    half; the trap is turning the refusal into an exit 0, which abandons the drafts SILENTLY
    rather than loudly. An unreadable listing is still not an empty queue."""
    api, count = _flaky_listing(tmp_path, -1)
    r = _run(tmp_path, api, PR_QUEUE_ORDER_TRIES="3", PR_QUEUE_ORDER_BACKOFF="0")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    out = r.stdout + r.stderr
    assert "REFUSING after 3 attempt(s)" in out, out
    assert "is NOT an empty queue" in out, "the refusal's reason must survive the retry"
    assert _calls(count) == 3, _calls(count)


def test_the_refusal_names_the_attempt_count_it_ACTUALLY_made(tmp_path):
    """Pinned against a hardcoded number: with two tries it must say two AND have read twice.
    A message naming a count it did not make is the vacuous-check shape this repo refuses."""
    api, count = _flaky_listing(tmp_path, -1)
    r = _run(tmp_path, api, PR_QUEUE_ORDER_TRIES="2", PR_QUEUE_ORDER_BACKOFF="0")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "REFUSING after 2 attempt(s)" in r.stdout + r.stderr
    assert _calls(count) == 2, _calls(count)


def test_a_listing_that_fails_ONCE_then_answers_does_not_abort_the_drain(tmp_path):
    """THE MEASURED CASE. Two seconds after naming two PRs as waiting drafts, one failed
    read took the drain with it and both were left queued with nothing coming back for them."""
    api, count = _flaky_listing(tmp_path, 1)
    r = _run(tmp_path, api, PR_QUEUE_ORDER_TRIES="3", PR_QUEUE_ORDER_BACKOFF="0")
    assert "REFUSING" not in r.stdout + r.stderr, r.stdout + r.stderr
    assert _calls(count) == 2, _calls(count)


def test_a_READABLE_listing_is_read_exactly_once__control(tmp_path):
    """No gratuitous retries on the happy path: the retry is for a transient, not a poll loop.
    PASSES ON BASE by construction -- base reads once too, having no retry at all -- which is why
    it is a control and claims only that the fix added no extra reads."""
    api, count = _flaky_listing(tmp_path, 0)
    r = _run(tmp_path, api, PR_QUEUE_ORDER_TRIES="3", PR_QUEUE_ORDER_BACKOFF="0")
    assert "REFUSING" not in r.stdout + r.stderr, r.stdout + r.stderr
    assert _calls(count) == 1, _calls(count)
# ------------------------------------------------- a red is the owner's to clear

RED_SHA = "41" * 20


def test_a_RED_pr_tells_its_OWNER_not_only_the_draining_session(tmp_path):
    """A drain reports a red into the log of the session that STARTED it. Measured: a PR
    went red in a drain started by another session, and its owner -- live, idle
    and addressable throughout -- learned only when a third party relayed it by hand.

    The message must name the PR and carry the command that resolves the failure, because a nudge
    saying only "your PR is red" costs the reader the same lookup the drain already did."""
    api, _log = _stub(
        tmp_path,
        order=(41,),
        pulls=_open_pr(41),
        labels="", blockers="",
        red="%s) echo \"total_count=1 state='failure' skipped=0\"; exit 1 ;;" % RED_SHA,
    )
    r = _run(tmp_path, api)
    out = r.stdout + r.stderr
    # No worktree in this fixture owns b41, so the resolution finds nobody -- which is the branch of
    # `_notify_owner` that still PROVES the call was made, and names what would have been sent.
    assert "nobody was messaged about: your PR #41 is RED" in out, out
    assert "pr why-red" in out, "the nudge must carry the command that names the failing job:\n" + out


def test_a_skip_that_is_NOT_a_red_notifies_nobody__control(tmp_path):
    """THE CONTROL, and it is the one that matters. If the notify fired on every rc-7 skip it would
    pass the test above while turning the channel into the thing once measured -- eight messages
    to one session, seven unactionable, and a reader who stopped looking. A DRAFT is skipped with
    rc 7 and is not a red: nothing is wrong and nobody needs telling."""
    api, _log = _stub(
        tmp_path,
        order=(41,),
        pulls=('41) echo \'{"state":"open","title":"WIP: pr 41",'
               '"head":{"sha":"%s","ref":"b41"}}\' ;;' % RED_SHA),
        labels="", blockers="",
        # Green, but the suite is skipped -- the draft shape, which returns 7 via rc 4.
        red=("%s) echo \"  Test / pytest   skipped\"; "
             "echo \"total_count=1 state='success' skipped=1\"; exit 0 ;;" % RED_SHA),
    )
    r = _run(tmp_path, api)
    out = r.stdout + r.stderr
    assert "#41" in out, "the drain never reached the PR, so this control measured nothing:\n" + out
    assert "is RED" not in out, "a non-red skip notified the owner:\n" + out
    assert "nobody was messaged about" not in out, out


# --- a landed PR names the wayfinder tasks it leaves open ---------------------------------------
#
# `refuse_if_closes_wayfinder` makes the close deliberate, and nothing prompts for it. Measured
# three times, the third by a handoff that called a finished ticket
# outstanding eight lines above its own section describing the pattern.

TASK_30 = {"state": "open", "title": "the task this PR finished",
             "labels": [{"name": "wayfinder"}, {"name": "wayfinder:chore"},
                        {"name": "wayfinder:map-26"}]}
MAP_26 = {"state": "open", "title": "the map it belongs to",
           "labels": [{"name": "wayfinder"}, {"name": "wayfinder:map"},
                      {"name": "wayfinder:map-26"}]}


def _resolve_stub(tmp_path, body, issues, pr_num=41, title=None):
    """A stub serving the reads the post-merge scan makes: the PR, its commits, and each ticket.

    `issues` maps number -> JSON dict, or to the string "unreadable" for a ticket that answers
    nothing -- the fail-soft case, which must print NOT RUN and never "nothing open".
    """
    log = tmp_path / "stub.log"
    api = tmp_path / "stub-api.sh"
    pr = {"state": "open", "title": title or ("pr %d" % pr_num), "body": body,
          "head": {"sha": str(pr_num) * 40, "ref": "b%d" % pr_num}}
    arms = []
    for num, val in issues.items():
        if val == "unreadable":
            arms.append('"/api/v1/repos/"*"/issues/%s") exit 1 ;;' % num)
        else:
            arms.append('"/api/v1/repos/"*"/issues/%s") printf "%%s" %s ;;'
                        % (num, "'" + json.dumps(val) + "'"))
    api.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "%s"\n'
        'case "$1" in\n'
        '"/api/v1/repos/"*"/wiki/pages") echo "[]" ;;\n'
        '"/api/v1/repos/"*"/pulls?state=open"*) printf "%%s" %s ;;\n'
        '"/api/v1/repos/"*"/pulls/%d/commits") echo "[]" ;;\n'
        '"/api/v1/repos/"*"/pulls/%d") printf "%%s" %s ;;\n'
        '"/api/v1/repos/"*"/issues/"*"/labels") echo "[]" ;;\n'
        "%s\n"
        "issue) echo \"#$4 open_blockers=0\" ;;\n"
        "pr)\n"
        'case "$2" in\n'
        'checks) echo "OK: 1 registered, 0 skipped, 1 actually ran"; exit 0 ;;\n'
        'merge)  echo "merged: http=200"; exit 0 ;;\n'
        "esac ;;\n"
        "esac\n"
        "exit 0\n"
        % (log, "'" + _listing((pr_num,)) + "'",
           pr_num, pr_num, "'" + json.dumps(pr) + "'", "\n".join(arms))
    )
    api.chmod(0o755)
    return api, log


def test_a_landed_pr_names_the_open_wayfinder_task_it_cannot_close(tmp_path):
    """THE MAP IS THE CONTROL, and it is why this cannot be a plain "list every #N" scan: the body
    says `Part of #26` as well, and telling a session to resolve the map its task belongs to would
    be worse than saying nothing. Maps are excluded by the `wayfinder:map` label they carry and
    tasks do not."""
    api, _log = _resolve_stub(tmp_path, "Part of #26. Implements #30.",
                              {26: MAP_26, 30: TASK_30})
    out = _run(tmp_path, api).stdout
    assert "STILL OPEN" in out, out
    assert "#30  the task this PR finished" in out, out
    assert "issue resolve o/r 30 26" in out, "the map number makes the line runnable:\n" + out
    assert "resolve o/r 26" not in out, "it told someone to resolve the MAP:\n" + out


def test_a_landed_pr_whose_tickets_are_all_closed_says_nothing__control(tmp_path):
    """The control that stops this being an unconditional reminder. A prompt that fires on every
    merge is muted by its readers within a day -- the rationale the owner notice rests on too.

    `__control` because it PASSES ON BASE and that was measured, not assumed: the unfixed script
    prints neither string, so this asserts the silence the fix must preserve rather than anything
    the fix adds. Naming it otherwise is the differential's exact complaint."""
    closed = dict(TASK_30, state="closed")
    api, _log = _resolve_stub(tmp_path, "Part of #26. Implements #30.",
                              {26: MAP_26, 30: closed})
    out = _run(tmp_path, api).stdout
    assert "merged #41" in out, "the merge itself must still have happened:\n" + out
    assert "STILL OPEN" not in out, out
    assert "issue resolve" not in out, out


def test_a_ticket_that_cannot_be_read_is_NOT_RUN_and_not_silence(tmp_path):
    """FAIL SOFT, LOUDLY, and never fail the merge: it has already happened when this runs. An
    unreadable ticket rendering as "nothing left open" would recreate the defect exactly."""
    api, _log = _resolve_stub(tmp_path, "Implements #30.", {30: "unreadable"})
    out = _run(tmp_path, api).stdout
    assert "merged #41" in out, "an unreadable ticket must not have failed the merge:\n" + out
    assert "open-task check NOT RUN" in out, out
    assert "this is not 'nothing left open'" in out, out


# --- a bare `#N` in prose is a CITATION, not this PR's subject ---------------------------------
#
# Produced by this very check on the first drain after every number began resolving against the
# queue's own repo: it listed an unrelated ticket #13 on another map because the body said
# "draining Widget #13" -- a PR in ANOTHER repo -- and a foreign PR number now resolved to a real
# ticket here instead of failing.
#
# MEASURED ON FOUR REAL PRs before narrowing: they matched 6, ten, six and four references -- and
# in every case exactly ONE was the subject. Narrowing to the two SUBJECT shapes
# this repo already enforces (the `(#N)` cite a hub-api merge requires, and the
# `Part of`/`Implements` markers every body here uses) kept the true subject on all four and
# dropped the rest. That is why this is not the "guessing which reference is the one" that the
# first version of this check declined to do: it reads a convention, it does not infer intent.


def test_a_cross_repo_PR_number_in_prose_is_not_listed_as_a_ticket(tmp_path):
    """THE REGRESSION CASE, with the real sentence that caused it. #13 is open, wayfinder, and on
    a different map -- everything the old matcher needed to list it."""
    other = {"state": "open", "title": "a task on another map",
             "labels": [{"name": "wayfinder"}, {"name": "wayfinder:task"},
                        {"name": "wayfinder:map-5"}]}
    api, _log = _resolve_stub(
        tmp_path, "Part of #26. Implements #30. Found while draining Widget #13.",
        {13: other, 26: MAP_26, 30: TASK_30})
    out = _run(tmp_path, api).stdout
    assert "#30  the task this PR finished" in out, out
    assert "#13 " not in out and "resolve o/r 13 " not in out, (
        "a PR number from another repo was listed as a ticket here:\n" + out)


def test_a_subject_cite_in_the_title_still_lists_its_ticket__control(tmp_path):
    """THE ANTI-UNDER-REPORT GUARD, and the reason narrowing is safe here: a PR that finishes a
    ticket carries `(#N)` in its subject, because a hub-api merge requires it. So the ticket is
    still found even when the body never names it.

    `__control` -- it PASSES ON BASE, where the bare matcher finds `#30` inside `(#30)` too.
    It pins the behaviour the narrowing must NOT break, which is the whole risk of this change."""
    api, _log = _resolve_stub(tmp_path, "No markers in this body at all.",
                              {30: TASK_30}, title="pr-queue: something (#30)")
    out = _run(tmp_path, api).stdout
    assert "#30  the task this PR finished" in out, out


def _liveness(*args):
    """Run pr-queue.sh's `_starttime` + `_run_is_live` on their own, as the file defines them."""
    fns = subprocess.run(["sed", "-n", "/^_starttime()/p;/^_run_is_live() {/,/^}/p", str(QUEUE)],
                         capture_output=True, text=True, check=True).stdout
    assert "_run_is_live" in fns and "_starttime" in fns, "extraction found nothing to test"
    return subprocess.run(["sh", "-c", fns + '\n_run_is_live "$@"', "sh", *args]).returncode


def test_a_recycled_pid_reading_pr_queue_is_not_the_queue_operator(tmp_path):
    """The queue claim's liveness grepped `pr-queue.sh` out of /proc/<pid>/cmdline, so a
    recycled pid running `less scripts/pr-queue.sh` held the queue. The claim records the start time,
    and a different process under the same pid is not the claimant."""
    p = subprocess.Popen(["bash", "-c", 'exec -a "less scripts/pr-queue.sh" sleep 20'])
    try:
        time.sleep(0.3)
        assert _liveness(str(p.pid), "1") != 0, "a pager under a recycled pid held the queue"
    finally:
        p.kill()
        p.wait()


def test_the_claimant_itself_is_still_live__control(tmp_path):
    p = subprocess.Popen(["sleep", "20"])
    try:
        time.sleep(0.3)
        st = open("/proc/%d/stat" % p.pid).read().rsplit(") ", 1)[1].split()[19]
        assert _liveness(str(p.pid), st) == 0
    finally:
        p.kill()
        p.wait()
