#!/bin/sh
# doctor: one read-only verb for a confused agent.
#
#   doctor.sh            diagnose; changes nothing
#   doctor.sh --fix      also apply the two fixes that are individually safe (below)
#
# ONE VERB, NOT A TOOLBOX. An agent that is confused picks the wrong tool from a toolbox; running
# this is never the wrong move because by default it changes nothing. It COMPOSES the instruments
# this repo already has rather than re-deriving any diagnosis -- each one carries its own measured
# fail-opens and its own tests, and a second implementation would carry neither:
#   worktrees        scripts/worktree-reap.sh (report mode)      LIVE / ORPHAN / DIRTY / HELD / OCCUPIED
#                    scripts/worktree-create.sh --check <wt>     settings.local.json, node_modules wiring
#   queue            hub: /pulls?state=open (the derived order), /pulls/N/reviews, /issues/N/dependencies
#   divergence       hub: /pulls?state=open  vs  local branch shas
#                    scripts/pr-queue.sh prune-merged            dry run: merged-but-not-pruned, orphan remotes
#   mid-flight       the queue lock's recorded pid vs /proc; rebase-merge / rebase-apply per worktree
#
# EVERY SECTION ENDS IN ONE OF THREE WORDS, and silence is never one of them: findings are listed
# with `FINDING`, a section that could not be measured says `NOT CHECKED` and why, and only a
# section that ran and found nothing says `OK`. The exit code carries the same distinction --
#   0  every section checked, no findings
#   1  findings
#   2  no findings, but at least one section was NOT CHECKED
# so a green exit can never be produced by a section that measured nothing (verification.md:
# fail-safe is indistinguishable from success unless the degradation is loud).
#
# Every verdict names the command it read (`(read: …)` on the section header), because a derived
# verdict that does not say which output it read can only be audited by its author.
#
# `--fix` IS NARROW, AND THE LIST IS THE WHOLE POINT (premise 18): remove CLEAN orphan worktrees
# (`worktree-reap.sh --delete`, whose own refusals stand -- it never forces) and delete local
# branches HUB SAYS MERGED (`pr-queue.sh prune-merged --delete`). Dirty orphans, ahead-of-remote
# branches and in-progress rebases are reported and stop; a conflicting rebase is the
# `resolving-merge-conflicts` skill's job and this prints the pointer rather than reimplementing it.
#
# Every diagnosis here has its fault injected in scripts/tests/test_doctor.py -- seen reported,
# removed, seen clear -- because a doctor whose checks were never proven able to fail is a machine
# for confident all-clears, which is worse than no doctor (the ticket's own words).
#
# Environment (tests point every instrument at a stub; production uses the defaults):
#   DOCTOR_HUB       the checkout diagnosed    (FORGE_TOOLS_CHECKOUT, else the git top level of the cwd)
#   DOCTOR_API       hub-api.sh (default: this script's sibling)
#   DOCTOR_REPO      owner/repo                (FORGE_TOOLS_REPO, else FORGE_TOOLS_OWNER/<remote's name>)
#   DOCTOR_REMOTE    the forge remote          (FORGE_TOOLS_REMOTE, default origin)
#   DOCTOR_REAP      worktree-reap.sh    DOCTOR_CHECK worktree-create.sh    DOCTOR_PRUNE pr-queue.sh
#   PR_QUEUE_WT      the queue's scratch tree (default: every per-run $FORGE_TOOLS_MERGE_SCRATCH_PREFIX-<pid>)
#   DOCTOR_WORKTREES a file listing worktree paths, one per line    (default: `git worktree list`)
#   DOCTOR_FETCH=0   skip the `git fetch` before the divergence section (offline tests)
set -u

SELF="[doctor]"
# TWO DIRECTORIES, NOT ONE. The instruments are this script's SIBLINGS, found from
# its real path (a symlink on PATH resolved); the checkout they diagnose is the one you run it in.
# One `$HUB/scripts/` stood for both only while the tools lived inside the repo they diagnosed.
TOOLS_DIR=$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")
[ -r "$TOOLS_DIR/ft-config.sh" ] || { echo "$SELF cannot read $TOOLS_DIR/ft-config.sh -- Forge-Tools' config reader must sit beside this script" >&2; exit 2; }
. "$TOOLS_DIR/ft-config.sh"
HUB="${DOCTOR_HUB:-$(ft_checkout)}"
API="${DOCTOR_API:-$TOOLS_DIR/hub-api.sh}"
REPO="${DOCTOR_REPO:-}"
REAP="${DOCTOR_REAP:-$TOOLS_DIR/worktree-reap.sh}"
CHECK="${DOCTOR_CHECK:-$TOOLS_DIR/worktree-create.sh}"
PRUNE="${DOCTOR_PRUNE:-$TOOLS_DIR/pr-queue.sh}"
HELD_LABEL="${PR_QUEUE_HELD_LABEL:-queue:needs-human-review}"
NOT_ADMITTED_LABEL="${PR_QUEUE_NOT_ADMITTED_LABEL:-queue:not-admitted}"
HUB_REMOTE="${DOCTOR_REMOTE:-$FORGE_TOOLS_REMOTE}"

usage() { sed -n '2,5p' "$0" >&2; }
FIX=0
case "${1:-}" in
    --fix) FIX=1 ;;
    "") ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
esac
[ -n "$REPO" ] || { FORGE_TOOLS_REMOTE=$HUB_REMOTE FORGE_TOOLS_CHECKOUT=$HUB ft_repo; REPO=$FORGE_TOOLS_REPO; }

FINDINGS=0
UNCHECKED=0
say()       { printf '%s %s\n' "$SELF" "$*"; }
finding()   { FINDINGS=$((FINDINGS + 1)); say "FINDING      $*"; }
unchecked() { UNCHECKED=$((UNCHECKED + 1)); say "NOT CHECKED  $*"; }
section()   { say ""; say "== $1  (read: $2)"; }
# `verdict <before> <name>`: OK only if this section added no findings and no NOT CHECKED.
verdict()   { [ "$FINDINGS" -eq "$1" ] && [ "$UNCHECKED" -eq "$2" ] && say "OK           $3"; }

worktrees() {
    {
        if [ -n "${DOCTOR_WORKTREES:-}" ]; then
            cat "$DOCTOR_WORKTREES"
        else
            # Every worktree except the first entry, which is the main checkout (the reference tree).
            git -C "$HUB" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p' | sed '1d'
        fi
        # The queue's own worktrees, where an interrupted run leaves its rebase -- one per run since
        # the queue lock was retired. Usually in the list above already; named here so they are
        # scanned even when they are not. Unquoted on purpose: the default is a glob.
        for _q in ${PR_QUEUE_WT:-${PR_QUEUE_WT_BASE:-$FORGE_TOOLS_MERGE_SCRATCH_PREFIX}-*}; do
            [ -d "$_q" ] && printf '%s\n' "$_q"
        done
    } | sort -u
}

# ---------------------------------------------------------------- 1. worktrees ----------
f0=$FINDINGS; u0=$UNCHECKED
section "worktrees" "sh $REAP  (report mode); sh $CHECK --check <each>"
if [ ! -f "$REAP" ]; then
    unchecked "worktree-reap.sh is not at $REAP"
else
    rep=$(sh "$REAP" 2>&1) || true
    # The loop runs in a subshell (a pipeline), so it prints and the count is taken separately.
    printf '%s\n' "$rep" | sed -n 's/^\[worktree-reap\] \(LIVE\|ORPHAN\|DIRTY\|HELD\|OCCUPIED\|REFUSED\) *\(.*\)$/\1 \2/p' | while read -r kind rest; do
        case "$kind" in
            LIVE) ;;
            ORPHAN)   printf '%s\n' "$SELF FINDING      clean orphan worktree: $rest" ;;
            DIRTY|REFUSED) printf '%s\n' "$SELF FINDING      DIRTY orphan (reported, STOP -- unmerged work is what you would most regret automating away): $rest" ;;
            HELD)     printf '%s\n' "$SELF FINDING      held worktree (its work has not landed per hub): $rest" ;;
            OCCUPIED) printf '%s\n' "$SELF FINDING      occupied worktree (no agent owns it, a live process is in it): $rest" ;;
        esac
    done
    n=$(printf '%s\n' "$rep" | grep -c '^\[worktree-reap\] \(ORPHAN\|DIRTY\|HELD\|OCCUPIED\|REFUSED\)')
    [ -n "$n" ] || n=0
    FINDINGS=$((FINDINGS + n))
    # The reaper's own caveat is load-bearing: sessions launched in the reference tree own trees it
    # cannot locate, so no ORPHAN verdict above is proof of absence. Relay it verbatim.
    printf '%s\n' "$rep" | grep 'CANNOT locate' >/dev/null 2>&1 && say "CAVEAT       $(printf '%s\n' "$rep" | grep 'CANNOT locate' | head -1 | sed 's/^\[worktree-reap\] //')"
    if [ "$FIX" -eq 1 ] && [ "$n" -gt 0 ]; then
        say "FIX          sh $REAP --delete  (its refusals stand; dirty trees are never forced)"
        sh "$REAP" --delete 2>&1 | sed "s/^/$SELF   /"
    fi
fi
if [ ! -f "$CHECK" ]; then
    unchecked "worktree-create.sh is not at $CHECK, so no worktree's wiring was health-checked"
else
    for wt in $(worktrees); do
        [ -d "$wt" ] || continue
        out=$(sh "$CHECK" --check "$wt" 2>&1) || finding "worktree $wt fails its health check: $(printf '%s' "$out" | tr '\n' ';' | cut -c1-200)"
    done
fi
verdict "$f0" "$u0" "worktrees: every tree is owned or landed, and every tree passes its health check"

# ---------------------------------------------------------------- 2. queue --------------
f0=$FINDINGS; u0=$UNCHECKED
section "queue" "$API /pulls?state=open (the order), /pulls/N/reviews, /issues/N/dependencies"
# THE ORDER IS THE OPEN PRs, oldest first -- what `pr-queue.sh drain` derives. The
# hand-kept fence it replaced is gone. Read ONCE; the divergence section reuses it.
# ponytail: one page of 50, as this listing always was; page it the way `_derived_order` does if the
# queue ever passes 50.
open_json=$("$API" "/api/v1/repos/$REPO/pulls?state=open&limit=50" 2>/dev/null || true)
queued=$(printf '%s' "$open_json" | HELD="$HELD_LABEL" NA="$NOT_ADMITTED_LABEL" python3 -c '
import json, os, sys
try:
    ps = json.load(sys.stdin)
except ValueError:
    print("?"); sys.exit()
if not isinstance(ps, list):
    print("?"); sys.exit()
for p in sorted(ps, key=lambda p: p.get("number") or 0):
    names = {l.get("name") for l in p.get("labels") or []}
    print("%s:%s:%s" % (p.get("number"), "held" if os.environ["HELD"] in names else "-",
                        "suppressed" if os.environ["NA"] in names else "-"))
' 2>/dev/null)
if [ "$queued" = "?" ]; then
    unchecked "the open PR listing could not be read, so no queued PR was checked"
else
    nums=$(printf '%s\n' "$queued" | sed -n 's/:.*//p' | tr '\n' ' ')
    say "order: ${nums:-(no open PRs)}"
    edges=""
    for q in $queued; do
        n=${q%%:*}; flags=${q#*:}
        case "$flags" in
            held:*)
                approved=$("$API" "/api/v1/repos/$REPO/pulls/$n/reviews" 2>/dev/null | python3 -c '
import json, sys
try:
    rs = json.load(sys.stdin)
except ValueError:
    print("?"); sys.exit()
ok = [r.get("user", {}).get("login") for r in rs if isinstance(rs, list) and r.get("state") == "APPROVED" and not r.get("dismissed")] if isinstance(rs, list) else []
print(",".join(ok) if ok else "none")
' 2>/dev/null)
                case "$approved" in
                    "?") unchecked "queued #$n wears $HELD_LABEL but its reviews could not be read" ;;
                    none) say "held: #$n wears $HELD_LABEL and no approving review has landed (the hold is doing its job)" ;;
                    *) finding "queued #$n wears $HELD_LABEL but an APPROVED review by $approved already landed -- the hold is stale: pr-queue approve $n" ;;
                esac ;;
        esac
        case "$flags" in
            *:suppressed) say "gate-suppressed: #$n wears $NOT_ADMITTED_LABEL (never claims green until admitted)" ;;
        esac
        deps=$("$API" "/api/v1/repos/$REPO/issues/$n/dependencies" 2>/dev/null | python3 -c '
import json, sys
try:
    ds = json.load(sys.stdin)
except ValueError:
    print("?"); sys.exit()
if not isinstance(ds, list):
    print("?"); sys.exit()
for d in ds:
    print("%s:%s" % (d.get("number"), d.get("state")))
' 2>/dev/null)
        if [ "$deps" = "?" ]; then
            unchecked "queued #$n: its dependency edges could not be read (anything unreadable is treated as blocking by the frontier; here it is reported)"
        else
            for d in $deps; do
                m=${d%%:*}; st=${d##*:}
                [ "$st" = "open" ] || finding "queued #$n is blocked by #$m, which is $st -- a dead edge; drop it or the drain waits for ever"
                edges="$edges $n>$m"
            done
        fi
    done
    cyc=$(printf '%s' "$edges" | python3 -c '
import sys
edges = [e.split(">") for e in sys.stdin.read().split() if ">" in e]
g = {}
for a, b in edges:
    g.setdefault(a, set()).add(b)
seen, stack = set(), set()
def visit(n):
    if n in stack: return True
    if n in seen: return False
    seen.add(n); stack.add(n)
    if any(visit(m) for m in g.get(n, ())): return True
    stack.discard(n); return False
print("cycle" if any(visit(n) for n in list(g)) else "acyclic")
')
    [ "$cyc" = "cycle" ] && finding "the dependency edges among queued PRs form a CYCLE -- nothing in it can ever be first; a human must cut one edge"
fi
verdict "$f0" "$u0" "queue: every open PR holds only while unapproved, and its edges point at open PRs with no cycle"

# ---------------------------------------------------------------- 3. divergence ---------
f0=$FINDINGS; u0=$UNCHECKED
section "forge/local divergence" "$API /pulls?state=open vs git rev-parse <branch>; sh $PRUNE prune-merged (dry run)"
if [ "${DOCTOR_FETCH:-1}" = "1" ]; then
    git -C "$HUB" fetch -q "$HUB_REMOTE" 2>/dev/null || unchecked "git fetch $HUB_REMOTE failed in $HUB, so the remote-tracking refs below are a cache of unknown age"
fi
open_prs=$(printf '%s' "$open_json" | python3 -c '
import json, sys
try:
    ps = json.load(sys.stdin)
except ValueError:
    print("?"); sys.exit()
if not isinstance(ps, list):
    print("?"); sys.exit()
for p in ps:
    print("%s %s %s" % (p.get("number"), (p.get("head") or {}).get("ref"), (p.get("head") or {}).get("sha")))
' 2>/dev/null)
if [ "$open_prs" = "?" ]; then
    unchecked "the open PR listing could not be read, so no local branch was compared to its PR head"
else
    printf '%s\n' "$open_prs" | while read -r num ref sha; do
        [ -n "$num" ] || continue
        local_sha=$(git -C "$HUB" rev-parse -q --verify "refs/heads/$ref" 2>/dev/null) || continue
        [ "$local_sha" = "$sha" ] && continue
        if ! git -C "$HUB" cat-file -e "$sha^{commit}" 2>/dev/null; then
            # The PR head is a commit this tree has never seen: the queue rebased it, or it was
            # pushed from elsewhere. No ahead/behind count is honest without it.
            printf '%s\n' "$SELF FINDING      PR #$num's head $(printf '%.10s' "$sha") is NOT IN THIS TREE (local $ref is $(printf '%.10s' "$local_sha")) -- the queue rebased it, or it was pushed elsewhere; check-runs on it are about a head this tree does not have. Fetch before trusting any verdict"
            continue
        fi
        ahead=$(git -C "$HUB" rev-list --count "$sha..$local_sha" 2>/dev/null || echo "?")
        behind=$(git -C "$HUB" rev-list --count "$local_sha..$sha" 2>/dev/null || echo "?")
        if [ "$ahead" != "0" ]; then
            printf '%s\n' "$SELF FINDING      local branch $ref is $ahead commit(s) AHEAD of PR #$num's head ($(printf '%.10s' "$sha")) -- reported, STOP: the PR does not carry that work until you push it"
        else
            printf '%s\n' "$SELF FINDING      PR #$num's head moved $behind commit(s) past local $ref (the queue rebased it?) -- check-runs on it are about a head this tree does not have; fetch before trusting any verdict"
        fi
    done > "${TMPDIR:-/tmp}/doctor.div.$$"
    cat "${TMPDIR:-/tmp}/doctor.div.$$"
    n=$(grep -c . "${TMPDIR:-/tmp}/doctor.div.$$" 2>/dev/null)
    [ -n "$n" ] || n=0
    FINDINGS=$((FINDINGS + n))
    rm -f "${TMPDIR:-/tmp}/doctor.div.$$"
fi
if [ ! -f "$PRUNE" ]; then
    unchecked "pr-queue.sh is not at $PRUNE, so merged-but-unpruned branches were not looked for"
else
    prune_out=$(PR_QUEUE_HUB="$HUB" sh "$PRUNE" prune-merged 2>&1) || true
    printf '%s\n' "$prune_out" | grep -q 'behind [^ ]*/main\|REFUSING' && unchecked "prune-merged refused: $(printf '%s\n' "$prune_out" | grep 'behind [^ ]*/main\|REFUSING' | head -1 | cut -c1-160)"
    n=$(printf '%s\n' "$prune_out" | grep -c 'WOULD DELETE\|ORPHAN ')
    [ -n "$n" ] || n=0
    printf '%s\n' "$prune_out" | grep 'WOULD DELETE\|ORPHAN ' | sed "s/^/$SELF FINDING      merged per hub, not pruned: /" | cut -c1-200
    FINDINGS=$((FINDINGS + n))
    if [ "$FIX" -eq 1 ] && [ "$n" -gt 0 ]; then
        say "FIX          sh $PRUNE prune-merged --delete  (deletes only what hub says merged)"
        PR_QUEUE_HUB="$HUB" sh "$PRUNE" prune-merged --delete 2>&1 | sed "s/^/$SELF   /"
    fi
fi
verdict "$f0" "$u0" "divergence: every local branch with an open PR matches its PR head, and nothing merged is left unpruned"

# ---------------------------------------------------------------- 4. mid-flight ---------
f0=$FINDINGS; u0=$UNCHECKED
section "mid-flight" "git rev-parse --git-path rebase-merge|rebase-apply per worktree"
for wt in $(worktrees); do
    [ -d "$wt" ] || continue
    for kind in rebase-merge rebase-apply; do
        p=$(git -C "$wt" rev-parse --git-path "$kind" 2>/dev/null) || continue
        case "$p" in /*) ;; *) p="$wt/$p" ;; esac
        [ -d "$p" ] && finding "rebase IN PROGRESS in $wt ($kind) -- a conflicting rebase is the resolving-merge-conflicts skill's job; nothing here touches it"
    done
done
# Not counted as NOT CHECKED: there is no mechanism to check. Batch assembly was unbuilt when this was written, so
# an orphan integration branch cannot exist yet; the line is here so its absence is a statement.
say "integration branches: nothing to diagnose until batch assembly is built"
verdict "$f0" "$u0" "mid-flight: no rebase in progress"

# ---------------------------------------------------------------- summary ---------------
say ""
say "SUMMARY      findings=$FINDINGS not-checked=$UNCHECKED fix=$FIX"
if [ "$FINDINGS" -gt 0 ]; then exit 1; fi
if [ "$UNCHECKED" -gt 0 ]; then exit 2; fi
exit 0
