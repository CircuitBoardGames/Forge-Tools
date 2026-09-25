#!/bin/sh
# Hold a NAMED shared resource for the lifetime of a command.
#
#   resource-lock.sh <resource> [--wait SECONDS | --no-wait] -- <command...>
#   resource-lock.sh --list
#   resource-lock.sh --who <resource>
#
# It QUEUES by default (30 min). No session grants this to another; you take it, or you wait for
# the kernel to hand it over. See "WAITING IS THE DEFAULT" below for why an immediate refusal is
# the wrong default here.
#
# The box has resources only one agent may use at a time, and until now the only one with
# a holder was the PR queue. The suite slot was coordinated by MEASURING /proc and then MESSAGING
# the next session -- and nothing holds a slot between a measurement and a message. Measured
# 2026-08-26: a coordinator read the box clear and pinged the next session; an armed waiter in
# another started a run in the gap; the pinged session gated correctly and refused. Every reading was true when taken. The race is
# structural, and an armed waiter always beats a human-latency handoff because it has no turn
# latency.
#
# ============================================================================================
# THE METHOD IS NOT NEW HERE. `pr-queue.sh` already settled it for the merge queue and this is
# that decision reused rather than a second design:
#
#   AN fd LOCK, NOT A LOCKFILE. A lockfile needs hygiene nobody maintains -- a crashed run leaves
#   a stale one, and every reader then needs a staleness rule, which is a second thing to get
#   wrong. `flock` is held by the KERNEL on an open file description, so there is NO on-disk state
#   to go stale.
#
# That single property answers three of the four requirements outright:
#   * a holder            -- the fd is the holder; "free" and "mine" stop being the same reading
#   * survives the gap    -- the kernel holds it across message latency, turns, and sleep
#   * no deadlock on a dead holder -- the kernel drops it when the last descriptor closes. There
#                            is no recorded pid to go stale, so there is no staleness rule to get
#                            wrong, which is what a /proc liveness check would have
#                            needed.
#
# THAT THIRD PROPERTY IS NARROWER THAN IT FIRST READS, AND AN EARLIER DRAFT OF THIS COMMENT GOT IT
# WRONG. It said "the 5h cap kill releases it". It does not, necessarily. The lock drops
# when the LAST DESCRIPTOR closes, not when the process you killed exits -- `pr-queue.sh:154` says
# exactly this and this file still managed to contradict it. `flock` passes the fd to the command,
# so killing the wrapper while the command lives leaves the lock HELD BY THE COMMAND. Measured by
# `test_the_lock_survives_a_killed_wrapper_while_its_command_lives`, which was written expecting
# the opposite and is kept because the behaviour it found is the correct one:
#
#   * a killed wrapper whose suite is STILL RUNNING keeps the lock -- right, the resource is still
#     in use, and releasing it there would readmit the concurrency this exists to prevent;
#   * the lock frees when the work itself ends, however its wrapper died;
#   * the genuine deadlock case is an ORPHANED child holding the fd while doing nothing, which is
#     the same residue `pr-queue.sh` documents and is not repaired here. `--who` reports HELD for
#     it, a human kills the pid, and there is deliberately no override flag: a `--force` that
#     stole a lock would be indistinguishable from one that broke a real holder.
#
# So the accurate claim is "no stale on-disk state", NOT "any kill frees it".
#
# THE FOURTH REQUIREMENT IS NOT SOLVED BY THIS FILE, AND SAYING SO IS THE POINT. The fourth asks that it
# "work for a session that never asks". A lock binds only whoever takes it, so this script cannot
# bind a session that runs the suite directly -- exactly the armed waiter above, which was legitimate. What
# closes that is the CALLER side refusing an unwrapped run.
# This file is the holder. It is not the enforcement, and a reader who takes it for enforcement
# has the same false confidence this file exists to remove.
#
# WHY THE COMMAND RUNS UNDER THE LOCK RATHER THAN THE LOCK BEING TAKEN AND RELEASED. A hook cannot
# hold this: a PreToolUse hook exits before the tool runs, and its fds close with it, so a lock it
# took protects nothing. The holder must be the process whose work the resource is for. Hence the
# `-- <command>` shape: `flock` owns the fd and the command inherits it for exactly as long as it
# runs.
#
# WHY A REGISTRY AND NOT A FREE-FORM PATH. `flock /tmp/whatever.lock` ALWAYS SUCCEEDS. A misspelled
# resource takes a private lock nobody else contends for and reports success identically to a lock
# that is doing its job -- a guard that measures nothing, in the exact shape
# a check that cannot fail has. So an unknown name is REFUSED. Adding a resource is
# one line below, which is the cost of making a typo loud.
# ============================================================================================
set -eu

SELF="[resource-lock]"

# The registry. `<name>|<one-line description of what may not overlap>`.
# Add a line to add a resource; there is deliberately no way to lock an unregistered name.
RESOURCES='
suite|the local full pytest suite (2 cores; concurrent runs make reds in files the diff never touched)
forge-runner|the single shared Forgejo runner at capacity 1
box-build|a native build or npm install that can exhaust 3.9 GB
'

LOCKDIR="${RESOURCE_LOCK_DIR:-/tmp/agent-resource-locks}"
HERE=$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")   # real dir: `running-suites` is a sibling, even through a symlink

# ============================================================================================
# THE WITNESS: WAS *MY* RUN EXCLUSIVE? A separate question from "is the lock held", and the one
# this file could not answer.
#
# `--who` reports the lock's state AT AN INSTANT, deliberately (see its comment). That is the right
# answer to "may I start", and it is no answer at all to the question a session actually asks after
# a suite goes red in a file its diff never touched: *did anything else run alongside me?* Measured
# 2026-08-30 -- a locked full-suite run failed on `test_pr_queue_merges_what_it_opens.py`, a
# neighbour was running unwrapped in another worktree throughout, and `--who` could say only "HELD
# right now", which was true, useless, and about a different moment. The same suite passed with the
# box to itself. The attribution was recoverable only by walking /proc by hand afterwards.
#
# WHY THE ANSWER IS UNAMBIGUOUS RATHER THAN A HEURISTIC. While this script holds the lock, no other
# TAKER can be running -- that is what the lock guarantees. So any suite the probe sees during the
# held region is, by construction, a run that did not take the lock. There is no third case to
# distinguish and no judgement for the reader to make.
#
# CEILING, AND IT IS PRINTED RATHER THAN LEFT HERE: two samples, one at each end of the held
# region. A neighbour that starts AND finishes inside your run is missed. Sampling continuously
# would need a poller and a second process to reap, for a case that a minutes-long suite makes
# rare; the honest fix is to say what was measured. `EXCLUSIVE` therefore means "no unwrapped run
# was visible at either end", never "nothing else ran".
#
# THIS IS A DIAGNOSTIC, NOT ENFORCEMENT, AND THE DISTINCTION IS THE SAME ONE THE HEADER DRAWS
# ABOVE. It does not stop an unwrapped run -- a lock binds only whoever takes it, and the caller
# side (a guard in the consumer's harness) is where that is addressed, advisory by design. What
# this adds is the evidence to attribute a red, which nothing had.
# ============================================================================================

# One `<pid>\t<tree>` row per running suite. REUSES `running-suites` (the detector a harness
# guard can also use) rather than reimplementing it: `_running_suites()` has already been
# corrected once (a `uv run ... pytest` is two pids and was reported as two suites) and a
# second copy would drift from it in the direction that produces invisible waiting.
#
# `RESOURCE_LOCK_SUITE_PROBE` exists for the tests, which cannot spawn a real suite. It is an
# OVERRIDE and never an unset: a harness is on record that omitted a
# variable and reached production with a live credential, so the default here stays the real probe
# and `test_the_default_probe_is_the_real_command` asserts it.
# NON-ZERO ON A PROBE THAT COULD NOT RUN, never a silent empty. The first draft of this function
# ended both arms with `|| true`, which makes a missing `python3`, a moved hook or a typo'd
# override report exactly what a quiet box reports -- and the verdict built on it would have read
# `EXCLUSIVE`. That is the fail-safe-looks-like-success shape,
# written into the very tool whose job is to stop a green being trusted for the wrong reason.
# An empty result and a failed probe are different findings and the caller must be able to tell.
probe_suites() {
    if [ -n "${RESOURCE_LOCK_SUITE_PROBE:-}" ]; then
        sh -c "$RESOURCE_LOCK_SUITE_PROBE" 2>/dev/null || return 1
    else
        "$HERE/running-suites" 2>/dev/null || return 1
    fi
}

# Report the witness. `$1`/`$2` are the two samples, `$3` whether both probes actually ran.
# Anything in either sample is a run that did not take the lock.
report_exclusivity() {
    if [ "$3" -ne 1 ]; then
        echo "$SELF $res: COULD NOT MEASURE exclusivity -- the probe failed to run." >&2
        echo "$SELF   This is NOT 'nothing else was running'. Check:" >&2
        echo "$SELF     $HERE/running-suites" >&2
        return 0
    fi
    _seen=$(printf '%s\n%s\n' "$1" "$2" | sort -u | sed '/^$/d')
    if [ -z "$_seen" ]; then
        echo "$SELF $res: EXCLUSIVE for this run -- no unwrapped run seen at either end." >&2
        echo "$SELF   (two samples, start and end; one that began and ended inside your run is missed)" >&2
        return 0
    fi
    echo "$SELF $res: *** NOT EXCLUSIVE *** -- these did not take the lock and ran alongside you:" >&2
    printf '%s\n' "$_seen" | while IFS="$(printf '\t')" read -r _pid _tree; do
        echo "$SELF     pid $_pid in $_tree" >&2
    done
    echo "$SELF   A red in a file your diff never touched may be CONTENTION, not your change" >&2
    echo "$SELF   Re-run with the box to itself before believing it." >&2
    echo "$SELF   The lock binds only whoever takes it; this reports, it does not prevent." >&2
}

usage() {
    echo "usage: $0 <resource> [--wait SECONDS | --no-wait] -- <command...>" >&2
    echo "       (queues by default; nobody grants or releases this for you)" >&2
    echo "       $0 --list" >&2
    echo "       $0 --who <resource>" >&2
}

# Print the registry's description for a name, or nothing if unregistered.
describe() {
    printf '%s\n' "$RESOURCES" | while IFS='|' read -r _n _d; do
        [ "$_n" = "$1" ] && { printf '%s' "$_d"; return 0; }
    done
}

known() { [ -n "$(describe "$1")" ]; }

list_resources() {
    echo "$SELF registered resources:"
    printf '%s\n' "$RESOURCES" | while IFS='|' read -r _n _d; do
        [ -n "$_n" ] || continue
        printf '  %-14s %s\n' "$_n" "$_d"
    done
}

case "${1:-}" in
    ''|-h|--help) usage; exit 2 ;;
    --list) list_resources; exit 0 ;;
    --who)
        res="${2:?resource name}"
        known "$res" || { echo "$SELF unknown resource '$res'. $0 --list" >&2; exit 2; }
        lock="$LOCKDIR/$res.lock"
        # A REPORT, NOT A RESERVATION, and the distinction is this ticket's whole subject. By the
        # time you read this line the holder may have exited or a new one may have taken it.
        # Nothing here holds anything. Use the `-- <command>` form to actually hold it.
        if [ ! -e "$lock" ]; then
            echo "$SELF $res: never taken on this box since boot"
        elif ( flock -n 9 ) 9<"$lock" 2>/dev/null; then  # read-only fd: see the lock-dir block below
            echo "$SELF $res: free as of this instant (NOT reserved by asking)"
        else
            echo "$SELF $res: HELD right now (NOT reserved by asking)"
        fi
        # THE LOCK'S STATE IS NOT THE BOX'S STATE, and reporting only the former is how this verb
        # misled a reader. A run that never took the lock is invisible to `flock` and is exactly
        # the thing that makes a suite red, so it is named here beside the lock state.
        #
        # BUT THESE ARE NOT LABELLED "unwrapped", AND THE FIRST VERSION OF THIS BLOCK GOT THAT
        # WRONG. It printed "run(s) NOT holding this lock" and was caught doing so against a live
        # holder: a suite was running legitimately under `flock`, and `--who` named it as if
        # it had skipped the lock. The inference "anything the probe sees did not take the lock" is
        # sound ONLY while the asker HOLDS it, which is true in the `-- <command>` path and is
        # exactly what `--who` refuses to do. So here the honest report is the raw sighting, with
        # the holder explicitly not excluded -- a verb that reserves nothing cannot attribute
        # anything either.
        if [ "$res" = suite ]; then
            if ! _live=$(probe_suites); then
                echo "$SELF $res: could not probe for running suites -- NOT 'none are running'."
            elif [ -n "$_live" ]; then
                echo "$SELF $res: $(printf '%s\n' "$_live" | wc -l | tr -d ' ') suite run(s) visible on the box:"
                printf '%s\n' "$_live" | while IFS="$(printf '\t')" read -r _pid _tree; do
                    echo "$SELF     pid $_pid in $_tree"
                done
                echo "$SELF   ONE OF THESE MAY BE THE HOLDER above; this verb cannot tell which."
                echo "$SELF   To learn whether YOUR OWN run was exclusive, run it under this script:"
                echo "$SELF   the verdict it prints is measured from inside the held region."
            else
                echo "$SELF $res: no suite run visible on the box either"
            fi
        fi
        exit 0 ;;
esac

res="$1"; shift
known "$res" || {
    echo "$SELF unknown resource '$res' -- refusing." >&2
    echo "$SELF A lock on an unregistered name would SUCCEED and protect nothing, which is" >&2
    echo "$SELF indistinguishable from working. Register it in this script or fix the name." >&2
    list_resources >&2
    exit 2
}

# WAITING IS THE DEFAULT, AND THAT IS THE WHOLE POINT RATHER THAN A CONVENIENCE.
#
# The PR queue's drain is orchestrator-free because whoever arrives does the work when they can: no grant is
# issued, nobody is asked, and a dead holder does not strand the queue. This has to behave the same
# way or it does not fix the race, it relocates it.
#
# An immediate refusal LOOKS like the safe default and is not. A session told "held, try later" has
# to decide what later means, and the cheapest way for an agent to decide that is TO ASK ANOTHER
# SESSION -- which is the measurement-plus-message handoff this whole file exists to delete, walking
# back in through the error path. The fix has to be self-service on BOTH arms: taking the slot, and
# not getting it.
#
# So the blocked session simply blocks. The kernel wakes it the instant the holder's last descriptor
# closes -- including when that holder was killed by the 5h cap -- with nobody sequencing
# the handoff and no window for a waiter to slip into. That is `flock`'s queue doing the job a
# coordinator was doing badly.
#
# `--no-wait` stays for the caller that genuinely wants an answer rather than a slot: a status
# check, or a cheap run worth skipping entirely if the box is busy.
DEFAULT_WAIT=1800
wait_secs="$DEFAULT_WAIT"
case "${1:-}" in
    --wait)
        shift
        wait_secs="${1:?--wait needs SECONDS}"
        case "$wait_secs" in *[!0-9]*) echo "$SELF --wait takes whole seconds, got '$wait_secs'" >&2; exit 2 ;; esac
        shift ;;
    --no-wait)
        wait_secs=0
        shift ;;
esac

[ "${1:-}" = "--" ] || { echo "$SELF expected -- before the command" >&2; usage; exit 2; }
shift
[ $# -gt 0 ] || { echo "$SELF no command given after --" >&2; exit 2; }

# ONE LOCK DIR FOR EVERY USER ON THE BOX. The CI runner and the agent sessions are
# different users, and the lock binds nobody it cannot be opened by. So: a dir this script creates
# is 1777 (sticky, like /tmp) rather than the first caller's umask, the file is created 0644, and
# it is opened READ-ONLY -- `flock` needs no write access, and in a sticky dir the kernel's
# `protected_regular` refuses any O_CREAT open (`>`, `>>`) of a file another user owns, whatever
# its mode. A dir that already exists is not re-moded: only its owner could, and a dir the caller
# named is theirs to decide.
[ -d "$LOCKDIR" ] || { mkdir -p "$LOCKDIR" && chmod 1777 "$LOCKDIR"; } 2>/dev/null || :
lock="$LOCKDIR/$res.lock"
[ -e "$lock" ] || ( umask 022; : >>"$lock" ) 2>/dev/null || :
if [ ! -r "$lock" ]; then
    # Checked, not left to `exec 9<` failing: a failed redirection on `exec` exits dash on the
    # spot with a bare "cannot open", and this message is the one that says what to do.
    _what="$lock"; [ -e "$lock" ] || _what="$LOCKDIR"
    _owner=$(stat -c %U "$_what" 2>/dev/null || echo unknown)
    echo "$SELF cannot open $lock -- refusing rather than running unprotected." >&2
    echo "$SELF   $_what is owned by $_owner and not usable by $(id -un)." >&2
    echo "$SELF   fix, as $_owner or root: chmod 1777 $LOCKDIR (and a+r on any lock file in it)" >&2
    exit 2
fi

# ACQUIRE ON AN fd, RATHER THAN LETTING `flock` EXEC THE COMMAND. The previous form was
# `flock -E 2 <file> <command>`, which is shorter and gives no moment INSIDE the held region to
# stand in -- flock replaces itself with the command, so there is nowhere to take the witness
# sample from. Holding fd 9 puts this script inside its own lock, with the command as a child.
#
# THE PROPERTY THAT MUST SURVIVE THE REWRITE, and it does: the command still inherits the fd, so a
# killed wrapper whose command lives keeps the lock -- which is correct, the resource is still in
# use. `test_the_lock_survives_a_killed_wrapper_while_its_command_lives` pins exactly that and was
# fault-injected against this change rather than assumed to still hold.
#
# `-E 2` went with the exec form, so contention is mapped to 2 HERE instead. Same contract, same
# reason: without it flock exits 1 on contention, and 1 is the single most common exit code a real
# command produces -- "someone else holds the suite" would be indistinguishable from "the suite ran
# and failed", which is the reading this whole file exists to prevent.
exec 9<"$lock"

# SAY THAT YOU ARE QUEUEING, ONCE, BEFORE BLOCKING. A wait with no output is indistinguishable from
# a hang, and the reasonable response to an apparent hang is to kill it and ask someone -- which is
# the handoff again. One line on stderr costs nothing and makes the wait legible.
#
# The non-blocking attempt comes FIRST and is the acquisition, not a probe: on the fd form a
# successful `flock -n 9` has taken the lock. The old code probed a SECOND fd and threw it away,
# which was correct there and would be a race here.
rc=0
if flock -n 9 2>/dev/null; then
    :
elif [ "$wait_secs" -gt 0 ]; then
    echo "$SELF '$res' is held; waiting up to ${wait_secs}s. Nobody needs to grant it -- the kernel" >&2
    echo "$SELF hands it over the moment the holder's last descriptor closes, including a 5h-cap kill." >&2
    flock -w "$wait_secs" 9 2>/dev/null || rc=2
else
    rc=2
fi

if [ "$rc" -eq 0 ]; then
    # INSIDE the held region: anything the probe sees here did not take the lock.
    probes_ok=1
    before=""; after=""
    if [ "$res" = suite ]; then
        before=$(probe_suites) || probes_ok=0
    fi
    "$@" && rc=0 || rc=$?
    if [ "$res" = suite ]; then
        after=$(probe_suites) || probes_ok=0
        report_exclusivity "$before" "$after" "$probes_ok"
    fi
fi
if [ "$rc" -eq 2 ]; then
    echo "$SELF REFUSED: '$res' is held by another agent." >&2
    echo "$SELF   $(describe "$res")" >&2
    if [ "$wait_secs" -gt 0 ]; then
        echo "$SELF   Waited ${wait_secs}s and it did not free. That is long enough that the holder is" >&2
        echo "$SELF   likely stuck rather than busy: sh $0 --who $res, then look at the pid." >&2
        echo "$SELF   Do NOT ask another session to release it -- there is no release verb, by design." >&2
    else
        echo "$SELF   You passed --no-wait. Drop it to queue instead: $0 $res -- <command>" >&2
    fi
    echo "$SELF Nothing was run." >&2
fi
exit "$rc"
