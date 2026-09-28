#!/bin/sh
# Serial PR admission for a forge repo — one PR in flight, so no PR is ever rebased after CI.
#
# THE FACT THIS RESTS ON, measured 2026-08-20 against .forgejo/workflows/*.yml:
# NOTHING triggers on a push to a feature branch. The CI workflows (test, lint, fallow,
# skillspector) trigger on `pull_request` only -- since 2026-09-15 no workflow has a
# `push:` trigger at all, because nothing runs after a merge (operator).
# So pushing a branch is FREE; OPENING A PR is what spends a CI run.
# Do not maintain that list by hand -- test_pr_queue_premise.py is parametrized over the
# whole directory, so a new workflow is covered the moment it lands, named here or not.
#
# Therefore: let branches pile up, and admit them one at a time. Each branch is rebased
# onto main BEFORE its PR exists, so the PR is born up to date, never trips
# `block_on_outdated_branch`, and gets exactly ONE CI run.
#
#   all-open (today):   N runs, then merge 1 -> N-1 stale -> rebase+rerun -> ...
#                       worst case N(N+1)/2 runs and N-1 force-pushes. N=4 -> 10 runs.
#   serial admission:   exactly N runs, zero force-pushes, zero stale-green traps.
#
# It is not slower in wall-clock: merges were ALREADY serial (only one branch can be
# up to date at a time). This removes the wasted runs, not the ordering.
#
# Usage:  pr-queue.sh <<'EOF'
#         branch-name<TAB>PR title
#         branch-name<TAB>PR title<TAB>review      (open it, prove it green, then STOP for a human)
#         ...
#         EOF
#         pr-queue.sh approve <N> [<N>...]                 (merge PRs the queue held for review, in order, one claim)
#         pr-queue.sh drain [--dry-run] [--batch]          (land every READY PR from the
#                                                           oldest open PR downward;
#                                                           --batch assembles the ready ones into ONE
#                                                           integration PR and lands it by fast-forward,
#                                                           halving on red)
#                                                           (no flag: ready PRs at the front land
#                                                           serially, whoever holds the lock; waiting
#                                                           DRAFTS land as ONE batch, a lone one
#                                                           un-drafted first. Order:
#                                                           the open PRs, oldest first. A PR marked
#                                                           queue:serial never batches: it lands
#                                                           alone at the front.)
#         pr-queue.sh merge-requested                      (merge every open PR labelled
#                                                           `queue:merge-requested` -- intent
#                                                           recorded on the forge, merged here)
#         pr-queue.sh prune-merged [--delete] [branch...]   (see the `prune-merged` block below)
#         pr-queue.sh merge-paths [N]                       (which path the last N merges on main
#                                                           took -- a REPORT)
#         pr-queue.sh freeze "<reason>" ["<until>"]         (stop every queue merge, from every
#                                                           box, until `thaw`; see the freeze block)
#         pr-queue.sh thaw
#
# THE `review` FLAG EXISTS SO THAT NEEDING A HUMAN DOES NOT MEAN LEAVING THE QUEUE. Before it,
# a PR the operator wanted to see had to be opened by hand -- which skips the rebase-before-open
# that makes a PR born up to date, so it raced whatever the queue was doing and paid a rebase and
# a second CI run. Measured 2026-08-21: two PRs were opened outside the queue for exactly
# that reason and the second took a 405, a rebase and a full re-run. The flag keeps the cheap path and
# moves only the MERGE decision to a human.
# Branches must already be pushed. Nothing here force-pushes: a queued branch is rebased
# before it has a PR, so its history is not yet anything anyone is reading.
#
# --------------------------------------------------------------------------------------
# THE COORDINATION PROTOCOL, AGREED BETWEEN TWO AGENT SESSIONS 2026-08-21 AND WRITTEN DOWN
# HERE BECAUSE A PROTOCOL THAT LIVES IN A CHAT LOG BINDS NOBODY WHO DID NOT READ THE CHAT.
#
# Both collisions that produced it were the same shape: a merge landed under an already-open
# PR, leaving it outdated. Neither was caused by a queue RUN.
#
#   1. ONE QUEUE OPERATOR AT A TIME -- retired 2026-09-16 as a `flock`, REINSTATED 2026-09-22 as a
#      claim (`_claim_queue`). The retirement rested on "draft-first keeps the runway to
#      one PR", which holds only for ONE operator: the front is decided per drain, so two drains keep
#      two fronts. Measured 2026-09-22: two live drains put two PRs in two batches at once,
#      then each split its red batch and un-drafted a lone member -- two non-draft PRs gating at once.
#   2. HAND OVER LINES, NOT RUNS. A session with a branch ready sends the operator
#      `branch<TAB>title[<TAB>review]`; it does not run its own queue.
#   3. REVIEW-FLAGGED LINES LAST in a batch. The first hold ends the run (exit 9), so a
#      flagged line at the front strands every unflagged one behind it for no benefit.
#   4. DISTINCT WORKTREES STOP TWO RUNS DELETING EACH OTHER'S CHECKOUT and do nothing about the
#      runway. Since point 1's retirement the default tree is per-run for exactly that reason, and
#      the runway is draft-first's job; `PR_QUEUE_WT` remains the test seam.
#   5. A SESSION ANNOUNCES BEFORE IT MERGES, not only before running -- merging is the moment
#      `main` moves, and moving `main` is what outdates someone else's PR. Scoped to sessions
#      deliberately; see 5c for why that scope is the honest one.
#   5b. ANNOUNCE, THEN WAIT for an ack, or for an explicit "nothing of mine is open" already
#      on record. Measured: an announcement sent seconds before the merge gives no window in
#      which "say so now and I will hold" can be exercised, which makes it a changelog rather
#      than a coordination point.
#   5c. SILENCE IS NOT A MEASUREMENT. No session may treat the ABSENCE of an announcement as
#      evidence that `main` has not moved. Measured 2026-08-21, hours after 1-5b were written:
#      a PR was merged from the WEB UI at 21:59:31 by an operator, with no session in the loop
#      and nothing to announce it. Points 1-5b bind sessions; the merge path does not go
#      through a session, so an unannounced merge is not a breach of the protocol, it is
#      OUTSIDE it. Before relying on a base, re-measure:
#
#          git fetch hub -q && git merge-base --is-ancestor hub/main <FETCHED head>
#
# 5c IS THE ONLY ONE OF THE FIVE THAT PROTECTS UNILATERALLY, which is why it is worth more than
# the rest combined. Everything above is cooperative and therefore only as good as the other
# party's agreement; 5c is defensive and holds against someone who never agreed to any of it.
# The whole set was written as though the queue and its two sessions were the only ways `main`
# moves. That was false when it was written and neither session noticed, because until that
# evening every merge had happened to route through one of them.
#
# ONLY POINT 1 WAS MECHANISED, and the split is deliberate rather than lazy: a script neither
# session runs at announce time cannot enforce an announcement. 5c needs no mechanism here --
# `merge_queued`'s `is-ancestor` pre-check below IS 5c, applied to the one moment that matters.
# --------------------------------------------------------------------------------------
set -u
# SITE CONFIGURATION: scripts/ft-config.sh, beside this script's REAL path.
_ft_cfg="$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")/ft-config.sh"
[ -r "$_ft_cfg" ] || { echo "pr-queue: cannot read $_ft_cfg -- Forge-Tools' config reader must sit beside this script" >&2; exit 1; }
. "$_ft_cfg"
# The consumer checkout the queue runs in: PR_QUEUE_HUB (the test seam), else FORGE_TOOLS_CHECKOUT,
# else the cwd's git top level.
HUB="${PR_QUEUE_HUB:-$(ft_checkout)}"
# ONE SCRATCH TREE PER RUN, NOT ONE FOR THE BOX. There is no queue lock any more (operator,
# 2026-09-16: `pr create` opens every PR behind the front as a draft, so the runway no longer needs
# one operator), and a fixed path shared by two runs is how one `remove --force` deletes the other's
# checkout mid-rebase. Keyed on this run's pid, so two runs never name the same tree.
WT_BASE="${PR_QUEUE_WT_BASE:-$FORGE_TOOLS_MERGE_SCRATCH_PREFIX}"
WT="${PR_QUEUE_WT:-$WT_BASE-$$}"
# `--repo <owner/repo>` AS THE FIRST ARGUMENT drains or approves on a TARGET repo (operator
# 2026-09-08: the queue is the default landing path everywhere, a hand `pr merge` is the fallback).
# The CODE that runs stays this hub checkout's -- the stale-checkout refusal below still compares
# $HUB against hub/main -- and the lock, the derived order and every fetch/push name the target.
# PR_QUEUE_REPO alone (no flag) keeps the forge remote (FORGE_TOOLS_REMOTE): the test seam, unchanged.
# FT_REMOTE is always that remote's NAME -- the one $HUB's own currency is measured against -- while
# REMOTE is where the target's refs are fetched and pushed, a URL under `--repo`.
FT_REMOTE=$FORGE_TOOLS_REMOTE
REMOTE="${PR_QUEUE_REMOTE:-$FT_REMOTE}"
# This script's own absolute path and the --repo it was given, taken BEFORE the `cd "$HUB"` below,
# so hand_on_to_drain can re-enter it for the same target from wherever it was launched.
SELF=$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")
# FORGE-TOOLS' OWN FILES ARE FOUND HERE, NEVER UNDER $HUB. $HUB is the CONSUMER
# repo the queue operates on; the pruner, gate-watch, agent_comms and the client are siblings of
# this script, and `readlink -f` above makes that true through a symlink on PATH.
SELF_DIR=$(dirname "$SELF")
# Overridable ONLY so a test can stand stubs in for those siblings, as PR_QUEUE_API does for the
# client. `forge.sh` is NOT behind it: it is the one probe, and a stub dir would silently lack it.
TOOLS_DIR="${PR_QUEUE_TOOLS_DIR:-$SELF_DIR}"
REPO_ARG=""
# WHERE TICKETS LIVE IS NOT WHERE THE PR LIVES. Captured HERE because the `--repo` branch
# below overwrites `PR_QUEUE_REPO`, and this needs the value as the caller set it.
_TICKET_REPO_ENV="${PR_QUEUE_REPO:-}"
if [ "${1:-}" = "--repo" ]; then
    case "${2:-}" in
        */*/*|*" "*|/*|*/) echo "pr-queue: --repo needs <owner/repo>, got '${2:-}'" >&2; exit 2 ;;
        ?*/?*) ;;
        *) echo "pr-queue: --repo needs <owner/repo>" >&2; exit 2 ;;
    esac
    PR_QUEUE_REPO=$2
    REPO_ARG=$2
    # THE TARGET'S URL IS THIS CHECKOUT'S OWN FORGE-REMOTE URL WITH <owner/repo> SWAPPED. It was hardcoded
    # `git@forge-ssh:<repo>.git`, an ssh alias one host had and its successor did not: the
    # post-merge branch retirement pushes there with stderr discarded, so a target PR landed and
    # its branch silently stayed (measured 2026-09-15). Deriving it follows whatever this box uses.
    if [ -n "${PR_QUEUE_REMOTE:-}" ]; then
        REMOTE=$PR_QUEUE_REMOTE
    else
        _hub_url=$(git -C "$HUB" remote get-url "$FT_REMOTE" 2>/dev/null) || _hub_url=""
        case "$_hub_url" in
            *://*/*/*|/*/*/*) REMOTE="${_hub_url%/*/*}/$2.git" ;;   # https://host/o/r.git, /path/o/r.git
            ?*:*/*) REMOTE="${_hub_url%%:*}:$2.git" ;;              # user@host:o/r.git
            *) echo "pr-queue: --repo $2: $HUB has no \`$FT_REMOTE\` remote (FORGE_TOOLS_REMOTE) to derive the target's URL from -- set PR_QUEUE_REMOTE" >&2; exit 2 ;;
        esac
    fi
    shift 2
fi
# No literal default: FORGE_TOOLS_REPO, else FORGE_TOOLS_OWNER + the remote's name.
[ -n "${PR_QUEUE_REPO:-}" ] || { FORGE_TOOLS_CHECKOUT="$HUB" ft_repo; PR_QUEUE_REPO=$FORGE_TOOLS_REPO; }
REPO=$PR_QUEUE_REPO
# THE REPO TICKET NUMBERS RESOLVE AGAINST, and it is NOT `REPO`.
#
# Issues all live on the hub repo; target repos carry CI configs and PRs and no tickets of their
# own. Measured 2026-09-22: a target's `issues?state=all` returned five items and every one was a
# `pull_request`. So every `#N` in a body is a HUB number wherever the PR sits, and reading it out
# of `REPO` is reading the wrong repo the moment `--repo` is used.
#
# FOUND ON THE FIRST LIVE CROSS-REPO RUN, draining a target's PR: `ticket #N unreadable: 'state'` --
# the target's `issues/N` answers `The target couldn't be found`, a payload with no `state` key,
# while the hub's #N exists and is closed. A correct lookup prints nothing; the wrong one cannot run
# at all on a target repo. `refuse_if_closes_wayfinder` had the same bug, failing open with a NOT
# RUN line, which is why it went unnoticed -- one wrong definition, two callers.
#
# THE NO-FLAG SEAM IS PRESERVED DELIBERATELY: `PR_QUEUE_REPO` alone (no `--repo`) means "this IS the
# hub for this run", which is what every test stub relies on, so tickets resolve there too. Only the
# flag splits the two.
TICKET_REPO="${PR_QUEUE_TICKET_REPO:-${_TICKET_REPO_ENV:-}}"
[ -n "$TICKET_REPO" ] || { FORGE_TOOLS_CHECKOUT="$HUB" ft_repo; TICKET_REPO=$FORGE_TOOLS_REPO; }
# WHAT THIS RUN FETCHES LANDS IN PRIVATE REFS, NEVER READ BACK FROM FETCH_HEAD. FETCH_HEAD belongs to
# the checkout, and any other `git fetch` there overwrites it between our fetch and our read --
# `hub-api.sh` fetches hub/main on every call, so a client call running beside the queue in $HUB made
# `pr_head` read the hub's main as a target PR's head: a false "head MOVED" stop (2026-09-15). The per-repo lock keeps two runs off the same refs.
QREF="refs/pr-queue/$(printf '%s' "$REPO" | tr '/' '_')"
API="${PR_QUEUE_API:-$TOOLS_DIR/hub-api.sh}"
# THE BRANCH THIS REPO'S PRs LAND ON. A fork carries its patches on its DEFAULT branch, which need
# not be `main`: one fork's is `fix/quiet-...` and another's is `<org>/0.9.2`, with plain upstream
# on `main`. Hardcoding `main` made
# `--repo` measure freshness against upstream. Read once, only for `--repo`: the hub is `main` and
# stays byte-identical. An unreadable answer REFUSES -- defaulting to `main` is the bug itself.
# `PR_QUEUE_BASE` overrides, the same test seam as PR_QUEUE_REMOTE.
BASE_BRANCH="${PR_QUEUE_BASE:-main}"
if [ -n "$REPO_ARG" ] && [ -z "${PR_QUEUE_BASE:-}" ]; then
    BASE_BRANCH=$("$API" "/api/v1/repos/$REPO" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["default_branch"])' 2>/dev/null) || BASE_BRANCH=""
    case "$BASE_BRANCH" in
        ''|*[!A-Za-z0-9._/-]*|-*)
            echo "pr-queue: --repo $REPO: cannot read its default branch (got '$BASE_BRANCH') -- refusing rather than assume main" >&2
            exit 2 ;;
    esac
fi
cd "$HUB" || exit 1
log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$1"; }

# Non-empty lines in $1. `sed`+`wc` rather than `grep -c`: this box's `grep` is a shell function
# over ugrep that silently drops matching lines, and the CI runner has no `busybox` to
# fall back to -- so the one construct that is identical in both places is the one to use here.
# `tr` strips the leading blanks some `wc` implementations emit, which would otherwise reach `-gt`.
_count_lines() { printf '%s\n' "$1" | sed '/^$/d' | wc -l | tr -d ' \n'; }

# ABOVE THE VERB DISPATCH ON PURPOSE. This sat below `prune-merged` and `approve` until
# 2026-08-21, so it covered only the queue path -- the two verbs exec'd from a stale tree
# without ever reaching it. MEASURED the same evening, an hour after the guard shipped:
# `pr-queue.sh prune-merged` with PR_QUEUE_HUB unset exec'd the SHARED tree's copy of
# prune-landed-branches-forgejo.sh, ten commits behind, and printed an older verdict
# `hub lists NO merged PR with this head branch` -- an overclaim already removed upstream.
# `grep -c 'closed PRs searched'` was 1 in the current tree and 0 in the one that ran.
# Nothing was mis-deleted only because every candidate happened to be inside the old
# 50-PR window; an older branch would have been silently kept.
# A STALE CHECKOUT RUNS A STALE QUEUE, AND THE QUEUE CANNOT TELL FROM THE INSIDE.
#
# MEASURED 2026-08-21, and this guard exists because of it. `$HUB` was 4 commits behind
# `hub/main`, so `scripts/pr-queue.sh` there PREDATED the `review` flag -- `grep -c review` on
# that copy returned 0. Its two-field `read` folded the third field into the title, and with no
# hold logic it MERGED a PR that was flagged for a human, landing the subject
# `...rather than dropped<TAB>review (#N)` on `main`. `main` refuses force push, so both the
# unreviewed merge and the mangled subject are permanent.
#
# Nothing detected it: an old script does not know a newer one exists, and every line it printed
# was true of the queue it WAS. The tree is the proxy the script can actually check -- if the
# checkout is behind, the file being executed may be too.
#
# THREE EXIT CODES, NOT TWO. `merge-base --is-ancestor` answers yes(0), no(1), and COULD NOT
# TELL(>1). Collapsing the third into either is a known defect shape, where a
# transient git fault printed a confident explanation it had never established. Here an
# unanswerable question refuses, like every other "could not ask" path in this file.
git fetch "$FT_REMOTE" -q 2>/dev/null
if git rev-parse --verify -q "$FT_REMOTE/main" >/dev/null; then
    git merge-base --is-ancestor "$FT_REMOTE/main" HEAD 2>/dev/null
    case $? in
        0) : ;;                                    # up to date (or ahead) -- the normal case
        1) log "REFUSING: $HUB is behind $FT_REMOTE/main, so this script may predate the queue's own"
           log "  features -- that is how a PR was merged past a review hold. Pull, then re-run."
           exit 2 ;;
        *) log "REFUSING: cannot tell whether $HUB is current with $FT_REMOTE/main (git could not answer)"
           exit 2 ;;
    esac
fi

# --------------------------------------------------------------------------------------
# THE QUEUE FREEZE.
#
#   pr-queue.sh freeze "<reason>" ["<until>"]   create the wiki page "Queue Freeze", read it back
#   pr-queue.sh thaw                            delete it, and confirm it is gone from the listing
#
# While the page exists `approve`, `drain`, `merge-requested` and the stdin path refuse, printing
# its text. `merge_queued` and `try_batch` check again just before they merge, so a freeze set while
# a run waited on a gate still stops that run's next merge -- a refused batch leaves its integration
# PR STANDING, members drafted, for the next drain after `thaw` to resume. `hub-api.sh pr
# merge` checks too, so a hand merge refuses unless HUB_API_FREEZE_OVERRIDE names a reason.
#
# WHY A FORGE PAGE AND NOT A LOCK. A lock lives in one machine's /tmp, so it does not
# serialise two boxes: measured 2026-09-10, sessions on two different hosts both
# had `approve` in flight, and only a manual yield kept two landings apart. A wiki page is read
# through the same client from every box, and it is the only way to stop the queue at all -- a
# forge migration freezes it ahead of the cutover.
#
# WHY FIRST. `freeze` must work while another run is mid-drain -- that run is what a freeze exists
# to stop -- so both verbs dispatch before anything a run does.
#
# THREE OUTCOMES, NOT TWO, for the reason handoff.sh's sub_url_for_title gives: an unread
# wiki listing looks exactly like an empty one, and "not frozen" drawn from it merges during the one
# window this exists to protect. That function reaches its client at a fixed path, so PR_QUEUE_API's
# stub cannot drive it; this is its shape through the queue's own client.
#   0 = frozen, page text on stdout   1 = listing read, page not in it   2 = cannot tell
#
# ponytail: `_freeze_sub` reads ONE listing page. The wiki held 4 pages on 2026-09-11, so a freeze
# past page one is unreachable today; `hub-api.sh pr merge` pages its own read and re-checks every
# merge this script makes, so a freeze missed here is still refused there. Page it here if the wiki
# ever outgrows a page.
# --------------------------------------------------------------------------------------
FREEZE_PAGE="Queue Freeze"

_freeze_sub() {
    # A never-initialised wiki answers non-2xx with exactly this not-found body (measured on
    # a fresh fork): that one reads as "page not in it"; any other failure stays CANNOT TELL. The same
    # predicate lives in `hub-api.sh pr merge` -- change both together.
    _fz_pages=$("$API" "/api/v1/repos/$REPO/wiki/pages" -w '\n%{http_code}' 2>/dev/null) && _fz_rc=0 || _fz_rc=$?
    printf '%s' "$_fz_pages" | python3 -c '
import json, re, sys
raw = sys.stdin.read()
body, _, code = raw.rpartition("\n")
if not re.fullmatch(r"\d{3}", code):
    body, code = raw, ("200" if sys.argv[2] == "0" else "")   # a client that printed no status line
try:
    pages = json.loads(body)
except ValueError:
    sys.exit(2)          # not JSON -- an error page, a proxy, an empty read. NOT an absence.
if not code.startswith("2"):
    uninit = code == "404" and isinstance(pages, dict) and pages.get("message") == "The target couldn'"'"'t be found." \
        and "no such file or directory" in (pages.get("errors") or [])
    sys.exit(1 if uninit else 2)
if not isinstance(pages, list):
    sys.exit(2)          # an error object. Also not an absence.
for p in pages:
    if isinstance(p, dict) and p.get("title") == sys.argv[1]:
        sys.stdout.write(p.get("sub_url") or "")
        sys.exit(0)
sys.exit(1)
' "$FREEZE_PAGE" "$_fz_rc"
}

freeze_state() {
    _fz_sub=$(_freeze_sub) || return $?
    [ -n "$_fz_sub" ] || return 2
    _fz_page=$("$API" "/api/v1/repos/$REPO/wiki/page/$_fz_sub" 2>/dev/null) || return 2
    printf '%s' "$_fz_page" | python3 -c '
import base64, json, sys
try:
    sys.stdout.write(base64.b64decode(json.loads(sys.stdin.read())["content_base64"]).decode("utf-8", "replace"))
except (ValueError, KeyError, TypeError):
    sys.exit(2)
' || return 2
}

refuse_if_frozen() {
    _fz_text=$(freeze_state); _fz_rc=$?
    case "$_fz_rc" in
        1) return 0 ;;
        0) log "REFUSING: $1 -- the queue is FROZEN: wiki page \"$FREEZE_PAGE\" exists on $REPO."
           printf '%s\n' "$_fz_text" | sed 's/^/  | /'
           log "  Nothing merges while it exists. Lift it with: pr-queue thaw"
           exit 2 ;;
        *) log "REFUSING: $1 -- cannot tell whether the queue is frozen: $REPO's wiki could not be read."
           log "  An unread listing is not an empty one, so this is NOT 'not frozen'."
           exit 2 ;;
    esac
}

# refuse_if_closes_wayfinder <number> -- Returns 1 when landing PR <number> would CLOSE an
# open wayfinder ticket as a side effect of the merge.
#
# MEASURED 2026-09-15 on a scratch repo: the forge closes a ticket for a close keyword beside its
# number in the PR BODY (on a fast-forward merge too, 2s after it) or in a landed commit message --
# and it reads the keyword and the number only, so a NEGATED or NARRATED sentence closes it as well
# (the cases documented in close_ref.py). A `(#N)` subject only cross-references. A
# wayfinder ticket closed that way leaves `issue frontier` without the resolution its map needs,
# and nothing says so.
#
# THE PATTERN IS `close_ref.py`'S, imported rather than copied: a commit-subject hook can
# import the same module to deny the shape in the text of an agent's command, and cannot see a body
# passed from a file, another harness or a web edit -- which is why the landing checks it too. One
# definition, so the two cannot drift.
#
# FAILS OPEN, LOUDLY. An unreadable PR, commit list or ticket proceeds with a NOT RUN line, as the
# merge client does for bodies: a close is repairable (reopen), a merge blocked by a read is not
# what this exists to cause. `PR_QUEUE_ALLOW_CLOSES="<N> ..."` lands a close that is meant.
refuse_if_closes_wayfinder() {
    _cw=$(API="$API" REPO="$REPO" TICKET_REPO="$TICKET_REPO" N="$1" ALLOW="${PR_QUEUE_ALLOW_CLOSES:-}" \
        CLOSE_REF_DIR="$(dirname "$SELF")" python3 -c '
import json, os, subprocess, sys
def api(path):
    return json.loads(subprocess.run([os.environ["API"], path], capture_output=True, text=True).stdout)
repo, n = os.environ["REPO"], os.environ["N"]
tickets = os.environ["TICKET_REPO"]          # tickets live on the hub, not on the PR repo
try:
    sys.path.insert(0, os.environ["CLOSE_REF_DIR"])
    from close_ref import CLOSE_REF
    pr = api("/api/v1/repos/%s/pulls/%s" % (repo, n))
    commits = api("/api/v1/repos/%s/pulls/%s/commits" % (repo, n))
    if not isinstance(pr, dict) or not isinstance(commits, list):
        raise ValueError("PR or commit list is not the expected shape")
except Exception as e:
    print("PR #%s unreadable: %s" % (n, e)); sys.exit(3)
texts = [("PR title", pr.get("title") or ""), ("PR body", pr.get("body") or "")]
texts += [("commit " + (c.get("sha") or "")[:10], (c.get("commit") or {}).get("message") or "")
          for c in commits]
refs = {}
for where, text in texts:
    for m in CLOSE_REF.finditer(text):
        refs.setdefault(m.group(1)[1:], where)
allow, found = set(os.environ["ALLOW"].split()), 0
for num in sorted(refs, key=int):
    if num in allow or num == n:
        continue
    try:
        issue = api("/api/v1/repos/%s/issues/%s" % (tickets, num))
        state = issue["state"]
    except Exception as e:
        print("ticket #%s unreadable in %s: %s" % (num, tickets, e)); sys.exit(3)
    labels = [l.get("name", "") for l in issue.get("labels") or []]
    if state == "open" and not issue.get("pull_request") and any(x.startswith("wayfinder") for x in labels):
        print("#%s (%s), named in the %s" % (num, ", ".join(labels), refs[num])); found = 1
sys.exit(found)
' 2>&1); _cwrc=$?
    case "$_cwrc" in
        0) return 0 ;;
        1) log "REFUSING to merge #$1: the merge would CLOSE open wayfinder ticket(s):"
           printf '%s\n' "$_cw" | while read -r _l; do log "    $_l"; done
           log "  A close keyword beside a number in the PR body or a landed commit closes it -- negated or"
           log "  narrated too -- and the ticket leaves issue frontier without its resolution. Reword to"
           log "  'Part of' plus the number and close it with 'issue resolve'; if the close is meant, re-run"
           log "  with PR_QUEUE_ALLOW_CLOSES=\"<number> ...\"."
           return 1 ;;
        *) log "  close-keyword check NOT RUN for #$1: $(printf '%s' "$_cw" | tail -n 1)"
           log "  -- proceeding; this is not 'closes nothing'"
           return 0 ;;
    esac
}

# name_open_wayfinder_tasks <number> -- after PR <number> LANDS, name the wayfinder tasks it
# references that are still open, because nothing else will.
#
# THE GAP IS THE SHAPE OF THE FIX ABOVE. `refuse_if_closes_wayfinder` correctly refuses a body that
# would close a wayfinder ticket, so the close must be a deliberate `issue resolve` -- and NOTHING
# PROMPTS FOR IT. Landing the code is not the last step, and a finished task sits on `issue frontier`
# indistinguishable from one nobody has started. Measured three times: two tickets were found
# already-fixed and open; a third's handoff called it outstanding eight lines above its own section
# describing this very pattern. Three for three says the knowledge was never the missing part.
#
# WHY NOT FOLDED INTO THE SCAN ABOVE, since both read the same references: different TIME, different
# VERDICT, and a different reference SET. That one runs before the merge and REFUSES; this one runs
# after and can only inform. That one matches a close KEYWORD beside a number; this one matches every
# `#N`, because `Part of #N` is exactly the shape that does not close and does need resolving.
#
# MAPS ARE EXCLUDED BY LABEL, not by guesswork: a map carries `wayfinder:map` and a task does not
# (measured 2026-09-22 on two maps and two tasks). Without that, every PR
# saying `Part of #<map>` would be told to resolve the map it belongs to.
#
# A SUPERSET, AND IT SAYS SO. A PR that cites related open tickets as context gets them listed too.
# Narrowing that means guessing which reference is "the" one, so it prompts and lets the reader
# decide rather than pretending to know. The map number comes off the ticket's own `wayfinder:map-N`
# label so the printed command is runnable, not a template.
#
# NEVER FAILS THE MERGE -- always returns 0. The merge has already happened when this runs; a read
# error here must print a NOT RUN line and nothing else. An unreadable answer is not "nothing open".
name_open_wayfinder_tasks() {
    _nw=$(API="$API" REPO="$REPO" TICKET_REPO="$TICKET_REPO" N="$1" python3 -c '
import json, os, re, subprocess, sys
def api(path):
    return json.loads(subprocess.run([os.environ["API"], path], capture_output=True, text=True).stdout)
repo, n = os.environ["REPO"], os.environ["N"]
tickets = os.environ["TICKET_REPO"]          # a `#N` in a body is a HUB number
try:
    pr = api("/api/v1/repos/%s/pulls/%s" % (repo, n))
    commits = api("/api/v1/repos/%s/pulls/%s/commits" % (repo, n))
    if not isinstance(pr, dict) or not isinstance(commits, list):
        raise ValueError("PR or commit list is not the expected shape")
except Exception as e:
    print("PR #%s unreadable: %s" % (n, e)); sys.exit(3)
texts = [pr.get("title") or "", pr.get("body") or ""]
texts += [(c.get("commit") or {}).get("message") or "" for c in commits]
# SUBJECT-SHAPED REFERENCES ONLY. A bare `#N` in prose is a citation, and since every number
# resolves against the hub, a PR number from ANOTHER repo ("draining <target> #13") resolves to a
# real hub ticket. These two patterns are the conventions of THIS repo for "this change
# is about N": the `(#N)` subject cite that a hub-api merge requires, and the `Part of`/`Implements`
# markers `issue child-create` and every PR body here already use. So this is not a guess about which
# reference is the subject -- it reads the convention the queue enforces elsewhere.
# `refs` IS A SUBJECT MARKER HERE, MEASURED -- a peer read it as a citation shape and proposed
# dropping it, and the numbers say otherwise. Over the last 400 commits on main: `Part of` 13,
# `Refs` 5, `Closes` 1, and `Implements`/`Fixes`/`Resolves` 0 (so part of this set is aspirational,
# which is harmless). All five `Refs` lines name their OWN commit ticket -- the subjects are near
# verbatim the ticket titles (a `Refs #N` on a subject that was exactly the title of that ticket, and
# likewise for the other four) -- and NONE of
# those five carries a `(#N)` subject cite, so `Refs` is their only subject marker. Dropping it would
# UNDER-report all five, which is the direction this check must never fail in.
#
# ONLY THE FIRST NUMBER AFTER A MARKER IS TAKEN, and that is deliberate rather than accidental. One
# line is `Refs #A #B`: the first is the subject, the second a sibling. Matching every number
# after the marker would re-admit exactly the citation this ticket removes, so the regex stops at
# one -- measured right in 5 of 5, not an arbitrary pick among members.
MARKER = re.compile(r"\b(?:part of|implements|closes|fixes|resolves|refs)\s+#(\d+)\b", re.I)
CITE = re.compile(r"\(#(\d+)\)")
refs = {m.group(1) for t in texts for pat in (MARKER, CITE) for m in pat.finditer(t)}
refs = sorted(refs - {n}, key=int)
found = 0
for num in refs:
    try:
        issue = api("/api/v1/repos/%s/issues/%s" % (tickets, num))
        state = issue["state"]
    except Exception as e:
        print("ticket #%s unreadable in %s: %s" % (num, tickets, e)); sys.exit(3)
    labels = [l.get("name", "") for l in issue.get("labels") or []]
    if state != "open" or issue.get("pull_request"):
        continue
    if not any(x.startswith("wayfinder") for x in labels) or "wayfinder:map" in labels:
        continue
    maps = [x[len("wayfinder:map-"):] for x in labels if x.startswith("wayfinder:map-")]
    print("%s\t%s\t%s" % (num, maps[0] if len(maps) == 1 else "<map#>", issue.get("title") or ""))
    found = 1
sys.exit(found)
' 2>&1); _nwrc=$?
    case "$_nwrc" in
        0) return 0 ;;
        1) log "  #$1 landed and these wayfinder task(s) it names are STILL OPEN -- a merge cannot"
           log "  close them, so nothing but a person will. If this PR finished one:"
           printf '%s\n' "$_nw" | while IFS="$(printf '\t')" read -r _nwn _nwmap _nwtitle; do
               log "    #$_nwn  $_nwtitle"
               log "      hub-api issue resolve $TICKET_REPO $_nwn $_nwmap \"<what landed, and how it was verified>\""
           done
           log "  (the map number is off each ticket's own wayfinder:map-N label, so those lines run"
           log "   as printed. Listed because this PR names them as its SUBJECT -- a (#N) cite or a"
           log "   Part of/Implements/Closes reference; a bare #N in prose is a citation and is NOT"
           log "   listed. Still not a claim about which one it finished.)"
           return 0 ;;
        *) log "  open-task check NOT RUN for #$1: $(printf '%s' "$_nw" | tail -n 1)"
           log "  -- this is not 'nothing left open'"
           return 0 ;;
    esac
}

if [ "${1:-}" = "freeze" ]; then
    shift
    _fz_reason=${1:-}
    { [ -n "$_fz_reason" ] && [ "$#" -le 2 ]; } || { log "usage: pr-queue freeze \"<reason>\" [\"<until>\"]"; exit 2; }
    _fz_until=${2:-"until someone runs pr-queue thaw"}
    _fz_text=$(freeze_state); _fz_rc=$?
    case "$_fz_rc" in
        1) : ;;
        0) log "ALREADY FROZEN -- not overwriting the freeze that stands:"
           printf '%s\n' "$_fz_text" | sed 's/^/  | /'
           exit 1 ;;
        *) log "REFUSING: cannot read $REPO's wiki, so cannot tell whether a freeze already stands."
           exit 2 ;;
    esac
    _fz_body=$(python3 -c '
import base64, datetime, json, sys
page, reason, until, who = sys.argv[1:5]
text = ("**The PR queue is FROZEN.** `pr-queue approve`, `drain` and `merge-requested` refuse "
        "while this page exists.\n\n"
        "- Why: %s\n- Until: %s\n- Frozen by: %s\n- Frozen at: %s\n\n"
        "Lift it with `pr-queue thaw`, which deletes this page and confirms it is gone.\n"
        % (reason, until, who, datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
print(json.dumps({"title": page, "content_base64": base64.b64encode(text.encode()).decode(),
                  "message": "queue: freeze"}))
' "$FREEZE_PAGE" "$_fz_reason" "$_fz_until" "session ${FORGE_TOOLS_SESSION_ID:-not recorded} on $(hostname 2>/dev/null || echo 'an unnamed host')")
    printf '%s' "$_fz_body" | "$API" "/api/v1/repos/$REPO/wiki/new" -X POST --json @- >/dev/null 2>&1 || {
        log "REFUSING: creating \"$FREEZE_PAGE\" failed -- the queue is NOT frozen."; exit 1; }
    # THE READ-BACK IS THE ASSERTION: a write's own response is not evidence it applied.
    _fz_text=$(freeze_state); _fz_rc=$?
    case "$_fz_rc:$_fz_text" in
        0:*"$_fz_reason"*)
            log "FROZEN -- \"$FREEZE_PAGE\" created on $REPO and read back:"
            printf '%s\n' "$_fz_text" | sed 's/^/  | /'
            exit 0 ;;
        *)  log "the create was accepted, but \"$FREEZE_PAGE\" does not read back with that reason (rc=$_fz_rc)."
            log "  Treat the queue as NOT frozen, and look at the wiki."
            exit 1 ;;
    esac
fi

if [ "${1:-}" = "thaw" ]; then
    shift
    [ "$#" -eq 0 ] || { log "thaw takes no arguments"; exit 2; }
    _fz_sub=$(_freeze_sub); _fz_rc=$?
    case "$_fz_rc" in
        1) log "NOT FROZEN -- $REPO's wiki has no \"$FREEZE_PAGE\" page. Nothing to thaw."; exit 0 ;;
        0) [ -n "$_fz_sub" ] || { log "REFUSING: \"$FREEZE_PAGE\" is listed with no address -- cannot delete it."; exit 2; } ;;
        *) log "REFUSING: cannot read $REPO's wiki -- the freeze may or may not stand."; exit 2 ;;
    esac
    "$API" "/api/v1/repos/$REPO/wiki/page/$_fz_sub" -X DELETE >/dev/null 2>&1 || {
        log "REFUSING: DELETE of \"$FREEZE_PAGE\" failed -- the queue is STILL FROZEN."; exit 1; }
    # Believed only when the LISTING no longer shows it -- the listing is what refuse_if_frozen reads.
    _freeze_sub >/dev/null; _fz_rc=$?
    case "$_fz_rc" in
        1) log "THAWED -- \"$FREEZE_PAGE\" deleted, and absent from $REPO's wiki listing."; exit 0 ;;
        0) log "the DELETE was accepted but \"$FREEZE_PAGE\" is STILL LISTED -- treat the queue as frozen."; exit 1 ;;
        *) log "the DELETE was accepted, but the listing could not be re-read -- the thaw is NOT confirmed."; exit 1 ;;
    esac
fi

# The two real clocks in this script, overridable for the same reason PR_QUEUE_WT is: the test
# suite drives every arm below through a stub forge, and four of its tests sat in `sleep 10` for
# 10.7-12.5 s each (46 s of a 433 s gate, measured 2026-09-05) waiting on a rebase that a stub
# answers instantly. Production keeps the defaults; the tests set both to 0.
#   REWAIT_SECS       CEILING on how long to wait for the forge to replay the branch after
#                     `update?style=rebase`; the wait itself is on the HEAD SHA CHANGING, polled
#                     every REWAIT_POLL_SECS. It used to be a bare `sleep 10`.
#   POLL_SECS         between check-run polls while waiting for green. 10, not 20: measured over
#                     110 PRs merged 2026-09-01..03, green-to-merge latency was median 48 s
#                     (p25 32, p75 94), and a 20 s poll is up to 20 s of that for a one-line
#                     change against a gate that is now 44-190 s. One `pr checks` per
#                     10 s per drain is one process's load on the forge, not a fleet's.
REWAIT_SECS="${PR_QUEUE_REWAIT_SECS:-30}"
REWAIT_POLL_SECS="${PR_QUEUE_REWAIT_POLL_SECS:-2}"
POLL_SECS="${PR_QUEUE_POLL_SECS:-10}"
# How long a gate may stay unsettled before the wait gives up: 180 x 10s = 30 min. The runner has
# capacity 1, so a run can sit "Waiting to run" behind other PRs' pushes for a long time while
# measuring nothing -- 2026-09-08, one batch waited 14 min without a single job starting.
WAIT_POLLS="${PR_QUEUE_WAIT_POLLS:-180}"
# The draft marker. Set HERE, with the other config, because `approve` dispatches
# before the batch section below runs: assigned there, it was unset under `set -u` on the serial
# path and `approve_one` died instead of refusing (measured live, 2026-09-15).
DRAFT_PREFIX="WIP: "

# reap_dead_trees -- a DEAD RUN'S SCRATCH TREE IS REAPED BY THE NEXT RUN THAT ADDS ONE. The fixed path
# used to be recycled by that `add`; a per-run path is not, so an early exit that keeps its tree (4,
# 5, 6 in the admission loop) would otherwise leave it for ever. Only a tree whose pid is no longer
# running is touched, and only on the default path -- a caller that named PR_QUEUE_WT owns its tree.
# Called just before each `worktree add`, never at startup: read-only verbs have no business here.
reap_dead_trees() {
    [ -z "${PR_QUEUE_WT:-}" ] || return 0  # a caller-named tree is the caller's; nothing of ours to reap
    for _old in "$WT_BASE"-*; do
        [ -d "$_old" ] || continue
        _pid=${_old##*-}
        case "$_pid" in ''|*[!0-9]*) continue ;; esac
        kill -0 "$_pid" 2>/dev/null && continue
        git -C "$HUB" worktree remove --force "$_old" 2>/dev/null && log "reaped a dead run's scratch tree: $_old"
    done
    return 0
}

# prune-merged -- the OTHER end of a PR's life, and the one git answers wrongly.
#
#   pr-queue.sh prune-merged [branch...]            dry run: what hub says landed
#   pr-queue.sh prune-merged --delete [branch...]   delete the ones it says landed
#
# A branch squash-merged (the old landing style) has its commits on `main` under a different sha and is an
# ancestor of nothing. Every ergonomic git question -- `merge-base --is-ancestor`, `branch -r
# --contains`, `main..branch`, `branch -d` -- therefore answers "not merged" about work that IS
# merged. `branch -d` refusing is the dangerous one, because the obvious next keystroke is `-D`:
# force-deleting on an UNANSWERED question rather than an answered one. The authority is the forge:
# `pulls?state=closed` filtered on `merged`, which was available the whole time.
# NOW `main` TAKES TWO-PARENT MERGE COMMITS, so a branch merged unrewritten after it IS an
# ancestor. The divergence is one-directional: "not an ancestor" concludes nothing, because
# the landing may have been rewritten, while "is an ancestor" is sound in every era. The forge
# answers the inconclusive half -- it is the fallback, not a strictly better instrument.
#
# NO SECOND IMPLEMENTATION. `prune-landed-branches-forgejo.sh` already makes exactly
# that call, around three measured Forgejo fail-opens (`state=merged` is not a filter; `head.ref`
# becomes `refs/pull/N/head` once the branch is gone; a renamed `merged` key reads as
# nothing-landed). It was only unreachable -- it fired as a PostToolUse hook and nowhere else. This
# verb gives it a name to be typed. DRY RUN unless `--delete` is passed, because the verb deletes
# branches and a wrong default here is unrecoverable in the direction nobody checks.
if [ "${1:-}" = "prune-merged" ]; then
    shift
    PRUNE="$TOOLS_DIR/prune-landed-branches-forgejo.sh"
    [ -x "$PRUNE" ] || { log "missing $PRUNE"; exit 1; }
    case "${1:-}" in
        # PRUNE_REPO carries the TARGET: the pruner otherwise derives its repo from the
        # `hub` remote of $HUB, which under `--repo` is still the hub while the branch is not.
        --delete) shift; exec env PRUNE_REPO="$REPO" sh "$PRUNE" --delete "$@" ;;
        *)                exec env PRUNE_REPO="$REPO" sh "$PRUNE" --dry-run "$@" ;;
    esac
fi

# merge-paths -- which path each recent merge on main took, read from the Merge-Path trailer written
# at merge time. The queue's guarantees -- the review hold, branch auto-retire, hold-labelling, the
# head CAS -- cover its own merges and not the repo; roughly a third of recent merges were measured
# outside it, and DETECTION, not prevention, was asked for (prevention was offered and declined). A REPORT, exit 0 always: a gate here would refuse a legitimate
# hand-merge during an incident, the one moment the queue is most likely to be unavailable.
#
#   pr-queue.sh merge-paths [N]     classify the last N first-parent merges on hub/main (default 60)
#
# Four buckets, and the fourth is honest rather than fitted: `pr-queue` and `hub-api-direct` are
# the trailer; `web-ui` is NO trailer plus Forgejo's own subject template, the one partition
# history can decide; `unknown` is no trailer and no template -- everything before
# 2026-09-02, and the pre-migration GitHub merges, which must not be reported as bypasses
# (their subject is `Merge pull request #70 from ...`, no quote, so the template test misses them
# by construction). Unknown is unknown, and the summary says so.
if [ "${1:-}" = "merge-paths" ]; then
    shift
    N="${1:-60}"
    case "$N" in ''|*[!0-9]*) log "merge-paths: N must be a number, got '$N'"; exit 1 ;; esac
    git -C "$HUB" log --merges --first-parent -n "$N" \
        --format='%H%x1f%s%x1f%(trailers:key=Merge-Path,valueonly)' "$FT_REMOTE/main" 2>/dev/null \
    | python3 -c '
import sys
counts = {"pr-queue": 0, "hub-api-direct": 0, "web-ui": 0, "unknown": 0}
rows = []
for line in sys.stdin.read().split("\n"):
    if not line.strip():
        continue
    sha, subj, trailer = (line.split("\x1f") + ["", ""])[:3]
    t = trailer.strip()
    if t in ("pr-queue", "hub-api-direct"):
        path = t
    elif subj.startswith("Merge pull request \x27"):
        path = "web-ui"
    else:
        path = "unknown"
    counts[path] += 1
    rows.append("%s  %-14s  %s" % (sha[:10], path, subj))
print("\n".join(rows))
print("merge-paths: %d merge COMMIT(s) on %s/main: pr-queue %d, hub-api-direct %d, web-ui %d, unknown %d."
      % (sum(counts.values()), sys.argv[1], counts["pr-queue"], counts["hub-api-direct"], counts["web-ui"], counts["unknown"]))
print("  web-ui and hub-api-direct SKIPPED the queue guarantees (review hold, branch auto-retire,")
print("  hold-labelling, head CAS). unknown carries no Merge-Path trailer -- before 2026-09-02,")
print("  or a client that wrote none -- and is not evidence either way. The trailer records intent,")
print("  not proof: a caller can stamp pr-queue by hand.")
' "$FT_REMOTE"
    # THE FAST-FORWARD ERA LEAVES NO MERGE COMMIT, so the log above cannot see a
    # landing made after it began. The record is the PR's `merge-path:*` label, read from the forge:
    # the N most recently updated closed PRs that are MERGED, classified by that label; a merged PR
    # with none is the web UI or a client older than the label -- named as such, not bucketed as unknown.
    # NOT CHECKED, said out loud, when the client cannot answer.
    _pulls=$("$API" "/api/v1/repos/$REPO/pulls?state=closed&sort=recentupdate&limit=$N" 2>/dev/null) || _pulls=""
    printf '%s' "$_pulls" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    ps = json.load(sys.stdin) if False else json.loads(raw)
except ValueError:
    print("merge-paths (fast-forward era): NOT CHECKED -- the merged-PR listing could not be read; this is not \"no ff landings\"")
    sys.exit(0)
if not isinstance(ps, list):
    print("merge-paths (fast-forward era): NOT CHECKED -- the merged-PR listing was not a list; this is not \"no ff landings\"")
    sys.exit(0)
counts = {"pr-queue": 0, "hub-api-direct": 0, "unlabelled": 0}
rows = []
for p in ps:
    if not p.get("merged"):
        continue
    labels = [l.get("name", "") for l in p.get("labels") or []]
    path = next((l[len("merge-path:"):] for l in labels if l.startswith("merge-path:")), None)
    key = path if path in counts else "unlabelled"
    counts[key] += 1
    rows.append("#%-5s %-14s %s" % (p.get("number"), path or "unlabelled", (p.get("title") or "")[:70]))
print("\n".join(rows))
print("merge-paths (fast-forward era): %d merged PR(s) among the last %s closed: pr-queue %d, hub-api-direct %d, unlabelled %d."
      % (sum(counts.values()), len(ps), counts["pr-queue"], counts["hub-api-direct"], counts["unlabelled"]))
print("  A fast-forward writes no merge commit, so the record is the PR label `merge-path:*`.")
print("  unlabelled is the web UI or a client older than the label -- a bypass or history, not evidence either way.")
'
    exit 0
fi

# --------------------------------------------------------------------------------------
# THE QUEUE MERGES WHAT IT OPENS. Until 2026-08-21 it opened a PR, logged "queue drained"
# and exited, so its runway could only be cleared from OUTSIDE. Step 1 was a poll for
# "nothing open at all", which meant the NEXT invocation blocked 90x20s and died with exit
# 2 unless a human merged in the meantime. Measured that day: a PR sat green while a second
# session's queue run burned its whole 30-minute wait against it.
#
# Two consequences, both deliberate:
#
#  * THE RUNWAY WAIT IS GONE, not lengthened. Merging before opening the next PR maintains
#    "one queue PR in flight" BY CONSTRUCTION -- there is nothing left to poll for. A wait
#    that exists to be cleared by someone else is the hold-up, not the protection.
#  * A PR THAT DID NOT COME THROUGH THE QUEUE NO LONGER BLOCKS IT. The old count was of
#    every open PR regardless of origin, so one hand-opened PR stalled all queued work. The
#    cost is real and is accepted: whichever of the two merges second is left outdated and
#    must rebase, because `main` carries `block_on_outdated_branch: true` (read off this
#    repo's own branch_protections, not assumed). When that one is ours, merge_queued()
#    repairs it; when it is theirs, they pay the rebase the queue used to pay in waiting.
#
# was_cancelled() MOVED HERE from the end of the file. It was defined AFTER the only loop
# that could call it and was called from nowhere -- the vestige of this merge phase, written
# and never wired. sh resolves a function at call time, but the definition must still be
# executed first, and that loop was the last thing the script did.
# --------------------------------------------------------------------------------------

# --------------------------------------------------------------------------------------
# was_cancelled <full-40-sha> -- FOUR answers, because the question has four:
#   0  a run was cancelled -- the answer this function has always given, and the one every
#      caller and test written before rc 3 existed asserts. Narrowed, NOT repurposed: it now
#      means cancelled AND settled, which is the only case those tests ever exercised
#      (the CANCELLED fixture is one cancelled + one success -- a finished head).
#   3  a run was cancelled and a re-queue may still be coming -- waiting is correct. This is
#      the case rc 3 made expressible; it had no number before because it had no name,
#      and it takes a FREE one rather than the one rc 0 already meant.
#
# THE FIRST VERSION OF THIS CHANGE PUT 0 ON THE IN-FLIGHT CASE AND MOVED THE FINISHED CASE TO
# 3. That is a silent contract break, not a new answer: `test_a_cancelled_run_is_detected`
# asserts `_run(CANCELLED, FULL) == 0` and got 3, and the branch never touches the test file
# -- it changed the meaning underneath a check that still looked like it passed. It reached
# the gate because that file was never run. Diagnosed by one session, repaired by its successor
# after a handover.
#   1  no run was cancelled -- the red is a real verdict
#   2  unreadable, zero runs, or a short sha -- cannot tell, which is not "not cancelled"
#
# WHY THIS EXISTS. `hub-api.sh pr checks` reads the commit-STATUS API, which renders a
# cancelled job as `failure`. Forgejo cancels the in-flight run when a PR head is
# force-pushed, so a driver that rebases and then reads `pr checks` sees a red it caused
# itself and cannot tell it from a test failure. Measured 2026-08-20: force-pushing
# e1465a7b86 cancelled the run for the prior head 6f7abffa30 three seconds later, and all
# the resulting statuses were stamped at the same second -- `pr checks` said `failure`,
# while the run's real `status` was `cancelled`. Serial admission prevents the cause; this
# detects it when prevention fails (a human force-push, a second driver, a cancelled job).
#
# THE ROUTE IS NOT OBVIOUS AND TWO WRONG ONES WERE TRIED FIRST:
#   * `target_url` carries a repo-local run NUMBER; /actions/runs/{n} resolves {n} as a
#     global run ID. Same-looking integers, different spaces -- fetching one returned a
#     well-formed run from two days earlier ending `Job succeeded`.
#   * A client-side scan is worse: the response field is `commit_sha`, NOT `head_sha`
#     (populated in 0 of 1021 runs), `run_number` is empty in all of them, and `limit` is
#     IGNORED (asking 50 returns everything). So the scan looks exhaustive, matches
#     nothing, and reads as "no runs for this commit" -- a false absence, silently.
# Only the SERVER-side filter works, and it needs the FULL 40 characters.
#
# Controls, all measured, all required -- the parameter accepts anything without complaint:
#   full sha of a cancelled head -> 3 runs, one `status: cancelled`
#   all-zero sha                 -> 0 runs
#   SHORT sha (10 chars)         -> 0 runs   <-- indistinguishable from "no such commit"
# A control built by truncating or fabricating a SHA is a second negative wearing a
# positive's clothes; one such control passed here before anyone noticed 0 runs for `main`
# was implausible.
# _head_of <pr-number> -- that PR's current head sha, or empty if the forge does not answer.
#
# DEFINED HERE, ABOVE ITS CALLERS, AND THAT IS LOAD-BEARING. `wait_for_green` consults it on a
# cancellation , and `sh` resolves a function at call time but only after the definition
# has been EXECUTED -- so a definition below a verb's dispatch is absent for that verb. This file
# has already paid for that once: `was_cancelled` below sat after the only loop that could call it
# and was called from nowhere. `test_pr_queue_defines_every_function_before_it_calls_it` is the
# guard, and it caught this move on the runner after a local run of the three pr-queue test files
# passed -- the structural test lives in test_pr_queue_owner_notify.py, which none of them is.
# _pr_field <pr> <key>... -- ONE field off ONE pull request, EMPTY on any failure.
#
# Four readers fetched this same endpoint and pulled one value out of it, each with its own copy of
# the fetch and the parse, and the duplication had already spread past them: an inline copy of
# `_head_ref_of` sat in the batch mark-and-delete loop, byte-identical to the function it could have
# called. That is the cost this collapses -- not the lines, the number of places the FAIL-SOFT
# behaviour is defined. Empty on a missing key, a null, an unreadable answer or no answer at all,
# once, here, where every caller's "" comes from.
#
# MUST STAY ABOVE ITS CALLERS: see the note above `_head_of`. `sh` resolves at call time but only
# over definitions already EXECUTED, so a helper below a verb's dispatch is absent for that verb.
# `test_pr_queue_defines_every_function_before_it_calls_it` is the guard.
#
# NOT FOR EVERY READER, AND THE TWO EXCEPTIONS ARE DELIBERATE. `_pr_is_merged` keeps its own body:
# its contract is yes/no with a fail-CLOSED default of "no", and routing it through a helper that
# fails to "" would either change that or need a flag. `_drain_facts` reads state AND the draft
# marker out of ONE request on purpose -- that is one request doing two jobs, not a
# duplicate of this one.
_pr_field() {
    _pfn=$1
    shift
    "$API" "/api/v1/repos/$REPO/pulls/$_pfn" 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in sys.argv[1:]:
    d = (d or {}).get(k)
print("" if d is None else d)' "$@" 2>/dev/null
}

_head_of() {
    _pr_field "$1" head sha
}

was_cancelled() {
    # THE BODY IS forge.sh's hub_was_cancelled: three drifted copies became one.
    # The four answers above are its answers, unmapped -- this drain is the caller they were
    # written for. `$API` rides along so a stubbed client stays the one that is asked.
    [ -r "$SELF_DIR/forge.sh" ] || { log "was_cancelled: no $SELF_DIR/forge.sh -- CANNOT TELL (rc 2, a red stays a red)"; return 2; }
    FORGE_HUB_API="$API" sh "$SELF_DIR/forge.sh" was-cancelled "$REPO" "$1"
}

# wait_for_green <full-40-sha> -- poll until this commit's check-runs are green.
#
# `pr checks` EXIT CODES DO NOT DISCRIMINATE WAITING FROM FAILING: 1 for state='pending' and
# 1 for state='failure' alike, 2 for zero registered. So this reads the printed STATE and
# never the exit code alone. Zero registered is the registration race
# and resolves by waiting -- but it is also the shape of a workflow that never triggered, so
# the wait is bounded and its expiry is a STOP, never a pass.
#
# GREEN WITH THE SUITE SKIPPED IS NOT GREEN. A draft (`WIP: ` title) skips the pytest job,
# and `pr checks` counts a skipped context toward a `success` state, so without this a drafted PR
# landed serially would merge with no test having run on its head. rc 4 says so; the batch path
# never sees it, because an integration PR is never a draft. `_AWAIT_RERUN=yes` keeps waiting on
# that state instead, for a caller that has just re-triggered the run and needs it to register.
MUST_RUN_CONTEXT="${PR_QUEUE_MUST_RUN_CONTEXT:-Test / pytest}"
wait_for_green() {
    _sha=$1
    # OPTIONAL SECOND ARGUMENT: the PR number this sha is supposed to be the head of.
    # Optional because one caller has no PR to name -- `merge_queued` waits on a BATCH head, which
    # is an integration branch's tip and is nobody's PR head. Passing a number there would make the
    # cancellation check below compare against an unrelated PR, so the check is skipped when it is
    # absent rather than guessed at.
    _pr=${2:-}
    for _i in $(seq 1 "$WAIT_POLLS"); do
        _out=$("$API" pr checks "$REPO" "$_sha" 2>&1); _rc=$?
        # 127 IS NOT A CHECK STATE, it is the instrument gone: a detached drain's worktree was deleted
        # under it and every poll came back 127 with no state= to read, which the `pending|""` arm
        # below slept through for WAIT_POLLS. Nothing was measured; say so and stop.
        if [ "$_rc" = 127 ]; then
            log "  '$API' cannot be run (rc 127) -- STOPPING: nothing was measured, PR left open"
            return 3
        fi
        if [ "$_rc" = 0 ]; then
            _skip=$(printf '%s\n' "$_out" | awk -v p="$MUST_RUN_CONTEXT" 'index($0, "  " p) == 1 && $NF == "skipped" { print; exit }')
            [ -z "$_skip" ] && { log "  checks green on $_sha"; return 0; }
            if [ "${_AWAIT_RERUN:-}" != yes ]; then
                log "  green on $_sha but '$MUST_RUN_CONTEXT' SKIPPED -- the suite never ran on this head"
                return 4
            fi
            sleep "$POLL_SECS"; continue
        fi
        _state=$(printf '%s' "$_out" | sed -n "s/.*state='\([a-z]*\)'.*/\1/p")
        case "$_state" in
            pending|"") : ;;   # still running, or none registered yet -- keep waiting
            failure)
                # A red here can be a cancellation Forgejo rendered as `failure` -- it
                # cancels the previous run on every push to a PR head. Treating that as a
                # test failure halts the queue on a red it caused itself.
                was_cancelled "$_sha"; _wc=$?
                if [ "$_wc" = 0 ] || [ "$_wc" = 3 ]; then
                        # A CANCELLATION HAS TWO CAUSES THAT WANT OPPOSITE RESPONSES.
                        # The forge cancelled a run it will re-queue -- waiting is correct. Or a
                        # force-push SUPERSEDED this head, and no run will ever settle on this sha
                        # again: waiting cannot terminate except by timeout. Measured 2026-09-20:
                        # 168 polls over 3m43s on a sha which had not been the PR's head since
                        # before the first of them, ending "not red: nothing was measured".
                        #
                        # ONE READ OF AN OBJECT THIS FILE ALREADY FETCHES ELSEWHERE. Same shape as
                        # `_pr_is_merged`'s 405 arm: assume neither cause, ask the question that separates
                        # them.
                        #
                        # AN UNREADABLE HEAD IS NOT A MOVED HEAD. `_head_of` prints nothing when the
                        # forge does not answer, and treating that as "moved" would abandon a PR on a
                        # transport blip. Empty falls through to waiting, which is the behaviour that
                        # existed before this check.
                        _cur=""
                        [ -n "$_pr" ] && _cur=$(_head_of "$_pr")
                        if [ -n "$_cur" ] && [ "$_cur" != "$_sha" ]; then
                            log "  red on $_sha was a CANCELLATION, and #$_pr's head has since moved to $_cur"
                            log "  -- no run will settle on $_sha again. STOPPING this wait, PR left open."
                            return 8
                        fi

                    # THEN: cancelled, head unchanged, and EVERY run on it is terminal.
                    # ORDER IS A DECISION. The superseded check runs FIRST because when both are
                    # true its answer is strictly more useful: there IS a newer head to measure, so
                    # "skip and read the new one" beats "nothing measured this one". Only once the
                    # head is confirmed unchanged does a finished run set mean the wait is over.
                    #
                    # NOT rc 6. That code means RED SPECIFICALLY -- "this PR's tests failed" -- and
                    # returning it here would assert the very verdict the cancellation destroyed.
                    # rc 10 is its own outcome, translated to a skip by `approve_one` with its own
                    # words, because the superseded-head message ("moved under this wait") is false for this case.
                    if [ "$_wc" = 0 ]; then
                        log "  red on $_sha is a CANCELLATION and every run on it has FINISHED --"
                        log "  no run will ever settle here. STOPPING this wait; nothing was measured."
                        return 10
                    fi
                    log "  red on $_sha but a run was CANCELLED, not failed -- still waiting"
                else
                    log "  CHECKS RED on $_sha -- STOPPING, PR left open for a human"
                    printf '%s\n' "$_out" | sed 's/^/    /'
                    # rc 6 is RED specifically, distinct from the `return 1` below and from every
                    # other STOPPING condition here. A caller that must tell "this PR's
                    # tests failed" from "something went wrong" checks 6; one that only asks
                    # whether it is green still checks `= 0`, `!= 0`, `= 3` or `= 4` -- which is
                    # every existing caller. None tests `= 1`, which is what makes 6 safe to add.
                    return 6
                fi ;;
            *) log "  unexpected check state '$_state' on $_sha -- STOPPING"; return 1 ;;
        esac
        sleep "$POLL_SECS"
    done
    # Expiry is its OWN answer, rc 3: nothing was measured, so it is neither green nor red. A
    # caller that halves on red must not halve on this -- one batch was split after 14 minutes of
    # "Waiting to run", on a gate that had not started (2026-09-08).
    log "  checks NEVER SETTLED on $_sha after $WAIT_POLLS polls -- not red: nothing was measured; STOPPING, PR left open"
    return 3
}

# merge_queued <number> <title> <sha> -- merge a PR THIS SCRIPT opened.
#
# `pr merge` PRINTS the HTTP code and EXITS 0 REGARDLESS, so gating on its exit status reads
# a refusal as a merge. Parse the code. 405 is `block_on_outdated_branch` firing: something
# merged while our checks ran. The repair is Forgejo's update endpoint -- which creates a
# merge commit and thereby DETACHES the green runs, so the checks must be re-waited on the
# NEW head rather than trusted from before the update.
#
# `(#N)` IS WRITTEN HERE and stays written here: this client always sends MergeTitleField, so
# Forgejo's default "<title> (#N)" can never apply, and `main` refuses force push for admins
# too -- a missing number is permanent. It has happened once. Since then
# `pr merge` also appends the number when a subject arrives without one, and does not double
# one that is already correct -- so this call is unchanged by that and is not made redundant
# by it: the log line below names the subject, and it should name the real one.
# pr_head <number> -- print the full sha hub CURRENTLY has for that PR, or nothing.
#
# FETCHED, NEVER RESOLVED LOCALLY. `git rev-parse <branch>` answers about this checkout, and the
# queue rebases every branch it admits -- so a local sha can be one the remote never had. The
# only honest source is `refs/pull/N/head`, which Forgejo serves.
pr_head() {
    git fetch "$REMOTE" -q "+refs/pull/$1/head:$QREF/pull/$1" 2>/dev/null || return 1
    git rev-parse "$QREF/pull/$1" 2>/dev/null
}

# update_and_rewait <number> -- POST the update endpoint, then re-wait on the NEW head.
# Sets NEW_SHA. The update moves the head, which DETACHES the green runs, so the previous
# green is void and must not be carried forward.
#
# `?style=rebase` IS LOAD-BEARING AND MUST NOT BE DROPPED. `style` is a QUERY
# parameter, enum `merge|rebase`, and it DEFAULTS TO `merge` -- the endpoint's own swagger
# summary is "Merge PR's baseBranch into headBranch". Without it this call injects a
# two-parent merge commit into every branch it touches. It has been doing exactly that;
# `Do=squash` at merge time WAS the only reason none had ever reached `main`, so the defect
# was invisible rather than absent (measured: updating one PR produced a commit with two parents,
# flattened away at merge).
#
# THE MASK IS GONE. Merges are `Do=merge` now, so nothing flattens anything: a
# stray two-parent commit injected by a bare update lands on `main` exactly as it is. This
# parameter is no longer belt-and-braces over a squash that would have hidden the mistake --
# it is the ONLY thing standing between an update and a malformed history, and the failure it
# prevents is now permanent (`main` refuses force push, admin included).
#
# Prove it by reading the resulting commit's PARENT COUNT, never by observing that the
# parameter is present. The distinction matters more now than when it was written.
#
# THE RE-WAIT IS STILL REQUIRED WITH `rebase`, and that is the part a later reader will be
# tempted to remove. A rebase replays the branch onto `main`, so the head sha changes just as
# it does under `merge`; only the SHAPE differs. Dropping the re-wait would carry a green
# forward onto a sha it never ran against, which is the stale-green trap this queue exists
# to prevent.

# `_head_of`'s sibling, for the one question a 405 cannot answer about itself. The forge sends
# 405 for "not allowed to merge" and the code does not say WHY -- `block_on_outdated_branch`
# firing and the PR ALREADY BEING MERGED both land here, and they want opposite responses.
#
# MEASURED 2026-09-20. Two drains reached merge for the same green PR five seconds
# apart; the loser read its 405 as "main moved in the gap" and ran three `update_and_rewait`
# rounds against a PR that was already on main, stopping rc=8 about two minutes later. Its own
# log disproved the diagnosis on every attempt:
#
#     #49 head moved by THIS update step (405 repair): 38a8e2839f -> 38a8e2839f
#
# the same sha on both sides of the arrow, reported as a move. Nothing was wrong with the repair
# -- it repaired faithfully, and there was nothing to repair.
#
# ANSWERS `no` ON AN UNREADABLE FORGE, deliberately. A read failure must not be able to invent a
# merge that did not happen; falling back to `no` leaves exactly the repair path that was here
# before this function existed, so the failure mode is the old behaviour rather than a new one.
# _pr_bool <pr> <field> -- one BOOLEAN field as yes/no, and `no` on any failure. `_pr_field`
# is the wrong helper for these: it answers "" on a read error, and every caller here is asking a
# question where the safe answer is the one that preserves the behaviour that existed before the
# question was asked. That default is the whole design and is argued per caller below.
_pr_bool() {
    "$API" "/api/v1/repos/$REPO/pulls/$1" 2>/dev/null | FIELD="$2" python3 -c 'import json,os,sys
try:
    print("yes" if json.load(sys.stdin).get(os.environ["FIELD"]) else "no")
except Exception:
    print("no")' 2>/dev/null
}

_pr_is_merged() {
    _pr_bool "$1" merged
}

# THE THIRD CAUSE OF A 405, and the one the arm above could not see.
#
# `block_on_outdated_branch` firing and the PR already being merged were causes one and two. A PR
# the forge considers a DRAFT is refused the merge whatever its base is, and NO UPDATE CAN CHANGE
# THAT, so the repair path is not merely wasted there -- it cannot terminate in a merge.
#
# MEASURED 2026-09-22 on a real PR, with the cause isolated to one variable: base sha == `hub/main` ==
# 2840f06e95 throughout (so NOT behind its base) and `merge-base --is-ancestor` true (so no
# conflict), while `draft` was True and `mergeable` False. Removing the `WIP: ` prefix and changing
# nothing else flipped `draft` to False and `mergeable` to True.
#
# WHY THE EXISTING DRAFT CHECK DID NOT CATCH IT. `approve_one` already refuses a draft at the WAIT
# stage (its rc 4 arm: a green head whose suite skipped because the PR is a draft). That fires
# before the merge, and here the PR became a draft AFTER it went green -- a peer's drain un-drafted
# it, reopened it to run the suite, stopped at rc 8 on a head my own drain had moved, and re-drafted
# it on the way out as `restore_drafts` requires. Every step correct alone; the order is what left a
# green non-draft PR draft again by merge time.
#
# ANSWERS `no` ON AN UNREADABLE FORGE, and the direction matters more here than for `_pr_is_merged`.
# A false `yes` would stop a PR that could have landed, and say something false about why. A false
# `no` leaves exactly the repair path that was here before this function existed. So the unreadable
# case keeps the old behaviour rather than inventing a new refusal.
_pr_is_draft() {
    _pr_bool "$1" draft
}

# _log_head_move <pr> <label> <old> <new> -- ONE definition of "did the update step move the head",
# because the answer was being asserted rather than compared.
#
# TWO of the three call sites printed `head moved by THIS update step<label>: $old -> $new`
# unconditionally, so a head that did NOT move was reported as moved -- the same sha on both sides of
# the arrow. It is in this file's history twice: one report quotes `#49 ... 38a8e2839f ->
# 38a8e2839f`, another `#15 ... 28762170a4 -> 28762170a4`, each beside a "did not move"
# line from one line earlier, and both were diagnosed THROUGH that contradiction rather than from the
# log saying so.
#
# THE THIRD SITE WAS ALREADY RIGHT, which is the part worth keeping: one fix added the comparison to
# the rc-1 arm and its note there said the 405 arm still had the bug. The ff-repair arm had it too and
# was named nowhere, so the same defect stood in two arms behind a fix that looked complete. One
# definition removes the class rather than the instance. A line whose job is to tell a reader whether
# a step did anything must not claim it did.
_log_head_move() {
    if [ "$3" = "$4" ]; then
        log "  #$1 head UNCHANGED by the update step$2: still $3 -- the update replayed nothing"
    else
        log "  #$1 head moved by THIS update step$2: $3 -> $4"
    fi
}

# The BRANCH, not the sha. `head.ref` becomes `refs/pull/N/head` once the branch is gone, which is a
# documented Forgejo fail-open -- that string matches no worktree, so `_owner_pid` simply finds
# nobody and the caller falls back, which is the right behaviour for a retired branch.
_head_ref_of() {
    _pr_field "$1" head ref
}

# _update_head <n> -- ask the forge to rebase PR <n> onto main and wait until its head moves.
# Sets NEW_SHA. Returns 1 on an explicit refusal or an unreadable head. No gate: callers decide.
_update_head() {
    _un=$1
    _old=$(_head_of "$_un")
    # READ WHAT THE FORGE ANSWERED. This was `-o /dev/null >/dev/null 2>&1`: the
    # response was discarded three ways, so a REFUSED update was indistinguishable from an accepted
    # one that replayed nothing, and the caller retried a call that would be refused identically.
    #
    # MEASURED 2026-09-22 on a real PR. The forge could not rebase the branch because
    # it CONFLICTED, refused the update, and the refusal went to /dev/null; the loop then ran three
    # rounds ~35s apart and stopped rc 8 with the queue blocked behind it. Every layer was
    # individually reasonable and the stack reported nothing true.
    #
    # THE DISCRIMINATOR IS THE RESPONSE, NOT THE SHA. A head that did not move is the SYMPTOM that
    # made this visible, and stopping on it would be wrong: the forge may have accepted and simply
    # be slower than REWAIT_SECS, and a legitimate move must still loop and merge -- the re-wait
    # exists because a rebase changes the sha and a green measured on the old one is the stale-green
    # trap this queue exists to prevent. So only an explicit refusal stops.
    _up=$("$API" "/api/v1/repos/$REPO/pulls/$_un/update?style=rebase" -X POST -w '\n%{http_code}' 2>&1)
    _up_code=$(printf '%s\n' "$_up" | tail -1)
    #
    # AN UNREADABLE CODE IS NOT A REFUSAL, and falls back to the behaviour that was here before
    # this check existed -- the same choice `_pr_is_merged` makes for the same reason:
    # a read failure must not be able to invent a verdict, so the failure mode stays the OLD one
    # rather than becoming a new one. Only an explicit non-2xx stops.
    case "$_up_code" in
        2??) : ;;
        ''|*[!0-9]*) : ;;
        *) log "  the forge REFUSED to update #$_un (http=$_up_code) -- STOPPING on the first"
           log "  attempt rather than retrying a call that will be refused identically."
           printf '%s\n' "$_up" | sed '$d; s/^/    /' >&2
           return 1 ;;
    esac
    # WAIT ON THE CONDITION, NOT THE CLOCK: the rebase is done when the head sha has
    # moved. A bare `sleep 10` paid ten seconds on every update whether the forge took one second
    # or twelve, and said nothing when it took more. The ceiling is for a forge that replays
    # nothing (a branch already current) -- then the old head IS the new head and we proceed.
    _waited=0
    NEW_SHA=$(_head_of "$_un")
    while [ -n "$_old" ] && [ "$NEW_SHA" = "$_old" ] && [ "$_waited" -lt "$REWAIT_SECS" ]; do
        sleep "$REWAIT_POLL_SECS"
        _waited=$((_waited + REWAIT_POLL_SECS))
        NEW_SHA=$(_head_of "$_un")
    done
    [ -n "$NEW_SHA" ] || { log "  cannot read #$_un's new head -- STOPPING"; return 1; }
    [ "$NEW_SHA" = "$_old" ] && log "  #$_un's head did not move within ${REWAIT_SECS}s of the update -- proceeding on $NEW_SHA"
    return 0
}

update_and_rewait() {
    _update_head "$1" || return $?
    wait_for_green "$NEW_SHA" || return $?   # propagate rc 6, RED on the head the update made
}

# --------------------------------------------------------------------------------------
# `queue:needs-human-review` -- the label that says a green PR is deliberately unmerged.
#
# WHY A LABEL AT ALL. A held PR is green, open, and indistinguishable in a listing from one
# nobody has looked at. The hold lives in this script's exit code and in a log line that scrolls
# away; the forge shows nothing. So an operator scanning open PRs cannot tell "waiting for me"
# from "waiting for CI" from "abandoned".
#
# THE DETACH MATTERS MORE THAN THE ATTACH. A merged PR still wearing `queue:needs-human-review` is a label
# asserting a decision that has already been made -- the same family as every stale claim this
# repo gates against, and worse than no label because it is confidently wrong.
#
# FAIL SOFT, ALWAYS. The hold is the safety property; the label is a convenience on top of it.
# A labelling failure that could block a hold or a merge would be a decoration with the power to
# stop work, so every path here logs and returns 0.
#
# RESOLVED BY NAME, NOT HARDCODED. The id is 65 in this repo and nothing in any other, including
# the throwaway repo the test suite drives. Resolving also makes the script say what it means.
#
# THE API SHAPE IS NOT THE OBVIOUS ONE, and one arm of it fails silently:
#   * `EditIssueOption` has NO `labels` field, so `PATCH /issues/{n}` with `"labels"` is
#     ACCEPTED AND IGNORED -- HTTP 200, identical labels before and after. Do not use it.
#   * the labels SUB-RESOURCE is the working path, and PRs share the issue number namespace.
#   * DELETE by NAME works here -- measured on two live PRs, 2026-08-21. `/labels/queue:needs-human-review`
#     removed it, and an unresolvable name errors with `label does not exist [label_id: 0]`, so the
#     name path does a real lookup rather than being ignored. The ID form is used anyway: it is
#     documented for every version, it costs a GET already being made for the attach, and it does
#     not depend on name resolution staying the same across Forgejo releases. This is a
#     version-independence choice, NOT a claim that names are broken.
#
# THE QUIET FAILURE IS ON THE DETACH, and it is not the one that was expected. Measured in the
# same sequence: deleting a label that is NOT on the PR returns rc=0 with no output -- BYTE
# IDENTICAL to a successful removal. So the exit status discriminates "does this label exist in
# the REPO", never "was it on this PR". `rc` is not evidence that anything was removed.
#
# VERIFIED BY READ-BACK ON BOTH SIDES, never by the request's status. On the attach because
# "accepted and ignored" is a measured behaviour of the PATCH arm; on the detach because success
# and no-op are indistinguishable. The detach is the half that matters more -- a merged PR still
# wearing this label asserts a decision already made -- so it is the half that most needs a
# verdict the API will not give it.
HELD_LABEL="${PR_QUEUE_HELD_LABEL:-queue:needs-human-review}"

# --------------------------------------------------------------------------------------
# `queue:merge-requested` -- INTENT recorded on the forge, AUTHORITY kept on this box.
#
# WHY A SECOND LABEL RATHER THAN REUSING THE HOLD. `queue:needs-human-review` is the queue saying something
# ("I stopped, a human should look"). This one is a human -- or a comment-triggered workflow
# acting for one -- saying something back ("merge it"). They CO-EXIST on the ordinary path: an
# approved held PR carries both. Collapsing them would make `queue:needs-human-review` mean two opposite
# things depending on who attached it.
#
# WHY THE FORGE DOES NOT DO THE MERGE ITSELF, measured on hub 2026-08-24 (Forgejo 16.0.2,
# runner v13.0.0) rather than assumed:
#   * the Actions token is a SYNTHETIC user `forgejo-actions` (id -2). It has real repo write
#     (label POST -> 201) but CANNOT push: merging a PR on a branch with NO protection rule at
#     all returned 409 `PushRejected ... User 'forgejo-actions' is not allowed to push`.
#   * it cannot be whitelisted onto a protected branch either -- 422 `user does not exist
#     [uid: 0, name: forgejo-actions]`, where the identical call naming a real user returns 200.
#   * it CAN schedule `merge_when_checks_succeed` (201), but the queued merge never executes,
#     because auto-merge runs as the scheduling doer. Same PR, same green check, scheduled by a
#     user who can push: merged in ~35s.
# So the only workflow-side merge needs a real user's PAT in a repo secret -- and secret masking
# there is literal substring replacement: `printf '%s' "$SEC" | base64` printed the canary
# verbatim into a job log that any signed-in forge user can read. Fork PRs are NOT given secrets
# (same-repo run: len=20; fork run: len=0), so that exposure is bounded to people who can push
# in-repo -- which on THIS repo is every agent session.
#
# Hence the split: the LABEL crosses the trust boundary, the CREDENTIAL never does. The merge
# runs here, under the freshness guard, as `claude`.
MERGE_LABEL="${PR_QUEUE_MERGE_LABEL:-queue:merge-requested}"

# Gate suppression, premise 3. A PR wearing this reports NOT-PASSING rather than green, so
# forgetting to admit it can never read as success. The drain skips it for that reason and no
# other: it has never CLAIMED to be green, which is a different fact from being red.
#
# IT MUST NOT SHARE A NAME WITH `HELD_LABEL`, and premise 4 is explicit about why -- one name for
# both would make "passed, waiting for a human" indistinguishable from "never ran", which is the
# whole point of suppressing fail-closed. They are read separately in `_drain_facts` and reported
# in separate branches, so a reader of the log can always tell which one stopped a PR.
NOT_ADMITTED_LABEL="${PR_QUEUE_NOT_ADMITTED_LABEL:-queue:not-admitted}"

# THE SERIAL MARK (operator, 2026-09-16): a PR carrying it never joins a batch. It waits
# its turn like any other and lands ALONE at the front. `hub-api.sh pr create --serial` attaches it;
# the default is shared with that file through the same variable, and test_late_hold_is_honoured.py
# asserts both assignments agree.
SERIAL_LABEL="${PR_QUEUE_SERIAL_LABEL:-queue:serial}"

# label_id <name> -- the repo-local id, or empty. Also the EXISTENCE PROOF the listing needs;
# see the fail-open documented on the `merge-requested` verb.
# `hub-api.sh issue label-id` is now the one place that turns a label NAME into an id.
# This used to hand-roll the `/labels` fetch plus a python one-liner, one of six independent
# copies across three files -- which is how `forge.sh label rm` came to be written later, by an
# author who had read none of them, without a guard at all.
#
# THE FAIL-SOFT IS PRESERVED AND IT IS NOT INHERITED. `label-id` REFUSES an unresolvable name
# (exit 2), which is right for its other callers: an unverifiable filter makes Forgejo return the
# whole repo. Here the empty string is still the answer, because the hold-labelling path at
# :364-403 fails soft ON PURPOSE -- a labelling failure must never block a merge. So the exit
# code is discarded deliberately rather than by omission, and `merge-requested` below turns the
# same empty string into its own REFUSAL, because there the stakes invert.
#
# Note which arm is dangerous: a label that EXISTS but matches nothing behaves correctly, so a
# test covering only the happy path and the empty path never reveals the fail-open.
# THE DIGIT CHECK IS THE GUARD, NOT THE CALL. Measured while writing this: routing through the
# verb without it re-opened the exact fail-open this function exists to close. Callers test
# `[ -n "$_lid" ]`, so ANY non-empty stdout reads as "resolved" -- and a client that printed a
# diagnostic to stdout, or a stand-in that answered an unknown verb with text, would hand
# `merge-requested` a truthy id and it would list and merge EVERY open PR. The old inline python
# printed only on a match, so the contract was implicit; through a verb it has to be stated.
# Only an all-digits id counts as resolved. Anything else is unresolved, whatever it says.
label_id() {
    [ "${PR_QUEUE_LABEL:-1}" = "1" ] || return 1
    _id=$("$API" issue label-id "$REPO" "$1" 2>/dev/null) || return 1
    case "$_id" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s\n' "$_id"
}
held_label_id() { label_id "$HELD_LABEL"; }

# DEFINED HERE, ABOVE EVERY CALL SITE, AND THAT IS THE WHOLE POINT.
#
# THE VERB DISPATCH BLOCKS IN THIS SCRIPT ARE INTERLEAVED WITH FUNCTION DEFINITIONS, so WHICH
# HELPERS EXIST DEPENDS ON WHICH VERB YOU RAN: `approve` dispatches roughly 900 lines above
# `drain`. `sh` binds a function name when the definition is EXECUTED, not when the file is
# parsed, so a helper defined below `approve`'s dispatch does not exist on the approve path.
#
# MEASURED 2026-09-21, both directions, same code, same branch:
#   approve #76 -> `pr-queue.sh: 1072: _notify_owner: not found`; merge fine, owner never told.
#   drain   #80 -> `no live session resolves for agent/... -- nobody was messaged about: ...`
# The drain reached the function because its dispatch is BELOW the definition. Nothing else
# differed. The bug was invisible from the one place that would notice it: `_notify_owner` is
# written never to fail its caller, so the run still exits 0 and the only trace is one stderr
# line in a detached log.
#
# WHAT THIS DEFEATED: the owner notification had never once fired on
# `approve`. Its own comment states the failure it exists to prevent -- four merged branches
# surviving on hub because the session holding the worktree was never told.
#
# KEEP NEW HELPERS HERE, above the first dispatch. `test_pr_queue_defines_before_it_calls` fails
# on any function this file calls at a line before its own definition.

# `pr create` registers a gate-watch for NON-draft heads only, so a PR drafted at creation
# had none -- and when the queue un-drafted it and its suite went red, nothing told the session
# (2026-09-16). The un-draft is the first time that PR runs a suite, so the watch is owed
# here, for the session this run answers to: PR_QUEUE_DETACHED from a detached run, FORGE_TOOLS_WAKE_PID from
# a foreground one. A cron or bare-shell drain has no session to wake and skips. Best-effort: a
# failed register is one log line, never a refusal to land.
# ONE QUEUE OPERATOR PER QUEUE. Every one-front guarantee here (draft-first, the batch,
# the split that un-drafts a lone member) is decided INSIDE one drain, so a second concurrent drain
# is a second front: two batches claimed the same drafts, then two lone members were un-drafted at
# once. A drain that finds another live drain on the same queue DEFERS rather than waits: the live
# one re-derives the queue until it stops changing, so whatever is ready now is its.
#
# A CLAIM, NOT A HELD LOCK. The 09-16 `flock` was held for the run, and a lock fd is inherited by
# every child (measured 2026-09-22: a background child of `flock -n f cmd` kept `f` locked after its
# parent exited) -- one lingering helper would defer every later drain and stall the queue with no
# error. So the claim is a pid in a file, written under a lock held only for the check-and-write
# (milliseconds, no children), and nothing releases it: a dead or reused pid is a free claim.
#
# KEPT IN THE SHARED GIT DIR, keyed on the remote and repo: every worktree of one clone shares that
# directory, so every drain on this box sees one claim, and each test's throwaway repo has its own.
# CEILING: a drain from a DIFFERENT clone of the same forge repo claims separately. Every real drain
# here runs from a consumer worktree. `$$` inside the subshell is still this drain's pid.
# A CLAIM IS `pid starttime`: the pid is live AND still the process that wrote it. Matching
# `pr-queue.sh` in its argv made a recycled pid running `less scripts/pr-queue.sh` the queue's
# operator -- the self-match trap agent detection by comm refuses. A pid-only claim (a run from before this
# change) keeps the old argv test, so an upgrade never ignores a drain that is in flight.
_starttime() { sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f20; }
_run_is_live() {
    [ -n "${2:-}" ] && { [ "$(_starttime "$1")" = "$2" ]; return; }
    tr '\0' ' ' 2>/dev/null < "/proc/$1/cmdline" | grep -q 'pr-queue\.sh'  # stderr first: a dead pid's failed open is the shell's own message
}
_claim_queue() {
    _qkey=$(printf '%s %s' "$(git -C "$HUB" remote get-url "$REMOTE" 2>/dev/null)" "$REPO" | sha256sum | cut -c1-16)
    _qc="$(git -C "$HUB" rev-parse --path-format=absolute --git-common-dir)/pr-queue-operator-$_qkey"
    QUEUE_HOLDER=$( (
        flock 9
        read -r _p _st 2>/dev/null < "$_qc"
        if [ -n "${_p:-}" ] && [ "$_p" != "$$" ] && _run_is_live "$_p" "${_st:-}"; then printf '%s' "$_p"; exit 0; fi
        printf '%s %s\n' "$$" "$(_starttime $$)" > "$_qc"
    ) 9>>"$_qc.lock" )
    [ -z "$QUEUE_HOLDER" ]
}

# _owner_pid <branch> [pr] -- the pid of the agent session whose worktree holds that branch, else the
# live subscriber `pr create` recorded for that PR number, or empty.
#
# Every per-PR outcome this script produces went to the session that RAN it: the drain's
# exit message, the gate-watch verdict, the prune refusal. The draining session owns no worktree on
# the branch and has nothing to do with it, while the one session that can act is told nothing. Four
# refused prunes and two un-drafts landed that way on 2026-09-19/20, including this script's own
# ticket -- merged by another session's drain, with its author never messaged.
#
# THE RESOLUTION USES /proc, BECAUSE NOTHING ELSE ON THIS BOX ANSWERS IT. Commits are all authored
# `claude <claude@forge.example.org>` by design and the forge records one account for every
# PR, so neither git nor the tracker can name an owner. `head.ref` gives the BRANCH; git's worktree
# registry maps branch -> path; and a process whose cwd is under that path is the session working
# it. `agent_comms.AGENT_COMMS` decides which comms count, read from `comm` rather than matched
# against command text -- the same doctrine `worktree-reap.sh` uses, and reused rather than redone.
#
# BEST EFFORT, AND THE CEILING IS STATED RATHER THAN SMOOTHED: finding no owner is NOT proof there
# is none. A session working outside its worktree reads as absent by cwd, which is what the
# subscription arm answers when the caller knows the PR; a respawn between the scan and the send, or
# a cwd owned by another user (EACCES), still read as absent. So a miss falls back to the log line that
# was always there -- best-effort delivery to the RIGHT party beats reliable delivery to the wrong
# one, which is what this replaces.
_owner_pid() {
    [ -n "${1:-}${2:-}" ] || return 0   # no branch and no PR: nothing to resolve, and every caller falls back
    PR_QUEUE_PROC="${PR_QUEUE_PROC:-/proc}" python3 - "${1:-}" "$HUB" "${2:-}" "${REPO:-}" "$TOOLS_DIR" <<'OWNER' 2>/dev/null
import importlib.util, os, subprocess, sys

branch, hub, pr, repo, self_dir = sys.argv[1:6]
sys.path.insert(0, self_dir)   # Forge-Tools' own dir; `hub` is the consumer checkout
try:
    from agent_comms import AGENT_COMMS
except Exception:
    raise SystemExit  # cannot tell an agent from anything else: say nothing rather than guess
proc = os.environ.get("PR_QUEUE_PROC", "/proc")


def by_cwd():
    if not branch:
        return None
    try:
        reg = subprocess.run(["git", "-C", hub, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    path = None
    for line in reg.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == "branch refs/heads/" + branch and path:
            break
    else:
        return None
    want = os.path.realpath(path)
    for e in sorted(os.listdir(proc), key=lambda x: int(x) if x.isdigit() else 0):
        if not e.isdigit():
            continue
        try:
            with open(os.path.join(proc, e, "comm")) as f:
                if f.read().strip() not in AGENT_COMMS:
                    continue
            cwd = os.path.realpath(os.path.join(proc, e, "cwd"))
        except OSError:
            continue
        if cwd == want or cwd.startswith(want + os.sep):
            return e
    return None


def by_subscription():
    # `pr create` records its author as a gate-watch subscription on (repo, PR number),
    # so a session that works its worktree from outside it still owns the PR. Newest first, and
    # only while that pid is still the same agent process: a recycled pid names a stranger.
    if not (pr and repo):
        return None
    os.environ["GATE_WATCH_PROC"] = proc
    try:
        spec = importlib.util.spec_from_file_location("gate_watch", os.path.join(self_dir, "gate-watch.py"))
        gw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gw)
        subs = gw.pr_authors()   # not open_subscriptions: after a merge the author's has ended
    except Exception:
        return None
    for s in sorted(subs, key=lambda s: s.get("at", ""), reverse=True):
        pid = str(s.get("pid"))
        if (s.get("repo"), str(s.get("pr"))) == (repo, pr) and gw.comm(pid) in AGENT_COMMS \
                and gw.proc_start(pid) == str(s.get("proc_start")):
            return pid
    return None


owner = by_cwd() or by_subscription()
if owner:
    print(owner)
OWNER
}

# The seat a pid registered with Session-Notify, as " (seat CC-1)", or nothing. A label
# for the reader beside a pid that means nothing to a human; absent registry, absent label.
_seat_of_pid() {
    command -v session-notify-list >/dev/null 2>&1 || return 0   # silence is correct: no registry, no label; the pid still prints
    session-notify-list --json 2>/dev/null | python3 -c '
import json, sys
pid = sys.argv[1]
for r in json.load(sys.stdin):
    m = r.get("meta") or {}
    if str(m.get("pid")) == pid and m.get("seat"):
        print(" (seat %s)" % m["seat"]); break
' "$1" 2>/dev/null
}

_watch_undrafted() {
    # THE OWNER FIRST. This used to resolve only the session that ran the queue, so a
    # drain run by anyone else armed the watch for a session with no stake in the PR while its
    # author heard nothing. The draining session stays as the fallback: it is the right answer when
    # it IS the owner, and better than silence when no owner resolves.
    _wp=$(_owner_pid "$(_head_ref_of "$1")" "$1")
    [ -n "$_wp" ] || _wp="${PR_QUEUE_DETACHED:-${FORGE_TOOLS_WAKE_PID:-}}"
    [ -n "$_wp" ] || return 0   # no session to wake: a cron or bare-shell drain, and nobody is owed a message
    _wsha=$(_head_of "$1")
    if _wo=$(FORGE_TOOLS_WAKE_PID="$_wp" python3 "$TOOLS_DIR/gate-watch.py" register "$REPO" "$_wsha" "PR #$1 (un-drafted by the queue)" 2>&1); then
        log "  batch: #$1 gate-watched for pid $_wp -- it is peer-messaged the verdict on $_wsha"
    else
        log "  batch: #$1 NOT gate-watched: $(printf '%s' "$_wo" | tail -n 1)"
    fi
    return 0
}


# _notify_owner <branch> <one-line subject> [pr] -- tell the session that can act, or say nobody could
# be found. Never fails the caller: a notification that can refuse a merge is a worse bug than a
# missed notification.
# _peer_send_missing -- print why the peer-message command cannot run, or nothing when it can.
# It is reached BY COMMAND NAME (Session-Notify provides it), and a missing one must be said as that,
# not read as "the session could not be messaged".
_peer_send_missing() {
    command -v "${PR_QUEUE_PEER_SEND:-session-notify}" >/dev/null 2>&1 && return 1
    printf 'MISSING COMMAND `%s` (from the Session-Notify repo) -- not on PATH' "${PR_QUEUE_PEER_SEND:-session-notify}"
}

_notify_owner() {
    _no_pid=$(_owner_pid "$1" "${3:-}")
    if [ -z "$_no_pid" ]; then
        log "  no live session resolves for $1 -- nobody was messaged about: $2"
        return 0
    fi
    if _ps_why=$(_peer_send_missing); then
        log "  could not message pid $_no_pid about $1: $_ps_why"
        return 0
    fi
    printf '[machine-nudge: pr-queue.sh] %s\n\nbranch: %s\nYou own the worktree on it; this ran from another session.\n' "$2" "$1" \
        | "${PR_QUEUE_PEER_SEND:-session-notify}" --to "claude-code:$_no_pid" \
            --from-name "pr-queue.sh (acting on your branch)" >/dev/null 2>&1 \
        && log "  messaged pid $_no_pid$(_seat_of_pid "$_no_pid"), which owns $1: $2" \
        || log "  could not message pid $_no_pid about $1: $2"
    return 0
}

_set_title() {
    "$API" "/api/v1/repos/$REPO/pulls/$1" -X PATCH -H 'Content-Type: application/json' \
        -d "$(python3 -c 'import json,sys; print(json.dumps({"title": sys.argv[1]}))' "$2")" -o /dev/null >/dev/null 2>&1
}

# DRAFTS AND A STANDING INTEGRATION PR (operator, 2026-09-08).
#
# As first landed, the batch was assembled only when every member was GREEN on its own head, the
# integration PR existed only while the drain waited on it, and its branch was deleted after every
# attempt. Between drains nothing was visible, and a member sat as an ordinary, mergeable PR that a
# hand `pr merge` could take out of the batch. Three changes, each measured against the stub:
#   ADMISSION IS "NOT RED": a member whose own head is pending joins the batch; the integration run
#     gates the same bytes, and a red member is still skipped before assembly (cheap pre-filter).
#   MEMBERS ARE DRAFTS WHILE BATCHED: the forge has no draft field to set, so the `WIP: ` title
#     prefix is the draft (it is what the forge itself reads as draft); restored before the mark,
#     or when the member leaves the batch red or dropped.
#   THE INTEGRATION PR STANDS: a gate that does not settle within the wait leaves the PR and its
#     branch open and SAYS so; the next drain resumes it if its members' heads have not moved,
#     and closes and rebuilds it if they have. Red still closes and retires it as before.
# MEMBER RUNS: ONLY THE SUITE IS SKIPPED, AND ONLY FOR DRAFTS (operator, 2026-09-14).
#   The pytest job carries `if: !draft || held` (test.yml). A planned batch opens its members as
#   `WIP: `, so they run the cheap jobs, which caught every red member measured across one map's tickets, and
#   skip the suite, which the integration PR runs over the same bytes. Admission is unchanged: the
#   cheap runs are a member's own verdict, so `none` still means a workflow did not fire.
#   Two guards stop the skip reading as a pass: `wait_for_green` returns 4 for a green head whose
#   suite skipped, and `approve_one` refuses a draft or reopens an un-drafted PR to run its suite.
#   Measured before building: the payload carries `draft`, derived from the
#   prefix; and `Do: manually-merged` does not consult branch protection, so a member is marked
#   without the forge checking its contexts -- the integration run and the landed-head check
#   below are what protect `main`.
_title_of() {
    _pr_field "$1" title
}

UNDRAFTED=""; FOUND_READY=""
# _landed <n> -- this run LANDED it, so nothing is owed. Struck here rather than left to the forge's
# `state`: a listing can lag a merge, and a merged PR titled `WIP: ` is a known shape -- the worse
# of the two errors. `restore_drafts` still reads the state, as the second guard and not the first.
_landed() {
    _ln=""
    for _lx in $UNDRAFTED; do [ "$_lx" = "$1" ] || _ln="$_ln $_lx"; done
    UNDRAFTED="$_ln"
}

# A RUN STOPS WHEN ITS OWN CODE IS SUPERSEDED. The stale-checkout refusal above runs
# ONCE, at launch; a drain then repeats until the queue stops changing and can outlive
# the code it started with by an hour, landing PRs -- including fixes to this very file -- under
# rules main has already replaced. Measured 2026-09-22: a drain started at 21:17, an
# update-before-gate fix was approved at 21:39, and at 21:41 the drain still un-drafted a PR two
# commits behind main and reopened it on that stale head -- the exact defect the fix removes.
#
# THE FILE, NOT THE TREE. The launch guard asks "is $HUB behind hub/main", which a drain answers
# NO the moment it lands its first PR, so that question cannot be re-asked mid-run. The one that
# can: is the script this process is executing still the one on hub/main?
#
# CALLED WHERE A LANDING IS DECIDED -- `_approve_once` and `try_batch` -- so a split member, a
# serial PR and a batch all pass through it. It EXITS; the EXIT trap re-drafts what this run
# un-drafted (`restore_drafts`) and the detached run messages its session. Only a PROVEN change
# stops: an unreadable blob continues, as every run did before this, since the launch guard
# already vouched for the code this process started with.
#
# WHERE "MAIN" IS. When the consumer carries its own scripts/pr-queue.sh (the layout this was written
# for), its main is the reference. An INSTALLED Forge-Tools runs from its own checkout and the
# consumer carries none, so the reference is that checkout's upstream -- without this second arm the
# check found nothing to compare and passed every run, however far main had moved.
_stop_if_code_superseded() {
    git -C "$HUB" fetch -q "$FT_REMOTE" 2>/dev/null
    if ! _cur_blob=$(git -C "$HUB" rev-parse -q --verify "$FT_REMOTE/main:scripts/pr-queue.sh" 2>/dev/null); then
        _self_top=$(git -C "$SELF_DIR" rev-parse --show-toplevel 2>/dev/null) || return 0  # not a checkout: unproven, so continue
        # AN UNCOMMITTED EDIT HAS NO UPSTREAM TO BE BEHIND. A development checkout running its own
        # edited pr-queue.sh differs from upstream by construction, and this stopped every drain with
        # "changed since this run started" -- false, and it sent the developer hunting for a landing.
        # Said once and skipped: a dirty checkout is a developer's, not an installed queue's.
        if ! git -C "$_self_top" diff --quiet HEAD -- "$SELF" 2>/dev/null; then
            [ -n "${_SUPERSEDE_SKIP_SAID:-}" ] || log "NOTE: $SELF has uncommitted changes, so it is not compared with its upstream -- the superseded-code check is skipped for this run."
            _SUPERSEDE_SKIP_SAID=1
            return 0
        fi
        git -C "$_self_top" fetch -q 2>/dev/null
        _cur_blob=$(git -C "$_self_top" rev-parse -q --verify "@{upstream}:${SELF#"$_self_top"/}" 2>/dev/null) || return 0  # no upstream: unproven, so continue
    fi
    _run_blob=$(git hash-object "$SELF" 2>/dev/null) || return 0  # cannot hash this run's own file: unproven, so continue
    [ -n "$_cur_blob" ] && [ -n "$_run_blob" ] && [ "$_cur_blob" != "$_run_blob" ] || return 0  # the same code main carries: nothing superseded
    log "STOPPING: scripts/pr-queue.sh on $FT_REMOTE/main changed since this run started ($_run_blob -> $_cur_blob)."
    log "  Landing more under the old code would apply rules main has replaced. Nothing is"
    log "  in flight here; start a fresh drain from a tree level with $FT_REMOTE/main."
    exit 2
}

# Defined ABOVE its first caller, `undraft_member` (the define-before-call test); its rationale is
# the "A DRAIN LEAVES WHAT IT DID NOT LAND" block below.
restore_drafts() {  # restore_drafts [spare] -- re-draft every unlanded un-draft but `spare`
    _rkeep=""
    for _rn in $UNDRAFTED; do
        if [ "$_rn" = "${1:-}" ]; then _rkeep="$_rn"; continue; fi
        _rf=$("$API" "/api/v1/repos/$REPO/pulls/$_rn" 2>/dev/null | PFX="$DRAFT_PREFIX" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
if d.get("state") == "open" and not d.get("merged") and not (d.get("title") or "").startswith(os.environ["PFX"]):
    print(d.get("title") or "")' 2>/dev/null)
        [ -n "$_rf" ] || continue
        if _set_title "$_rn" "$DRAFT_PREFIX$_rf"; then
            log "  #$_rn RE-DRAFTED -- this run un-drafted it and did not land it, so it is a waiting draft again ('$DRAFT_PREFIX' title prefix)"
        else
            log "  #$_rn COULD NOT BE RE-DRAFTED -- it is open and un-drafted; it will run the full suite on every push until a drain lands it"
        fi
    done
    UNDRAFTED="$_rkeep"
}

# undraft_member <n> [merged]
# `merged` says this un-draft is POST-LANDING bookkeeping, so no watch is registered. A watch is
# owed only where the PR is LEFT OPEN and will therefore run its own suite. On the landed path the
# PR is already closed and merged, its head never runs one, and a watch there polls a permanently
# `skipped` pytest until it gives up at 7200s -- then reports "NOT a verdict ... do not read this
# as green" about a change that is already on main (measured on two PRs).
undraft_member() {
    [ "${2:-}" = merged ] && _landed "$1"
    # AT MOST ONE UNLANDED UN-DRAFT PER RUN. The front is ONE PR; re-drafting only at EXIT
    # let a run that carries on (the repeat, a split, a red batch's members) leave each PR it
    # un-drafted and did not land open beside the next one: measured 2026-09-22, one drain had one PR
    # (red) and another un-drafted at once. So every earlier unlanded un-draft goes back first. `$1` is
    # spared: re-drafting the PR about to be un-drafted would flip its title twice, and each flip is a
    # `pull_request` edit that starts a suite.
    [ "${2:-}" = merged ] || restore_drafts "$1"
    _t=$(_title_of "$1")
    case "$_t" in
        "$DRAFT_PREFIX"*)
            if _set_title "$1" "${_t#"$DRAFT_PREFIX"}"; then
                log "  batch: #$1 un-drafted"
                [ "${2:-}" = merged ] || _watch_undrafted "$1"
                # Owed back as a draft if this run does not land it -- see `restore_drafts`. NOT on the
                # post-landing un-draft: `_landed` above has just struck it, and appending it again here
                # re-drafted every member of a batch that landed (caught by the standing-batch tests).
                if [ "${2:-}" != merged ]; then
                    case " $FOUND_READY " in *" $1 "*) ;; *) UNDRAFTED="$UNDRAFTED $1" ;; esac
                fi
            fi ;;
    esac
}

# A DRAIN LEAVES WHAT IT DID NOT LAND IN THE DRAFT STATE IT FOUND IT IN.
#
# Measured 2026-09-21: four open PRs, every one OPENED as a draft, every one un-drafted by a drain
# that then did not land it -- a red suite after the un-draft, a head that moved under the wait, a
# drain killed mid-poll. `undraft_member` had a caller on each of those paths and `draft_member` had
# none, so un-drafting was ONE-WAY. A PR left that way runs the full suite on every push and is
# outdated by every landing, which is the N(N+1)/2 cost the draft-first queue exists to remove; the
# walk's own premise -- "`pr create` never opens a ready PR behind the front" -- was true of
# `pr create` and false of this script.
#
# So the un-draft is recorded, and on the way out -- ANY way out, which is why this is a trap and not
# a call at each return -- whatever is still open and unmerged gets its prefix back. The next drain
# meets it as a waiting draft and un-drafts it again when it is the front: one suite per attempt,
# not one per push.
#
# FOUND_READY is the other half. `--batch` drafts members that were never drafts and un-drafts them
# on red; those are left as they were found, which is READY.
#
# NOT COVERED: SIGKILL runs no trap. A drain that dies that way still leaks, and the next un-draft of
# the same PR is what heals it.
trap restore_drafts EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# _labels_among <number> <name>... -- one 0/1 digit per name, in order, for whether the PR carries
# it. Exit 2, printing nothing, when the label set is unreadable: not JSON, or JSON that is not a
# list of labels -- an error object once iterated as strings, raised, exited 1, and read as "not
# labelled", so a hold nobody could read merged. `_has_label` is this for one name.
_labels_among() {
    _la_pr=$1; shift
    "$API" "/api/v1/repos/$REPO/issues/$_la_pr/labels" 2>/dev/null | python3 -c '
import json, sys
try:
    ls = json.load(sys.stdin)
except Exception:
    raise SystemExit(2)
if not isinstance(ls, list) or not all(isinstance(l, dict) for l in ls):
    raise SystemExit(2)
names = {l.get("name") for l in ls}
print("".join("1" if want in names else "0" for want in sys.argv[1:]))
' "$@" 2>/dev/null
}

# _has_label <number> <name> -- 0 if the PR currently carries it. The read-back.
_has_label() {
    case $(_labels_among "$1" "$2") in 1) return 0 ;; 0) return 1 ;; *) return 2 ;; esac
}

# unlabel_one <number> <name> -- the detach, generalised. Both labels this script attaches are
# CLAIMS ABOUT A DECISION -- "a human should look", "a human said merge" -- so both are wrong
# the moment the PR merges, in the same way and for the same reason.
unlabel_one() {
    _ln=$1; _lname=$2
    _has_label "$_ln" "$_lname" || return 0     # nothing to remove, or unreadable -- either way, done
    _lid=$(label_id "$_lname")
    # NOT A SILENT RETURN, and invariant 17 is why this line got read again. Reaching here means
    # the read-back above said the label IS on this PR while the repo's label list does not
    # resolve its name -- a contradiction (a rename mid-flight, or PR_QUEUE_LABEL flipped between
    # the hold and the merge). Returning 0 quietly would leave the stale `queue:needs-human-review` on a merged
    # PR, which is the precise thing this function exists to prevent, so it says so instead.
    [ -n "$_lid" ] || {
        log "  #$_ln carries '$_lname' but the repo does not resolve that name -- NOT cleared;"
        log "    remove it by hand, a merged PR must not claim a decision that is already made."
        return 0
    }
    "$API" "/api/v1/repos/$REPO/issues/$_ln/labels/$_lid" -X DELETE -o /dev/null >/dev/null 2>&1
    if _has_label "$_ln" "$_lname"; then
        log "  '$_lname' still on merged #$_ln -- remove it by hand; a merged PR must not still claim it"
    else
        log "  cleared '$_lname' from #$_ln"
    fi
    return 0
}


_has_held_label() { _has_label "$1" "$HELD_LABEL"; }


label_held() {
    _ln=$1
    _lid=$(held_label_id)
    # A SKIP THAT NAMES ITS OWN FIX. Otherwise this is a line that recurs on every held PR and
    # nobody ever acts on, because acting on it means going and finding out how.
    [ -n "$_lid" ] || {
        log "  '$HELD_LABEL' not found in $REPO -- the hold stands, unlabelled. Create it with:"
        log "    hub-api /api/v1/repos/$REPO/labels -X POST -H 'Content-Type: application/json' \\"
        log "      --data-binary '{\"name\":\"$HELD_LABEL\",\"color\":\"#fbca04\"}'"
        log "  or set PR_QUEUE_LABEL=0 to stop asking."
        return 0
    }
    "$API" "/api/v1/repos/$REPO/issues/$_ln/labels" -X POST -H 'Content-Type: application/json' \
        --data-binary "{\"labels\":[$_lid]}" -o /dev/null >/dev/null 2>&1
    if _has_held_label "$_ln"; then
        log "  labelled #$_ln '$HELD_LABEL'"
    else
        log "  '$HELD_LABEL' did NOT stick on #$_ln -- the hold stands, the label does not"
    fi
    return 0
}

unlabel_held() { unlabel_one "$1" "$HELD_LABEL"; }


# delete_merged_branch <ref> [pr] -- retire a branch hub has just told us landed.
#
# LOCAL goes through `prune-landed-branches-forgejo.sh`, which already asks hub's
# `pulls?state=closed`+`merged` around three measured Forgejo fail-opens. No second
# implementation: every ergonomic git question (`--is-ancestor`, `branch -r --contains`,
# `git cherry`, `main..branch`) answers "not merged" about work squash-merged under the old landing style, and
# git cannot tell you which side of that change a branch is on.
#
# REMOTE NEEDS A GUARD THAT DOES NOT EXIST IN GIT, which is why the pruner deliberately does not
# do it. Its own words: git refuses to delete a branch checked out in ANY worktree, and there is
# NO equivalent protection on the remote side; this box routinely has several sessions with live
# worktrees on branches in that list. That applies HARDER here, because a queue line is by
# definition someone else's branch. So the guard the pruner says is missing is supplied here:
# enumerate the worktrees and refuse the remote delete while any tree holds the ref.
#
# WHAT THAT GUARD DOES AND DOES NOT COVER, stated as narrowly as it is measured: it sees THIS
# REPOSITORY'S worktrees. Not this box -- a separate CLONE on the same machine is equally
# invisible, and one that matters exists. Measured 2026-08-21:
#
#     /home/user/app/.git   and   /opt/app-deploy/.git
#
# are different git dirs tracking the SAME hub remote, and `git worktree list` here returns 0
# lines mentioning `/opt/app-deploy` -- which is the tree cron actually deploys from. It sits on
# `main` today, so nothing is at risk, and the guard is right to stop where it does: git offers no
# way to enumerate arbitrary clones. The first draft of this comment said "this box only", which
# invites exactly the wrong conclusion about the one same-box checkout worth worrying about.
#
# THE RESIDUAL, named rather than left to be discovered: a FOREIGN PR merged through `approve`,
# whose branch is checked out only in such a clone, has its remote branch deleted with nothing to
# refuse it. Low impact -- the clone keeps its local ref -- but it is the case the guard cannot see.
#
# The queue's own `$WT` is added `--detach`, so it holds no branch and cannot block a delete of
# the branch it just merged.
delete_merged_branch() {
    _dref=${1:-}; _dpr=${2:-}
    [ -n "$_dref" ] || { log "  no branch name recorded -- nothing deleted"; return 0; }
    _prune="$TOOLS_DIR/prune-landed-branches-forgejo.sh"
    if [ -x "$_prune" ]; then
        # PRUNE_REPO carries the TARGET. $HUB is the hub checkout even under `--repo`, so
        # the pruner's own `hub`-remote derivation names the wrong repo for a target-repo branch and
        # its KEEP verdict is then reached from a history that could not hold the answer.
        PRUNE_REPO="$REPO" sh "$_prune" --delete "$_dref" 2>&1 | sed 's/^/    /'
    else
        log "  no pruner at $_prune -- local branch $_dref left alone"
    fi
    _held=$(git worktree list --porcelain 2>/dev/null | sed -n "s#^branch refs/heads/##p" \
            | grep -Fx "$_dref")
    if [ -n "$_held" ]; then
        log "  NOT deleting remote $_dref: a live worktree still has it checked out"
        # The refusal is CORRECT -- yanking a branch from under a live worktree is not
        # something to do -- but it was reported only into this run's log, which belongs to whoever
        # drained. The one session that can release the worktree heard nothing, so four merged
        # branches survived on hub on 2026-09-19/20 and were cleaned up by hand or not at all.
        # The PR number reaches the subscription arm: an author working its worktree by per-command
        # `cd` has no cwd in it, and without the number resolved to nobody (measured 2026-09-26 on a queue-landed PR).
        _notify_owner "$_dref" "merged, but its remote branch was NOT deleted: your worktree still has it checked out. Release it and delete the branch." "$_dpr"
        return 0
    fi
    if git push "$REMOTE" --delete "$_dref" -q 2>/dev/null; then
        log "  deleted remote branch $_dref"
    else
        log "  remote $_dref not deleted (already gone, or the push was refused)"
    fi
}

# _landed_elsewhere <number> <ref> <why-it-matters-here> -- true, having said so and cleaned up, when
# the forge reports #N merged. ONE body for both places merge_queued asks.
#
# THE CLEANUP IS RUN FROM THE LOSING SIDE ON PURPOSE. The merging party's own 200 path runs it --
# but only if that party was a merge_queued(). A web-UI merge or a hand `pr merge` skips the hold
# label, the merge-requested label and the branch retire entirely, and then nothing ever runs
# them. Both unlabel calls return early when the label is absent and the pruner reports an
# already-gone branch rather than failing, so this costs nothing when the winner already did it.
#
# RETURNS 0 TO ITS CALLER'S CALLER TOO, because the postcondition merge_queued exists to establish
# holds: the PR is merged. Every caller counts a 0 as ITS merge, so the count is kept here and the
# drain's summary subtracts it out loud. This comment's first version said the tally "cannot"
# tell the two apart; it could not only because nothing counted, and a drain reporting another
# run's merge as its own is the misattribution measured the same night.
LANDED_ELSEWHERE=0
_landed_elsewhere() {
    [ "$(_pr_is_merged "$1")" = yes ] || return 1
    log "  #$1 is ALREADY MERGED, and not by this run -- $3"
    unlabel_held "$1"
    unlabel_one "$1" "$MERGE_LABEL"
    delete_merged_branch "$2" "$1"
    LANDED_ELSEWHERE=$((LANDED_ELSEWHERE + 1))
    return 0
}

# _refusal_detail <captured-output> -- print a refused call's OWN WORDS, indented.
#
# `hub-api.sh` refuses a write from a client behind `hub/main` with `REFUSING: this client is
# STALE ...`, which names the cause and the remedy: update the checkout and re-run. Both refusal
# sites below threw that away and logged only `http=${_code:-unreadable}`, and the drain's caller
# then added "main moved under us (another drain, a hand merge or the web UI)". So a specific,
# actionable failure was downgraded to a generic one that suggests the WRONG remedy -- "main moved"
# reads as a benign race to re-poll, and re-polling never refreshes a checkout.
#
# MEASURED 2026-09-21 on a target repo's PR: a hub commit landed four seconds before
# that merge POST. Seen again 2026-09-22 on a batch PR, which is how the second site was found.
#
# BOTH SITES, because they fail differently and only one is in the ticket. `merge_queued` CAPTURES
# the client's stderr in `$_out` and then drops it when logging; the batch landing never captured
# it at all -- it piped the client straight into `sed`, so the words were consumed by the pipe.
# Fixing only the site the ticket names would have left the measured instance untouched.
_refusal_detail() {
    printf '%s\n' "$1" | sed '/^[[:space:]]*$/d; s/^/  /' | while read -r _rd; do log "$_rd"; done
}

merge_queued() {
    _num=$1; _title=$2; _sha=$3; _ref=${4:-}; _try=0
    while [ "$_try" -lt 3 ]; do
        # THE SAME QUESTION BEFORE THE POST, EVERY TRY. A PR that landed while this run
        # waited on its gate reads as `current` to the ancestry check below -- after a fast-forward
        # main IS its head -- so nothing else stops a POST whose only possible answer is a 405.
        _landed_elsewhere "$_num" "$_ref" "it landed while this run waited; no merge attempted" && return 0
        # ---- SELF-HEAL, BEFORE SPENDING A REFUSED MERGE TO LEARN THE SAME THING ----
        #
        # `is-ancestor` answers for free what a 405 costs a round trip to discover, and nothing
        # else in the API substitutes for it. Measured 2026-08-21 on this repo: `mergeable` is
        # True on an outdated PR -- it answers "does this apply cleanly", not "will branch
        # protection accept it" -- and `base.sha` is a LIVE READ of main's tip rendered inside
        # the per-PR object, identical on an open PR that is behind, on one merged minutes ago,
        # and on one merged eight weeks ago. Three fields in one listing, none predicting the
        # merge.
        #
        # THE 405 ARM BELOW STAYS. This check cannot be atomic with the POST, so main can move
        # in the gap; belt and braces, not one replacing the other.
        _rs=$(pr_head "$_num")
        if [ -n "$_rs" ]; then
            # A MOVED HEAD IS A STOP, NOT A REFRESH. We waited green on `$_sha`; if hub now has
            # something else, the checks we are about to merge on measured a tree that is no
            # longer the one that would land.
            if [ "$_rs" != "$_sha" ]; then
                log "  #$_num's head MOVED: green was measured on $_sha, hub now has $_rs"
                log "  -- NOT merging code no check-run examined"
                MQ_MOVED=yes
                return 1
            fi
            # AGAINST THE MAIN OF THE REPO BEING MERGED INTO. This was `hub/main` unconditionally, which
            # on a --repo target is another repo's history: every target PR read as behind, the update
            # moved nothing, and three tries ended in STOPPING (measured on a target repo, 2026-09-15).
            # Fetching `main` from $REMOTE also refreshes hub/main when the target is the hub.
            _main=$(git fetch -q "$REMOTE" "+$BASE_BRANCH:$QREF/main" 2>/dev/null && git rev-parse -q --verify "$QREF/main") || _main=""
            if [ -z "$_main" ]; then
                log "  cannot read $REPO's $BASE_BRANCH to tell whether #$_num is current -- STOPPING"; return 1
            fi
            git merge-base --is-ancestor "$_main" "$_rs" 2>/dev/null
            case $? in
                0) : ;;                                       # current -- proceed
                1) log "  #$_num is behind $BASE_BRANCH -- updating BEFORE attempting the merge"
                   _pre_update=$_sha
                   update_and_rewait "$_num" || return $?   # rc 6 = RED after the rebase
                   # The OTHER head-moving step. Naming the step that moved it, and both
                   # shas, is what lets a later reader attribute the change instead of inferring it.
                   # SAYS WHICH ACTUALLY HAPPENED. This asserted a move
                   # unconditionally and printed `X -> X` for an unchanged head, contradicting
                   # `update_and_rewait`'s own "did not move" line one line earlier, at the single
                   # moment a reader could have diagnosed the deadlock. The same was measured in the
                   # sentence on the 405 arm; this is the rc-1 arm.
                   #
                   # THE COMPARISON MOVED INTO `_log_head_move`. An earlier fix put it HERE and
                   # its note above correctly said the 405 arm still had the bug; the ff-repair arm
                   # had it too and was named nowhere. Three sites, one of them right, is how the
                   # second instance was reported as a new finding. One definition instead.
                   _log_head_move "$_num" "" "$_pre_update" "$NEW_SHA"
                   _sha=$NEW_SHA; _try=$((_try + 1)); continue ;;
                *) log "  cannot tell whether #$_num is current with main -- STOPPING"; return 1 ;;
            esac
        fi

        # `pr merge` PRINTS the HTTP code and EXITS 0 REGARDLESS, so gating on its exit status
        # reads a refusal as a merge. Parse the code.
        #
        # `$_sha` IS PASSED AS THE EXPECTED HEAD, which closes the gap the pre-check above cannot.
        # That pre-check reads the head, compares it to the sha green was measured on, and then
        # POSTs -- a read-then-act, with a window between the two that no amount of re-reading
        # shortens to zero. `head_commit_id` moves the comparison INTO the write, so the forge
        # refuses a moved head atomically. With no queue lock this is THE guard against any other
        # merge -- another drain, a hand `pr merge`, or the WEB UI (measured -- a PR landed that way
        # at 21:59:31 with no session in the loop).
        #
        # A REFUSAL HERE IS NOT REPAIRED, and that is correct. It lands in the `*)` arm below,
        # which prints the code and STOPS. The head moving under a verified-green merge means
        # something happened this drain did not see, and the right response to that is to stop
        # and let a human look, not to re-read and try again.
        # SAY WHICH PATH THIS MERGE TOOK, at the one moment it is knowable.
        #
        # Everything `merge_queued` guarantees over a hand-called `pr merge` -- the review hold,
        # branch auto-retire, hold-labelling, the `head_commit_id` CAS -- is invisible afterwards:
        # the subject form is byte-identical between the two paths (measured) and branch
        # survival conflates "the queue retired it" with "a human tidied up later" (measured on
        # a real PR). This env var is the whole recording mechanism; `hub-api.sh pr merge` turns it into
        # a `Merge-Path:` trailer on the merge commit, which is immutable and survives cleanup.
        #
        # It is set HERE rather than exported at the top of the file on purpose: it must cover this
        # call and nothing else, so a `pr merge` run by any other code path in this script -- or by
        # a human in the same shell -- still records itself honestly as `hub-api-direct`.
        # THE SUBJECT NO LONGER CARRIES A HAND-WRITTEN `(#N)`. The client lands
        # by fast-forward, which writes no merge commit and therefore no subject at all; the PR
        # number lives on the forge's PR record. Under an operator's `HUB_API_MERGE_STYLE=merge`
        # the client appends the number itself (it has `$_num` in argv), so the queue hand-writing
        # one was only ever a second copy of the client's own rule.
        # AGAIN, AT THE LAST MOMENT: a freeze set while this run waited on a gate stops
        # this merge, not only the next run. Exits the script, as every entry check does.
        refuse_if_frozen "merge #$_num"
        # rc 12, NOT 1. A close keyword is the AUTHOR'S to fix and is per-member, so
        # it means "this PR cannot land right now", never "this run cannot continue". Returning 1
        # fell into the caller's `*)` and stopped the whole drain, which is the third instance of
        # a family whose other two cases were already fixed.
        refuse_if_closes_wayfinder "$_num" || return 12
        _out=$(HUB_API_MERGE_PATH=pr-queue "$API" pr merge "$REPO" "$_num" "$_title" "$_sha" 2>&1)
        _code=$(printf '%s\n' "$_out" | sed -n 's/.*http=\([0-9][0-9]*\).*/\1/p' | tail -n 1)
        case "$_code" in
            200) log "  merged #$_num \"$_title\" -- main is now $_sha (fast-forward: the head CI tested, no merge commit)"
                 printf '%s\n' "$_out" | sed -n 's/^merge-path: /  /p' | while read -r _l; do log "$_l"; done
                 # BEFORE the branch delete, because this one is about the PR and that one can
                 # print several lines. Fires on the `approve` path too, deliberately: a held PR
                 # merged later is merged the same way a queued one is, and a cleanliness that
                 # depended on which path a PR took would rot.
                 unlabel_held "$_num"
                 # Same reasoning, the other label: a merged PR still asking to be merged is a
                 # request that outlived its answer, and it is the shape `merge-requested` would
                 # act on again on the next run.
                 unlabel_one "$_num" "$MERGE_LABEL"
                 # Deliberately only on THIS path: the merge this script performed.
                 # A PR already merged elsewhere (the 405 `_landed_elsewhere` arm) was not landed
                 # by us, and its prompt belongs to whoever landed it -- covering it here would
                 # print a reminder to a session that did nothing and skip the one that did.
                 name_open_wayfinder_tasks "$_num"
                 delete_merged_branch "$_ref" "$_num"
                 return 0 ;;
            500)
                # A fast-forward that cannot fast-forward answers 500 with `DivergingFastForwardOnly`
                # (forge-probe-269 probe 6). Same cause as the 405 -- main moved -- so the same
                # repair. Any OTHER 500 is a dependency block or a forge fault, and stops.
                case "$_out" in
                    *DivergingFastForwardOnly*|*"Not possible to fast-forward"*)
                        log "  #$_num cannot fast-forward -- main moved in the gap; repairing"
                        _pre_update=$_sha
                        update_and_rewait "$_num" || return $?   # rc 6 = RED after the rebase
                        _log_head_move "$_num" " (ff repair)" "$_pre_update" "$NEW_SHA"
                        _sha=$NEW_SHA ;;
                    *)  log "  merge of #$_num refused: http=500 -- STOPPING"
                        printf '%s\n' "$_out" | sed -n 's/^reason: /  /p' | while read -r _l; do log "$_l"; done
                        return 1 ;;
                esac ;;
            405)
                # ASK BEFORE REPAIRING. `merged` is the one reading of a 405 under which repair is
                # not merely wasted but incoherent -- there is no base to catch up to, because the
                # branch is already in it.
                _landed_elsewhere "$_num" "$_ref" "the 405 is that, not an outdated base; nothing to repair" && return 0
                # ASK THE OTHER QUESTION A 405 CANNOT ANSWER ABOUT ITSELF. A DRAFT is
                # refused the merge whatever its base is, so there is nothing for an update to
                # repair and the loop below cannot end in a merge. rc 11 so the caller can say WHY
                # and skip rather than halt the queue: main has NOT moved in this case, so the next
                # PR's freshness is still established -- which is exactly what the generic stop line
                # would deny.
                if [ "$(_pr_is_draft "$_num")" = yes ]; then
                    log "  #$_num took a 405 and the forge says it is a DRAFT -- NOT repairing:"
                    log "    no update can make a draft mergeable, and main has not moved."
                    log "    Un-draft it, then drain again:"
                    log "      hub-api \"/api/v1/repos/$REPO/pulls/$_num\" -X PATCH \\"
                    log "        -H 'Content-Type: application/json' -d '{\"title\":\"<title without the $DRAFT_PREFIX prefix>\"}'"
                    return 11
                fi
                log "  #$_num took a 405 despite the pre-check -- main moved in the gap; repairing"
                _pre_update=$_sha
                update_and_rewait "$_num" || return $?   # rc 6 = RED after the rebase
                # SECOND update site. Missing this one is how the fix stayed half-done:
                # the ancestry pre-check above catches the common case, but the measured transcript
                # went through HERE, so attributing only the other site would have left the
                # measured instance unattributed.
                _log_head_move "$_num" " (405 repair)" "$_pre_update" "$NEW_SHA"
                _sha=$NEW_SHA ;;
            *)  log "  merge of #$_num refused: http=${_code:-unreadable} -- STOPPING"
                _refusal_detail "$_out"
                return 1 ;;
        esac
        _try=$((_try + 1))
    done
    log "  #$_num still unmerged after $_try attempts -- STOPPING"
    return 1
}

_approve_once() {
    _n=$1; _verb=${2:-approve}; MQ_MOVED=""; APPROVE_REGATES=""
    _stop_if_code_superseded

    # Piped, and the emptiness checks below are what stand in for the discarded exit status:
    # every failure here -- an unreachable forge, a 404, unparseable JSON -- yields empty
    # fields, and each is refused by name rather than falling through to a merge.
    _info=$("$API" "/api/v1/repos/$REPO/pulls/$_n" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
print(d.get("title") or "")
print((d.get("head") or {}).get("sha") or "")
print(d.get("state") or "")
# The branch name, so a held PR merged later retires its branch like a queued one does.
# `head.ref` becomes `refs/pull/N/head` once the branch is gone, which is one of the Forgejo
# fail-opens the pruner documents -- so it is passed on as-is and the pruner judges it.
print((d.get("head") or {}).get("ref") or "")
print((d.get("base") or {}).get("ref") or "")
' 2>/dev/null)
    _t=$(printf '%s\n' "$_info" | sed -n 1p)
    _sha=$(printf '%s\n' "$_info" | sed -n 2p)
    _st=$(printf '%s\n' "$_info" | sed -n 3p)
    _hr=$(printf '%s\n' "$_info" | sed -n 4p)
    _br=$(printf '%s\n' "$_info" | sed -n 5p)
    [ -n "$_t" ] && [ -n "$_sha" ] || { log "$_verb: cannot read #$_n from hub -- STOPPING"; return 1; }
    [ "$_st" = open ] || { log "$_verb: #$_n is '$_st', not open -- nothing to merge"; return 1; }
    # A PR INTO ANOTHER BRANCH IS NOT OURS TO LAND. Everything below (the currency check,
    # the update, the merge) measures against $BASE_BRANCH, the repo's landing branch. Measured
    # 2026-09-23 on a fork, a PR into `hub` (the line a consumer vendors) while
    # the fork's `main` tracks upstream: the queue logged "behind main" and was about to update the PR
    # FROM main, carrying declined upstream drift into `hub`. Refused with rc 7, the skip that keeps
    # the queue's position, as a draft refusal does. It is never updated from the wrong branch.
    # ponytail: an ABSENT base.ref (the test stubs, never the real forge) proceeds as before; a
    # present one that differs is the case this exists for.
    if [ -n "$_br" ] && [ "$_br" != "$BASE_BRANCH" ]; then
        log "$_verb: #$_n targets '$_br', not $REPO's landing branch '$BASE_BRANCH' -- REFUSING: the queue measures, updates and lands against '$BASE_BRANCH' only. Check base, head and green checks yourself, then: hub-api pr merge $REPO $_n \"<subject> (#$_n)\" <full-head-sha>"
        return 7
    fi

    # A REPO WITH NO CI CAN NEVER GO GREEN. wait_for_green reads "zero checks registered"
    # as the registration race and polls WAIT_POLLS (30 min) for checks no workflow will register;
    # measured on a fork before it got a gate. So ask the question `pr create` asks
    # (`hub-api.sh repo ci`, base OR head) and refuse NOW, by name. Only a MEASURED `none` on both
    # refuses: an unreadable listing keeps the bounded wait rather than refusing on no evidence.
    _ci=none
    for _cr in "$BASE_BRANCH" "$_sha"; do
        "$API" repo ci "$REPO" "$_cr" >/dev/null 2>&1; _circ=$?
        [ "$_circ" = 0 ] && { _ci=yes; break; }
        [ "$_circ" = 4 ] || _ci=unknown
    done
    if [ "$_ci" = none ]; then
        log "$_verb: #$_n -- $REPO runs NO CI on $BASE_BRANCH or this head, so no check will ever register; SKIPPING now instead of waiting $WAIT_POLLS polls. Gate it (hub-api repo provision $REPO --kind ...), or land it by hand: hub-api pr merge $REPO $_n '<subject> (#$_n)'"
        return 7
    fi

    # CURRENT BEFORE THE GATE, NOT AFTER IT. The merge refuses a PR behind main
    # (`block_on_outdated_branch`), and the rebase it forces moves the head -- so a gate run on a
    # behind head measured code that can never land, and the suite ran twice. Measured 2026-09-22 on
    # a real PR: un-drafted 5 commits behind main, reopened to run the suite on that head, with the
    # merge-time rebase and its second full run still to come. Brought current here, the one run
    # measures what lands. A head whose currency cannot be read keeps the old order: the merge-time
    # check below still catches it.
    _main=$(git fetch -q "$REMOTE" "+$BASE_BRANCH:$QREF/main" 2>/dev/null && git rev-parse -q --verify "$QREF/main") || _main=""
    if [ -n "$_main" ] && git fetch -q "$REMOTE" "+refs/pull/$_n/head:$QREF/pull/$_n" 2>/dev/null; then
        git merge-base --is-ancestor "$_main" "$QREF/pull/$_n" 2>/dev/null; _cur=$?
        if [ "$_cur" = 1 ]; then
            log "$_verb: #$_n is behind $BASE_BRANCH -- updating it BEFORE the gate, so its one suite run measures what would land"
            # A REFUSED UPDATE SKIPS THIS PR; IT DOES NOT STOP THE QUEUE. `|| return $?`
            # handed the drain rc 1, its STOP, so one conflicting PR at the front halted every PR
            # behind it (2026-09-23 00:33). A batch drops a conflicting member and keeps its
            # position; this step keeps that property. Only the PR's owner can clear a conflict.
            _update_head "$_n" || {
                log "$_verb: #$_n could not be brought current (the forge refused its rebase) -- SKIP, position kept; it needs a rebase by its owner"
                _notify_owner "$_hr" "your PR #$_n conflicts with $BASE_BRANCH: the queue could not rebase it, so it is skipped until you rebase and push" "$_n"
                return 7
            }
            _log_head_move "$_n" "" "$_sha" "$NEW_SHA"
            _sha=$NEW_SHA
        fi
    fi
    log "$_verb: merging #$_n \"$_t\" at $_sha"
    wait_for_green "$_sha" "$_n"; _wrc=$?
    # rc 8 is "this sha is no longer the PR's head". It is not a gate verdict: the PR is
    # not red and nothing about it failed, so the drain's generic "checks are not green" line would
    # be false. It still SKIPS -- the next drain reads the new head from scratch, which is the
    # correct next action -- so this returns 7 like every other non-green outcome and only the
    # explanation differs.
    if [ "$_wrc" = 8 ]; then
        log "$_verb: #$_n moved under this wait (force-push during the run) -- its new head will be read fresh"
        APPROVE_MOVED=yes; APPROVE_REGATES=yes
        return 7
    fi
    # rc 10. Same ACTION as 8 (skip, keep position, let the next drain read fresh) and a
    # different CAUSE, so it gets its own words rather than borrowing 8's: nothing moved, the head is
    # simply finished and a cancellation means nothing measured it. Saying "moved under this wait"
    # here would be a false statement about why the PR was skipped.
    if [ "$_wrc" = 10 ]; then
        log "$_verb: #$_n's head is FINISHED and carries a cancellation -- nothing measured it; skipping, its next head will be read fresh"
        APPROVE_REGATES=yes; return 7
    fi
    # A MEMBER THAT LEFT ITS BATCH GETS ITS OWN RUN BEFORE IT CAN LAND ALONE. rc 4 is a
    # green head whose suite skipped because the PR was a draft. Still a draft: refuse, since a run
    # now would skip again. No longer a draft: reopen it, which fires `pull_request` `reopened` with
    # full PR context (the new-tests gate included, which a dispatched run would skip), and wait
    # for that run to register and settle.
    if [ "$_wrc" = 4 ]; then
        case "$_t" in
            "$DRAFT_PREFIX"*)
                log "$_verb: #$_n is a DRAFT ('$DRAFT_PREFIX'), so its suite is skipped -- batch it with 'drain --batch', or remove the prefix and approve again"
                return 7 ;;
        esac
        log "$_verb: #$_n is no longer a draft but its suite skipped on $_sha -- reopening it to run the suite"
        "$API" "/api/v1/repos/$REPO/pulls/$_n" -X PATCH -H 'Content-Type: application/json' -d '{"state":"closed"}' -o /dev/null >/dev/null 2>&1
        "$API" "/api/v1/repos/$REPO/pulls/$_n" -X PATCH -H 'Content-Type: application/json' -d '{"state":"open"}' -o /dev/null >/dev/null 2>&1
        _AWAIT_RERUN=yes wait_for_green "$_sha"; _wrc=$?
        [ "$_wrc" = 0 ] || log "$_verb: #$_n's suite did not run and pass after the reopen -- STOPPING, PR left open"
    fi
    # A RED IS THE ONE DRAIN OUTCOME ONLY THE PR'S OWNER CAN CLEAR. Everything else
    # that lands here is someone else's problem or nobody's: rc 1 is an unclassified state and rc 3
    # is "nothing was measured", both instrument faults the owner cannot fix; rc 8 (a superseded
    # head) and rc 4 (still a draft) return above and are not failures at all. So this
    # notifies on SIX and nothing else.
    #
    # WHY THIS IS NOT "NOTIFY MORE". An earlier announcer is the measured counter-example -- one report
    # records eight messages reaching one session from a single branch, seven unactionable, and the
    # receiver correctly stopped reading after the first. A channel that carries things its reader
    # cannot act on becomes a channel nobody reads, and that is indistinguishable from one that
    # never fires. A red is on the other side of that line: the queue will skip this PR on every
    # future pass until its owner does something, and nobody else can.
    #
    # THE MEASUREMENT THAT MOTIVATED IT (2026-09-21): a drain started by one session found a PR
    # red, and reported it into that session's log. The owner -- live, idle and addressable the
    # whole time -- learned only when a third party relayed it by hand. `_owner_pid`/`_notify_owner`
    # already existed for exactly this and had ONE call site, the prune refusal.
    #
    # ONCE PER PR PER RUN. A drain can re-derive and walk again whenever a pass merged
    # something, so without this the same red PR is re-offered and re-notified every pass.
    if [ "$_wrc" = 6 ]; then
        case " ${_NOTIFIED_RED:-} " in
            *" $_n "*) : ;;
            *) _NOTIFIED_RED="${_NOTIFIED_RED:-} $_n"
               _notify_owner "$(_head_ref_of "$_n")" "your PR #$_n is RED on $_sha and the queue is skipping it until you act. The failing job and its assertion: hub-api pr why-red $REPO $_sha" "$_n" ;;
        esac
    fi
    [ "$_wrc" = 0 ] || { APPROVE_REGATES=yes; return 7; }

    # THE HOLD IS READ HERE, AFTER THE WAIT AND IMMEDIATELY BEFORE THE POST.
    #
    # `_drain_facts` reads the hold once, at the top of the PR's turn, and `wait_for_green` then
    # burns the entire CI wall clock before anything merges. Measured on a real PR: the drain read it at
    # 03:51:18Z and merged at 03:51:24Z, and the author's hold -- correct label, correct verb, read
    # back and confirmed -- landed in between and did nothing. The window is not a race in the
    # abstract; it is minutes wide and it is exactly the interval in which a human watching CI
    # decides to stop something.
    #
    # THIS NARROWS, IT DOES NOT CLOSE. What remains is the latency of the merge POST itself, and no
    # amount of re-reading removes it -- only a precondition the forge evaluates atomically would,
    # and Forgejo offers none. Say so wherever holds are applied (`hub-api.sh pr hold` prints it):
    # a hold is honoured at read time and CANNOT stop a merge already in flight. The author of that PR
    # believed it worked at any point, and that belief is the defect this narrowing does not fix.
    #
    # DRAIN ONLY, AND THE EXEMPTION IS THE POINT -- not an oversight narrowed after the fact.
    #
    # `approve <N>` IS THE DOCUMENTED WAY TO MERGE A HELD PR. The hold path twenty lines below
    # prints `merge it with: sh scripts/pr-queue.sh approve $num` as it applies the label, and
    # `test_the_label_is_cleared_when_the_held_pr_is_finally_merged` asserts that exact sequence:
    # hold, then approve, then the label is detached. A hold check here would make the escape
    # hatch refuse the state it exists to discharge, which is a deadlock, not a safety property.
    # Measured the hard way -- an earlier revision of this change put the check on all three
    # callers and reddened three tests that say so in their names.
    #
    # `merge-requested` NOW HONOURS THE HOLD AND REFUSES -- operator decision, 2026-09-03.
    # It was left undecided here on purpose ("nothing states it is an escape hatch and no test
    # covers `/merge` on a held PR"), and the decision is:
    #
    #   A HOLD IS HONOURED UNLESS THE NEED FOR IT CHANGES, OR A HUMAN APPROVES THE MERGE ITSELF --
    #   by merging through Forgejo's web UI, or by `approve` on the terminal.
    #
    # `/merge` is none of those three. It is a COMMENT, written elsewhere, possibly before the hold
    # existed, by an allowlisted human who may never have seen it -- and the commenter and the
    # holder can be different people. That is the asymmetry with `approve`, which is typed on this
    # box by whoever is running the queue and has just seen the hold in their own terminal.
    #
    # WHAT THIS MAKES `pr unhold` FOR, and it is the reason that verb exists rather than a general
    # override: the "need for review changed" path. A PR that was held, then edited until the
    # review is moot, is released by lifting the hold -- not by merging past it. Before that verb
    # existed there was no way to say that, which is how "merge past it" became a habit
    # (merging past a hold, and leaving a false review artifact behind).
    #
    # So the rule is now: a hold binds EVERY automated merge -- unattended or requested. Only a
    # human acting directly discharges one.
    #
    # `_has_label` returns 2 for an unreadable label set, which is NOT "no hold" -- same rule as
    # `_drain_facts`, refused by name rather than merged on a failed request.
    if [ "$_verb" = drain ] || [ "$_verb" = merge-requested ]; then
        _has_label "$_n" "$HELD_LABEL"; case $? in
            0) if [ "$_verb" = drain ]; then
                   log "$_verb: #$_n acquired '$HELD_LABEL' while its checks ran -- NOT merging"
                   log "  The hold arrived after this PR's turn began and before the merge POST. It is"
                   log "  honoured. Nothing here rewrites its queue position (premise 6)."
               else
                   # NAME THE WAYS OUT. A refusal a reader cannot act on becomes a refusal someone
                   # routes around, which is the habit it produced -- merging past a hold and
                   # leaving a merged PR carrying a false `needs-human-review` artifact.
                   log "$_verb: #$_n carries '$HELD_LABEL' -- NOT merging"
                   log "  A /merge comment does NOT discharge a hold. It is written elsewhere,"
                   log "  possibly before the hold existed, and its author and the holder can be"
                   log "  different people. Three things release this PR, all of them a human"
                   log "  acting directly or the need for review going away:"
                   log "    the need changed :  hub-api pr unhold $REPO $_n"
                   log "    approve it here  :  pr-queue approve $_n"
                   log "    merge it yourself in the Forgejo web UI"
               fi
               return 9 ;;
            1) : ;;
            *) log "$_verb: cannot read the label set of #$_n before merging -- STOPPING"
               log "  An unreadable hold is not an absent hold; nothing merges on a failed request."
               return 1 ;;
        esac
    fi

    merge_queued "$_n" "$_t" "$_sha" "$_hr"; _mq=$?
    # The head moved between green and the merge: not a refusal about main.
    if [ "$_mq" != 0 ] && [ "$MQ_MOVED" = yes ]; then APPROVE_MOVED=yes; return 7; fi
    # A RED on the head the update produced (rc 6) is the SAME answer as a red on the head this
    # function first waited on, which returns 7 above -- the PR is not green and someone must look
    # at it. Reporting it as 8 made `batch_run`, which SKIPS a red member and keeps its position,
    # stop the entire drain instead (measured on a bisect tail).
    # rc 11. Same ACTION as 6 (skip, keep position, let a later drain read it fresh) and a
    # different CAUSE, so it gets its own words rather than borrowing them: the PR is not red and
    # nothing about it failed a gate, it is simply a draft again by merge time. Saying "checks are
    # not green" here would be false, and STOPPING would deny the next PR's freshness on the one
    # 405 cause that does not involve main moving at all.
    case "$_mq" in
        0) : ;;
        6) APPROVE_REGATES=yes; return 7 ;;
        11) log "$_verb: #$_n is a DRAFT again by merge time -- skipping it; its position is kept and a later drain reads it fresh"
            return 7 ;;
        # rc 12. Same ACTION as 6 and 11 (skip, keep position, let a later drain read
        # it fresh once the author rewords), and a third distinct CAUSE, so it says its own words:
        # nothing failed a gate and main did not move -- the body names a ticket a merge would
        # close. The refusal already printed which ticket and how to reword it.
        12) log "$_verb: #$_n would CLOSE an open wayfinder ticket -- skipping it; its position is kept and a later drain reads it fresh once the body is reworded"
            return 7 ;;
        *) return 8 ;;
    esac
    return 0
}

# approve <number> -- merge a PR the queue OPENED and HELD for review.
#
# The other half of the `review` flag below. The queue stops at a held PR rather than merging
# it, so this is how the runway is cleared once a human has looked. It is NOT a general "merge
# any PR" verb: it reuses merge_queued(), which exists because `pr merge` PRINTS its HTTP code
# and EXITS 0 REGARDLESS, and which carries the 405 `block_on_outdated_branch` repair. A human
# merging by hand instead gets neither, and must hand-write `(#N)` -- the omission that put
# e6bf204cd9 on `main` without its number, permanently.
#
# THE TITLE IS READ FROM THE FORGE, not retyped. merge_queued() appends `(#N)` to whatever it
# is given, so a retyped title is a chance to land a subject that does not match the PR.
#
# wait_for_green FIRST, even though the queue already waited before holding. The hold is
# open-ended -- minutes or days -- and `main` may have moved or a re-run may have gone red in
# between. A green measured before the hold is a fact about a tree that may no longer be the
# one being merged. It returns on the first poll when the checks are still green, so the
# ordinary case costs one API call.
#
# THIS BLOCK MUST STAY ABOVE `QUEUE=$(cat)` IN EXECUTION ORDER BUT BELOW THE FUNCTIONS IT
# CALLS. That is why the stdin read moved down here from the top of the file: `cat` blocks on a
# terminal, so a verb that runs before it can never call a function defined after it.
# approve_one <number> [verb] -- the body of `approve`, lifted so `merge-requested` reuses it
# rather than growing a second merge path. Returns 0 merged, 1 unreadable/not-open, 7 red, 8
# refused -- the codes the `approve` verb already published, so callers below just pass them on.
# approve_one <n> [verb] -- gate and land one PR, RE-GATING it when its head moves under the run.
#
# A MOVED HEAD IS STILL THE FRONT. The owner pushed while this run gated it; nothing
# failed. Both places that notice (the wait, rc 8 of `wait_for_green`, and the merge, `MQ_MOVED`)
# used to give the PR up: the wait as a skip "for the next drain", the merge as a bare 1 that became
# 8 and STOPPED THE WHOLE DRAIN as "main moved under us" -- false. Measured 2026-09-22: a push onto
# a PR mid-gate did both in two concurrent drains, and it was re-drafted with nothing left to
# drive the queue. With one operator per queue there IS no next drain to leave it to, so this run
# reads the new head and gates it -- bounded, since an owner who keeps pushing is not converging.
approve_one() {
    _ao_try=0; _ao_max=${PR_QUEUE_MOVED_RETRIES:-2}
    while :; do
        APPROVE_MOVED=""
        _approve_once "$@"; _ao_rc=$?
        if [ "$_ao_rc" != 7 ] || [ "$APPROVE_MOVED" != yes ] || [ "$_ao_try" -ge "$_ao_max" ]; then
            return "$_ao_rc"
        fi
        _ao_try=$((_ao_try + 1))
        log "  #$1 is still the front and only its head moved -- gating its NEW head in this run (re-gate $_ao_try of $_ao_max)"
    done
}

# hand_on_to_drain -- THE RUN THAT CLEARS THE FRONT LANDS WHAT WAITS BEHIND IT. Draft-first parks every
# PR behind the front as a draft for `drain`, but nothing RAN that drain: an admission run
# merged its own PR, printed "every PR it opened is merged" and exited, and the draft opened meanwhile
# sat at the front with nobody told (measured 2026-09-16). gate-watch does not
# watch drafts -- their suite is skipped -- so it could not say so either. Called after a merge by the
# ADMISSION run only; `approve` names the drafts instead (below); `drain` walks every PR.
# An unreadable listing is SAID with the command, never read as "no drafts": that is the gap again.
hand_on_to_drain() {
    _drafts=$("$API" "/api/v1/repos/$REPO/pulls?state=open&limit=50" 2>/dev/null | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(v, list):
    sys.exit(1)
print(" ".join("#%s" % p["number"] for p in v if p.get("draft")))') || {
        log "hand-on: could not read $REPO's open PRs, so whether a draft now waits at the front is UNKNOWN -- run: pr-queue${REPO_ARG:+ --repo $REPO_ARG} drain"
        return 0
    }
    [ -n "$_drafts" ] || return 0  # no draft waits: nothing is parked behind what just landed
    log "hand-on: draft(s) waiting behind what just landed:$(printf ' %s' $_drafts) -- draining now"
    exec sh "$SELF" ${REPO_ARG:+--repo "$REPO_ARG"} drain
}

# name_waiting_drafts -- what `approve` does INSTEAD of handing on (operator 2026-09-22).
# `approve N` is a grant for N. The hand-on turned it into a drain of every open draft, and on a
# --repo target where a merge is a deploy that landed other owners' PRs nobody had approved. So it
# lands N only, then names what waits -- with the owning session where `_owner_pid` resolves one --
# and the command that would land it, WITHOUT running it. A queue with a drain-duty hook strands nothing:
# the hook drains it on any session's next turn. The admission run still hands on (undecided).
name_waiting_drafts() {
    _drafts=$("$API" "/api/v1/repos/$REPO/pulls?state=open&limit=50" 2>/dev/null | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(v, list):
    sys.exit(1)
for p in v:
    if p.get("draft"):
        print(p["number"], (p.get("head") or {}).get("ref") or "-")') || {
        log "approve: could not read $REPO's open PRs, so whether a draft waits is UNKNOWN -- to land any: pr-queue${REPO_ARG:+ --repo $REPO_ARG} drain"
        return 0
    }
    [ -n "$_drafts" ] || return 0  # no draft waits: nothing to name, and approve lands only what it was given
    log "approve: landed only what it was given. Draft(s) still waiting, NOT drained:"
    printf '%s\n' "$_drafts" | while read -r _dn _dref; do
        _dp=$(_owner_pid "$_dref")
        log "  #$_dn ($_dref) -- owner: $([ -n "$_dp" ] && echo "pid $_dp$(_seat_of_pid "$_dp")" || echo "not resolved")"
    done
    log "  to land them: pr-queue${REPO_ARG:+ --repo $REPO_ARG} drain"
}

# --------------------------------------------------------------------------------------
# A SESSION IS NEVER PINNED TO A GATE (operator, 2026-09-16: "fix the CI pinning").
#
# Every verb that lands (admission, `approve`, `drain`, `merge-requested`) waits for green inside
# the run -- 2 to 30 minutes of `pr checks` polls. Run from a session, that held the session's TURN
# for the whole wait: measured the same afternoon, a one-line PR's admission kept a session busy
# ~3 minutes while its operator asked why it was still in a turn -- after the CI rule had just
# been changed to say "go idle, do not poll". The rule cannot fix a tool that blocks.
#
# So FROM A SESSION (FORGE_TOOLS_WAKE_PID set) the run DETACHES: it re-execs itself under `setsid` with the
# stdin it was given, logs to a file, and returns at once. On exit the detached run peer-messages
# that session its exit code and the log's tail -- the same channel gate-watch uses, never typed
# input. A bare shell, cron and the tests (no FORGE_TOOLS_WAKE_PID) run in the foreground exactly as before;
# PR_QUEUE_FOREGROUND=1 forces that from a session. `--dry-run` never waits, so it never detaches.
# The refusals above (stale checkout, freeze) have already run in the foreground by this point.
case "${1:-}" in ""|approve|drain|merge-requested) _waits=yes ;; *) _waits=no ;; esac
case " $* " in *" --dry-run "*) _waits=no ;; esac
# ANOTHER RUN IS VISIBLE, NOT REFUSED. Only a drain-duty hook looked before starting one,
# so two hand-typed drains collided and the second stopped at rc=8 on "main moved under us" with
# nothing naming the run that moved it. Concurrency stays legal; this says who else is here. Here,
# before the detach, so it reaches the session's turn rather than only a log -- and a detached child
# (PR_QUEUE_DETACHED) skips it, since its parent already said it and would read as its own peer.
if [ "$_waits" = yes ] && [ -z "${PR_QUEUE_DETACHED:-}" ] \
        && _live=$(LIVE_DRAINS_WT_BASE="$WT_BASE" LIVE_DRAINS_LOG_DIR="${PR_QUEUE_LOG_DIR:-${HOME:-/tmp}/.cache/pr-queue}" \
                   sh "$TOOLS_DIR/live-drains.sh" 2>/dev/null); then
    log "ANOTHER pr-queue RUN IS LIVE -- a second drain DEFERS to it; any other verb that"
    log "  refuses below on \"main moved\" or \"head MOVED\" is most likely that run landing first:"
    printf '%s\n' "$_live" | while IFS= read -r _ln; do log "    $_ln"; done
fi
# NOT WITH A TERMINAL ON STDIN. The bare form reads its queue from stdin, and a terminal there is
# a reader after the usage, never a queue: the `[ -t 0 ]` arm below prints it and exits 2. This
# block ran first and did `cat > file` on that terminal -- a known hang again, from a session
# only, where the forge (no FORGE_TOOLS_WAKE_PID) could not see it. The usage arm now wins.
if [ "$_waits" = yes ] && [ -n "${FORGE_TOOLS_WAKE_PID:-}" ] && [ -z "${PR_QUEUE_DETACHED:-}" ] \
        && [ -z "${PR_QUEUE_FOREGROUND:-}" ] && { [ -n "${1:-}" ] || [ ! -t 0 ]; }; then
    # A detached run reports its end by peer message; without the command that message never comes.
    if _ps_why=$(_peer_send_missing); then
        log "REFUSING to detach: $_ps_why, so the run could not tell this session when it ends. PR_QUEUE_FOREGROUND=1 runs it here."
        exit 2
    fi
    _dlog_dir="${PR_QUEUE_LOG_DIR:-${HOME:-/tmp}/.cache/pr-queue}"
    mkdir -p "$_dlog_dir" 2>/dev/null || {
        log "REFUSING to detach: cannot create $_dlog_dir for the run's log. PR_QUEUE_FOREGROUND=1 runs it here."
        exit 2
    }
    _dlog="$_dlog_dir/$(date -u +%Y%m%dT%H%M%SZ)-$$-${1:-admission}.log"
    _din=/dev/null
    if [ -z "${1:-}" ]; then _din="$_dlog.stdin"; cat > "$_din"; fi
    PR_QUEUE_DETACHED="$FORGE_TOOLS_WAKE_PID" PR_QUEUE_DETACHED_LOG="$_dlog" PR_QUEUE_HUB="$HUB" \
        setsid sh "$SELF" ${REPO_ARG:+--repo "$REPO_ARG"} "$@" < "$_din" > "$_dlog" 2>&1 &
    log "DETACHED: pr-queue ${1:-admission} runs as pid $! -- this session is NOT held for the gate."
    log "  log: $_dlog"
    log "  pid $FORGE_TOOLS_WAKE_PID is peer-messaged the exit code and the log's tail when it ends. End the turn; do not poll."
    exit 0
fi
if [ -n "${PR_QUEUE_DETACHED:-}" ]; then
    # Re-armed by every process in the chain: hand_on_to_drain `exec`s a drain, which replaces this
    # process without running its EXIT trap, and the drain arrives here and sets its own.
    # `sh` keeps ONE handler per signal, so this REPLACES the `restore_drafts` trap set above -- and a
    # detached run is every real drain. It is therefore called here, after the status is captured
    # and before the log's tail is read, so the nudge shows what was re-drafted.
    trap '_rc=$?; restore_drafts; { printf "[machine-nudge: pr-queue.sh] detached %s run pid %s EXITED %s\n" "${1:-admission}" "$$" "$_rc"; printf "log: %s\n\n" "${PR_QUEUE_DETACHED_LOG:-?}"; tail -n 25 "${PR_QUEUE_DETACHED_LOG:-/dev/null}" 2>/dev/null; } | "${PR_QUEUE_PEER_SEND:-session-notify}" --to "claude-code:$PR_QUEUE_DETACHED" --from-name "pr-queue.sh (detached run)" >/dev/null 2>&1' EXIT
fi

if [ "${1:-}" = "approve" ]; then
    shift
    : "${1:?approve needs a PR number: pr-queue approve <N> [<N>...]}"
    for _n in "$@"; do
        case "$_n" in ''|*[!0-9]*) log "approve: '$_n' is not a PR number"; exit 1 ;; esac
    done
    # APPROVE IS AN OPERATOR TOO. It moves main, so it takes the claim `drain` takes: a
    # drain started beside it (by hand, or in the gap between two approvals) sees a live operator and
    # defers, instead of batching the very PRs being landed. It REFUSES rather than defers when a drain
    # holds the queue: a human named these PRs, and silently landing nothing would answer a different
    # question. Measured 2026-09-22: an operator-ordered "#98, then #96" by hand needed every other
    # session asked not to drain, and nothing enforced it.
    #
    # SEVERAL PRs ARE ONE CLAIM. `approve 98 96` lands them in order under one claim, so the gap
    # between two separate approvals -- when no run is live and a drain can start -- does not exist.
    # The first that does not land stops the sequence; the rest are not attempted.
    if ! _claim_queue; then
        log "approve: REFUSING -- drain pid $QUEUE_HOLDER is this queue's operator; it lands"
        log "  what is ready in order. Approve after it exits, or let it take these PRs."
        exit 3
    fi
    _rc=0
    for _n in "$@"; do
        refuse_if_frozen "approve #$_n"
        approve_one "$_n" approve; _rc=$?
        if [ "$_rc" -ne 0 ]; then
            [ "$#" -eq 1 ] || log "approve: STOPPING the sequence at #$_n (rc=$_rc) -- the PRs after it were not attempted"
            break
        fi
    done
    [ "$_rc" -ne 0 ] || name_waiting_drafts
    exit "$_rc"
fi

# --------------------------------------------------------------------------------------
# merge-requested -- merge every open PR a human has asked for, from THIS box, as `claude`.
#
# The consuming half of the label-then-merge split described at MERGE_LABEL above. The forge
# records intent it has no power to act on; this verb is the authority, and it runs behind the
# same protections every other path here does: the staleness refusal at the top of the file
# (a stale checkout runs a stale queue) and merge_queued()'s head CAS.
#
# ORDERED OLDEST FIRST, AND SERIAL. Each merge moves `main` and outdates the rest, so every PR
# after the first pays merge_queued()'s update-and-rewait. That churn is NOT created here: these
# PRs are already open and already outdating each other, which is the state the queue's serial
# admission exists to prevent upstream. Paying it is the honest cost of having several requests
# outstanding at once; the fix is to request fewer, not to merge them without re-checking.
#
# A NON-ZERO FROM ANY PR STOPS THE RUN rather than moving on. The codes come straight from
# approve_one -- 7 red, 8 refused, 1 unreadable -- and every one of them means the assumption
# the next merge would rest on is no longer established.
if [ "${1:-}" = "merge-requested" ]; then
    shift
    [ "$#" -eq 0 ] || { log "merge-requested takes no arguments"; exit 2; }
    refuse_if_frozen merge-requested
    # It merges, so it is an operator too -- the same claim and the same refusal as approve.
    if ! _claim_queue; then
        log "merge-requested: REFUSING -- drain pid $QUEUE_HOLDER is this queue's operator"
        exit 3
    fi

    # THE LISTING FAILS OPEN ON AN UNRESOLVABLE LABEL NAME. This is the whole reason the id is
    # resolved before anything is listed. Measured against the live forge 2026-08-24 on this
    # repo, 28 open issues:
    #     labels=ready-for-agent   ->  8   filter applied
    #     labels=needs-triage      ->  4   filter applied
    #     labels=queue:needs-human-review        ->  0   name resolves, on nothing -- correctly empty
    #     labels=zz-no-such-label  -> 28   <-- THE ENTIRE UNFILTERED SET
    # So a typo, a rename, or a label nobody created yet returns EVERY open PR, and a verb that
    # merged what the listing returned would merge all of them. Note which arm is the dangerous
    # one: the label that exists-but-matches-nothing behaves correctly, so testing only the
    # happy path and the empty path would never reveal this.
    _mid=$(label_id "$MERGE_LABEL")
    [ -n "$_mid" ] || {
        log "REFUSING: '$MERGE_LABEL' does not resolve in $REPO."
        log "  An unresolvable label makes the listing return EVERY open PR, which this verb"
        log "  would then merge. Create it with:"
        log "    hub-api /api/v1/repos/$REPO/labels -X POST -H 'Content-Type: application/json' \\"
        log "      --data-binary '{\"name\":\"$MERGE_LABEL\",\"color\":\"#0e8a16\"}'"
        exit 2
    }

    # PAGED, AND THE STOP CONDITION IS "THIS PAGE ADDED NOTHING NEW".
    #
    # This was ONE call with `limit=50`. The forge caps the listing and ignores a larger limit, so
    # past 50 labelled PRs it drained a silently partial list and reported success -- in the verb
    # whose entire job is not to miss requested work. The consumer below re-reads each PR's labels,
    # so a truncated page never MERGED the wrong thing; it left work undone and said "merged N of
    # M" with a truncated M, which is the harder failure to see.
    #
    # WHY NOT `len(batch) < LIMIT`, which is how hub-api.sh's `paged()` stops. That is correct only
    # while the requested limit is at or below the server's cap. Ask for more than the cap and the
    # first page looks short, the loop ends early, and the truncation is back wearing a pager. The
    # cap for `type=pulls` is NOT MEASURED -- only the ISSUES listing was -- so assuming it
    # is also 50 would be the same guess that produced this bug. Stopping on a page that adds no NEW
    # number is cap-agnostic: it costs one extra request and is right whatever the cap turns out to
    # be. It also turns a server that ignores `page` into a clean stop instead of a loop that merges
    # the same PR repeatedly.
    _nums=""
    _page=1
    while :; do
        _batch=$("$API" "/api/v1/repos/$REPO/issues?state=open&type=pulls&labels=$MERGE_LABEL&limit=50&page=$_page" \
                2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if not isinstance(d, list):          # an error object is not an empty listing
    raise SystemExit(1)
for i in d:
    n = i.get("number")
    if isinstance(n, int):
        print(n)
') || { log "merge-requested: cannot read the PR listing from hub -- STOPPING"; exit 2; }
        _all=$(printf '%s\n%s\n' "$_nums" "$_batch" | sed '/^$/d' | sort -nu)
        [ "$(_count_lines "$_all")" -gt "$(_count_lines "$_nums")" ] || break
        _nums="$_all"
        _page=$((_page + 1))
        # A CEILING, because "no new numbers" cannot save us from a forge that pages forever with
        # fresh ids. Refuses rather than draining half of something unbounded.
        [ "$_page" -le 40 ] || {
            log "merge-requested: the listing did not terminate after 40 pages -- STOPPING"
            exit 2
        }
    done

    # NOT "nothing to do, success" phrased as a drain. An empty listing here is a real answer
    # and says so in its own words, because the failure this file keeps filing cases about is a
    # no-op that reports the success of the work it skipped.
    [ -n "$_nums" ] || { log "no open PR carries '$MERGE_LABEL' -- nothing requested"; exit 0; }

    _merged=0; _seen=0
    for _n in $(printf '%s\n' "$_nums" | sort -n); do
        _seen=$((_seen + 1))
        # THE SECOND LINE AGAINST THE FAIL-OPEN, and it is not redundant with the id check. That
        # one proves the NAME resolves; this proves the SERVER APPLIED THE FILTER TO THIS
        # RESPONSE. Nothing in the listing itself distinguishes "these 3 carry the label" from
        # "the filter was dropped and here is everything". `_has_label` returns 2 when the read
        # fails, which lands here too -- an unreadable label set is not permission to merge.
        if ! _has_label "$_n" "$MERGE_LABEL"; then
            log "  #$_n came back from a '$MERGE_LABEL' listing but does not carry it (or could"
            log "    not be read) -- SKIPPING. Nothing here merges on a listing alone."
            continue
        fi
        approve_one "$_n" merge-requested; _rc=$?
        # A HELD PR SKIPS, IT DOES NOT STOP -- the same answer `drain` gives rc=9, and for the same
        # reason. This verb walks every PR carrying `queue:merge-requested`, so stopping at a held
        # one would let a single hold block unrelated merges other humans asked for. The hold binds
        # the PR it is on and nothing else.
        if [ "$_rc" = 9 ]; then
            _held_skipped=$((${_held_skipped:-0} + 1))
            continue
        fi
        [ "$_rc" = 0 ] || {
            log "merge-requested: STOPPING at #$_n (rc=$_rc) -- $_merged merged before it"
            exit "$_rc"
        }
        _merged=$((_merged + 1))
    done
    # THE HELD COUNT IS REPORTED, NOT ABSORBED. "merged 0 of 3" with no other line reads as a
    # broken verb; "merged 0 of 3, 3 held" reads as the hold working, which is what happened.
    if [ "${_held_skipped:-0}" != 0 ]; then
        log "merge-requested: merged $_merged of $_seen labelled PR(s); ${_held_skipped} SKIPPED, held for review"
    else
        log "merge-requested: merged $_merged of $_seen labelled PR(s)"
    fi
    exit 0
fi

# --------------------------------------------------------------------------------------
# drain -- land every READY PR from the oldest open PR downward.
#
#   pr-queue.sh drain [--dry-run]      order: the open PRs, oldest first
#
# THE POINT IS THAT NOBODY IS THE OPERATOR. Premises 1, 2 and 13. `merge-requested` above is a
# drain over a LABEL set -- it lands what humans asked for. This lands what the QUEUE says is next,
# from the head down, whoever happens to run it. With self-merge, an agent that
# dies holding the head blocks the queue for ever, and agents dying mid-flight is measured here
# too. With a drain, the next agent through lands the dead one's PR. It needs no new
# primitive: it is the admission loop with its body changed from "my PR" to "every ready PR".
#
# THE ORDER IS NOT MINE TO INVENT, AND NOBODY KEEPS IT: it is the open PRs, oldest first, derived
# on every pass by `_derived_order` below. A listing that cannot be read REFUSES -- "could not read
# the list" must never arrive here as "nothing to merge", which prints the same silence and exit 0.
#
# ============================================================================================
# SKIP vs STOP -- the whole design of this loop, and the thing to change deliberately or not at
# all. `merge-requested` STOPS on any non-zero, and says why: the assumption the next merge would
# rest on is no longer established. THE DRAIN CANNOT USE THAT RULE, because the case it exists for
# -- a PR at the head that nobody can land right now -- is exactly the case that rule halts on. A
# drain that stopped at the first unready PR would reproduce the blockage it was built to remove.
#
# So the two are separated by WHAT THE ANSWER IS ABOUT:
#
#   SKIP, and keep draining -- the answer is about THIS PR, and the next one is unaffected:
#     * not open (merged, closed, gone)      listed open a moment ago; the listing lags, it is not damage
#     * carries the human hold               premise 6: passing over must NOT reorder it, and
#                                            nothing here writes the list, so its position stands
#     * gate-suppressed (`queue:not-admitted`)  premise 3: it never claimed to be green
#     * has an open dependency edge          Forgejo enforces these; merging is not ours to force
#     * checks are red                       "ready" means gates ACTUALLY green
#
#   STOP the whole run -- the answer is about the BOX or the FORGE, so every later verdict rests
#   on something no longer established:
#     * the PR cannot be read from hub        an unreadable forge is not an empty queue
#     * a merge was REFUSED after its repair   `main` moved under us: another drain, a hand
#                                             merge, or the web UI (5c: a PR landed that way with
#                                             no session in the loop). The next PR's freshness is
#                                             now unknown.
#
# WHY SKIPPING A RED PR DOES NOT REORDER THE QUEUE. Edges mean needs; the order means next. Age is
# a PREFERENCE, not a dependency chain -- real dependencies are edges and are checked above. So landing #2 while #1 is red asserts nothing
# about #1, and #1 keeps its line and its position for the next drain.
#
# EACH MERGE OUTDATES THE REST. `block_on_outdated_branch` is on, so PR n+1 pays
# merge_queued()'s update-and-rewait after PR n lands. That churn is real, it is measured (
# five PRs cost NINE gated heads), and it is the accepted cost of the SERIAL drain. Reducing it is
# batch admission (`drain --batch`), deliberately not here.
# ============================================================================================

# Facts about one PR, read ONCE: state, then the two labels, then the dependency edges. Prints
# `state<TAB>held<TAB>notadmitted<TAB>blockers` or nothing at all if the PR could not be read --
# and the caller treats "nothing" as STOP, never as "no reasons to skip".
_drain_facts() {
    # The draft marker rides the SAME read as the state: a `WIP: ` title is the forge's
    # draft flag (measured), and a draft at the front of the queue waits to be batched
    # rather than landed serially. No second request per PR.
    _sd=$("$API" "/api/v1/repos/$REPO/pulls/$1" 2>/dev/null \
          | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print("%s\t%s" % (d.get("state") or "", "draft" if (d.get("title") or "").startswith(sys.argv[1]) else "ready"))
except Exception: pass' "$DRAFT_PREFIX" 2>/dev/null)
    _st=${_sd%%	*}; _dr=${_sd#*	}
    [ -n "$_st" ] || return 1
    # THE THREE LABELS IN ONE READ: one labels request and one python start per PR, not three of
    # each. An unreadable label set is not "no hold" -- that would merge a PR a human is holding, on
    # the strength of a failed request -- and the same holds for the serial mark, which would batch a
    # PR that asked to land alone. So anything but a JSON list of labels prints nothing and STOPS.
    _lb=$(_labels_among "$1" "$HELD_LABEL" "$NOT_ADMITTED_LABEL" "$SERIAL_LABEL") || return 1
    case "$_lb" in [01][01][01]) ;; *) return 1 ;; esac
    case "$_lb" in 1??) _hd=held ;; *) _hd=no ;; esac
    case "$_lb" in ?1?) _na=suppressed ;; *) _na=no ;; esac
    case "$_lb" in ??1) _sr=serial ;; *) _sr=no ;; esac
    # hub-api's `issue blockers` prints `open_blockers=N` and DIES rather than reporting 0 when
    # the dependency list is unreadable, so an empty parse here is a read failure, not "no edges".
    _bk=$("$API" issue blockers "$REPO" "$1" 2>/dev/null | sed -n 's/.*open_blockers=\([0-9][0-9]*\).*/\1/p')
    [ -n "$_bk" ] || return 1
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$_st" "$_hd" "$_na" "$_bk" "$_dr" "$_sr"
}

# _derived_order -- THE QUEUE: the open PRs, oldest first (operator 2026-09-16). One `#N`
# per line. Returns 1 when the listing cannot be read -- never an empty order.
#
# WHY DERIVED. The hand-kept fence rotted: 22 entries, every one merged, none of the week's
# landings ever added. A queue nobody has to maintain cannot go stale, and a TARGET repo (`--repo`)
# gets one with no setup. The fence's override (PR_QUEUE_ORDER_ISSUE, queue_order.py) went with it
# too: nothing set it, and a second order source is a second thing to rot.
#
# The queue's OWN integration PRs are excluded: they are a batch's vehicle, not a member, and
# `try_batch` resumes a standing one by title regardless of where it sits.
#
# PAGED UNTIL A PAGE ADDS NOTHING NEW, the cap-agnostic stop `merge-requested` uses: the
# pulls listing's server cap is unmeasured, and a short-page stop would truncate silently above it.
# _retry_order <command...> -- ONE bounded retry for reading the queue order.
#
# MEASURED 2026-09-21, two seconds apart, in one detached approve log:
#   18:58:02 hand-on: draft(s) waiting behind what just landed: #85 #83 -- draining now
#   18:58:04 REFUSING: ... the open PR listing for <repo> could not be read.
# The hand-on had the numbers IN HAND and printed them; the next read failed and took the whole
# drain with it. #85 and #83 stayed queued with nothing scheduled to come back for them, and
# #85 later landed ALONE -- which read as a batching bug and was not one. A hand-on fires seconds
# after a merge, when the forge is busiest, so a transient read is the expected case there.
#
# THE REFUSAL IS CORRECT AND IS NOT WEAKENED. "A listing that cannot be read is NOT an empty queue"
# is the whole reason this code refuses rather than reporting an empty queue it never measured.
# This retries the READ; it does not soften the verdict. When the tries are spent the caller still
# refuses, still exits 2, and now says how many attempts it made -- a fix that turned the refusal
# into an exit 0 would abandon the drafts silently instead of loudly, which is worse.
#
# Exhausts before returning non-zero, so "attempts made" is always PR_QUEUE_ORDER_TRIES and the
# refusal can name it without smuggling a value out of a command substitution -- which runs in a
# subshell, so a count assigned in here would never reach the caller at all.
_retry_order() {
    _ro_i=0
    while :; do
        _ro_i=$((_ro_i + 1))
        _ro_out=$("$@" 2>&1) && { printf '%s' "$_ro_out"; return 0; }
        [ "$_ro_i" -ge "${PR_QUEUE_ORDER_TRIES:-3}" ] && { printf '%s' "$_ro_out"; return 1; }
        sleep "${PR_QUEUE_ORDER_BACKOFF:-2}"
    done
}

_derived_order() {
    _do_all=""
    _do_page=1
    while :; do
        _do_batch=$("$API" "/api/v1/repos/$REPO/pulls?state=open&limit=50&page=$_do_page" 2>/dev/null \
            | PFX="$BATCH_PREFIX" python3 -c '
import json, os, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if not isinstance(rows, list):           # an error object is not an empty listing
    raise SystemExit(1)
for p in rows:
    n = p.get("number")
    if isinstance(n, int) and not ((p.get("head") or {}).get("ref") or "").startswith(os.environ["PFX"]):
        print(n)
') || return 1
        _do_new=$(printf '%s\n%s\n' "$_do_all" "$_do_batch" | sed '/^$/d' | sort -nu)
        [ "$(_count_lines "$_do_new")" -gt "$(_count_lines "$_do_all")" ] || break
        _do_all="$_do_new"
        _do_page=$((_do_page + 1))
        [ "$_do_page" -le 40 ] || return 1
    done
    printf '%s\n' "$_do_all" | sed '/^$/d; s/^/#/'
}

# _land_alone <number> -- un-draft a PR and land it on its own run: a lone draft at the front, or a
# SERIAL one. Un-drafted first, so `approve_one` sees a skipped suite under a title that is
# no longer a draft and reopens the PR to run it once (its rc-4 path); a no-op for a PR that was
# never drafted. Counts into the drain's totals; a refusal STOPS the drain, as any refused merge does.
# Called only inside `drain`, after `_jv` exists.
_land_alone() {
    undraft_member "$1"
    approve_one "$1" drain; _la_rc=$?
    case "$_la_rc" in
        0) _landed "$1"; _merged=$((_merged + 1)); _jv merged merged ;;
        7) _jv skip checks_not_green
           log "  #$1 SKIP -- checks are not green after un-drafting; position kept"
           _skipped=$((_skipped + 1)) ;;
        9) _jv skip late_hold
           log "  #$1 SKIP -- held after its turn began; position untouched"
           _skipped=$((_skipped + 1)) ;;
        *) _jv stop merge_refused
           log "drain: STOPPING at #$1 (rc=$_la_rc) -- $_merged merged, $_skipped skipped before it"
           exit "$_la_rc" ;;
    esac
}


# ==========================================================================================
# BATCH ASSEMBLY. `drain --batch`.
#
# THE COST IT REMOVES. `block_on_outdated_branch` outdates every other open PR each time one
# lands, so a serial drain of N ready PRs pays up to N(N+1)/2 gate runs and N-1 force-pushes
# (measured 2026-08-24: 5 PRs, 9 gated heads, 29 runs). A batch pays ONE run for N PRs when
# green, and O(log N) when one member is red.
#
# THE SHAPE, as decided and not relitigated here:
#   assemble by CHERRY-PICK onto a fresh branch from hub/main -- source refs are untouched, so
#     assembly force-pushes nothing and a batch minus one member is a replay of a subset;
#   gate the batch through ONE integration PR (CI runs on `pull_request` and pushes to main
#     only, so a bare branch has no gate);
#   land by FAST-FORWARD (the client's default): main moves to the integration head exactly;
#   mark every member `manually-merged` with its REPLAYED tip as MergeCommitID -- a forge probe
#     measured that this marks a PR merged only once the commit is reachable
#     from base, which the fast-forward has just made true;
#   on red, BINARY-SPLIT (bors-style): the first half is retried as its own batch, then the
#     second half on the main the first half produced. A single red member takes the ordinary
#     path and is skipped with its position kept, exactly as `drain` skips a red PR.
#   NO speculative execution: the runner is contended (max concurrent 3, measured), so testing
#     PR2 stacked on PR1 concurrently would queue behind the thing it speculates on.
#
# WHAT A MEMBER'S NEW SHA COSTS, said plainly: PRs 2..N land under replayed shas, so the replayed-sha problem is NOT
# dissolved by this (it shrinks to batches of 2 or more) and the pruner's forge query stays
# necessary for batch-landed branches. Members' own branches are retired through
# `delete_merged_branch`, which asks the forge -- and the forge says merged because of the
# manually-merged mark, which is why that mark is not optional.
#
# `allow_manual_merge` MUST BE ON for the repo and CANNOT BE READ BACK (probe 5). A 405 on the
# mark is therefore the one failure that is expected on a fresh repo: the code is already on
# main by then, so the batch is landed and the members are left OPEN and SAID -- with the PATCH
# that turns the setting on. Nothing here retries a landing that already happened.
# ==========================================================================================
BATCH_PREFIX="${PR_QUEUE_BATCH_PREFIX:-queue/batch}"

# ==========================================================================================
# THE LANDING INVALIDATES THE CLIENT THAT DID IT.
#
# `hub_api_require_current` refuses every WRITE verb from a client that is a former version of
# `scripts/hub-api.sh` on `hub/main` (it exists because five PRs were merged by a stale client, each returning
# 200 while silently discarding the body). Correct guard, and a batch that LANDS a change to that
# file trips it on itself: the moment `main` moves, the queue's own client is a former version by
# definition, and every post-landing write is refused. Measured on a drain that landed two PRs
# -- both left OPEN, still carrying the `WIP: ` draft prefix, with their code already on main.
# That measurement was made by one session and recorded in its handoff duty; it was
# relayed by a later session, and a comment that names the wrong measurer is the failure this
# file keeps finding elsewhere. I did not reproduce it -- the handoff is the record.
# Deterministic, not a race, and it fails at the last step, after the irreversible part.
#
# WHY NOT THE THREE OPTIONS THE TICKET LISTED, all rejected on evidence:
#   * "update the queue checkout" -- `$HUB` is routinely the CALLER'S OWN WORKTREE
#     (`PR_QUEUE_HUB=$PWD` is the documented usage), often on a feature branch. There is nothing to
#     fast-forward, and doing it anyway moves a branch out from under whoever is working in it.
#   * "re-exec the client from a temp copy" -- outside a git checkout the guard reports
#     `is not inside a git checkout`, which is CANNOT TELL, which PROCEEDS. That is the guard
#     switching off by accident of where the file sits, in the accepting direction.
#   * "exempt the bookkeeping" -- weakens the guard exactly where the stale-client damage happened, and an
#     exemption fails toward proceeding. That damage was a write that succeeded while being wrong.
#
# WHAT THIS DOES INSTEAD: stand up a THROWAWAY DETACHED WORKTREE at the new `main` and run the
# bookkeeping from its `scripts/hub-api.sh`. The guard then passes ON ITS OWN TERMS -- the script is
# inside a checkout, tracked, and its blob equals `hub/main`'s -- with nothing exempted and nothing
# pinned. Verified both directions before building: a detached worktree at `hub/main` satisfies all
# four of the guard's conditions, and the same simulation at the parent of the last commit touching
# that file REFUSES, so the check can still fail.
#
# IT ENGAGES ONLY WHEN STALENESS IS POSITIVELY ESTABLISHED. `_client_is_stale` returns true only if
# it can read a tracked blob and see a mismatch; every "could not tell" answers NO and leaves the
# old behaviour untouched. That keeps the ordinary drain (which lands nothing in that file) on the
# path it has always taken, and keeps a stubbed `PR_QUEUE_API` -- which is not tracked in a
# checkout -- out of this entirely.
CLIENT_WT=""

_client_is_stale() {
    _cs_dir=$(dirname "$API")
    _cs_root=$(git -C "$_cs_dir" rev-parse --show-toplevel 2>/dev/null) || return 1
    _cs_rel=$(git -C "$_cs_root" ls-files --full-name --error-unmatch -- "$API" 2>/dev/null) || return 1
    _cs_mine=$(git -C "$_cs_root" hash-object -- "$API" 2>/dev/null) || return 1
    _cs_theirs=$(git -C "$_cs_root" rev-parse --verify -q "$REMOTE/main:$_cs_rel" 2>/dev/null) || return 1
    [ -n "$_cs_theirs" ] || return 1
    [ "$_cs_mine" != "$_cs_theirs" ]
}

# Sets CLIENT_WT on success. It does NOT echo the path, and that is deliberate: the first version
# both set the global and printed it, so the caller used `x=$(_client_worktree_make)` -- a COMMAND
# SUBSTITUTION, which runs in a subshell, so the assignment never reached the parent. The worktree
# was created and `_client_worktree_drop` then saw an empty CLIENT_WT and silently leaked it.
# Measured: `worktree list` still showed it and the path still existed after the drop. A function
# that returns a value AND sets state has to be called two different ways at once; this one only
# sets state. Caller owns cleanup via `_client_worktree_drop`.
_client_worktree_make() {
    _cw="${TMPDIR:-/tmp}/pr-queue-client-$$"
    rm -rf "$_cw"
    # FROM THE CLIENT'S OWN CHECKOUT, the one `_client_is_stale` just measured (`_cs_root`/`_cs_rel`),
    # never from $HUB: once the client is Forge-Tools', $HUB is a consumer with no hub-api.sh in it.
    git -C "$_cs_root" worktree add --detach "$_cw" "$REMOTE/main" -q 2>/dev/null || return 1
    [ -r "$_cw/$_cs_rel" ] || { git -C "$_cs_root" worktree remove --force "$_cw" 2>/dev/null; rm -rf "$_cw"; return 1; }
    CLIENT_WT="$_cw"; CLIENT_ROOT="$_cs_root"; CLIENT_API="$_cw/$_cs_rel"
}

_client_worktree_drop() {
    # Called unconditionally on the way out of the landing block, including the ordinary path where
    # the client was never stale and nothing was stood up.
    [ -n "$CLIENT_WT" ] || return 0   # nothing was created, so there is nothing to remove
    git -C "$CLIENT_ROOT" worktree remove --force "$CLIENT_WT" 2>/dev/null
    rm -rf "$CLIENT_WT"
    CLIENT_WT=""
}
# ==========================================================================================
_count() { echo $#; }
# _blame_red <members...> -- After a red integration run, ask which members' OWN diffs can
# reach the tests the red names (scripts/batch_blame.py, reading the committed impact map at BASE).
# Prints `eject: <prs> -- why` or `halve: why`. Halving blindly cost one batch three more gate
# runs to find culprits its failing test names already pointed at; a member is ejected only when the
# red implicates some members and clears the rest, so the answer stays `halve` whenever it cannot tell.
_blame_red() {
    [ -n "${BASE:-}" ] && [ -n "${BATCH_HEAD:-}" ] || { echo "halve: no assembly base to diff members against"; return 0; }
    _bl_log=$(mktemp); _bl_args=""
    "$API" pr why-red "$REPO" "$BATCH_HEAD" > "$_bl_log" 2>&1
    for _bl_m in "$@"; do _bl_args="$_bl_args $_bl_m=$QREF/pull/$_bl_m"; done
    # shellcheck disable=SC2086
    python3 "$TOOLS_DIR/batch_blame.py" "$BASE" "$_bl_log" $_bl_args 2>&1 | head -1
    rm -f "$_bl_log"
}
_first_half() { _n=$#; _h=$(( (_n + 1) / 2 )); _i=0; for _x in "$@"; do _i=$((_i + 1)); [ "$_i" -le "$_h" ] && printf '%s ' "$_x"; done; }
_second_half() { _n=$#; _h=$(( (_n + 1) / 2 )); _i=0; for _x in "$@"; do _i=$((_i + 1)); [ "$_i" -gt "$_h" ] && printf '%s ' "$_x"; done; }

draft_member() {
    _t=$(_title_of "$1")
    case "$_t" in
        "") log "  batch: #$1 has no readable title -- not drafted" ;;
        "$DRAFT_PREFIX"*) : ;;
        *) case " $UNDRAFTED " in *" $1 "*) ;; *) FOUND_READY="$FOUND_READY $1" ;; esac
           _set_title "$1" "$DRAFT_PREFIX$_t" && log "  batch: #$1 is a DRAFT while in the batch ('$DRAFT_PREFIX' title prefix)" ;;
    esac
}


# _gate_state <sha> -> success | failure | error | pending | none
_gate_state() {
    _go=$("$API" pr checks "$REPO" "$1" 2>&1) && { echo success; return 0; }
    _gs=$(printf '%s' "$_go" | sed -n "s/.*state='\([a-z]*\)'.*/\1/p" | head -n 1)
    echo "${_gs:-none}"
}
# _standing_batch <members...> -> "num branch head body" of an OPEN integration PR titled for
# exactly these members, else nothing. The body carries `members-at:` (each member's head when
# assembled) and `tips:` (each member's replayed tip), so a resume can check currency and mark.
_standing_batch() {
    _want="queue: batch of $(_count "$@") --$(printf ' #%s' "$@")"
    "$API" "/api/v1/repos/$REPO/pulls?state=open&limit=50" 2>/dev/null | WANT="$_want" PFX="$BATCH_PREFIX" python3 -c '
import json, sys, os
try:
    rows = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for p in rows:
    if p.get("title") == os.environ["WANT"] and ((p.get("head") or {}).get("ref") or "").startswith(os.environ["PFX"]):
        b = p.get("body") or ""
        # ONE FIELD PER LINE: the body carries spaces, so a word-split `set --` on this output
        # scattered it into the member list (the RESUMING path never fired; found 2026-09-08).
        print(p["number"]); print(p["head"]["ref"]); print(p["head"]["sha"]); print(b.replace("\n", "\\n"))
        break' 2>/dev/null
}
_body_field() {  # _body_field <escaped-body> <key>  -> the value after "key:" on its line
    printf '%s' "$1" | python3 -c 'import sys; key=sys.argv[1]+":"
for line in sys.stdin.read().replace("\\n", "\n").splitlines():
    if line.startswith(key): print(line[len(key):].strip()); break' "$2"
}

# assemble <members...> -- cherry-pick each member's own commits, in order, onto a fresh branch
# from hub/main. Sets BATCH_BRANCH, BATCH_HEAD, BATCH_TIPS ("num:sha ..."), BATCH_KEPT, BATCH_DROPPED.
# A member that conflicts is DROPPED (it keeps its queue position and is said), never forced.
assemble() {
    # THE BASE IS THE TARGET'S main, fetched now -- for the hub that is hub/main; for a target it is
    # a sha this checkout has no branch for, which is why the assembly is keyed on BASE, not a name.
    git fetch -q "$REMOTE" "+$BASE_BRANCH:$QREF/main" 2>/dev/null || { log "  batch: cannot fetch $BASE_BRANCH of $REPO -- STOPPING"; return 1; }
    BASE=$(git rev-parse "$QREF/main")
    reap_dead_trees
    git worktree remove --force "$WT" 2>/dev/null
    git worktree add --detach "$WT" "$BASE" -q 2>/dev/null || { log "  batch: worktree failed"; return 1; }
    BATCH_BRANCH="$BATCH_PREFIX-$(date +%s)-$$"
    BATCH_TIPS=""; BATCH_KEPT=""; BATCH_DROPPED=""
    for _m in "$@"; do
        if ! git fetch -q "$REMOTE" "+refs/pull/$_m/head:$QREF/pull/$_m" 2>/dev/null; then
            log "  batch: #$_m has no refs/pull/$_m/head on $REPO -- DROPPED from this batch"
            BATCH_DROPPED="$BATCH_DROPPED $_m"; continue
        fi
        # BEFORE THE REPLAY, so an offending member never enters the batch. This check
        # also runs at the last moment before the landing, as the freeze check does, because an
        # author can edit a body while the gate runs. But ONLY here can it be acted on cheaply:
        # once the batch head carries this member's commits, the member cannot be dropped without
        # re-assembling, so a refusal down there had nowhere to go and stopped the whole drain.
        # Dropped exactly as a CONFLICTING member is -- position kept, a human rewords it.
        if ! refuse_if_closes_wayfinder "$_m"; then
            log "  batch: #$_m would CLOSE an open wayfinder ticket -- DROPPED from this batch, position kept; reword the body and a later drain takes it"
            BATCH_DROPPED="$BATCH_DROPPED $_m"; continue
        fi
        _range=$(git rev-list --reverse "$BASE..$QREF/pull/$_m" 2>/dev/null)
        if [ -z "$_range" ]; then
            log "  batch: #$_m carries no commit beyond main of $REPO -- nothing to replay; DROPPED"
            BATCH_DROPPED="$BATCH_DROPPED $_m"; continue
        fi
        # shellcheck disable=SC2086
        if ! (cd "$WT" && git cherry-pick $_range >/dev/null 2>&1); then
            (cd "$WT" && git cherry-pick --abort >/dev/null 2>&1)
            log "  batch: #$_m CONFLICTS when replayed after$( [ -n "$BATCH_KEPT" ] && printf ' #%s' $BATCH_KEPT || printf ' main') -- DROPPED from this batch, position kept; it needs a rebase by a human"
            BATCH_DROPPED="$BATCH_DROPPED $_m"; continue
        fi
        _tip=$(cd "$WT" && git rev-parse HEAD)
        BATCH_TIPS="$BATCH_TIPS $_m:$_tip"; BATCH_KEPT="$BATCH_KEPT $_m"
    done
    [ -n "$BATCH_KEPT" ] || { log "  batch: nothing assembled"; return 2; }
    BATCH_HEAD=$(cd "$WT" && git rev-parse HEAD)
    (cd "$WT" && git push -q "$REMOTE" "HEAD:refs/heads/$BATCH_BRANCH" 2>/dev/null) || { log "  batch: push of $BATCH_BRANCH failed -- STOPPING"; return 1; }
    log "  batch: assembled$(printf ' #%s' $BATCH_KEPT) on $BATCH_BRANCH at $BATCH_HEAD"
    return 0
}

# mark_manually_merged <num> <sha> -- tell the forge a member landed, naming its replayed tip.
mark_manually_merged() {
    _mm_out=$("$API" "/api/v1/repos/$REPO/pulls/$1/merge" -X POST -H 'Content-Type: application/json' \
        -d "{\"Do\":\"manually-merged\",\"MergeCommitID\":\"$2\"}" -w '\nmarked: http=%{http_code}\n' 2>&1) || true
    _mm_code=$(printf '%s\n' "$_mm_out" | sed -n 's/^marked: http=\([0-9][0-9]*\)$/\1/p' | tail -n 1)
    case "$_mm_code" in
        2*) log "  marked #$1 merged at $2" ; return 0 ;;
        405) log "  #$1 is ON MAIN at $2 but the forge REFUSED to mark it merged (405): allow_manual_merge is off."
             log "     The code landed; the PR stays open and says nothing. Turn the setting on once, then re-run:"
             log "     hub-api /api/v1/repos/$REPO -X PATCH -H 'Content-Type: application/json' -d '{\"allow_manual_merge\":true}'"
             return 1 ;;
        *)   log "  #$1 is ON MAIN at $2 but marking it merged answered http=${_mm_code:-unreadable}: $(printf '%s\n' "$_mm_out" | sed -n 's/.*"message":"\([^"]*\)".*/\1/p' | head -1)"
             # THE THIRD `_refusal_detail` SITE. Stderr went to /dev/null here, so a client that
             # failed before any HTTP -- 2026-09-26, a credential helper naming a deleted script --
             # logged `http=unreadable:` with nothing after the colon.
             [ -n "$_mm_code" ] || _refusal_detail "$(printf '%s\n' "$_mm_out" | grep -v '^marked: http=')"
             return 1 ;;
    esac
}

# _retire_stale_batch <pr> <branch> <members-at> <members-now> -- close an integration PR whose
# members' heads moved after assembly, retire its branch, and tell each mover's owner. Used where a
# drain RESUMES a standing batch and, since the live-landing check below, just before one lands.
_retire_stale_batch() {
    _rs_pr=$1; _rs_br=$2; _rs_had=$3; _rs_now=$4
    # TELL THE SESSION THAT CAUSED IT. The rebuild is correct and the content is
    # never lost; what was missing is that the only party who can stop it recurring learns
    # nothing. This line goes to the DRAIN's log, and the drain usually belongs to a THIRD
    # party: measured 2026-09-22, a force-push onto a PR while it was a member invalidated
    # its batch, and its owner found out by reading someone else's log after the fact.
    #
    # WHY IT HAPPENS AT ALL, which is what the message has to carry: from outside, a batched
    # member is indistinguishable from an idle draft -- same `draft: true`, same `WIP: `
    # title, nothing on the PR naming the integration head that holds its tip. The owner has
    # been told "your PR is drafted behind the front, the queue will land it", which is true.
    #
    # ONLY WITH A RECORDED `members-at`: an older body has none, and then every member would
    # read as moved and every owner would get a message about something they did not do.
    if [ -n "$_rs_had" ]; then
        for _mv in $_rs_now; do
            case " $_rs_had " in
                *" $_mv "*) continue ;;          # this member is unchanged
            esac
            _mvn=${_mv%%:*}
            _notify_owner "$(_head_ref_of "$_mvn")" "your PR #$_mvn's head moved while it was a member of batch #$_rs_pr, so that batch was CLOSED AND REBUILT. Nothing is lost -- the rebuild uses your new head -- but its gate run was discarded. A PR drafted behind the front may be inside a live batch, and from the outside that looks like an idle draft: check the open PRs for a queue/batch-* head naming yours before force-pushing." "$_mvn"
        done
    fi
    "$API" "/api/v1/repos/$REPO/pulls/$_rs_pr" -X PATCH -H 'Content-Type: application/json' -d '{"state":"closed"}' -o /dev/null >/dev/null 2>&1
    git push "$REMOTE" --delete "$_rs_br" -q 2>/dev/null
}

# try_batch <members...> -- one attempt: assemble, open the integration PR, wait, land or report.
# Returns 0 landed, 7 red (caller splits), 2 nothing assembled, 3 one member left (caller lands it
# alone), 1 stop.
try_batch() {
    _stop_if_code_superseded
    _standing=$(_standing_batch "$@")
    _resumed=no
    if [ -n "$_standing" ]; then
        _spr=$(printf '%s\n' "$_standing" | sed -n 1p); _sbr=$(printf '%s\n' "$_standing" | sed -n 2p)
        _shead=$(printf '%s\n' "$_standing" | sed -n 3p); _sbody=$(printf '%s\n' "$_standing" | sed -n 4p)
        _at=$(_body_field "$_sbody" members-at); _cur=""
        for _m in "$@"; do _cur="${_cur:+$_cur }$_m:$(_head_of "$_m")"; done
        if [ -n "$_at" ] && [ "$_at" = "$_cur" ]; then
            BATCH_PR=$_spr; BATCH_BRANCH=$_sbr; BATCH_HEAD=$_shead
            BATCH_KEPT=" $*"; BATCH_DROPPED=""; BATCH_TIPS=" $(_body_field "$_sbody" tips)"
            _resumed=yes
            log "  batch: RESUMING standing integration PR #$BATCH_PR on $BATCH_BRANCH at $BATCH_HEAD -- members' heads unchanged"
        else
            log "  batch: standing integration PR #$_spr is STALE (a member's head moved: had '$_at', now '$_cur') -- closing it and rebuilding"
            _retire_stale_batch "$_spr" "$_sbr" "$_at" "$_cur"
        fi
    fi
    if [ "$_resumed" = no ]; then
        assemble "$@" || return $?
        # ONE MEMBER IS NEVER A BATCH -- AND THAT INCLUDES ONE LEFT OVER BY ASSEMBLY.
        # `batch_run` checks the count it is HANDED, but assembly drops members (a conflict, a close
        # keyword, nothing to replay), so a batch of three could reach this line as a batch of one.
        # Measured 2026-09-22: three drafts went in, one conflicted and one named a
        # ticket, and an integration PR "queue: batch of 1 -- #N" opened -- everything the rule
        # removed, back through the gap between the two checks. Returned as rc 3 so `batch_run`
        # offers the survivor to the one-member path it already has, instead of this function
        # growing a second copy of it.
        if [ "$(_count $BATCH_KEPT)" -eq 1 ]; then
            log "  batch: only #${BATCH_KEPT# } is left after assembly -- one member is never a batch; it lands on its own head"
            git push "$REMOTE" --delete "$BATCH_BRANCH" -q 2>/dev/null
            return 3
        fi
    fi
    _members="$BATCH_KEPT"
    for _m in $_members; do draft_member "$_m"; done
    if [ "$_resumed" = no ]; then
        _title="queue: batch of $(_count $_members) --$(printf ' #%s' $_members)"
        _at=""; for _m in $_members; do _at="${_at:+$_at }$_m:$(_head_of "$_m")"; done
        _body="Batch assembly. Members, replayed in queue order onto $(git rev-parse --short "$BASE") of $REPO:$(for _p in $BATCH_TIPS; do printf '\n- #%s at %s' "${_p%%:*}" "${_p#*:}"; done)

Each member is a DRAFT (WIP: title) while it is in this batch and is marked manually-merged at its replayed tip once this lands by fast-forward. If the gate does not settle within the drain's wait, this PR STANDS and the next drain resumes it. Dropped from this batch:${BATCH_DROPPED:- none}.

members-at: $_at
tips:$(printf ' %s' $BATCH_TIPS)"
        # The client verifies the head against a LOCAL ref or a STATED sha; the batch branch lives in
        # $WT, not $HUB, so state it.
        # `--no-draft`: THE INTEGRATION PR IS THE ONE THING A BATCH GATES. It opens while its
        # members are still open drafts, so `pr create`'s "not the front" rule would draft it too --
        # its suite would skip, it would never go green, and the batch would stand forever.
        if ! _out=$(HUB_API_EXPECT_HEAD="$BATCH_HEAD" "$API" pr create "$REPO" "$BATCH_BRANCH" "$BASE_BRANCH" "$_title" "$_body" --no-draft 2>&1); then
            log "  batch: integration PR CREATE FAILED -- $(printf '%s' "$_out" | tail -1)"
            for _m in $_members; do undraft_member "$_m"; done
            git push "$REMOTE" --delete "$BATCH_BRANCH" -q 2>/dev/null
            return 1
        fi
        BATCH_PR=$(printf '%s' "$_out" | sed -n 's/.*PR #\([0-9][0-9]*\).*/\1/p' | head -1)
        [ -n "$BATCH_PR" ] || { log "  batch: cannot read the integration PR number from \"$_out\" -- STOPPING"; return 1; }
        log "  batch: integration PR #$BATCH_PR on $BATCH_BRANCH; waiting for ONE gate run on $BATCH_HEAD"
        # THE MEMBERS' OWNERS WATCH THE BATCH TOO. `pr create` registers the integration
        # PR's watch for the session that RAN this drain, and while a PR sits inside a batch nothing
        # else is armed for it: measured 2026-09-22, a batch went red and closed with only the
        # drain's starter registered, and the owner of one member was never told. Each member's owner
        # is resolved as `_watch_undrafted` resolves one; the starter is skipped (already watching),
        # and an owner that does not resolve is named rather than skipped silently.
        for _m in $_members; do
            _mo=$(_owner_pid "$(_head_ref_of "$_m")" "$_m")
            if [ -z "$_mo" ]; then
                log "  batch: #$_m's owner does not resolve -- batch #$BATCH_PR's verdict reaches only this drain's session"
                continue
            fi
            [ "$_mo" != "${PR_QUEUE_DETACHED:-${FORGE_TOOLS_WAKE_PID:-}}" ] || continue   # the starter: `pr create` armed it
            if _wo=$(FORGE_TOOLS_WAKE_PID="$_mo" python3 "$TOOLS_DIR/gate-watch.py" register "$REPO" "$BATCH_HEAD" "batch #$BATCH_PR holds your PR #$_m" 2>&1); then
                log "  batch: #$_m's owner (pid $_mo) gate-watches batch #$BATCH_PR on $BATCH_HEAD"
            else
                log "  batch: #$_m's owner (pid $_mo) NOT gate-watched: $(printf '%s' "$_wo" | tail -n 1)"
            fi
        done
    fi
    wait_for_green "$BATCH_HEAD"; _wrc=$?
    if [ "$_wrc" -eq 3 ]; then
        log "  batch: gate on $BATCH_HEAD did not settle in the wait -- integration PR #$BATCH_PR is left STANDING on $BATCH_BRANCH; re-run drain to resume it (members stay drafts)"
        return 8
    fi
    if [ "$_wrc" != 0 ]; then
        log "  batch: RED -- closing integration PR #$BATCH_PR and retiring $BATCH_BRANCH"
        "$API" "/api/v1/repos/$REPO/pulls/$BATCH_PR" -X PATCH -H 'Content-Type: application/json' -d '{"state":"closed"}' -o /dev/null >/dev/null 2>&1
        git push "$REMOTE" --delete "$BATCH_BRANCH" -q 2>/dev/null
        # UN-DRAFT ONLY A BATCH THAT WILL NOT BE SPLIT. A red batch of two or more goes back to the
        # caller as rc 7 and is HALVED, and each half re-enters `try_batch`, which drafts its own
        # members and gates them one integration run at a time. Un-drafting every member here
        # pre-empts that: measured 2026-09-21, a red batch of two un-drafted BOTH two
        # seconds before the split, and since `undraft_member` retitles the PR -- a
        # `pull_request` edit the forge runs a suite for -- and `_watch_undrafted` arms a watch on
        # each, the drain produced two simultaneous full gates where the design gates one at a
        # time. The second is re-drafted moments later by the next `try_batch`, so the state
        # settles correctly and the WASTED RUN does not: it is already queued.
        #
        # A batch of ONE is the case that must still un-draft. The caller skips it with its
        # position kept, so its own suite has to run under a non-draft title for anyone to learn
        # why it is red -- measured when a queue-drafted PR went red and nothing told its session.
        if [ "$(_count $_members)" -le 1 ]; then
            for _m in $_members; do undraft_member "$_m"; done
        else
            log "  batch: members stay DRAFTS through the split -- each half is gated on its own run"
        fi
        return 7
    fi
    _title="queue: batch of $(_count $_members) --$(printf ' #%s' $_members)"
    # The commit `main` was at BEFORE this landing -- the point this client is known current with,
    # and the fallback currency reference if a throwaway checkout cannot be stood up.
    _pre_main=$(git -C "$HUB" rev-parse "$REMOTE/main" 2>/dev/null || true)
    # AGAIN, AT THE LAST MOMENT, as merge_queued does: a freeze set while this gate ran
    # stops this landing. The exit leaves the integration PR standing and its members drafted --
    # the state a gate that does not settle leaves -- so the next drain after `thaw` resumes it.
    refuse_if_frozen "batch merge #$BATCH_PR"
    # Each member's body and commits land here. A refusal leaves the integration PR
    # standing and the members drafted, the state an unsettled gate leaves.
    # THE BACKSTOP, for a body edited WHILE the gate ran -- assemble() already dropped anything
    # that named a close at selection time. rc 9, not 1: the batch as built cannot land, but the
    # run can continue by re-assembling without this member. Returning 1 here stopped the drain
    # and stranded every innocent member with it (measured: a GREEN batch of two was
    # abandoned because ONE body named a ticket).
    for _m in $_members; do
        if ! refuse_if_closes_wayfinder "$_m"; then
            log "  batch: #$_m named a close AFTER this batch was assembled -- the batch cannot land as built"
            log "  batch: integration PR #$BATCH_PR left STANDING; members stay drafts"
            BATCH_CLOSER="$_m"
            return 9
        fi
    done
    # A MEMBER WHOSE HEAD MOVED WHILE THE GATE RAN IS NOT THE PR THIS WOULD LAND. The integration head
    # carries each member's head AT ASSEMBLY; the resume path above compares `members-at` against the
    # heads now, and a live run never did. Measured 2026-09-24: a member was pushed at ~16:52 while batch
    # (assembled 16:51:18 on its previous head) gated. Green, it would have landed the old head
    # and marked the member manually-merged -- its newer commit silently off main, the PR reading as landed.
    # Rebuilt here on the new heads, as the resume path does; bounded, since an owner who keeps pushing
    # is not converging, and past the bound the PR STANDS for the next drain to find stale.
    _now=""; for _m in $_members; do _now="${_now:+$_now }$_m:$(_head_of "$_m")"; done
    if [ -n "$_at" ] && [ "$_now" != "$_at" ]; then
        log "  batch: a member's head MOVED while the gate ran (had '$_at', now '$_now') -- NOT landing $BATCH_HEAD, which carries the old head"
        if [ "${BATCH_REBUILDS:-0}" -ge "${PR_QUEUE_MOVED_RETRIES:-2}" ]; then
            log "  batch: rebuilt ${BATCH_REBUILDS:-0} time(s) already -- integration PR #$BATCH_PR left STANDING; the next drain finds it stale"
            return 8
        fi
        BATCH_REBUILDS=$(( ${BATCH_REBUILDS:-0} + 1 ))
        _retire_stale_batch "$BATCH_PR" "$BATCH_BRANCH" "$_at" "$_now"
        log "  batch: rebuilding on the members' current heads (rebuild $BATCH_REBUILDS)"
        try_batch "$@"; return $?
    fi
    # CAPTURED FIRST, then parsed. This was one pipeline into `sed`, so the client's words were
    # consumed by the pipe and no variable ever held them.
    _bl_out=$(HUB_API_MERGE_PATH=pr-queue "$API" pr merge "$REPO" "$BATCH_PR" "$_title" "$BATCH_HEAD" 2>&1)
    _code=$(printf '%s\n' "$_bl_out" | sed -n 's/.*merged: http=\([0-9][0-9]*\).*/\1/p' | tail -n 1)
    [ "$_code" = 200 ] || { log "  batch: landing integration PR #$BATCH_PR refused: http=${_code:-unreadable} -- STOPPING"
                            _refusal_detail "$_bl_out"; return 1; }
    log "  batch: LANDED -- main is now $BATCH_HEAD ($(_count $_members) PR(s), one gate run)"

    # EVERY post-landing write below goes through `$API`, not only the mark -- `undraft_member` is a
    # title PATCH and runs FIRST, which is why a measured batch's members were left BOTH unmarked AND still
    # drafted. So the client is refreshed once, here, for the whole bookkeeping block.
    _API_BEFORE="$API"
    git -C "$HUB" fetch "$REMOTE" main -q 2>/dev/null || true
    if _client_is_stale; then
        log "  batch: this landing changed scripts/hub-api.sh, so the queue's own client is now a"
        log "    former version and every write below would be refused."
        if _client_worktree_make; then
            API="$CLIENT_API"
            log "    bookkeeping runs the client that just landed, from a throwaway checkout of $REMOTE/main."
        elif [ -n "$_pre_main" ]; then
            HUB_API_CURRENCY_REF="$_pre_main"; export HUB_API_CURRENCY_REF
            log "    could not stand up a checkout; pinning currency to the pre-landing main $_pre_main instead."
        else
            log "    could not stand up a checkout and have no pre-landing sha to pin -- the writes below will likely refuse."
        fi
    fi
    # THE MARK PATH DOES NOT ENFORCE PROTECTION, SO THIS IS THE ONLY THING CHECKING THE LANDING --
    # `Do: manually-merged` returns 200 with a required context matching nothing and the
    # head still pending (measured on scratch subjects), so the forge will not object to marking a
    # member merged against a landing that did not happen the way this code believes it did. The
    # line above says "main is now $BATCH_HEAD" and, until now, nothing established that.
    #
    # ANCESTRY, NOT EQUALITY, and the difference is a false refusal. Another PR can land between
    # this merge and this fetch, leaving `main` a DESCENDANT of the measured head -- that is a
    # correct landing and equality would refuse it. What must hold is that the head the gate
    # measured actually REACHED `main`.
    #
    # FAILS CLOSED, and recoverably: on a mismatch the members stay open and drafted, which is the
    # state that already has a documented repair. The expensive direction is marking members
    # merged against a tip nobody gated.
    #
    # CANNOT-TELL IS NOT A PASS: if `rev-parse` cannot read the remote ref the check says so and
    # refuses, rather than treating an unreadable answer as agreement.
    _landed=$(git -C "$HUB" rev-parse "$REMOTE/main" 2>/dev/null) || _landed=""
    if [ -z "$_landed" ]; then
        log "  batch: CANNOT TELL whether $BATCH_HEAD reached main -- $REMOTE/main is unreadable."
        log "    Refusing the bookkeeping: members stay open and drafted."
        API="$_API_BEFORE"; unset HUB_API_CURRENCY_REF; _client_worktree_drop
        return 1
    fi
    if ! git -C "$HUB" merge-base --is-ancestor "$BATCH_HEAD" "$_landed" 2>/dev/null; then
        log "  batch: THE MEASURED HEAD DID NOT REACH MAIN. Gated $BATCH_HEAD;"
        log "    $REMOTE/main is $_landed and does not contain it. Refusing to mark members merged"
        log "    against a tip nobody gated -- they stay open and drafted."
        API="$_API_BEFORE"; unset HUB_API_CURRENCY_REF; _client_worktree_drop
        return 1
    fi
    [ "$_landed" = "$BATCH_HEAD" ] || \
        log "  batch: main has moved past the measured head since the merge ($_landed); $BATCH_HEAD is an ancestor, so the landing stands."

    _marks_failed=0
    for _p in $BATCH_TIPS; do
        _n=${_p%%:*}; _tip=${_p#*:}
        undraft_member "$_n" merged
        if mark_manually_merged "$_n" "$_tip"; then
            unlabel_held "$_n"; unlabel_one "$_n" "$MERGE_LABEL"
            "$API" "/api/v1/repos/$REPO/issues/$_n/labels" -X POST -H 'Content-Type: application/json' -d '{"labels":["merge-path:pr-queue"]}' -o /dev/null >/dev/null 2>&1
            _hr=$(_head_ref_of "$_n")   # was an inline copy of this function
            [ -n "$_hr" ] && delete_merged_branch "$_hr" "$_n"
        else
            _marks_failed=$((_marks_failed + 1))
        fi
    done
    git push "$REMOTE" --delete "$BATCH_BRANCH" -q 2>/dev/null
    # Put the client back before returning, whatever happened: the refresh is scoped to this
    # block, and a later verb in the same run must be held to the ordinary currency check.
    API="$_API_BEFORE"
    unset HUB_API_CURRENCY_REF
    _client_worktree_drop
    [ "$_marks_failed" -eq 0 ] || { log "  batch: $_marks_failed member(s) landed but are NOT marked merged -- see above; STOPPING so nobody re-lands them"; return 1; }
    return 0
}

batch_run() {
    _stack="$*"
    BATCH_LANDED=0; BATCH_SKIPPED=0
    while [ -n "$_stack" ]; do
        _cur=${_stack%%|*}
        _stack=${_stack#"$_cur"}; _stack=${_stack#|}
        set -- $_cur
        [ $# -gt 0 ] || continue
        if [ $# -eq 1 ]; then
            # ONE MEMBER IS NEVER A BATCH (operator 2026-09-20). The ordinary path lands
            # it against its own head and its own gate -- no replay, no `queue/batch-*` branch, no
            # integration PR. This was once conditional on `BATCH_LANDED -eq 0`, on the grounds
            # that a lone member behind main "is replayed like any other half (a batch of one),
            # which is one gate run either way and needs no update-and-rewait round trip". The RUN
            # COUNT is the only thing that was equal: a batch of one also burns a PR number,
            # creates and retires a branch and leaves a scratch worktree -- measured on one PR,
            # whose solo batch opened an integration PR only to close it on red -- and it gated
            # the member on a replayed commit rather than its own head, which is what produced the
            # watches that could never resolve.
            #
            # The round trip it avoided is real and is paid here instead. `merge_queued` updates a
            # behind-branch and re-waits; a red on the head THAT produces now returns rc 6, which
            # `approve_one` reports as 7 and the case below skips with its position kept. Before
            # that plumbing the same red surfaced as rc 8 and halted the drain.
            # UN-DRAFTED HERE, AT ITS TURN, AND NOT BEFORE. `approve_one` REFUSES a draft (its
            # rc-4 arm: "is a DRAFT -- batch it, or remove the prefix"), so this member has to be
            # non-draft by the time it is offered. Doing it here rather than for every member at
            # the red is the whole point: a split gates the halves ONE AT A TIME, and un-drafting
            # them all up front starts every member's suite at once -- measured 2026-09-21, a red
            # batch of two un-drafted both two seconds before `splitting` and produced two
            # simultaneous full gates. `undraft_member` also arms the watch the member is owed
            # too, so the watch still lands, one PR later than it used to.
            undraft_member "$1"
            approve_one "$1" drain; _rc=$?
            case "$_rc" in
                0) _landed "$1"; BATCH_LANDED=$((BATCH_LANDED + 1)) ;;
                7|9) log "  batch: #$1 SKIP (rc=$_rc) -- position kept"; BATCH_SKIPPED=$((BATCH_SKIPPED + 1)) ;;
                *) return "$_rc" ;;
            esac
            continue
        fi
        BATCH_REBUILDS=0; try_batch "$@"; _rc=$?
        case "$_rc" in
            0) BATCH_LANDED=$((BATCH_LANDED + $(_count $BATCH_KEPT))) ;;
            2) BATCH_SKIPPED=$((BATCH_SKIPPED + $#)) ;;
            3) _stack="${BATCH_KEPT# }${_stack:+|$_stack}" ;;   # one survivor: the one-member arm above takes it
            8) log "  batch: STANDING -- nothing else is landed until that integration PR settles"; return 8 ;;
            # rc 9. A member named a close after assembly. Same shape as the split at
            # rc 7: the run CONTINUES, with that member left behind for its author rather than
            # taking the queue down with it.
            9) if [ "$(_count $BATCH_KEPT)" -le 1 ]; then
                   log "  batch: #${BATCH_KEPT# } would CLOSE an open wayfinder ticket -- SKIP, position kept"
                   BATCH_SKIPPED=$((BATCH_SKIPPED + 1))
               else
                   _rest=$(printf '%s' "$BATCH_KEPT" | tr ' ' '\n' | command grep -vx "$BATCH_CLOSER" | tr '\n' ' ')
                   log "  batch: re-assembling without #$BATCH_CLOSER --$(printf ' #%s' $_rest)"
                   BATCH_SKIPPED=$((BATCH_SKIPPED + 1))
                   _stack="$_rest${_stack:+|$_stack}"
               fi ;;
            7) if [ "$(_count $BATCH_KEPT)" -le 1 ]; then
                   # A batch of one that is red on the main it was replayed onto: the member
                   # itself is the culprit (or the last half of an interaction), skipped with its
                   # position kept -- never split into itself, which the first run of the tests
                   # measured as a loop that never ended.
                   log "  batch: #${BATCH_KEPT# } is RED replayed on the current main -- SKIP, position kept"
                   BATCH_SKIPPED=$((BATCH_SKIPPED + 1))
               else
                   # BLAME BEFORE HALVING. Ejected members keep their position, as a red
                   # lone member does; the rest are re-gated ONCE, as one batch.
                   # shellcheck disable=SC2086
                   _blame=$(_blame_red $BATCH_KEPT)
                   case "$_blame" in
                       eject:*)
                           _ej=$(printf '%s' "${_blame#eject: }" | sed 's/ --.*//')
                           _rest=$(printf '%s\n' $BATCH_KEPT | command grep -vxF "$(printf '%s\n' $_ej)" | tr '\n' ' ')
                           log "  batch: RED -- ${_blame#*-- } -- EJECTING$(printf ' #%s' $_ej) (position kept), re-gating$(printf ' #%s' $_rest) once"
                           for _x in $_ej; do BATCH_SKIPPED=$((BATCH_SKIPPED + 1)); done
                           _stack="$_rest${_stack:+|$_stack}" ;;
                       *)
                           case "$_blame" in
                               halve:*) log "  batch: RED -- $_blame" ;;
                               *) log "  batch: RED -- halve: blame failed: ${_blame:-no output}" ;;
                           esac
                           log "  batch: splitting$(printf ' #%s' $BATCH_KEPT) -- first half, then the second on the main it produces"
                           _a=$(_first_half $BATCH_KEPT); _b=$(_second_half $BATCH_KEPT)
                           _stack="$_a|$_b${_stack:+|$_stack}" ;;
                   esac
               fi ;;
            *) return "$_rc" ;;
        esac
        # dropped members (conflicts, nothing to replay) are skipped, position kept
        for _d in $BATCH_DROPPED; do BATCH_SKIPPED=$((BATCH_SKIPPED + 1)); done
    done
    return 0
}

if [ "${1:-}" = "drain" ]; then
    shift
    _dry=no; _batch=no; _json=no
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) _dry=yes; shift ;;
            --batch) _batch=yes; shift ;;
            --json) _json=yes; shift ;;
            *) break ;;
        esac
    done
    case "${1:-}" in '') : ;; *) log "drain: unknown option '$1' -- expected --dry-run, --batch and/or --json"; exit 2 ;; esac
    [ "$#" -eq 0 ] || { log "drain: unexpected argument '$1'"; exit 2; }
    # `--json`: one JSON record per line -- verdict, refusal, summary -- on the ORIGINAL
    # stdout (fd 3), and everything else this run prints moves to stderr. Moving the whole stream is
    # what makes stdout pure: `log` and the helpers `approve_one` calls all write to stdout, and a
    # record interleaved with one stray line is not parseable. Records carry codes, never prose, so
    # printf needs no escaping. Exit codes are unchanged.
    if [ "$_json" = yes ]; then exec 3>&1 1>&2; fi
    _jrec() { [ "$_json" = yes ] && printf '%s\n' "$1" >&3; return 0; }
    _jv() { _jrec "$(printf '{"kind": "verdict", "position": %s, "pr": %s, "decision": "%s", "reason": "%s"}' "$_pos" "$_n" "$1" "$2")"; }
    refuse_if_frozen drain
    if [ "$_dry" = no ] && ! _claim_queue; then
        _jrec '{"kind": "refusal", "reason": "another_operator"}'
        log "drain: DEFERRING -- drain pid $QUEUE_HOLDER is this queue's operator. It re-derives"
        log "  the queue until it stops changing, so anything ready now is its to land. Nothing was done here."
        exit 0
    fi

    # DRAIN UNTIL THE QUEUE IS EMPTY, NOT UNTIL THE FIRST PASS ENDS.
    #
    # The walk below reads the order ONCE and iterates that snapshot, and the draft-first design
    # guarantees the snapshot is not the whole queue: "a READY PR behind waiting
    # drafts keeps its position for the NEXT drain", and a SERIAL draft behind them "lands alone
    # when it is the front". Both are correct rules about ONE pass, and both end a pass with work
    # still queued. Nothing re-entered the walk, so the queue emptied only as fast as something
    # happened to call `drain` again -- and the only automatic caller, a drain-duty hook,
    # sits behind a 15-minute cooldown. Measured 2026-09-21: two eligible PRs, both merging in a
    # dry run, and the queue idle because the last real drain had merged its one snapshot and exited.
    #
    # TERMINATION IS THE MERGE COUNT, NOT A PASS BUDGET. A pass that merges nothing ends the loop,
    # so every extra pass is paid for by a PR that actually left the queue -- and the queue is
    # finite. `_PASS_CAP` is a backstop against a queue being refilled as fast as it drains, not
    # the mechanism. `--dry-run` merges nothing and so is always exactly one pass.
    _PASS_CAP="${PR_QUEUE_DRAIN_PASSES:-20}"
    _pass=0; _tot_merged=0; _tot_skipped=0; _tot_pos=0; _emptied=no; _prev_nums=""; _stalled=no
    while :; do
    _pass=$((_pass + 1))
    # THE ORDER: DERIVED from the open PRs, oldest first, re-read every pass.
    _order=$(_retry_order _derived_order) || {
        log "REFUSING after ${PR_QUEUE_ORDER_TRIES:-3} attempt(s): the open PR listing for $REPO could not be read."
        log "  The queue IS the open PRs, oldest first -- and a listing that cannot be"
        log "  read is NOT an empty queue."
        _jrec '{"kind": "refusal", "reason": "order_unreadable"}'
        exit 2
    }
    [ -z "$_order" ] || log "queue order DERIVED from $(_count_lines "$_order") open PR(s), oldest first:$(printf ' %s' $_order)"
    _nums=$(printf '%s\n' "$_order" | sed -n 's/^#\([0-9][0-9]*\)$/\1/p')
    [ -n "$_nums" ] || {
        # AN EMPTY QUEUE MEANS TWO DIFFERENT THINGS and they must not print the same line. On the
        # first pass nothing was ever there; on a later one this drain emptied it, which is the
        # success this loop exists to reach.
        if [ "$_pass" -gt 1 ]; then
            _emptied=yes
            break
        fi
        log "no open PRs on $REPO -- the derived queue is EMPTY. Nothing to drain."
        log "  (This is not 'the drain ran and merged nothing unexpectedly'; the list is empty.)"
        _jrec "$(printf '{"kind": "summary", "merged": 0, "skipped": 0, "queued": 0, "dry_run": %s, "batch": %s}' "$( [ "$_dry" = yes ] && echo true || echo false)" "$( [ "$_batch" = yes ] && echo true || echo false)")"
        exit 0
    }

    # AN UNCHANGED QUEUE ENDS THE LOOP, and this is the condition that actually holds.
    #
    # "A pass that merged something may have more to do" is TRUE and is not sufficient: on the
    # `--batch` path the summary assigns `_merged=$BATCH_LANDED` rather than accumulating, so a
    # landed batch reports merges every pass and the merge count alone never falls to zero. Measured:
    # 14 failures across test_pr_queue_batch.py, every one a drain re-walking a queue it had already
    # served, to the pass cap. The merge count was the wrong thing to reason about.
    #
    # WHAT IS ALWAYS TRUE: if re-deriving returns the SAME list, another pass does the same work on
    # the same PRs, whatever the counters say. So the queue itself is the progress signal. In
    # production a merged PR leaves the open-PR listing and the list shrinks; where it does not --
    # a forge listing not yet consistent -- this stops after one extra derive
    # and the next drain picks up, which is the safe direction.
    if [ "$_pass" -gt 1 ] && [ "$_nums" = "$_prev_nums" ]; then
        _stalled=yes
        break
    fi
    _prev_nums="$_nums"
    _merged=0; _skipped=0; _pos=0
    _drafting=no; _drafts=""; _closed=no; _red_ahead=""
    for _n in $_nums; do
        _pos=$((_pos + 1))
        _f=$(_drain_facts "$_n") || {
            log "drain: cannot read #$_n from hub -- STOPPING at position $_pos"
            log "  An unreadable PR is not a skippable one: every later verdict would rest on a"
            log "  forge we just failed to reach. $_merged merged before this."
            _jv stop unreadable_pr
            exit 1
        }
        _st=$(printf '%s' "$_f" | cut -f1)
        _hd=$(printf '%s' "$_f" | cut -f2)
        _na=$(printf '%s' "$_f" | cut -f3)
        _bk=$(printf '%s' "$_f" | cut -f4)
        _dr=$(printf '%s' "$_f" | cut -f5)
        _sr=$(printf '%s' "$_f" | cut -f6)

        if [ "$_st" != open ]; then
            log "  $_pos. #$_n SKIP -- '$_st', not open. A stale queue line is data, not damage."
            _jv skip not_open
            _skipped=$((_skipped + 1)); continue
        fi
        if [ "$_hd" = held ]; then
            log "  $_pos. #$_n SKIP -- carries '$HELD_LABEL'; a human is looking at it."
            log "       Its position is untouched (premise 6): nothing here rewrites the list."
            _jv skip held
            _skipped=$((_skipped + 1)); continue
        fi
        if [ "$_na" = suppressed ]; then
            log "  $_pos. #$_n SKIP -- carries '$NOT_ADMITTED_LABEL', so its gates are suppressed"
            log "       and it has never claimed to be green (premise 3)."
            _jv skip not_admitted
            _skipped=$((_skipped + 1)); continue
        fi
        if [ "$_bk" != 0 ]; then
            log "  $_pos. #$_n SKIP -- $_bk open dependency edge(s). Forgejo enforces these; a"
            log "       dependency-blocked merge answers HTTP 500 and is not ours to force."
            _jv skip open_dependencies
            _skipped=$((_skipped + 1)); continue
        fi

        # THE DRAFT-FIRST QUEUE (2026-09-15/16).
        #
        # `pr create` opens every PR that is not at the front as a draft, so it waits off
        # the contended runner. When the walk reaches a draft, the drafts from here down are the
        # waiting batch: admitted exactly as `--batch` admits a member (red or no runs on its own
        # head is skipped, position kept; pending or green joins), then landed after the walk -- two
        # or more as ONE integration PR, a lone one un-drafted so its suite runs once.
        #
        # A READY PR BEHIND WAITING DRAFTS IS NOT THE FRONT, so it keeps its position for the next
        # drain rather than landing ahead of them. `pr create` never opens one there; only an
        # explicit `--no-draft` can, and that is for the queue's own opens.
        #
        # NOT UNDER `--batch`, which keeps its own meaning: batch every READY PR, drafted or not.
        if [ "$_batch" = no ]; then
            # A DRAFT BEHIND AN OPEN PR WHOSE CHECKS ARE NOT GREEN IS NOT THE FRONT EITHER. The front
            # is the oldest open PR (operator, 2026-09-16), and a red one skipped above is still open:
            # this drain has just messaged its owner to push a fix, and that push re-gates it. Un-
            # drafting -- or batching -- what waits behind it starts a SECOND gate beside that one.
            # Measured 2026-09-24, 16:39:47: a drain skipped a red PR, called the next one "the only
            # waiting draft ... reached the front" and un-drafted it; the red PR's fix was pushed 23 seconds
            # later and two PRs gated at once. The drafts keep their position for the drain after it.
            #
            # A READY PR behind one is NOT held: it lands, updated and re-gated if it must -- a red
            # PR's fix can take hours, and the drain exists to route around a stuck head. Drafts
            # differ: un-drafting one is a new gate by definition, and a ready PR already gated
            # once. Only a PR whose GATE is the problem counts as ahead (APPROVE_REGATES: red, moved,
            # unsettled). A wrong-base or no-CI refusal never gates, and a CONFLICT
            # waits on its owner's rebase, which the queue carries on past (measured: one
            # conflicting PR at the front halted every PR behind it) -- counting either would stall
            # everything behind it for as long as it stays open.
            if [ "$_dr" = draft ] && [ -n "$_red_ahead" ]; then
                log "  $_pos. #$_n SKIP -- a draft behind$_red_ahead, open and not green; its fix re-gates it, so this waits rather than gate beside it. Position kept."
                _jv skip behind_red
                _skipped=$((_skipped + 1)); continue
            fi
            # A SERIAL DRAFT NEVER JOINS THE BATCH. At the front -- nothing admitted ahead
            # of it -- it lands alone, now, and the drafts behind it are still a batch. Behind waiting
            # drafts it closes their batch where it stands: they land without it, and it and every
            # draft behind it wait for a drain in which it is the front.
            if [ "$_dr" = draft ] && [ "$_sr" = serial ]; then
                if [ "$_drafting" = no ]; then
                    if [ "$_dry" = yes ]; then
                        log "  $_pos. #$_n WOULD LAND ALONE -- a SERIAL draft at the front ('$SERIAL_LABEL')."
                        _jv would_merge serial
                        _merged=$((_merged + 1)); continue
                    fi
                    log "  $_pos. #$_n SERIAL draft at the front ('$SERIAL_LABEL') -- landing it ALONE; drafts behind it still batch"
                    _land_alone "$_n"
                    continue
                fi
                log "  $_pos. #$_n SKIP -- a SERIAL draft behind the waiting drafts, so their batch closes here; it lands alone when it is the front. Position kept."
                _jv skip serial_behind_drafts
                _skipped=$((_skipped + 1)); _closed=yes; continue
            fi
            if [ "$_dr" = draft ] && [ "$_closed" = yes ]; then
                log "  $_pos. #$_n SKIP -- a draft behind a SERIAL draft, so it is not the front. Position kept."
                _jv skip behind_serial
                _skipped=$((_skipped + 1)); continue
            fi
            if [ "$_dr" = draft ]; then
                _drafting=yes
                if [ "$_dry" = yes ]; then
                    log "  $_pos. #$_n WOULD JOIN the waiting drafts -- open, unheld, admitted, no open edges."
                    _jv would_merge draft
                    _merged=$((_merged + 1)); _drafts="${_drafts:+$_drafts }$_n"; continue
                fi
                _hs=$(_head_of "$_n")
                _gs=$( [ -n "$_hs" ] && _gate_state "$_hs" || echo none )
                case "$_gs" in
                    failure|error)
                        log "  $_pos. #$_n SKIP -- a waiting DRAFT, RED on its own head $_hs; position kept"
                        _jv skip red
                        _skipped=$((_skipped + 1)) ;;
                    none)
                        log "  $_pos. #$_n SKIP -- a waiting DRAFT with NO check-runs on $_hs, which is not a pass; position kept"
                        _jv skip no_check_runs
                        _skipped=$((_skipped + 1)) ;;
                    *)
                        log "  $_pos. #$_n WAITING DRAFT -- $_gs on $_hs; joins the drafts at the front"
                        _jv admitted "$_gs"
                        _drafts="${_drafts:+$_drafts }$_n" ;;
                esac
                continue
            fi
            if [ "$_drafting" = yes ]; then
                log "  $_pos. #$_n SKIP -- ready, but behind the waiting drafts, so it is not the front. Position kept for the next drain."
                _jv skip behind_drafts
                _skipped=$((_skipped + 1)); continue
            fi
        fi
        # `--batch` BATCHES EVERY READY PR -- EXCEPT A SERIAL ONE, which keeps its position;
        # a bare drain lands it alone when it is the front.
        if [ "$_batch" = yes ] && [ "$_sr" = serial ]; then
            log "  $_pos. #$_n SKIP -- marked '$SERIAL_LABEL', so it never joins a batch; a bare drain lands it alone at the front. Position kept."
            _jv skip serial
            _skipped=$((_skipped + 1)); continue
        fi

        if [ "$_dry" = yes ]; then
            log "  $_pos. #$_n WOULD MERGE -- open, unheld, admitted, no open edges."
            log "       (Checks NOT polled in --dry-run: that costs a wait per PR and this arm"
            log "        exists to be cheap. A real drain still refuses on red.)"
            _jv would_merge ready
            _merged=$((_merged + 1)); [ "$_batch" = yes ] && _ready="${_ready:+$_ready }$_n"; continue
        fi
        if [ "$_batch" = yes ]; then
            # READY means green on its CURRENT head, read once, not waited for: a batch is
            # assembled from what is green now, and a pending PR joins the next one.
            _hs=$(_head_of "$_n")
            _gs=$( [ -n "$_hs" ] && _gate_state "$_hs" || echo none )
            # NO RUNS AT ALL IS NOT A PASS, AND IT USED TO BE ADMITTED SILENTLY.
            #
            # `_gate_state` collapses every non-green answer that is not failure/error into `none`,
            # and `none` fell into the `*)` arm beside `pending` and `success`. But `hub-api.sh pr
            # checks` refuses that state in its own words -- "exists but has NO check-runs
            # registered -- not a pass. If a workflow should have fired, it did not" -- so the
            # queue was discarding a refusal the client had already made and admitting a member
            # nothing had measured.
            #
            # HARMLESS UNTIL IT IS NOT: today every member runs, so `none` means the workflows did
            # not fire, which is exactly the case worth refusing. The integration run would still
            # gate the bytes, but a member admitted this way joins a batch having been measured by
            # nothing on its own head, and the drain says ADMITTED as though it had been.
            #
            # THE MEMBER-RUN SKIP DOES NOT CHANGE THIS ARM: it skips only the suite, so a
            # drafted member still has its cheap runs and `none` still means nothing fired. A skipped
            # suite reads `success` here and is admitted, which is correct -- the integration run
            # executes it over the same bytes.
            case "$_gs" in
                failure|error)
                    log "  $_pos. #$_n SKIP -- RED on its own head $_hs; position kept"
                    _jv skip red
                    _skipped=$((_skipped + 1)) ;;
                none)
                    log "  $_pos. #$_n SKIP -- NO check-runs registered on its own head $_hs, which"
                    log "       is not a pass: if a workflow should have fired, it did not."
                    log "       Position kept; re-run the gate or push again to register one."
                    _jv skip no_check_runs
                    _skipped=$((_skipped + 1)) ;;
                *)
                    log "  $_pos. #$_n ADMITTED -- $_gs on $_hs; the integration run gates it"
                    _jv admitted "$_gs"
                    _ready="${_ready:+$_ready }$_n" ;;
            esac
            continue
        fi
        approve_one "$_n" drain; _rc=$?
        case "$_rc" in
            0) _merged=$((_merged + 1)); _jv merged merged ;;
            7) _jv skip checks_not_green
               log "  $_pos. #$_n SKIP -- checks are not green. 'Ready' means gates actually green,"
               log "       so it keeps its position and the drain continues below it."
               [ "$APPROVE_REGATES" != yes ] || _red_ahead="$_red_ahead #$_n"
               _skipped=$((_skipped + 1)) ;;
            # A LATE HOLD SKIPS, IT DOES NOT STOP. `approve_one` returns 9 when the hold
            # arrived during `wait_for_green`, and for a drain that is the same answer as a hold
            # read at the top of the turn: skip it, keep its position, carry on below it. The other
            # two callers were asked to merge ONE named PR, so their `*` arm stops -- which is the
            # right answer there and the wrong one here.
            9) _jv skip late_hold
               log "  $_pos. #$_n SKIP -- held after its turn began; see above. Position untouched."
               _skipped=$((_skipped + 1)) ;;
            *) _jv stop merge_refused
               log "drain: STOPPING at #$_n (rc=$_rc) -- $_merged merged, $_skipped skipped before it"
               log "  A refused merge is not about this PR alone: main moved under us (another drain,"
               log "  a hand merge or the web UI, protocol 5c). The next PR's freshness is no longer"
               log "  established."
               exit "$_rc" ;;
        esac
    done
    # THE WAITING DRAFTS LAND HERE, after the walk has served everything ahead of them.
    if [ "$_batch" = no ] && [ "$_dry" = no ] && [ -n "$_drafts" ]; then
        if [ "$(_count $_drafts)" -ge 2 ]; then
            log "drain: $(_count $_drafts) waiting drafts at the front --$(printf ' #%s' $_drafts); landing them as ONE batch"
            batch_run $_drafts; _rc=$?
            [ "$_rc" -ne 8 ] || { _jrec '{"kind": "refusal", "reason": "integration_pr_standing"}'; log "drain: an integration PR is STANDING with its gate unsettled -- $BATCH_LANDED landed before it; re-run drain to resume"; exit 8; }
            [ "$_rc" -eq 0 ] || { log "drain: STOPPING (rc=$_rc) -- $BATCH_LANDED landed, $BATCH_SKIPPED skipped"; exit "$_rc"; }
            _merged=$((_merged + BATCH_LANDED)); _skipped=$((_skipped + BATCH_SKIPPED))
        else
            # ONE DRAFT IS NOT A BATCH. Un-drafted first, so `approve_one` sees a PR whose suite
            # skipped under a title that is no longer a draft -- and reopens it so the suite runs
            # once, on the current base, before anything merges (the rc-4 path).
            _n=$_drafts
            log "drain: #$_n is the only waiting draft and has reached the front -- un-drafting it so its suite runs, then landing it"
            _land_alone "$_n"
        fi
    fi
    if [ "$_batch" = yes ] && [ "$_dry" = no ]; then
        if [ -n "${_ready:-}" ]; then
            log "drain --batch: $(_count $_ready) ready --$(printf ' #%s' $_ready)"
            batch_run $_ready; _rc=$?
            [ "$_rc" -ne 8 ] || { _jrec '{"kind": "refusal", "reason": "integration_pr_standing"}'; log "drain --batch: an integration PR is STANDING with its gate unsettled -- $BATCH_LANDED landed before it; re-run drain to resume"; exit 8; }
            [ "$_rc" -eq 0 ] || { log "drain --batch: STOPPING (rc=$_rc) -- $BATCH_LANDED landed, $BATCH_SKIPPED skipped"; exit "$_rc"; }
            _merged=$BATCH_LANDED; _skipped=$((_skipped + BATCH_SKIPPED))
        else
            log "drain --batch: nothing ready"
        fi
    fi
    # END OF ONE PASS. Accumulate, then decide whether the queue can still move.
    _tot_merged=$((_tot_merged + _merged)); _tot_skipped=$((_tot_skipped + _skipped)); _tot_pos=$((_tot_pos + _pos))
    if [ "$_dry" = yes ]; then break; fi
    # A PASS THAT MERGED NOTHING CANNOT BE IMPROVED BY REPEATING IT. Whatever is left is held,
    # red, not admitted, dependency-blocked or behind a draft that is none of this drain's doing.
    # Kept as the CHEAP exit -- it saves a derive when a pass plainly did nothing. It is no longer
    # the termination argument; the unchanged-queue check above is.
    if [ "$_merged" -eq 0 ]; then break; fi
    if [ "$_pass" -ge "$_PASS_CAP" ]; then
        log "drain: stopping after $_pass pass(es) -- the cap (PR_QUEUE_DRAIN_PASSES=$_PASS_CAP)."
        log "  The queue is still moving, so this is a refill outpacing the drain, not an empty queue."
        break
    fi
    log "drain: pass $_pass merged $_merged -- re-deriving the queue and continuing"
    # `_ready` is the one piece of per-pass state the top of the loop does not reset -- it is read
    # as `${_ready:-}`, so a stale value would follow a batch into the next pass.
    _ready=""
    done
    _merged=$_tot_merged; _skipped=$_tot_skipped; _pos=$_tot_pos
    [ "$_emptied" = no ] || log "drain: the queue is EMPTY -- every PR that could move has moved, in $((_pass - 1)) pass(es)."
    [ "$_stalled" = no ] || log "drain: the queue did not change after pass $((_pass - 1)) -- nothing further can move here; stopping."
    _jrec "$(printf '{"kind": "summary", "merged": %s, "skipped": %s, "queued": %s, "dry_run": %s, "batch": %s}' "$_merged" "$_skipped" "$_pos" "$( [ "$_dry" = yes ] && echo true || echo false)" "$( [ "$_batch" = yes ] && echo true || echo false)")"
    if [ "$_dry" = yes ]; then
        log "drain --dry-run: $_merged would merge, $_skipped skipped, of $_pos queued${_batch:+}$( [ "$_batch" = yes ] && printf ' -- as ONE batch of %s' "$(_count ${_ready:-})" )"
    else
        log "drain: merged $_merged, skipped $_skipped, of $_pos queued$( [ "$LANDED_ELSEWHERE" -eq 0 ] || printf ' -- %s of those merged had ALREADY LANDED by another run, not this one' "$LANDED_ELSEWHERE" )"
    fi
    exit 0
fi

# A MISTYPED VERB MUST NOT DRAIN. Every verb above matches an exact string and falls through
# otherwise -- so `pr-queue.sh aprove 42` reached the stdin read, got EOF or an empty queue, and
# printed "queue drained -- every PR it opened is merged" with exit 0, having done nothing at
# all. Observed 2026-08-21 while proving the review-hold tests fail on the pre-change script:
# the OLD queue answered `approve 42` exactly that way. A no-op that reports the success of the
# work it skipped is the shape this repo's standing verification rule exists for, so an
# unrecognised first argument is refused by name rather than silently becoming an empty queue.
#
# The queue itself is fed on STDIN and takes no arguments, so any argv here is a mistake.
if [ "$#" -gt 0 ]; then
    log "unknown verb '$1' -- expected: approve <N> | drain [--dry-run] | merge-requested | prune-merged [--delete] | merge-paths [N] | (queue on stdin, no args)"
    exit 2
fi


# A TERMINAL ON STDIN IS SOMEONE LOOKING FOR THE USAGE, and checked BEFORE `cat`
# because `cat` on a terminal BLOCKS. Running this bare hung until Ctrl-D and then printed
# "queue drained", so the one invocation a reader naturally tries to discover the verbs with was
# the one that hung and then claimed success. The five forms live in this file's header, which
# is only visible by opening it.
if [ -t 0 ]; then
    log "no queue on stdin. This form reads branch<TAB>title[<TAB>review] lines, one per PR:"
    # A single quote via a variable, not more escaping: this line is inside a double-quoted
    # string in a shell script, and the nested form rendered as <<'"EOF"'. Measured on a pty.
    _sq="'"
    log "    pr-queue <<${_sq}EOF${_sq}"
    log "    my-branch<TAB>a title"
    log "    EOF"
    log "  other verbs: approve <N> | drain [--dry-run] | merge-requested | prune-merged [--delete] | merge-paths [N]"
    exit 2
fi

QUEUE=$(cat)   # branch<TAB>title[<TAB>review], one per line
_queued=0      # entries actually processed, so the summary cannot claim a drain of none
_landed=0; _drafted=0  # of those: merged here, and opened as drafts left for `drain` -- not the same thing

# NOT a pipe. `echo "$QUEUE" | while ...` runs the loop in a SUBSHELL, where every `exit`
# below ends only that subshell -- the script then falls through to "queue drained" and
# returns 0. Measured on this box's busybox sh 2026-08-21: the pipe form printed the drained
# line and exited 0 after an `exit 4` inside the loop; the heredoc form stopped with 4. So a
# rebase conflict, a failed push and a red gate ALL reported success to any caller reading
# the status. Keep the redirect.
refuse_if_frozen "the queue's stdin path"
while IFS="$(printf '\t')" read -r ref title flag; do
    [ -z "$ref" ] && continue
    _queued=$((_queued + 1))

    # AN UNRECOGNISED THIRD FIELD REFUSES RATHER THAN MERGING. `[ "$flag" = review ]` alone
    # fails OPEN: `reveiw`, `Review`, or a stray trailing tab all fall through to the merge, and
    # the only signal is a PR that quietly landed. That is the same fail-open as the mistyped
    # verb above, on the field that decides whether a human sees the work. Checked BEFORE any
    # rebase or push, so a typo costs nothing but the message.
    case "${flag:-}" in
        ''|review) : ;;
        *) log "REFUSING: '$ref' has an unknown third field '$flag' -- expected 'review' or nothing"
           exit 2 ;;
    esac

    # 2. Rebase BEFORE the PR exists. No CI has run on this branch, so nothing is wasted
    #    and nothing is invalidated. Detached worktree: never another session's tree.
    git fetch "$FT_REMOTE" -q
    reap_dead_trees
    git worktree remove --force "$WT" 2>/dev/null
    git worktree add --detach "$WT" "$FT_REMOTE/$ref" -q 2>/dev/null || { log "$ref: worktree failed"; exit 3; }
    # THE ERROR IS KEPT, AND THE VERDICT NO LONGER NAMES A CAUSE IT DID NOT MEASURE. This was
    # `>/dev/null 2>&1` plus `log "REBASE CONFLICT"`, so EVERY rebase failure was reported as a
    # conflict. Measured 2026-08-21: a rebase dying with `unable to auto-detect email address`
    # (no committer identity) printed `REBASE CONFLICT — needs a human`, and that one wrong word
    # cost three wrong diagnoses before anyone read git's actual stderr. A conflict is one cause
    # among several; the others are an unreadable object, a missing identity, a busy index.
    # ATTRIBUTION, NOT NARRATION. Two steps in this queue move a PR head -- this rebase,
    # and `update_and_rewait` before the merge -- and each used to log one unconditional line. A
    # head observed later is consistent with EITHER having acted, so the log could not attribute and
    # a session reading it guessed. Measured once: this line said `rebased onto 2e525a3f57`
    # when the rebase produced NOTHING, and the new sha in fact came from the update step four
    # minutes later. Capturing before and after makes each line state what its own step did.
    _pre_rebase=$(cd "$WT" && git rev-parse --short HEAD 2>/dev/null || echo '?')
    if ! REBASE_ERR=$(cd "$WT" && git rebase "$FT_REMOTE/main" 2>&1); then
        (cd "$WT" && git rebase --abort >/dev/null 2>&1)
        log "$ref: REBASE FAILED — STOPPING, needs a human. git said:"
        printf '%s\n' "$REBASE_ERR" | sed 's/^/    /'
        exit 4
    fi
    (cd "$WT" && git push --force-with-lease "$FT_REMOTE" "HEAD:$ref" -q 2>/dev/null) || { log "$ref: push failed"; exit 5; }
    _post_rebase=$(cd "$WT" && git rev-parse --short HEAD 2>/dev/null || echo '?')
    if [ "$_pre_rebase" = "$_post_rebase" ]; then
        log "$ref already current with $(git rev-parse --short "$FT_REMOTE/main") — rebase moved nothing, head stays $_post_rebase; opening PR"
    else
        log "$ref rebased $_pre_rebase -> $_post_rebase onto $(git rev-parse --short "$FT_REMOTE/main"), opening PR"
    fi

    # 3. Open the PR. This is the moment CI starts, and it starts on an up-to-date tree.
    #
    # CAPTURED, NOT PIPED, and that is load-bearing. `hub-api.sh` exits NON-ZERO to refuse:
    # `pr checks` on a commit with no check-runs exits 2 with "REFUSING", because zero
    # registered checks is the shape of a workflow that never triggered. A pipe discards
    # that -- measured: bare `rc=2`, `| tail -1` `rc=0`. So `cmd | tail` turns
    # the client's deliberate refusal into a pass, reintroducing one pipe downstream the
    # exact failure `checks)` was written to prevent.
    #
    # `set -o pipefail` would also fix it and is deliberately NOT used: busybox ash
    # supports it, but where /bin/sh is dash (Debian) `set -o pipefail` is a syntax error -- a
    # portability trap that would present as the whole script failing on that host. Capturing keeps `$?` without needing the option.
    # THE DESCRIPTION SLOT BELONGS TO THE CHANGE, NOT TO THE QUEUE.
    #
    # This passed a fixed sentence about queue mechanics. Measured by a PR-authorship audit:
    # 17 of 25 merged PRs carried that identical 122-char string, and it is WORSE THAN AN EMPTY BODY
    # BECAUSE IT LOOKS LIKE ONE -- a reviewer sees a populated description and does not notice the
    # description is missing. The sentence was also true and useless: every PR the queue opens is
    # rebased-before-open, so it distinguishes nothing between one PR and the next.
    #
    # The commit bodies are already the description. This repo writes them long and reasoned, so the
    # slot fills itself with the right content, derived rather than retyped -- the same argument
    # `approve` makes for reading the TITLE from the forge instead of retyping it.
    #
    # `--reverse` so a multi-commit branch reads oldest-first, like the diff does. `hub/main..HEAD`
    # in the detached worktree, which step 2 left rebased onto `hub/main`, so this is exactly the
    # set of commits the PR proposes.
    #
    # AN EMPTY RESULT IS PASSED THROUGH AS EMPTY, deliberately. A commit with no body means the
    # author owed a description and did not write one; saying so plainly is the signal. Substituting
    # boilerplate is what produced this defect.
    _body=$(cd "$WT" && git log --reverse --format=%b "$FT_REMOTE/main..HEAD" 2>/dev/null)
    # NO `--no-draft` ANY MORE. It asserted this PR was the front because this run held the queue
    # lock; with the lock gone another run's PR may be open, and `pr create` measures that itself.
    # A PR it drafts has a skipped suite, so waiting for green here would wait on a run that never
    # starts -- a drafted PR is handed to `drain` instead, below.
    if ! OUT=$("$API" pr create "$REPO" "$ref" "$BASE_BRANCH" "$title" "$_body" 2>&1); then
        log "$ref: PR CREATE FAILED -- $(printf '%s' "$OUT" | tail -1)"
        exit 6
    fi
    num=$(printf '%s' "$OUT" | sed -n 's/.*PR #\([0-9][0-9]*\).*/\1/p')
    [ -n "$num" ] || { log "$ref: cannot read a PR number from \"$OUT\" -- STOPPING"; exit 6; }
    printf '%s\n' "$OUT" | tail -1
    case "$OUT" in
        *"opening as a DRAFT"*)
            [ "$flag" = review ] && label_held "$num"
            if [ -d "$WT" ] && ! _wt_err=$(git worktree remove "$WT" 2>&1); then
                log "  note: left $WT in place -- git refused to remove it: $(printf '%s' "$_wt_err" | tail -1)"
            fi
            log "  #$num opened as a DRAFT behind the front -- \`drain\` lands it in turn; not waiting here"
            _drafted=$((_drafted + 1))
            continue ;;
    esac

    # 4. Drain our own runway: wait for THIS PR's checks, then merge it.
    #
    # THE SHA IS THE ONE WE PUSHED, read from the worktree rather than from the PR. A queued
    # branch is rebased in step 2 BEFORE any CI exists, so this head is the only one its
    # checks ever ran against -- and re-reading it from the API would pick up a head someone
    # else moved. wait_for_green refuses a commit with zero registered checks, so a workflow
    # that never triggered stops the queue instead of merging unexamined.
    head_sha=$(cd "$WT" && git rev-parse HEAD)

    # THE SCRATCH TREE IS REDUNDANT FROM HERE, AND THIS IS THE FIRST LINE AT WHICH THAT IS TRUE
    # Before this point the tree can hold the ONLY copy of something: a rebase result
    # not yet pushed, or an aborted rebase's state. After it, the branch is on hub and both values
    # this tree exists to produce -- `_body` and `head_sha` -- have been read out of it.
    #
    # WHY HERE AND NOT A `trap`. The tree is only redundant from this line on (see above), and a
    # trap would also fire on the early exits that deliberately keep it. The path is this run's own
    # (`$WT_BASE-<pid>`, FORGE_TOOLS_MERGE_SCRATCH_PREFIX), so no other run can be holding it.
    #
    # NEVER `--force` HERE, though the `add` path above uses it. There the tree is a known-stale
    # leftover being replaced; here it is live state, and git's refusal to remove a dirty worktree
    # is the correct outcome -- uncommitted content in a scratch tree is evidence something went
    # wrong, and forcing destroys it. A refusal is reported and does NOT stop the queue: cleanup is
    # not this script's job, and failing the run over a leftover directory would be worse than the
    # leftover.
    #
    # THE EARLY EXITS ARE DELIBERATELY NOT COVERED, one decision per code rather than a default:
    #   exit 3 (worktree add failed) -- there is no tree to remove.
    #   exit 4 (rebase failed)       -- KEPT. The tree holds the failure state a human is being
    #                                   asked to look at; `--abort` has run but the situation is
    #                                   the diagnostic.
    #   exit 5 (push failed)         -- KEPT, and this one matters most: the rebase succeeded and
    #                                   was never pushed, so the tree is the only copy.
    #   exit 6 (PR create failed)    -- kept. The branch is already on hub so nothing is unique,
    #                                   but the operator is stopped at a failure and the tree costs
    #                                   one held line in the reaper's report until the next `add`.
    # Everything from here on (7 red, 8 refused merge, 9 review hold) runs with no tree, and 9 is
    # the one that used to leave it standing for days.
    if [ -d "$WT" ] && ! _wt_err=$(git worktree remove "$WT" 2>&1); then
        log "  note: left $WT in place -- git refused to remove it: $(printf '%s' "$_wt_err" | tail -1)"
    fi

    wait_for_green "$head_sha" || exit 7

    # 5. A third field of `review` HOLDS this PR for a human instead of merging it.
    #
    # WHY IT STOPS THE QUEUE RATHER THAN SKIPPING AHEAD. The queue's whole value is the
    # invariant "one PR in flight", which is what lets every branch be rebased BEFORE its PR
    # exists and get exactly one CI run. A held PR still occupies the runway: opening the next
    # one against an unmerged base is precisely the N(N+1)/2 rebase churn at the top of this
    # file. So a hold ends the run, and the unqueued branches stay unqueued -- costing nothing,
    # since a pushed branch with no PR triggers no workflow.
    #
    # WHAT THE HOLD COSTS THE HELD PR: nothing, in the ordinary case. Because the queue stops,
    # `main` does not move underneath it, so `approve` merges the same green head with no
    # second CI run. If something else merges meanwhile, merge_queued()'s 405 path repairs it.
    #
    # DISTINCT EXIT CODE. `7` is red checks, `8` is a refused merge, `9` is "green, reviewed by
    # nobody yet, deliberately not merged" -- a success the caller must not read as a failure,
    # and a stop it must not read as drained.
    if [ "$flag" = review ]; then
        log "  #$num is GREEN and HELD for review — the queue stops here, nothing else admitted"
        label_held "$num"
        log "  merge it with:  pr-queue approve $num"
        exit 9
    fi

    merge_queued "$num" "$title" "$head_sha" "$ref" || exit 8
    _landed=$((_landed + 1))
done <<QUEUED_BRANCHES
$QUEUE
QUEUED_BRANCHES
# NOT "drained" WHEN NOTHING WAS QUEUED. The old line was true in the vacuous
# sense (the set of PRs it opened is empty, so all of them are merged) and read as a report
# that work completed. An empty heredoc is a legitimate no-op, so this is not an error; it
# just has to say which of the two happened.
if [ "$_queued" -eq 0 ]; then
    log "no queue entries on stdin -- nothing was opened and nothing was merged"
elif [ "$_drafted" -eq 0 ]; then
    log "queue drained -- every PR it opened is merged ($_queued queued)"
else
    # NOT "every PR it opened is merged" when some were only drafted -- that line was printed over a
    # run whose one PR was a draft and nothing had merged (measured 2026-09-16).
    log "queue done -- $_landed merged, $_drafted opened as DRAFTS and left for \`drain\` ($_queued queued)"
fi
[ "$_landed" -eq 0 ] || hand_on_to_drain

