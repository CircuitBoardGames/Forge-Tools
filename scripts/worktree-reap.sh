#!/bin/sh
# Remove worktrees whose owning session is gone. REFUSE and REPORT the dirty ones.
#
#   worktree-reap.sh              report only (the default; nothing is removed)
#   worktree-reap.sh --delete     remove the clean orphans
#
# A killed agent runs no cleanup, and the 5h cap kill is a measured event on this box, so
# the reaper cannot be the dying agent itself: the next agent through, or cron, invokes it.
#
# ============================================================================================
# OWNERSHIP IS CWD, NOT THE PATH NAME. An early design originally wanted the worktree path to
# encode the owning session, so orphan detection would be a filename comparison. It was rewritten
# (2026-08-25) because no such identity is available here — see `worktree-create.sh`'s header for
# the two candidates and the measurement that rules each out.
#
# So: a worktree is LIVE iff some process with `comm` in {claude, omp} has it as cwd. That signal is
#   - reassignment-proof, which is the defect the ticket measured. On 2026-08-24 three worktrees
#     named for a ticket were each holding a DIFFERENT ticket's branch within hours. Under a
#     name-keyed reaper that comparison answers a question nobody asked and can reap live work.
#     Under cwd it attributes correctly and the stale name is merely cosmetic.
#   - respawn-proof: `claude-session-recycle.sh` respawns with a new pid and the same cwd.
#
# WHY THIS IS NOT A PROCESS SEARCH. A process search matches its OWN shell through any
# literal on its command line — every Bash tool call runs as `bash -c '<the whole command text>'`,
# so `pgrep -f <pattern>` hits that wrapper before any target. `self-match-guard.py` exists for
# exactly this. Nothing below matches command text. It reads `comm` and the `cwd` symlink per pid,
# which is `agent_comms.py`'s doctrine ("from comm + cwd out of /proc, never by matching command
# text") and its enumeration, reused rather than rewritten.
#
# WHY NOT A CUSTODY GUARD'S `others()` DIRECTLY: it answers "are there OTHER agents in MY tree" — it
# excludes this process's own ancestry, which is right for a custody guard and wrong here, because
# the session running the reaper is itself a legitimate owner of its own worktree and must be
# reported LIVE, not reaped. The module's `AGENT_COMMS` and its `/proc` reading ARE reused, so the
# set of things that count as an agent has one definition.
#
# IT DOES NOW ASK "did this branch land?", AND THIS COMMENT USED TO SAY THE OPPOSITE. Until a measured
# incident the reasoning was: `git worktree remove` leaves the branch ref, so reaping never destroys a commit,
# so the question does not arise. The first half is still true and the conclusion was still wrong —
# what reaping destroys is the WORKING TREE and the session's place in it, and a measurement caught this
# tool marking three peers' worktrees reapable, one of them with an open PR. See the block above the
# verdict loop for the measurement and for why the dirty-refusal does not cover it.
#
# THE ANSWER IS STILL NOT IMPLEMENTED HERE. It comes from
# `prune-landed-branches-forgejo.sh`, which asks the FORGE — because a git
# reachability answer (`branch -d`, `--is-ancestor`, `--contains`, `main..branch`) is a false
# negative on any branch whose commits landed REWRITTEN: every branch squash-merged before merges kept their shas
# (permanently — `main` refuses force push), and any branch the queue rebased.
#
# THE FAILURE IS ONE-DIRECTIONAL, AND THAT IS SETTLED. "Not an ancestor" concludes
# NOTHING — the landing may have been rewritten. "IS an ancestor" is sound in EVERY era: a branch
# whose commits are all on `main` has no rewritten content for ancestry to be wrong about. So the
# forge is not a better instrument, it is the fallback for the inconclusive half; the git arm can
# and does resolve the common case locally. `prune-landed-branches-forgejo.sh` states this in its
# forge-oracle section and measured it on a real branch. Do not add a second answer here; that
# hook documents four measured fail-opens in that single query, and
# `scripts/tests/test_prune_merged_verb.py` is why its fixture still builds a rewritten landing.
#
# ONE EXCEPTION, AND IT IS A DIFFERENT QUESTION. The DETACHED arm below does use
# `--is-ancestor`, and the paragraph above is not being ignored. "Did this branch land" is a
# false negative under an old squash merge — that landed branch is an ancestor of nothing. "Is this
# CHECKOUT redundant" is not: a detached HEAD sitting on a former `main` commit genuinely IS in
# main's history, so everything the tree contains is on main by construction. Measured 2026-08-27
# on this box: four of five held trees resolved IN_MAIN, one resolved NOT_IN_MAIN, and the control
# `hub/main~1` resolved IN_MAIN — the instrument discriminates in both directions.
# The rule is the question, not the command: ancestry is wrong for "did it land" and right for
# "is it redundant". Nothing here reads it as an answer about a branch.
#
# NEVER `--force`. Git already implements the requirement: `git worktree remove` refuses a dirty
# worktree by default (`fatal: '<path>' contains modified or untracked files, use --force to delete
# it`). That refusal IS "refuse and report dirty ones" (a design premise). `--force`
# would destroy UNCOMMITTED work — never in the object store, unrecoverable by reflog or forge.
# There is no flag below that reaches it and there must not be one.
# ============================================================================================
set -eu

SELF="[worktree-reap]"
MODE=report
case "${1:-}" in
    '') : ;;
    --delete) MODE=delete ;;
    *) echo "usage: $0 [--delete]" >&2; exit 2 ;;
esac

git rev-parse --git-dir >/dev/null 2>&1 || { echo "$SELF not a git repo" >&2; exit 2; }

# The ref a detached checkout is judged redundant against: the forge remote's main (FORGE_TOOLS_REMOTE,
# via ft-config.sh beside this script). Overridable so a test can build a fixture repo without that
# remote; NOT fetched (see the detached arm for why staleness fails safe).
_ft_cfg="$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")/ft-config.sh"
[ -r "$_ft_cfg" ] || { echo "$SELF cannot read $_ft_cfg -- Forge-Tools' config reader must sit beside this script" >&2; exit 2; }
. "$_ft_cfg"
MAIN_REF="${WORKTREE_REAP_MAIN:-$FORGE_TOOLS_REMOTE/main}"

# In-progress rebase / bisect / merge / cherry-pick / revert. `git status --porcelain` does NOT show
# these — a mid-rebase tree with no conflicts is porcelain-clean — so the dirty refusal that protects
# every other arm does not protect this one. A detached HEAD is exactly what these leave behind, and
# a bisect sits on a HISTORICAL commit, which is in `$MAIN_REF` by definition and would otherwise
# read as maximally safe to delete. Prints the operation's name and exits 0 when one is running.
_operation_in_progress() {
    _gd=$(git -C "$1" rev-parse --absolute-git-dir 2>/dev/null) || return 1
    for _m in rebase-merge:rebase rebase-apply:rebase BISECT_LOG:bisect \
              MERGE_HEAD:merge CHERRY_PICK_HEAD:cherry-pick REVERT_HEAD:revert; do
        [ -e "$_gd/${_m%%:*}" ] && { printf '%s' "${_m#*:}"; return 0; }
    done
    return 1
}

# Real path: installed as a symlink on PATH, the siblings are next to the target, not the link.
HERE="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")" 2>/dev/null && pwd)" || HERE=""

# Live agent cwds, one absolute realpath per line. `agent_comms.AGENT_COMMS` is the source of which
# comms count; if that import fails we must NOT fall back to an empty list, because an empty list
# means "every worktree is an orphan" and this script's next move is deletion. Fail closed, loudly.
#
# `WORKTREE_REAP_PROC` redirects the scan at a fixture directory, exactly as `COSESSION_PROC` does,
# which is how this is tested without spawning real sessions.
live_cwds() {
    HERE="$HERE" python3 -c '
import os, sys

sys.path.insert(0, os.environ.get("HERE", ""))
try:
    from agent_comms import AGENT_COMMS, foreign
except Exception as e:
    # An empty agent set would make every worktree look orphaned, and the caller deletes those.
    # A guard whose degraded mode is "delete everything" must not have a degraded mode.
    print("ERROR cannot import agent_comms.AGENT_COMMS (%s) — refusing to guess which processes are agents" % e)
    raise SystemExit

proc = os.environ.get("WORKTREE_REAP_PROC") or "/proc"
try:
    entries = os.listdir(proc)
except OSError as e:
    print("ERROR cannot read %s (%s)" % (proc, e))
    raise SystemExit

found = set()
occupied = set()
scanned = 0
for e in entries:
    if not e.isdigit():
        continue
    try:
        with open(os.path.join(proc, e, "comm")) as f:
            comm = f.read().strip()
    except OSError:
        continue
    scanned += 1
    if comm not in AGENT_COMMS:
        # OCCUPANCY, kept apart from ownership. A full suite is `uv pytest python
        # python bash sh tail` and no agent at all: nine processes with cwd in a tree the ownership
        # test read as empty. Whose tree this is stays unanswerable; whether anything is
        # RUNNING in it is cheap, and it is the question that matters before `rm -rf`.
        # CEILING: a cwd owned by another user is unreadable (EACCES), and a non-agent that vanished between
        # listdir and here is not worth refusing on -- so this is "occupied as far as readable",
        # never "empty". Only an AGENT with an unreadable cwd refuses, below, as before.
        try:
            occupied.add("%s\t%s\t%s" % (
                os.path.realpath(os.readlink(os.path.join(proc, e, "cwd"))), e, comm))
        except OSError:
            pass
        continue
    try:
        cwd = os.path.realpath(os.readlink(os.path.join(proc, e, "cwd")))
    except OSError:
        if foreign(e, proc):
            continue    # another uid: not our session, and it cannot write our trees
        # A process that vanished between listdir and here, or one we may not read. Either way we
        # did not establish that it is NOT in a worktree, and the caller is about to delete things.
        print("ERROR pid %s has comm %r but its cwd is unreadable — refusing to call anything an "
              "orphan while an agent is unaccounted for" % (e, comm))
        raise SystemExit
    found.add("%s\t%s\t%s" % (cwd, e, comm))

if scanned == 0:
    # Every pid directory failed to yield a comm. That is a broken scan, not a quiet box, and the
    # two produce the same empty list.
    print("ERROR read %d pid entries from %s and got a comm from none of them — that is a broken "
          "scan, not an idle box" % (len(entries), proc))
    raise SystemExit

print("OK %d" % scanned)
for line in sorted(found):
    print(line)
print("--")
for line in sorted(occupied):
    print(line)
'
}

OUT=$(live_cwds 2>&1) || { echo "$SELF /proc scan failed: $OUT" >&2; exit 3; }
case "$OUT" in
    ERROR*) echo "$SELF ${OUT#ERROR }" >&2; echo "$SELF nothing was removed." >&2; exit 3 ;;
    OK\ *) : ;;
    *) echo "$SELF unreadable scan output — not reaping: $OUT" >&2; exit 3 ;;
esac
SCANNED=$(printf '%s\n' "$OUT" | head -n 1 | cut -d' ' -f2)
# Two lists from one scan, split at the `--` line: agent cwds (ownership), then every other
# readable process cwd (occupancy). They answer different questions and both are wanted.
AGENTS=$(printf '%s\n' "$OUT" | tail -n +2 | sed '/^--$/,$d')
OCCUPANTS=$(printf '%s\n' "$OUT" | sed '1,/^--$/d')

# The reference tree is the main worktree and is never a reap candidate: it holds the object store
# and `node_modules`, and under premise 10 nobody works in it, so "no agent has it as cwd" is its
# NORMAL state and would otherwise make it the first thing reaped.
REF=$(git worktree list --porcelain | sed -n '1s/^worktree //p')

# ============================================================================================
# WHAT THE OWNERSHIP TEST ESTABLISHED, and the reason every "unowned" below is hedged.
#
# The cwd model is sound for the pattern `worktree-create.sh` advises (`cd ../CC-x && claude`),
# where the session's cwd IS its worktree. It is INERT for a session that launched in the reference
# tree and reaches its worktree with `cd <wt> && …` per command: the Claude Code harness restores
# cwd afterwards ("Shell cwd was reset to …"), so the process cwd never moves. Such a session owns a
# worktree that this test can never match, and the reference tree is excluded from candidacy anyway.
#
# Measured 2026-08-28, three live sessions, all comm `claude`,
# all cwd the reference tree. The live set was empty for every worktree, and would have
# been however long any of them had been working.
#
# So an empty match has TWO causes that print identically: nobody is here, or everybody here is
# unlocatable. They differ in exactly one observable — whether any agent sits in the reference tree —
# and that is what is counted here, from the same lines the ownership match reads.
#
# A quiet box (no agents at all) is NOT this case: there the empty match is a real answer, and cron
# reaping keeps working. Only "agents exist and all of them are unlocatable" disarms the signal.
# ============================================================================================
UNLOCATABLE=0
if [ -n "$AGENTS" ]; then
    UNLOCATABLE=$(printf '%s\n' "$AGENTS" | while IFS="$(printf '\t')" read -r cwd pid comm; do
        [ "$cwd" = "$REF" ] && echo x
    done | wc -l | tr -d ' ')
fi

# The words every unowned verdict uses, so none of them has to imply more than was measured.
if [ "$UNLOCATABLE" -gt 0 ]; then
    OWNERSHIP="the ownership test matched nothing AND ESTABLISHED NOTHING — $UNLOCATABLE live agent(s) sit in the reference tree, owning worktrees cwd cannot locate"
else
    OWNERSHIP="no live agent has this tree as cwd, and no agent is unlocatable"
fi

# No per-verdict counters, deliberately. The verdict loop runs in a subshell (it is the right-hand
# side of a pipe), so anything counted there would be lost at the `done` and a summary built from it
# would read zero however much was reaped — a wrong number that looks like a calm run. Every verdict
# is one printed line instead; `| grep -c REAPED` counts them accurately and this file cannot lie.

# `git worktree list --porcelain` also emits `prunable <reason>` for a worktree whose directory is
# gone — a first-class prunable concept with a machine-readable reason. Those are metadata, not
# checkouts, so `git worktree prune` is what clears them; `gc.worktreePruneExpire` defaults to three
# months, far too slow here, so it is done explicitly rather than waited for.
PRUNABLE=$(git worktree list --porcelain | sed -n 's/^prunable //p' || true)

# ============================================================================================
# "NO LIVE PROCESS" IS NOT "WORK FINISHED", and this is the second question the reaper
# has to answer before it may delete anything.
#
# cwd-ownership (premise 11) answers *is a live process sitting here*. The reaper was reading that
# as *is this work done*, and **a session that ENDED with its work unfinished is indistinguishable
# from one that is DONE**. This tool is per-box: run from any worktree it sees every other agent's
# tree on the machine.
#
# Measured 2026-08-25, running the report from one session's tree, three peer worktrees came back
# ORPHAN / WOULD REMOVE — and their forge states were all different:
#
#   agent/95-client-currency    open PR #35, 2 commits ahead    -> must NOT be reaped
#   agent/318-child-label       merged PR                       -> genuinely finished
#   agent/319-label-filter      no PR at all, not on the remote  -> unfinished, and the worst case
#
# **The obvious option — scope `--delete` to the invoking tree's siblings — would have prevented
# NONE of this.** Every worktree on this box is a sibling of every other by construction
# (`worktree-create.sh` puts them all next to the reference tree), so a sibling filter is satisfied
# by exactly the trees it needs to exclude. Measured, not assumed.
#
# **And the dirty-refusal does not cover it either.** It protects uncommitted work, so the tidiest
# session — the one that committed before it ended — is the least protected. That protection got
# *weaker* when worktrees were made clean at birth: the accidental shield of an untracked symlink is
# gone, deliberately, because it was also what made worktrees unreapable for ever.
#
# SO THE DEFAULT INVERTS: reap only what the forge positively says is finished. Not-finished,
# no-PR-at-all, detached HEAD and cannot-reach-the-forge all REFUSE. Refusing when unsure is the
# correct behaviour for a tool whose mistake destroys someone else's working state.
#
# THE ORACLE IS NOT REIMPLEMENTED HERE. `prune-landed-branches-forgejo.sh --dry-run`
# already answers "did this branch land" against hub, and its header documents four measured
# fail-opens in that one query — `state=merged` returning everything, `head.ref` becoming
# `refs/pull/N/head` once the branch is deleted, a schema change reading as nothing-landed, and a
# one-page window aging out. A second implementation here would be a second thing to get all four
# wrong. It also already exits non-zero when it could not ask, which is exactly the fail-closed
# signal this needs. Its contract, verified by running it: `WOULD DELETE <branch>(<sha>)` for
# merged, `KEEP <branch> — ...` otherwise, rc 3 when hub is unreachable.
#
# THE REAPER ASKS NO REACHABILITY QUESTION ABOUT A BRANCH, and that is the whole of the claim.
# The hook asks the FORGE because `branch -d`, `--is-ancestor`, `--contains` and `main..branch`
# all answer reachability while being read as "did this land" — and the two diverge for any branch
# that landed REWRITTEN (every old squash merge, and anything the queue rebased). Only the NEGATIVE
# diverges: an ancestor of `main` has landed, in any era. The forge answers the half git cannot.
# Two things narrow that, both measured 2026-08-27 rather than reasoned: the DETACHED arm below
# uses `--is-ancestor` for a different question (see above), and the hook ITSELF already
# lands a branch on `no PR, but every commit is already on refs/remotes/hub/main` — so ancestry is
# not banned from this system, it is banned from answering "did this BRANCH land".
# ============================================================================================

# Pass 1: which worktrees are unowned? Collected before anything is judged or removed, because the
# forge is asked ONCE for the whole set rather than per worktree.
unowned=$(git worktree list --porcelain | sed -n 's/^worktree //p' | while IFS= read -r wt; do
    [ "$wt" = "$REF" ] && continue
    [ -d "$wt" ] || continue
    real=$(cd "$wt" 2>/dev/null && pwd -P) || real="$wt"
    if [ -n "$AGENTS" ]; then
        printf '%s\n' "$AGENTS" | while IFS="$(printf '\t')" read -r cwd pid comm; do
            case "$cwd" in "$real"|"$real"/*) echo "OWNED"; break ;; esac
        done | grep -q OWNED && continue
    fi
    printf '%s\t%s\n' "$wt" "$(git -C "$wt" branch --show-current 2>/dev/null)"
done)

# Ask the forge about every unowned worktree's branch, in one call. LANDED holds the branch names
# hub says are merged; anything not in it is refused below.
LANDED=""
FORGE_ERR=""
if [ -n "$unowned" ]; then
    branches=$(printf '%s\n' "$unowned" | cut -f2 | grep -v '^$' | sort -u)
    if [ -n "$branches" ]; then
        # shellcheck disable=SC2086
        # `WORKTREE_REAP_ORACLE` redirects this at a stub, the way `WORKTREE_REAP_PROC` redirects
        # the /proc scan — so the forge arms are testable without a forge, and the tests can drive
        # the one case that matters most (an OPEN PR) without opening one.
        ORACLE="${WORKTREE_REAP_ORACLE:-$HERE/prune-landed-branches-forgejo.sh}"
        # Unquoted on purpose: the split into words IS the branch list.
        # shellcheck disable=SC2086
        if out=$(sh "$ORACLE" --dry-run $branches 2>&1); then
            LANDED=$(printf '%s\n' "$out" | sed -n 's/^.*WOULD DELETE \([^(]*\)(.*$/\1/p')
        else
            FORGE_ERR="$out"
        fi
    fi
fi

git worktree list --porcelain | sed -n 's/^worktree //p' | while IFS= read -r wt; do
    [ "$wt" = "$REF" ] && continue
    [ -d "$wt" ] || continue   # gone from disk; `git worktree prune` below owns these

    real=$(cd "$wt" 2>/dev/null && pwd -P) || real="$wt"

    # LIVE if an agent's cwd is the worktree or anything under it. A custody guard's stated ceiling is
    # that cwd must EQUAL the toplevel, and it says to widen the comparison if that shows up in
    # practice. Here it must be widened: a session that `cd`s into `scripts/` still owns its
    # worktree, and under an equality test it would read as an orphan — with deletion on the other
    # side of that answer. Prefix match, on a path boundary so a sibling `CC-272-worktree-lifecycle2`
    # cannot satisfy `CC-272-worktree-lifecycle`.
    owner=""
    if [ -n "$AGENTS" ]; then
        owner=$(printf '%s\n' "$AGENTS" | while IFS="$(printf '\t')" read -r cwd pid comm; do
            case "$cwd" in
                "$real"|"$real"/*) echo "$comm:$pid"; break ;;
            esac
        done)
    fi

    if [ -n "$owner" ]; then
        echo "$SELF LIVE    $wt — $owner"
        continue
    fi

    branch=$(git -C "$wt" branch --show-current 2>/dev/null || echo '')

    # OCCUPIED: no agent owns it, but something is RUNNING in it. Measured 2026-08-30: a
    # full suite (uv, pytest, four pythons, bash, sh, tail) cwd'd in a tree whose PR was not yet
    # merged read HELD for the forge's reason, not this one; had the PR merged, the same nine
    # processes would have read ORPHAN and been removed from under the run. Occupancy is not
    # ownership -- a shell left behind is not an owner -- but it is not emptiness either, and
    # emptiness is the precondition for deletion. So this refuses the DESTRUCTIVE arm and says why,
    # and the forge question is not asked: landed or not, nothing is removed from under a process.
    # Subdirectories count, because that is where a suite actually runs.
    occupants=""
    if [ -n "$OCCUPANTS" ]; then
        occupants=$(printf '%s\n' "$OCCUPANTS" | while IFS="$(printf '\t')" read -r cwd pid comm; do
            case "$cwd" in
                "$real"|"$real"/*) printf '%s:%s ' "$comm" "$pid" ;;
            esac
        done)
    fi
    if [ -n "$occupants" ]; then
        echo "$SELF OCCUPIED $wt [${branch:-detached}] — no agent owns it, but live process(es) have it (or a subdirectory) as cwd: ${occupants% }. Not an owner; not empty either, and empty is the precondition for removal. NOT removed."
        continue
    fi

    # THE SECOND QUESTION. Unowned is necessary and not sufficient; hub must also say the work
    # landed. Every arm that is not a positive "merged" refuses, and says which arm it was —
    # "hub could not be reached" and "hub says this is still open" are different facts and only one
    # of them is about this branch.
    if [ -n "$FORGE_ERR" ]; then
        echo "$SELF HELD    $wt [${branch:-detached}] — no owner was located, and hub could not be asked whether its work landed, so it is NOT an orphan as far as anything here knows:"
        printf '%s\n' "$FORGE_ERR" | sed "s/^/$SELF         /"
        continue
    fi
    # A DETACHED HEAD HAS NO BRANCH TO ASK HUB ABOUT — AND DOES NOT NEED ONE WHEN ITS COMMIT IS
    # ALREADY IN `$MAIN_REF`. Everything the checkout contains is then on main by construction, so
    # there is nothing to lose whatever any PR says. That is a STRONGER guarantee than the branch
    # arm below accepts, which trusts the forge's word about a name.
    #
    # WHY THIS IS NOT THE PRUNER'S FALSE NEGATIVE. That one is real for a branch SQUASH-merged:
    # it is an ancestor of nothing and `--is-ancestor` wrongly reports it unlanded. The
    # question here is different — not "did this branch land" but "is this checkout redundant" — and
    # a detached HEAD sitting on a former `main` commit genuinely IS in main's history. Ancestry is
    # the wrong instrument for the first question and the right one for the second. Do not
    # generalise either way.
    #
    # A STALE `$MAIN_REF` FAILS SAFE: fewer trees qualify, never more. So this deliberately does not
    # fetch — a reaper that reached the network would make a routine report cost a round trip, and
    # the failure it would prevent is "we kept a tree one run longer".
    detached_reason=""
    if [ -z "$branch" ]; then
        if op=$(_operation_in_progress "$wt"); then
            echo "$SELF HELD    $wt [detached] — no owner was located, and a $op is IN PROGRESS here. A detached HEAD is what a session mid-rebase or mid-bisect leaves behind, and that state is not visible to \`git status --porcelain\`."
            continue
        fi
        if ! git -C "$wt" merge-base --is-ancestor HEAD "$MAIN_REF" 2>/dev/null; then
            # WHAT THIS MEASURED IS ANCESTRY, AND ANCESTRY IS NOT CONTENT. This arm used to
            # say the tree "holds commits that exist nowhere else". It does not know that. A commit
            # that landed under a NEW sha — every merge, every squash, every queue rebase — is never
            # an ancestor of `main`, so a pre-merge copy of fully-shipped work fails this test for
            # ever. Measured: `aeb06e8f5b` had tree `ca2cdea377`, identical to `327a4812c4` which
            # merged as a PR, and was held for a day as unique. Refusing is still right; the REASON
            # is "not an ancestor", and the reader must not be told it is "nowhere else".
            echo "$SELF HELD    $wt [detached] — $OWNERSHIP; and its HEAD is not an ancestor of $MAIN_REF. That is ANCESTRY, not content: work that landed under a new sha is never an ancestor, so this may be a pre-merge copy of something already shipped. Refusing rather than guessing."
            continue
        fi
        branch="detached"
        detached_reason="its HEAD is already in $MAIN_REF"
    fi
    if [ -z "$detached_reason" ] && ! printf '%s\n' "$LANDED" | grep -qxF "$branch"; then
        echo "$SELF HELD    $wt [$branch] — no owner was located, and hub lists NO merged PR for this branch, so the work is not finished. NOT an orphan; not removed."
        continue
    fi

    if [ "$MODE" = report ]; then
        # Say what WOULD happen, and say it differently for clean and dirty, because the two need
        # different action from the reader and a single "would reap" line hides the refusal.
        # The two arms reach "safe to remove" by DIFFERENT evidence, so they say different things.
        # "hub says its PR merged" is simply untrue of the detached arm, and a reader who acts on a
        # reason that was never measured is the failure this whole script is careful about.
        # NOT "hub says its PR merged": the oracle also lands a branch on "no PR, but every commit is
    # already on hub/main", and reporting a merged PR that never existed is a reason nobody
    # measured (the same defect exists one hook over). Say what the oracle is, not what it
    # might have found.
    why="the forge oracle says its work has landed"
        [ -n "$detached_reason" ] && why="$detached_reason"
        if [ -n "$(git -C "$wt" status --porcelain 2>&1)" ]; then
            echo "$SELF DIRTY   $wt [$branch] — $why, but has modified or untracked files; WOULD REFUSE (never --force)"
        else
            echo "$SELF ORPHAN  $wt [$branch] — $OWNERSHIP; tree clean, and $why; WOULD REMOVE (rerun with --delete)"
        fi
        continue
    fi

    # THE REMOVAL. The dirty check is git's, made by git, at the moment of removal — not a
    # `status --porcelain` this script read a moment earlier and then acted on. A status read before
    # the act is a status the act can invalidate; `git worktree remove` re-decides atomically, and
    # its refusal is the guarantee. That is why the report arm above may use `status` (it only
    # describes) and this arm may not.
    # THE SIGNAL THAT SAYS "THE SESSION IS GONE" IS THE ONE THAT CAN BE INERT. The forge
    # arm answers "did the work land", which is NOT "has everyone left" — a session that lands its PR
    # and keeps working in its tree passes the forge arm and is held only by ownership. When
    # ownership established nothing, deletion has no evidence for its precondition at all, and the
    # measured cost of proceeding anyway was a live peer's worktree removed out from under it.
    # Report mode above still says exactly what it would do; only the destructive arm refuses.
    if [ "$UNLOCATABLE" -gt 0 ]; then
        echo "$SELF REFUSED $wt [$branch] — $OWNERSHIP. The forge says this work landed, but landing is not leaving: nothing here measured whether a session is still using this tree. NOT removed."
        continue
    fi

    if err=$(git worktree remove "$wt" 2>&1); then
        echo "$SELF REAPED  $wt [$branch] — removed (branch ref untouched)"
    else
        echo "$SELF REFUSED $wt [$branch] — git would not remove it, and it was NOT forced:"
        printf '%s\n' "$err" | sed "s/^/$SELF         /"
    fi
done

if [ -n "$PRUNABLE" ]; then
    echo "$SELF prunable metadata for worktrees whose directory is gone:"
    git worktree prune --dry-run -v 2>&1 | sed "s/^/$SELF   /"
    if [ "$MODE" = delete ]; then
        git worktree prune -v 2>&1 | sed "s/^/$SELF   pruned: /"
    else
        echo "$SELF   (rerun with --delete to prune. gc.worktreePruneExpire defaults to 3 months, so waiting is not a plan.)"
    fi
fi

echo "$SELF scanned $SCANNED processes for an agent cwd; reference tree $REF was excluded."
if [ "$UNLOCATABLE" -gt 0 ]; then
    echo "$SELF $UNLOCATABLE live agent(s) have the reference tree as cwd. Those sessions own worktrees this test CANNOT locate, so no verdict above claims a tree is unowned, and --delete removes nothing."
fi
