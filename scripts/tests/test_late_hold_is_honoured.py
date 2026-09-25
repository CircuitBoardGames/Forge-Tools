"""A hold must reach the merge, and the verb that applies it must apply the
string the queue matches.

THE TWO DEFECTS ARE ONE MECHANISM FROM OPPOSITE ENDS, which is why they are tested together.
The first is *the hold verb writes a label nothing matches* -- wrong string, always broken. The
second is the inverse: right string, applied at the wrong instant, silently ineffective. Fixing the
first makes the second MORE likely to be hit, because more people will successfully apply holds and some of
them will be late. A file testing either alone would pass against a mechanism still broken the
other way.

WHAT THE INJECTION IS, and why it is not a counter. The obvious stub -- "return unheld on call 1,
held on call 2" -- is coupled to how many times the drain happens to read the label set, which is
twice per PR today (`$HELD_LABEL` then `$NOT_ADMITTED_LABEL`) and is not a contract. Instead the
stub's `pr checks` arm TOUCHES a file, and the label arm reports the hold if and only if that file
exists. So the hold arrives *while CI runs*, which is both robust to call-count changes and the
literal thing that happened to a real PR: read at 03:51:18Z, merged at 03:51:24Z, hold applied in
between by the author, using the correct verb, and ignored.

WHAT THIS FILE CANNOT PROVE. The stub is not Forgejo. It cannot show that the real merge endpoint
and the real label endpoint order themselves this way, and the remaining window -- the latency of
the merge POST itself -- is BY CONSTRUCTION not testable here, because no test can inject into an
interval the code does not control. The fix narrows; it does not close. That ceiling is stated in
`approve_one` and printed by `hub-api.sh pr hold`, and it is the reason saying so where holds are
applied is not a consolation prize.
"""

import os
import subprocess
from pathlib import Path


from pr_queue_drain_helpers import (
    _hub_repo,
    _open_pr,
    _stub,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUE = REPO_ROOT / "scripts" / "pr-queue.sh"
HUB_API = REPO_ROOT / "scripts" / "hub-api.sh"
HELD = "queue:needs-human-review"

# PR 41's sha, matching test_pr_queue_drain._open_pr's construction. Named here because the `pr
# checks` stub arm keys on it.
SHA41 = ("41" * 40)[:40]


def _labels_gated_on(marker, n=41):
    """A label-set arm for PR `n` that reports the hold only once `marker` exists."""
    return (
        '%d) if [ -f "%s" ]; then echo \'[{"name":"%s"}]\'; else echo "[]"; fi ;;'
        % (n, marker, HELD)
    )


def _checks_that_apply_the_hold(marker):
    """A `pr checks` arm that applies the hold as a side effect of CI running.

    This is the injection: the hold lands after the drain has read the label set for this PR and
    before anything merges, which is the window that was measured.
    """
    return ('%s) touch "%s"; echo "OK: 1 registered, 0 skipped, 1 actually ran"; exit 0 ;;'
            % (SHA41, marker))


def _run_drain(tmp_path, api, *args):
    env = dict(
        os.environ,
        PR_QUEUE_FOREGROUND="1",  # never detach under a session-launched pytest
        PR_QUEUE_API=str(api),
        PR_QUEUE_HUB=str(_hub_repo(tmp_path)),
        PR_QUEUE_TOOLS_DIR=str(tmp_path / "hub" / "scripts"),  # siblings stay the fixture's
        PR_QUEUE_WT=str(tmp_path / "merge-wt"),
        PR_QUEUE_REWAIT_SECS="0",
        PR_QUEUE_REWAIT_POLL_SECS="0",
        PR_QUEUE_POLL_SECS="0",
        PR_QUEUE_REPO="o/r",
        FORGE_TOOLS_REMOTE="hub",  # the fixture's forge remote, the name pr-queue.sh defaulted to
    )
    return subprocess.run(["sh", str(QUEUE), "drain", *args],
                          capture_output=True, text=True, env=env, timeout=180)


ONE_PR = (41,)


# ------------------------------------------------------------------ the injection


def test_a_hold_applied_while_checks_run_STOPS_the_merge(tmp_path):
    """THE INJECTION. Fails against the pre-change drain, which read the hold once and never again.

    The assertion is on the stub's call log, not on the drain's own summary: a drain that merged
    the PR and then printed a skip line would satisfy any wording check, and the forge write is
    the thing that cannot be taken back.
    """
    marker = tmp_path / "hold-arrived"
    api, log = _stub(
        tmp_path, order=ONE_PR,
        pulls=_open_pr(41),
        labels=_labels_gated_on(marker),
        blockers="",
        red=_checks_that_apply_the_hold(marker),
    )
    r = _run_drain(tmp_path, api)
    assert marker.exists(), "the injection never fired -- `pr checks` was not reached for #41"
    assert "pr merge" not in log.read_text(), (
        "a hold applied during CI did not stop the merge:\n" + log.read_text())
    assert "acquired" in r.stdout and HELD in r.stdout, r.stdout + r.stderr
    # The drain SKIPS a late hold rather than stopping. Stopping is right for `approve` and
    # `merge-requested`, which were asked to merge one named PR; here it would strand everything
    # below a PR someone happened to hold.
    assert r.returncode == 0, "a late hold must skip the PR, not abort the drain\n" + r.stdout


def test_the_same_fixture_MERGES_when_no_hold_arrives(tmp_path):
    """CONTROL ONE, and the file is worthless without it.

    Identical stub with the touch removed. If this does not merge, the test above proves nothing --
    it would be measuring a drain that merges nothing under any conditions, which is exactly the
    vacuous-check shape: a check that measures nothing and passes.
    """
    marker = tmp_path / "hold-arrived"
    api, log = _stub(
        tmp_path, order=ONE_PR,
        pulls=_open_pr(41),
        labels=_labels_gated_on(marker),
        blockers="",
        red="",   # checks pass without applying anything
    )
    r = _run_drain(tmp_path, api)
    assert not marker.exists()
    assert "pr merge" in log.read_text(), (
        "the control did not merge, so the injection above measured nothing:\n" + r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_hold_present_BEFORE_the_read_still_skips(tmp_path):
    """CONTROL TWO, demanded in as many words: a hold applied before the read must still
    skip the PR, or the fix has simply disabled the drain.

    This is the pre-existing `_drain_facts` path, which the new re-read must not have displaced.
    The marker exists from the start, so the label arm reports the hold on the very first read.
    """
    marker = tmp_path / "hold-arrived"
    marker.write_text("")
    api, log = _stub(
        tmp_path, order=ONE_PR,
        pulls=_open_pr(41),
        labels=_labels_gated_on(marker),
        blockers="",
        red="",
    )
    r = _run_drain(tmp_path, api)
    assert "pr merge" not in log.read_text()
    assert "a human is looking at it" in r.stdout, (
        "an early hold must be caught by _drain_facts, not by the late re-read\n" + r.stdout)
    assert r.returncode == 0, r.stdout + r.stderr


# ------------------------------------------------------------------ one owner for the string


def _fake_config(tmp_path):
    """A well-formed token file, so the client gets past `require_config` without a network call.

    The refusal under test fires during argument handling, before any request is built, so no HTTP
    stub is needed. The accept-path control below relies on the same thing: it gets PAST the
    refusal and fails later, elsewhere, which is precisely what distinguishes the two.
    """
    cfg = tmp_path / "hub-api.conf"
    cfg.write_text('header = "Authorization: token %s"\n' % ("a" * 40))
    cfg.chmod(0o600)
    return cfg


def _hub_api(tmp_path, *args):
    env = dict(os.environ, HUB_API_CONFIG=str(_fake_config(tmp_path)),
               FORGE_TOOLS_FORGE_URL="https://forge.example.org")   # any forge URL: the stub never dials it
    return subprocess.run(["sh", str(HUB_API), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def test_issue_label_REFUSES_an_already_namespaced_name(tmp_path):
    """`label` prefixes `wayfinder:`, so this would have created `wayfinder:queue:needs-human-review`
    -- a real label nothing matches, which is worse than an error because it looks like a hold."""
    r = _hub_api(tmp_path, "issue", "label", "o/r", "518", HELD)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "wayfinder:%s" % HELD in out, out
    assert "pr hold" in out, "the refusal must name the verb that does work: " + out


def test_issue_label_still_ACCEPTS_an_ordinary_type(tmp_path):
    """CONTROL. A guard that refused every name would pass the test above and break the client.

    `task` must get past the refusal. It then fails on the network, which is a different failure
    and is what this asserts -- the refusal's own wording must be absent.
    """
    r = _hub_api(tmp_path, "issue", "label", "o/r", "518", "task")
    out = r.stdout + r.stderr
    assert "would become" not in out, "the namespace guard fired on a bare type name: " + out


def test_pr_hold_and_the_queue_agree_on_the_held_label(tmp_path):
    """THE SHARED CONSTANT, enforced by a test rather than by a config module neither script needs.

    The first fix proposed was 'make the hold label a shared constant with one owner'. Two shell
    scripts cannot import one, and `pr-queue.sh` already reads `PR_QUEUE_HELD_LABEL` -- so the
    property that actually matters is that the applier and the matcher resolve the same string,
    including the same default. A divergence here is the original bug with a new spelling.
    """
    literal = 'PR_QUEUE_HELD_LABEL:-%s' % HELD
    queue_src = QUEUE.read_text()
    api_src = HUB_API.read_text()
    assert literal in queue_src, "pr-queue.sh no longer defaults HELD_LABEL to %r" % HELD
    assert literal in api_src, "hub-api.sh `pr hold` no longer defaults to %r" % HELD


def test_pr_create_and_the_queue_agree_on_the_serial_label(tmp_path):
    """The same shared-constant property as the hold above, for `queue:serial` -- the label
    `pr create --serial` attaches and `drain` matches to keep a PR out of every batch.

    STRICTER THAN ITS SIBLING, deliberately: the literal must be an ASSIGNMENT, because a check that
    searches source text is satisfied by the comment explaining it, and this string is exactly the
    kind a comment quotes."""
    import re
    assign = re.compile(r'^\s*\w+="\$\{PR_QUEUE_SERIAL_LABEL:-queue:serial\}"\s*$', re.M)
    assert assign.search(QUEUE.read_text()), "pr-queue.sh does not assign the serial label default"
    assert assign.search(HUB_API.read_text()), "hub-api.sh `pr create` does not assign the serial label default"


def test_pr_hold_states_the_window_it_is_honoured_in(tmp_path):
    """Saying where a hold is honoured, which is not a consolation prize.

    The author of the late-hold PR applied a correct hold believing it worked at any point. That belief is the
    defect the narrowing does not fix, so the verb that applies a hold has to say where its power
    ends. Asserted on the source rather than a run, because printing it requires a live forge.
    """
    src = HUB_API.read_text()
    assert "cannot stop a merge already in flight" in src.lower().replace("\n", " ") or \
           "CANNOT STOP A MERGE ALREADY IN FLIGHT" in src, \
           "`pr hold` no longer states the window a hold is honoured in"
