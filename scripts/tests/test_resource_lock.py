"""A shared resource needs a HOLDER, not a measurement plus a message.

The defect this covers: a coordinator read /proc clear, messaged the next session, and an armed
waiter took the slot in the gap. Every reading was true when taken; nothing held the slot between
them.

WHAT THESE TESTS CAN AND CANNOT REACH. They assert that the lock is held for the command's
lifetime, that contention is refused with a distinguishable status, and that an unregistered name
is refused rather than silently locked. They do NOT show that every session takes the lock -- that
is the fourth property and it lives at the caller, not here. A pass is not evidence the suite
slot is enforced.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "resource-lock.sh"


def run(args, lockdir, **kw):
    env = dict(os.environ, RESOURCE_LOCK_DIR=str(lockdir))
    return subprocess.run(
        ["sh", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=60, **kw
    )


@pytest.fixture
def lockdir(tmp_path):
    d = tmp_path / "locks"
    d.mkdir()
    return d


def test_a_command_runs_under_the_lock(lockdir):
    """The control. Without this, every refusal below could be a script that never runs anything."""
    r = run(["suite", "--", "sh", "-c", "echo ran-it"], lockdir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ran-it" in r.stdout, r.stdout


def test_the_lock_is_held_for_the_COMMANDS_LIFETIME_not_just_taken(lockdir):
    """The property the lock actually needs.

    A scheme that takes a lock, observes it, and releases before the work starts is exactly the
    measurement-plus-message defect wearing a lock's clothing. So: hold it from inside a running
    command and prove a second acquisition fails WHILE that command is still running.
    """
    holder = subprocess.Popen(
        ["sh", str(SCRIPT), "suite", "--", "sh", "-c", "echo up; sleep 10"],
        stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, RESOURCE_LOCK_DIR=str(lockdir)),
    )
    try:
        assert holder.stdout.readline().strip() == "up", "holder never started"
        second = run(["suite", "--no-wait", "--", "sh", "-c", "echo SHOULD-NOT-RUN"], lockdir)
        assert second.returncode == 2, (
            f"a second holder was admitted while the first was live: {second.stdout}{second.stderr}"
        )
        assert "SHOULD-NOT-RUN" not in second.stdout, "the command ran despite the refusal"
        assert "REFUSED" in second.stderr, second.stderr
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_the_lock_survives_a_killed_wrapper_while_its_command_lives(lockdir):
    """WRITTEN EXPECTING THE OPPOSITE, AND KEPT BECAUSE WHAT IT FOUND IS CORRECT.

    The first version asserted that killing the holder frees the lock, on the strength of a comment
    claiming the 5h cap kill releases it. It failed. `flock` hands the fd to the command,
    so killing the WRAPPER leaves the lock held by the COMMAND -- which `pr-queue.sh:154` already
    documents and the comment had contradicted.

    The behaviour is right: a suite that is still running should still hold the suite slot. What
    would be wrong is releasing here, which readmits exactly the concurrency this exists to
    prevent.
    """
    holder = subprocess.Popen(
        ["sh", str(SCRIPT), "suite", "--", "sh", "-c", "echo up; sleep 30"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
        env=dict(os.environ, RESOURCE_LOCK_DIR=str(lockdir)),
    )
    pgid = os.getpgid(holder.pid)   # before the kill: the pid is unreadable once reaped
    try:
        assert holder.stdout.readline().strip() == "up"
        holder.kill()          # the wrapper only; the `sleep` keeps the inherited fd
        holder.wait(timeout=10)
        still = run(["suite", "--no-wait", "--", "true"], lockdir)
        assert still.returncode == 2, (
            "the lock was released while the work it protects was still running -- that readmits "
            "the concurrent suites this exists to prevent"
        )
    finally:
        os.killpg(pgid, 9)      # the surviving `sleep`, which is what still holds the fd


def test_the_lock_frees_when_the_work_itself_ends(lockdir):
    """The other half, and the one that shows there is no on-disk state to go stale.

    Kill the whole group -- wrapper and command together, which is what a session dying takes with
    it -- and the next acquisition must succeed with no cleanup step, no staleness rule and no
    recorded pid to reconcile.
    """
    holder = subprocess.Popen(
        ["sh", str(SCRIPT), "suite", "--", "sh", "-c", "echo up; sleep 30"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
        env=dict(os.environ, RESOURCE_LOCK_DIR=str(lockdir)),
    )
    assert holder.stdout.readline().strip() == "up"
    blocked = run(["suite", "--no-wait", "--", "true"], lockdir)
    assert blocked.returncode == 2, "control: the lock was not held while the holder lived"

    os.killpg(os.getpgid(holder.pid), 9)
    holder.wait(timeout=10)

    after = run(["suite", "--", "sh", "-c", "echo free-again"], lockdir)
    assert after.returncode == 0, (
        f"the lock outlived every process holding it -- that is the deadlock a lease with a "
        f"staleness rule would have to repair: {after.stdout}{after.stderr}"
    )
    assert "free-again" in after.stdout


def test_an_unregistered_resource_is_REFUSED_not_silently_locked(lockdir):
    """`flock /tmp/typo.lock` always succeeds.

    A misspelled resource takes a private lock nobody contends for and reports success identically
    to one that is working. That is a guard measuring nothing, so the name has to be closed.
    """
    r = run(["sweet", "--", "sh", "-c", "echo SHOULD-NOT-RUN"], lockdir)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "SHOULD-NOT-RUN" not in r.stdout, "an unregistered name ran the command anyway"
    assert "unknown resource" in r.stderr, r.stderr
    assert not list(lockdir.iterdir()), (
        f"a lock file was created for an unregistered name: {list(lockdir.iterdir())}"
    )


def test_contention_and_command_failure_are_DIFFERENT_exit_codes(lockdir):
    """`flock` exits 1 on contention by default, and 1 is the commonest real failure code there
    is. "someone else holds the suite" and "the suite ran and failed" would be the same reading --
    the exact conflation this file exists to prevent. `-E 2` separates them.
    """
    failed = run(["suite", "--", "sh", "-c", "exit 1"], lockdir)
    assert failed.returncode == 1, "the command's own failure code was not passed through"
    assert "REFUSED" not in failed.stderr, "a failing command was reported as contention"

    holder = subprocess.Popen(
        ["sh", str(SCRIPT), "suite", "--", "sh", "-c", "echo up; sleep 10"],
        stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, RESOURCE_LOCK_DIR=str(lockdir)),
    )
    try:
        assert holder.stdout.readline().strip() == "up"
        contended = run(["suite", "--no-wait", "--", "true"], lockdir)
        assert contended.returncode == 2
        assert contended.returncode != failed.returncode, (
            "contention and command-failure share an exit code"
        )
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_it_QUEUES_by_default_rather_than_refusing(lockdir):
    """The property that makes this orchestrator-free, and the one the PR queue's drain has.

    An immediate refusal looks like the safe default and is not: a session told "held, try later"
    has to decide what later means, and the cheapest way for an agent to decide that is to ASK
    ANOTHER SESSION -- the measurement-plus-message handoff walking back in through the error path.

    So the default must be to block and be handed the slot by the kernel, with nobody sequencing it.
    """
    holder = subprocess.Popen(
        ["sh", str(SCRIPT), "suite", "--", "sh", "-c", "echo up; sleep 3"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
        env=dict(os.environ, RESOURCE_LOCK_DIR=str(lockdir)),
    )
    pgid = os.getpgid(holder.pid)
    try:
        assert holder.stdout.readline().strip() == "up"
        # No --wait, no --no-wait: the bare form a session would actually type.
        queued = run(["suite", "--", "sh", "-c", "echo queued-then-ran"], lockdir)
        assert queued.returncode == 0, (
            f"the default refused instead of queueing, which sends the caller looking for a human: "
            f"{queued.stdout}{queued.stderr}"
        )
        assert "queued-then-ran" in queued.stdout
        assert "waiting up to" in queued.stderr, (
            "it queued silently -- an invisible wait is indistinguishable from a hang, and the "
            "response to an apparent hang is to kill it and ask someone"
        )
    finally:
        os.killpg(pgid, 9)


def test_no_wait_is_available_for_a_caller_that_wants_an_answer(lockdir):
    """The opt-out, and the control for the test above: without it, "queues by default" could be
    "cannot refuse at all", which would make the exit-code test below meaningless."""
    holder = subprocess.Popen(
        ["sh", str(SCRIPT), "suite", "--", "sh", "-c", "echo up; sleep 10"],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
        env=dict(os.environ, RESOURCE_LOCK_DIR=str(lockdir)),
    )
    pgid = os.getpgid(holder.pid)
    try:
        assert holder.stdout.readline().strip() == "up"
        r = run(["suite", "--no-wait", "--", "sh", "-c", "echo SHOULD-NOT-RUN"], lockdir)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "SHOULD-NOT-RUN" not in r.stdout
    finally:
        os.killpg(pgid, 9)


def test_there_is_no_release_verb(lockdir):
    """A session must not be able to free a slot it does not hold.

    That would be the handoff restored with extra steps -- and worse, indistinguishable from
    breaking a live holder. The only ways out are: the work ends, or a human kills the pid.
    """
    for attempt in (["suite", "--release"], ["--release", "suite"], ["suite", "--force", "--", "true"]):
        r = run(attempt, lockdir)
        assert r.returncode != 0, f"{attempt} was accepted: {r.stdout}"


def test_wait_mode_acquires_once_the_holder_exits(lockdir):
    """--wait N overrides the default window; same mechanism, explicit bound."""
    holder = subprocess.Popen(
        ["sh", str(SCRIPT), "suite", "--", "sh", "-c", "echo up; sleep 2"],
        stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, RESOURCE_LOCK_DIR=str(lockdir)),
    )
    assert holder.stdout.readline().strip() == "up"
    waited = run(["suite", "--wait", "30", "--", "sh", "-c", "echo got-it"], lockdir)
    holder.wait(timeout=10)
    assert waited.returncode == 0, f"--wait never acquired: {waited.stdout}{waited.stderr}"
    assert "got-it" in waited.stdout


def test_who_reports_without_reserving(lockdir):
    """`--who` is a REPORT. If it reserved anything it would be a second way to take the lock
    without holding it, which is the defect wearing a different hat -- so assert that asking does
    not prevent a subsequent acquisition."""
    r = run(["--who", "suite"], lockdir)
    assert r.returncode == 0, r.stderr
    took = run(["suite", "--", "sh", "-c", "echo still-free"], lockdir)
    assert took.returncode == 0, "asking who held it left something holding it"
    assert "still-free" in took.stdout


def test_different_resources_do_not_contend(lockdir):
    """A per-resource lock, not one global mutex -- otherwise a suite run would block a build for
    no reason and sessions would route around the wrapper."""
    holder = subprocess.Popen(
        ["sh", str(SCRIPT), "suite", "--", "sh", "-c", "echo up; sleep 10"],
        stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, RESOURCE_LOCK_DIR=str(lockdir)),
    )
    try:
        assert holder.stdout.readline().strip() == "up"
        other = run(["box-build", "--", "sh", "-c", "echo other-ran"], lockdir)
        assert other.returncode == 0, f"an unrelated resource was blocked: {other.stderr}"
        assert "other-ran" in other.stdout
    finally:
        holder.kill()
        holder.wait(timeout=10)


# ==============================================================================================
# THE WITNESS -- "was MY run exclusive?", which is a different question from "is the lock held".
#
# `--who` answers about an INSTANT, by design. After a suite goes red in a file the diff never
# touched, the question is about an INTERVAL, and nothing could answer it: measured 2026-08-30, a
# locked run failed on `test_pr_queue_merges_what_it_opens.py` with a neighbour running unwrapped
# throughout, and the attribution took a hand-walk of /proc afterwards.
#
# The probe is STUBBED here because a test cannot spawn a real suite. It is an OVERRIDE and never
# an unset -- `test_the_default_probe_is_the_real_command` is the control that keeps the default
# honest, and without it every test below would pass against a script whose real probe was broken.
# ==============================================================================================

PROBE = Path(__file__).resolve().parents[1] / "running-suites"


def run_probed(args, lockdir, probe, **kw):
    """A locked run with the suite probe stubbed to `probe` (a shell command printing pid<TAB>tree)."""
    env = dict(os.environ, RESOURCE_LOCK_DIR=str(lockdir), RESOURCE_LOCK_SUITE_PROBE=probe)
    return subprocess.run(["sh", str(SCRIPT), *args],
                          capture_output=True, text=True, env=env, timeout=60, **kw)


def test_a_run_with_the_box_to_itself_reports_EXCLUSIVE(lockdir):
    r = run_probed(["suite", "--", "echo", "ran"], lockdir, "true")
    assert r.returncode == 0, r.stderr
    assert "EXCLUSIVE for this run" in r.stderr, r.stderr
    # The ceiling is printed, not merely known. A reader who takes EXCLUSIVE for "nothing else ran"
    # is the failure this whole verdict exists to prevent, one level up.
    assert "began and ended inside your run is missed" in r.stderr, r.stderr


def test_an_unwrapped_neighbour_is_NAMED_not_just_counted(lockdir):
    """The pid and tree are what make the verdict actionable: a count sends you to walk /proc."""
    r = run_probed(["suite", "--", "echo", "ran"], lockdir, "printf '4242\\tCC-neighbour\\n'")
    assert "NOT EXCLUSIVE" in r.stderr, r.stderr
    assert "pid 4242 in CC-neighbour" in r.stderr, r.stderr
    assert "CONTENTION" in r.stderr, r.stderr


def test_a_probe_that_CANNOT_RUN_is_not_reported_as_a_quiet_box(lockdir):
    """THE ARM THAT MAKES THE OTHER TWO WORTH ANYTHING.

    An earlier draft ended both probe arms with `|| true`, so a missing `python3`, a moved hook or
    a typo'd override produced exactly what a quiet box produces -- and the verdict read EXCLUSIVE.
    A fail-safe indistinguishable from success, written into the tool whose whole job is to stop a
    green being trusted for the wrong reason.
    """
    r = run_probed(["suite", "--", "echo", "ran"], lockdir, "exit 3")
    assert "COULD NOT MEASURE" in r.stderr, r.stderr
    assert "EXCLUSIVE for this run" not in r.stderr, "a broken probe claimed exclusivity: " + r.stderr
    assert r.returncode == 0, "a failed probe must not fail the command it observed"


def test_the_witness_never_changes_the_commands_exit_code(lockdir):
    """The diagnostic is an observer. If it could alter the status, every caller would have to know
    that, and the first surprising red would be blamed on the lock instead of the tests."""
    r = run_probed(["suite", "--", "sh", "-c", "exit 7"], lockdir, "printf '1\\tCC-x\\n'")
    assert r.returncode == 7, f"expected the command's own 7, got {r.returncode}: {r.stderr}"
    assert "NOT EXCLUSIVE" in r.stderr


def test_who_reports_running_suites_beside_the_lock_state(lockdir):
    """`--who` said only whether the LOCK was held, and a run that never took it is invisible to
    `flock`. That is the reading that misled a session: `free` while the box was contended."""
    r = run_probed(["--who", "suite"], lockdir, "printf '77\\tCC-elsewhere\\n'")
    assert r.returncode == 0, r.stderr
    assert "pid 77 in CC-elsewhere" in r.stdout, r.stdout


def test_who_does_NOT_claim_a_visible_run_skipped_the_lock(lockdir):
    """THE CORRECTION, AND IT WAS CAUGHT IN PRODUCTION RATHER THAN BY A TEST.

    The first version printed "run(s) NOT holding this lock" — and named a suite that was holding
    it perfectly legitimately under `flock`. The inference "anything visible did not take the lock"
    holds only while the ASKER holds it, which is precisely what `--who` refuses to do.

    A verb that reserves nothing cannot attribute anything, and a wrong attribution here is worse
    than the silence it replaced: it invites a session to go looking for a rule-breaker that does
    not exist.
    """
    r = run_probed(["--who", "suite"], lockdir, "printf '77\\tCC-elsewhere\\n'")
    assert "NOT holding this lock" not in r.stdout, (
        f"--who is attributing lock-skipping it cannot measure:\n{r.stdout}")
    assert "MAY BE THE HOLDER" in r.stdout, (
        f"--who names runs without saying it cannot tell the holder from a skipper:\n{r.stdout}")


def test_who_still_reserves_nothing_while_reporting_more(lockdir):
    """The property `--who` already had, re-asserted because this change gave it more to say and a
    verb that quietly started holding something would be worse than one that said too little."""
    run_probed(["--who", "suite"], lockdir, "true")
    took = run_probed(["suite", "--", "echo", "still-free"], lockdir, "true")
    assert took.returncode == 0 and "still-free" in took.stdout, took.stderr


def test_the_default_probe_is_the_real_command(lockdir):
    """THE CONTROL FOR EVERY STUBBED TEST ABOVE, and it is not optional.

    A harness is on record that OMITTED a variable and reached
    production with a live credential: overriding and unsetting are opposite acts. Here the risk is
    the mirror image -- every test above drives a stub, so a default probe that had rotted would be
    invisible. This asserts the unstubbed path resolves to the real `running-suites` and produces a verdict
    rather than the COULD-NOT-MEASURE arm.
    """
    assert PROBE.is_file(), f"{PROBE} is missing; the default probe cannot resolve"
    r = subprocess.run([str(PROBE)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"running-suites is broken: {r.stderr}"

    # And the script's default wiring reaches it: no override, so a MEASURED verdict must appear.
    #
    # EITHER measured verdict is correct here and the test must accept both: this runs on a box
    # where other agents run suites, so "NOT EXCLUSIVE" is the honest answer as often as not. What
    # is being pinned is that the probe RAN -- the failure mode is the COULD-NOT-MEASURE arm.
    #
    # SPELLED OUT RATHER THAN `"EXCLUSIVE" in stderr`, WHICH IS WHAT THIS LINE SAID FIRST AND IS
    # VACUOUS: "EXCLUSIVE" is a substring of "NOT EXCLUSIVE", so that assertion could not tell the
    # two verdicts apart and would have passed against a script that only ever printed one of them.
    # Found by injection -- suppressing the NOT-EXCLUSIVE branch failed this test for the right
    # reason by luck, not because the assertion was measuring what it claimed.
    plain = run(["suite", "--", "echo", "ran"], lockdir)
    assert "COULD NOT MEASURE" not in plain.stderr, (
        f"the DEFAULT probe could not run, so production reports nothing useful:\n{plain.stderr}")
    assert ("EXCLUSIVE for this run" in plain.stderr) or ("NOT EXCLUSIVE" in plain.stderr), (
        f"the default path produced no measured verdict at all:\n{plain.stderr}")


# ---- one lock for every USER on the box ---------------------------------------------------
# The CI runner and the agent sessions are different users; the lock file belongs to whoever ran
# first. A 0444 file / 0555 dir stands in for "another user's" -- the modes bind because the runner
# is not root, and the cases skip where they would not.

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the modes these cases rely on")


@needs_non_root
def test_a_lock_file_you_can_only_READ_is_still_taken(lockdir):
    f = lockdir / "suite.lock"
    f.touch()
    f.chmod(0o444)
    r = run(["suite", "--", "echo", "ran-under-it"], lockdir)
    assert r.returncode == 0, r.stderr
    assert "ran-under-it" in r.stdout


def test_a_lock_dir_it_creates_is_sticky_and_world_writable_not_the_callers_umask(tmp_path):
    d = tmp_path / "fresh-locks"
    r = run(["suite", "--", "true"], d)
    assert r.returncode == 0, r.stderr
    assert oct(d.stat().st_mode & 0o7777) == oct(0o1777)


@needs_non_root
def test_a_lock_dir_it_cannot_create_in_is_refused_NAMING_the_owner(lockdir):
    import pwd
    lockdir.chmod(0o555)
    try:
        r = run(["suite", "--", "echo", "ran-under-it"], lockdir)
    finally:
        lockdir.chmod(0o755)
    assert r.returncode == 2 and "ran-under-it" not in r.stdout, r.stdout + r.stderr
    owner = pwd.getpwuid(lockdir.stat().st_uid).pw_name
    assert f"is owned by {owner} and not usable" in r.stderr, r.stderr
    assert "chmod 1777" in r.stderr, "the refusal does not say how to fix it"
