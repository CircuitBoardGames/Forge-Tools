#!/bin/sh
# PostToolUse (Bash): after a `hub-api.sh pr merge`, delete local branches whose HUB PR is MERGED.
#
# The Forgejo counterpart of prune-landed-branches.sh. It hardcodes Forgejo exactly as that one
# hardcodes GitHub -- no forge detection, no resolver, no capability table. The precedent is
# `.forgejo/workflows/` vs `.github/workflows/`: each forge reads its own artefact and is blind to
# the other's. The two hooks share a doctrine and not a line of code, deliberately.
#
# WHY IT EXISTS AT ALL: squash-merging leaves the branch's own commits off `main` for ever, so a
# landed branch is indistinguishable from unlanded work by every obvious command. `git branch -d`
# refuses it, `git cherry` reports it unlanded, `git diff main...branch` shows its full diff either
# way. THE AUTHORITY IS THE FORGE, NEVER GIT.
#
# ============================================================================================
# THE THREE FAIL-OPENS A LITERAL PORT OF THE GITHUB HOOK WALKS INTO. All three measured against
# a real Forgejo on 2026-08-12, on a scratch repo (PRs #1 and #2, both merged) and on a second
# repo (#1, #3 closed-unmerged; #2 merged).
#
# 1. `state=merged` DOES NOT MEAN MERGED, AND DOES NOT RETURN AN EMPTY LIST EITHER.
#    Forgejo's pulls endpoint accepts open|closed|all; an unrecognised value falls back to `all`.
#    Measured on the second repo:
#        state=open 0   state=all 3   state=merged 3   state=bogusnonsense 3
#    So `?state=merged` returns EVERY pull request -- including closed-unmerged ones, and on a repo
#    with open PRs, the open ones too. A hook trusting it would delete the branch of a PR that is
#    still open. That is worse than the empty-list fail-open it is usually described as, and it is
#    worse in the deleting direction. This hook asks for `state=closed` and filters on `merged`.
#
# 2. `head.ref` IS NOT THE BRANCH NAME ONCE THE BRANCH IS GONE. For a PR whose head branch has been
#    deleted, Forgejo reports `head.ref = "refs/pull/<N>/head"` and keeps the real name only in
#    `head.label`. Measured: #2 `ref=refs/pull/2/head label=forge/protection-probe`, while #1 (branch
#    still present) reports `ref=label=forge/gate-probe`. A hook keyed on `ref` therefore matches
#    NOTHING for exactly the PRs that landed -- silently, with a successful API call. `head_name()`
#    below prefers `ref` and falls back to `label`.
#
# 3. A SCHEMA CHANGE READS AS "NOTHING LANDED". If Forgejo ever renames `merged`, the filter returns
#    an empty list from a 200 response and the hook goes quiet -- the same silence as a healthy
#    steady state. So the filter reports how many closed PRs it SAW, and a run that saw closed PRs
#    but found the `merged` key on none of them says so out loud instead of pruning nothing quietly.
#
# A FOURTH was found later and is NOT a porting trap, so it lives at the query it is about, below:
# a one-page window reads as "nothing landed" for every PR that has aged out of it.
# ============================================================================================
#
# Refuses to touch: `main`, and any branch checked out in ANY worktree -- git itself enforces the
# second (`git branch -D` exits 1 there), which is why this carries no guard of its own; the GitHub
# hook's comment records that mutation testing proved such a guard unreachable. Deletion stays
# recoverable: the sha is printed, and the reflog and the PR both still hold it.
#
# Silence means nothing was landed-and-stale. EVERY OTHER OUTCOME SPEAKS: a missing hub-api.sh, a
# missing `hub` remote, an absent token, an unreachable forge and an unreadable response all print a
# reason and delete nothing. A prune that could not reach hub must never read as a clean prune.
set -eu

SELF="[prune-landed-branches-forgejo]"

# ---------------------------------------------------------------------------------------------
# TWO CALLERS, ONE FORGE QUERY.
#
# As a HOOK (no arguments) this fires only after a `hub-api.sh pr merge` typed in THIS session, and
# deletes silently. That leaves the ordinary case uncovered: a session cleaning up branches it did
# not merge, or merged yesterday, reaches for `git branch -d` -- which asks ANCESTRY, and a squash
# merge makes ancestry a false negative on landed work, so `-d` refuses and invites `-D`, i.e. force
# deleting on an UNANSWERED question. The answer already lived in this file; it just could not be
# asked for. `--dry-run` / `--delete` ask it on demand, and `scripts/pr-queue.sh prune-merged` is the
# name a session types.
#
# The modes differ ONLY in the matcher and the last loop. Everything between -- the state=closed
# page walk, the four measured fail-opens, head_name(), the schema-change guard -- is the same code, on
# purpose: a second implementation is a second thing to get the `merged` filter wrong in.
MODE=hook
case "${1:-}" in
    --dry-run) MODE=dry; shift ;;
    --delete)  MODE=delete; shift ;;
    -*) echo "$SELF unknown option '$1' (--dry-run|--delete)" >&2; exit 2 ;;
esac
BRANCHES="$*"   # explicit branches to judge; empty means every local branch

# A FLAG AFTER THE FIRST ARGUMENT IS AN ERROR, NEVER A BRANCH NAME.
#
# The `case` above reads only `$1`, so `prune-merged <branch> --delete` put `--delete` into
# BRANCHES and left MODE=hook -- which is the ADVISORY mode: it exits 0 and deletes nothing. The
# caller got a confident per-branch verdict for a branch that does not exist:
#
#     [prune-landed-branches-forgejo] KEEP        --delete — no merged PR with this head branch
#                                                 among the 304 closed PRs searched
#
# and their real branch was never examined. Measured 2026-08-30 while retiring a landed branch by
# hand; the branch survived and the run looked like a considered decision to keep it.
#
# THAT IS THIS FILE'S OWN SUBJECT, one level up. The verb exists because `git branch -d` answers a
# question nobody asked and gets read as the one they did. A misparse that yields a plausible
# verdict rather than an error is the same defect in the tool built to fix it -- and the `-*` arm
# above shows the author already intended flags to be rejected, just not in this position.
#
# THE USAGE HALF NAMES A COMMAND, NOT THIS FILE'S LOG PREFIX. The first version of this message
# interpolated `$SELF` twice, so it rendered as
#     ... must come BEFORE the branch names: [prune-landed-branches-forgejo] [--dry-run|--delete] ...
# -- an example nobody can type, since `$SELF` is a bracketed log tag rather than an argv[0]. It
# shipped through review and CI because no test asserted on the usage string's SHAPE, only on the
# sentence before it. `pr-queue.sh prune-merged` is what a session actually types (this file's own
# header says so) and is what the usage now names. The existing `-*` arm above is the pattern to
# copy: prefix once, then say the thing.
#
# Rejected rather than accepted-anywhere on purpose: reordering `$@` would make `--delete` legal
# after a branch name, which is a second calling convention to remember and does nothing the
# documented order does not. The failure here was silence, not strictness.
for _arg in "$@"; do
    case "$_arg" in
        -*) echo "$SELF option '$_arg' must come BEFORE the branch names: pr-queue.sh prune-merged [--dry-run|--delete] [branch...]" >&2
            exit 2 ;;
    esac
done

# Every "could not ask hub" path ends here. As a hook it stays advisory and exits 0 (a PostToolUse
# hook that fails a tool call would block work over housekeeping); invoked BY HAND it must exit
# NON-ZERO, because a caller who cannot reach the forge has not been told the branch is unmerged --
# it has been told nothing, and that is the whole failure this change exists to remove.
bail() {
    echo "$SELF $1"
    if [ "$MODE" = hook ]; then exit 0; fi
    exit 3
}

# Only fire on an actual merge. The tool-call JSON arrives on STDIN. `$TOOL_INPUT` is set only for
# the inline commands in settings.json, so a script hook reading it would silently never fire --
# indistinguishable from a hook that fired and found nothing to prune.
#
# Heredoc bodies are stripped first: a `git commit` whose MESSAGE documents this command is not an
# invocation of it, and `bash_cmd_parse.strip_heredocs` is the shared parser five other guards use
# for exactly that. Falls back to the raw text if the import fails -- less accurate, never silent.
#
# Hook mode only. A hand invocation has no tool-call JSON on stdin, and `cat` would BLOCK on a
# terminal -- the mode was asked for explicitly, so there is nothing to match on.
if [ "$MODE" = hook ]; then
INPUT="$(cat 2>/dev/null || true)"
# This script's REAL directory: its siblings live there even when it is reached through a symlink.
SELF_DIR=$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")

# The Forgejo PR-merge moment. There is no `gh` here: Forgejo ships no official client CLI, so
# `hub-api.sh pr merge` IS the command a session types, and it is what this couples to.
#
# COMMAND POSITION, NOT A SUBSTRING. `bash_cmd_parse.invokes()` is the shared matcher -- heredoc
# bodies, `VAR=val` prefixes, wrappers and `sh <script>` all stripped, then the name must be the
# thing being RUN. The heredoc half was closed first; a mention outside a heredoc still matched
# here too, so prose quoting the command reached this hook exactly as it reached the GitHub one.
#
# `sh scripts/hub-api.sh pr merge` IS A REAL INVOCATION IN THIS REPO'S TRANSCRIPTS, and `sh` is not
# in `unwrap()`'s WRAPPERS -- naive anchoring on the first token would have silently stopped firing
# on it. `invokes()` strips shell runners itself, and a test pins that form.
#
# ON A PARSER FAILURE THIS REFUSES TO FIRE, AND SAYS SO -- the previous fallback was the raw
# substring, i.e. a fallback to the defect, in a hook that deletes branches.
GATE="$(printf '%s' "$INPUT" | HOOKDIR="$SELF_DIR" python3 -c '
import json, os, sys
sys.path.insert(0, os.environ.get("HOOKDIR", ""))
try:
    cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "") or ""
except Exception:
    raise SystemExit
try:
    import bash_cmd_parse
except Exception:
    print("NOPARSE")
    raise SystemExit
if bash_cmd_parse.invokes(cmd, "hub-api.sh", "pr", "merge"):
    print("FIRE")' 2>/dev/null || true)"
case "$GATE" in
    FIRE) ;;
    NOPARSE) bail "bash_cmd_parse is unimportable, so a merge cannot be told from a mention — not pruning" ;;
    *) exit 0 ;;
esac
fi

# Resolved from this script's own location, not from `$PWD` or `PATH`: hub-api.sh is its sibling in
# this repo, and a PostToolUse hook runs with whatever cwd the session happens to have.
HUB_API="${HUB_API_SH:-$SELF_DIR/hub-api.sh}"
[ -x "$HUB_API" ] || bail "hub-api.sh not executable at $HUB_API — not pruning"

git rev-parse --git-dir >/dev/null 2>&1 || bail "not a git repo — no branches exist to prune"

# WHICH repo on hub. `gh` infers this from `origin`; nothing infers it for Forgejo, so it comes from
# the forge remote (FORGE_TOOLS_REMOTE) -- the same remote a session pushes to before opening the PR. Its absence is a
# real condition and is reported, never assumed.
#
# `PRUNE_REPO` OVERRIDES THE DERIVATION, AND A CROSS-REPO CALLER MUST SET IT. The remote
# describes the CHECKOUT this runs in, which is only the right answer while the branch belongs to
# that checkout's repo. Under `pr-queue.sh --repo <target>` it is not: the queue runs from the hub
# tree and merges on the target, so the derivation searched the hub repo for the target's branch and
# printed `KEEP ... among the 823 closed PRs searched` -- the hub's history, against a repo with
# 13 PRs in total. The verdict was reached from a repo that could not have contained the answer.
#
# It is a separate mechanism from the queue's own fix for `$REPO` being the target where the hub
# was meant. This is the mirror: a repo derived from a git remote where the TARGET was meant. Same
# symptom, opposite direction, which is why one fix could not cover both.
#
# NOTHING WAS LOST ON THE RUN THAT EXPOSED IT -- the drain deleted the branch by its own path
# afterwards. The cost is that a KEEP reached this way is indistinguishable from one that searched
# the right history and found nothing, so on a day the drain does not also delete it, a branch
# survives for a reason nobody can audit.
#
# HUB_REMOTE IS SET ON BOTH PATHS, and that is not tidiness. `MAIN_REF` below is built from it
# (`refs/remotes/$HUB_REMOTE/main`) and this script runs under `set -u`, so assigning it only in the
# derivation branch made every PRUNE_REPO run die with `HUB_REMOTE: parameter not set` before it
# reached a verdict. Caught by the test added with this change, which is the argument for having
# written one: the fix for a wrong answer had turned into no answer at all, in the arm that is by
# construction the one nothing here had exercised before.
# The remote's NAME is configuration (FORGE_TOOLS_REMOTE, default origin). Loaded
# HERE rather than at the top so a missing sibling is reported by `bail` -- this also runs as a hook,
# and a hook must never fail the tool call.
_ft_dir=$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")
[ -r "$_ft_dir/ft-config.sh" ] || bail "cannot read $_ft_dir/ft-config.sh (Forge-Tools' config reader) — not pruning"
. "$_ft_dir/ft-config.sh"
HUB_REMOTE="${HUB_REMOTE:-$FORGE_TOOLS_REMOTE}"
if [ -n "${PRUNE_REPO:-}" ]; then
    REPO_PATH="$PRUNE_REPO"
    case "$REPO_PATH" in
        */*/*|"") bail "PRUNE_REPO must be <owner>/<repo>, got '$PRUNE_REPO' — not pruning" ;;
        */*) : ;;
        *) bail "PRUNE_REPO must be <owner>/<repo>, got '$PRUNE_REPO' — not pruning" ;;
    esac
else
    if ! url="$(git remote get-url "$HUB_REMOTE" 2>&1)"; then
        bail "no '$HUB_REMOTE' remote in this repo, so there is no hub repo to ask — not pruning"
    fi
    # Last two path components, `.git` stripped. Covers http(s)://host/owner/repo.git and
    # ssh://git@host:2222/owner/repo.git alike; both forms are in use on hub.
    REPO_PATH="$(printf '%s' "$url" | sed -e 's|\.git$||' -e 's|.*[/:]\([^/]*/[^/]*\)$|\1|')"
    case "$REPO_PATH" in
        */*) : ;;
        *) bail "cannot read owner/repo out of '$HUB_REMOTE' url ($url) — not pruning" ;;
    esac
fi

# ============================================================================================
# THE ONE QUESTION GIT ANSWERS SOUNDLY, AND THE ONLY ONE THIS FILE MAY ASK IT.
#
# Everything above exists because ancestry LIES about squash merges: the landed commits are on
# `main` under new shas, so `--is-ancestor` says "no" about work that landed. That is a false
# NEGATIVE, and it is why the forge is the authority here.
#
# MERGES SWITCHED TO `Do=merge`, AND THAT DOES NOT RETIRE THIS FILE. Two reasons, and
# the first is permanent: every branch merged BEFORE the switch is still an ancestor of nothing, and
# `main` refuses force push, so that history cannot be repaired. A pruner that asked git would
# fail-open on exactly those. Second, the queue REBASES before opening, so a branch's local ref can
# still differ from what landed even now. What the switch narrows is the justification, not the need --
# for post-switch branches ancestry is sound, so a future version could take the git answer as
# authoritative for that era and fall back to the forge for the rest. That split needs its own
# fault injection and is deliberately NOT done here.
#
# There is exactly one case where ancestry is not answering that question at all. If a branch is an
# ancestor of `main`, every commit it has is already on `main` -- there is no content that could
# have been rewritten, so there is no squash for ancestry to be wrong about. The signal this file
# was built to distrust is, in that one case, complete and correct.
#
# MEASURED, 2026-08-26. `agent/74-findings` held a ticket's findings and never carried a PR; the work
# landed on `agent/74-gate-suppression` as another PR. The branch was 0 commits ahead of `hub/main`, an
# ancestor of it, absent from hub's remote heads, and its worktree clean -- the safest possible
# case, and it fell in the "no PR at all" bucket that is reserved for the most dangerous one.
# `prune-merged` printed KEEP and would have for ever, because the oracle can only clear a branch a
# PR merged and no PR ever existed. A sound local answer existed and no sanctioned verb accepted it.
#
# THIS CANNOT WEAKEN THE FORGE ORACLE. **THE ORIGINAL REASON FOR THAT EXPIRED WITH THE SWITCH TO
# `Do=merge` AND THE CONCLUSION SURVIVES ON A DIFFERENT ONE.** What was written here, and why it is no longer
# load-bearing:
#
#   "it only fires on a branch with NO commits of its own. A branch with content is not an ancestor
#    of `main` under squash-merge ... every branch it does reach is a bare pointer."
#
# That was an emergent property of SQUASH, never a property of this code -- the predicate below is
# `--is-ancestor` and nothing else. Since the switch, merges preserve shas, so a content-bearing branch IS
# an ancestor once it lands, and this arm now reaches branches with real commits. MEASURED
# 2026-08-27: a local branch at `50d73cdfc5` (which introduced 4 files) took this arm and was
# correctly reported deletable.
#
# THE CONCLUSION IS UNCHANGED, and now rests on the predicate itself rather than on a merge style:
# `--is-ancestor b main` is true IFF every commit reachable from `b` is reachable from `main`. So
# whatever the branch holds, `main` holds it too, and deleting the ref destroys only the NAME --
# `git branch <name> <sha>` recreates it exactly. That is sound in either era and cannot be
# invalidated by a future merge-style change, which the old wording could and was.
#
# The direction of the remaining risk is unchanged too: ancestry can only WITHHOLD this verdict
# (a false negative, which falls through to the forge), never grant it wrongly.
#
# WHY THE REMOTE-TRACKING REF AND NOT A FETCH. A stale `hub/main` errs in the SAFE direction and
# cannot err in the other: ancestry is monotonic as `main` advances, so a branch contained in an
# older `main` is contained in every later one. A stale ref can therefore only withhold this
# verdict, never grant it wrongly -- and this file has no business running a network fetch that its
# callers (a PostToolUse hook, an unattended `pr-queue.sh`) did not ask for. Absent ref: no verdict.
#
# ponytail: `--is-ancestor` alone, not `--is-ancestor` AND `rev-list --count == 0`. The two are the
# same predicate -- b is an ancestor of main IFF main..b is empty -- so the second is a restatement
# that would read as a stronger check while measuring nothing new.
# ============================================================================================
MAIN_REF="refs/remotes/$HUB_REMOTE/main"
git show-ref --verify --quiet "$MAIN_REF" || MAIN_REF=""

# ONLY WHEN THE CALLER NAMED THE BRANCH, and this is the whole of what stopped it being a sweep.
#
# MEASURED on the real repo before this line existed: the arm fired on TEN local branches --
# `wayfinder/161`, `wayfinder/206`, `wayfinder/210`, `work`, `stage-296`, three `worktree-agent-*`
# refs, and the branch this change was being written on. Every one of them is contained in `main`
# and every one is a deliberate BOOKMARK. `prune-merged --delete` takes no arguments in its common
# form and is repo-wide, so an unconditional arm would have swept all ten to clear one findings
# branch. Losing no commits is not the same as losing nothing: the NAME is the information.
#
# The distinction that fixes it is already in the file -- `$BRANCHES` is empty for a repo-wide sweep
# and non-empty when a caller asked about specific branches. A sweep answers "what can I tidy",
# where silence about a bookmark is correct. A named query answers "is THIS one finished", which is
# a question the caller has already decided is worth asking. `scripts/worktree-reap.sh` passes the
# unowned worktrees' branches explicitly, so the case that motivated this -- a reaper refusing a tree whose
# branch is empty -- is reached, and `prune-merged <branch>` reaches it too when asked by name.
#
# ponytail: no allow-list of bookmark prefixes. It would need maintaining, it would guess at intent
# from a name, and the caller-asked distinction is both free and exactly the right question.
is_contained_in_main() {
    [ -n "$BRANCHES" ] || return 1
    [ -n "$MAIN_REF" ] || return 1
    git merge-base --is-ancestor "$1" "$MAIN_REF" 2>/dev/null
}

# `state=closed` because `merged` is not a state Forgejo accepts (see fail-open 1 above) and `all`
# would drag open PRs into the filter's input for no reason.
#
# ============================================================================================
# 4. A ONE-PAGE WINDOW READS AS "NOTHING LANDED" FOR EVERY OLDER PR. Measured 2026-08-21.
#    This used to make ONE call, `?state=closed&limit=50`, and report an unmatched branch as
#    "hub lists NO merged PR with this head branch". That window returned only the 50 newest on the day
#    it was measured, so every older PR was invisible and the sentence was a claim the query could
#    not support. `docs/orca-open-design-research` was landed by PR #78 (merged as c4aaee1266) and
#    was reported KEEP -- worded identically to a branch that never had a PR at all. Silent, and in
#    the SAFE direction, which is why it went unnoticed: branches accumulate as KEEP and the list
#    stops meaning anything. This hook exists BECAUSE git answers "did this land" wrongly under
#    squash-merge; a window that ages out turns the one authority git is not into a second false
#    negative with better wording.
#
#    So: page until a SHORT page comes back. Forgejo's MAX_RESPONSE_ITEMS default is 50 and hub does
#    not override it in /etc/forgejo/app.ini, so `limit` cannot usefully exceed that; 137 closed PRs
#    cost 3 calls. REJECTED ALTERNATIVE -- one call per branch via `/pulls/{base}/{head}`. It does
#    work (measured: it resolves #78 by branch name even though that branch is gone from hub), but
#    hub-api.sh surfaces its 404 as a NONZERO EXIT, indistinguishable from an unreachable forge. That
#    collapses "no PR" into "could not ask", which is the one distinction this whole file exists to
#    keep -- and it also assumes every PR is based on `main`. It was also not cheaper here: 7 local
#    branches, 3 pages.
#
#    PAGE_MAX exists because a forge that IGNORED `page` would return the same full page for ever.
#    On the cap the run does not pretend it saw everything -- `$truncated` reaches the verdict, and
#    every KEEP line cites the number of closed PRs actually searched.
# ============================================================================================
PAGE_LIMIT=50
PAGE_MAX="${PRUNE_PAGE_MAX:-20}"

# ============================================================================================
# THE WALK IS CACHED IN HOOK MODE ONLY, because merged is TERMINAL.
#
# A PR that is merged stays merged and its head branch name never changes, so pages 2..N are
# immutable history being re-derived on every merge. Measured 2026-09-04 over 49 recorded hook runs:
# ~9-11 pages at ~2s each, median 14.5s per run -- and 46 of those 49 runs deleted NOTHING. About
# 11 minutes of blocked tool loop across ten days to remove 7 branches. The cost grows with repo
# history for ever; the information does not.
#
# HOOK MODE ONLY, and that restriction is the correctness argument rather than caution. `$seen` and
# `$truncated` are what let a KEEP verdict say how much it actually searched -- the whole
# reason this file may not claim more than it measured. They are read at exactly two sites, both
# guarded by `[ "$MODE" != hook ]`, so a cache that exists only in hook mode can never put a stale
# count into a verdict. `--dry-run` and `--delete` are typed deliberately, are the paths where an
# honest count matters, and always do the full walk. `test_the_cache_is_never_read_outside_hook_mode`
# pins that, because the guarantee is a property of WHERE the cache is consulted, not of its content.
#
# STALENESS CAN ONLY WITHHOLD A DELETION, never invent one: a branch merged since the last refresh is
# absent from the cached set, so it takes the not-merged path and SURVIVES to the next run. That is
# the direction this file already fails in by design, and it is the behaviour actually observed --
# the 5-branch sweep in the measurement above was branches that had accumulated across earlier
# merges. A cache lengthens that window; it does not create it.
#
# An EMPTY cache file is treated as a miss, not as "no merged PRs": those are different facts, and a
# truncated write must not read as an answer.
# ============================================================================================
CACHE_TTL_MIN="${PRUNE_CACHE_TTL_MIN:-180}"
CACHE=""
if [ "$MODE" = hook ]; then
    # The COMMON dir, so every worktree shares one refresh rather than each paying its own cold
    # walk. It is outside the working tree by construction, so it can never be committed.
    #
    # RESOLVED TO ABSOLUTE, because `--git-common-dir` answers in two shapes: `.git` (relative) in a
    # plain checkout, an absolute path from a worktree -- measured 2026-09-04. Nothing in this file
    # cds today, so the relative form happens to work; a cache path that silently depends on that
    # is a trap for the next edit, and this costs one subshell.
    #
    # `if` rather than `[ ... ] && CACHE=...`, and NOT for the reason first written here. That claim
    # was that `set -e` kills the script when the test fails; measured 2026-09-04 it does not --
    # busybox ash and bash both survive `[ -n "$E" ] && Y=x`, against a `true && false` control that
    # dies (rc=1, no output) in both. The real reason is narrower: the forge runner's /bin/sh is
    # dash, which is NOT measured here, and `if` is unambiguous in every shell without anyone having
    # to know which. Kept as a note because a comment asserting a mechanism nobody ran is the thing
    # this file's own header spends 500 lines warning about.
    _cd="$(git rev-parse --git-common-dir 2>/dev/null || true)"
    if [ -n "$_cd" ]; then
        _abs="$(cd "$_cd" 2>/dev/null && pwd || true)"
        if [ -n "$_abs" ]; then
            CACHE="$_abs/prune-landed-merged.cache"
        fi
    fi
fi

merged=""
seen=0
truncated=""
page=1

# `find -mmin +N` lists the file only when it is OLDER than N minutes, so empty output is "fresh".
# Both arms proven to flip, 2026-09-04: fresh->HIT, aged->MISS, re-freshened->HIT, empty->MISS,
# absent->MISS. `-s` is load-bearing and was the bug in the first version of that proof: a zero-byte
# file is the truncated-write case above.
if [ -n "$CACHE" ] && [ -s "$CACHE" ] && [ -z "$(find "$CACHE" -mmin +"$CACHE_TTL_MIN" 2>/dev/null || true)" ]; then
    merged="$(cat "$CACHE" 2>/dev/null || true)"
else
while : ; do
if ! resp="$("$HUB_API" "/api/v1/repos/$REPO_PATH/pulls?state=closed&limit=$PAGE_LIMIT&page=$page" 2>&1)"; then
    bail "cannot read $REPO_PATH pulls from hub (page $page), not pruning: $resp"
fi

# First line is how many closed PRs this page held -- the only count the verdict may cite. Then the
# head branch names of the MERGED ones, one per line.
#
# NO APOSTROPHE MAY APPEAR IN THE PYTHON BELOW. It is passed as a single-quoted `python3 -c`
# argument, and shell single quotes admit no escapes -- so one quote in a docstring or a message ends
# the argument early and python gets a truncated program. That is not an error anyone sees: `|| true`
# swallows it and the hook prunes nothing from a healthy API call. It happened while writing this
# hook and nine tests caught it at once; `test_every_inline_python_block_survives_shell_quoting`
# now compiles each block. `$resp` cannot move to argv instead: MAX_ARG_STRLEN is 128 KB and 50 of
# Forgejo`s PR objects exceed that.
page_out="$(printf '%s' "$resp" | python3 -c '
import json, sys

try:
    prs = json.load(sys.stdin)
except Exception as e:
    print("ERROR unreadable response from hub: %s" % e)
    raise SystemExit

if not isinstance(prs, list):
    # hub answers an unauthenticated call with a JSON OBJECT carrying a message, at HTTP 200.
    print("ERROR hub did not return a pull list: %s" % str(prs)[:200])
    raise SystemExit


def head_name(pr):
    """head.ref when it is a branch, head.label when Forgejo has replaced it with a pull ref.

    Measured on hub: a PR whose head branch was deleted reports ref=refs/pull/<N>/head and keeps the
    branch name only in label. A cross-repo label is owner:branch, and a colon is illegal in a branch
    name, so taking the part after it is unambiguous.

    No backticks here, deliberately: shellcheck reads them as command substitution inside the
    single-quoted argument (SC2016). They are literal to the shell and harmless, but they are also
    pure decoration in a docstring, so deleting them is cheaper than a disable pragma."""
    head = pr.get("head") or {}
    ref = head.get("ref") or ""
    name = ref if ref and not ref.startswith("refs/") else (head.get("label") or "")
    return name.split(":", 1)[-1]


names, with_key = [], 0
for pr in prs:
    if "merged" in pr or "merged_at" in pr:
        with_key += 1
    if pr.get("merged") is True or pr.get("merged_at"):
        n = head_name(pr)
        if n:
            names.append(n)

if prs and with_key == 0:
    # Every closed PR lacked the field the filter reads. That is a schema change, not an empty
    # steady state, and the two are otherwise the same silence.
    print("ERROR %d closed PRs carried no merged/merged_at field — refusing to read that as "
          "nothing-landed" % len(prs))
    raise SystemExit

print(len(prs))
for n in names:
    print(n)
' 2>&1 || true)"

case "$page_out" in
    ERROR*) bail "${page_out#ERROR }" ;;
esac

# The count line and the names. A page that produced NEITHER (the python died before printing
# anything, e.g. the truncated-program trap above) must not read as an exhausted list.
count="$(printf '%s\n' "$page_out" | head -n 1)"
case "$count" in
    ''|*[!0-9]*) bail "unreadable page $page from hub (no PR count in the filter output) — not pruning" ;;
esac
names="$(printf '%s\n' "$page_out" | tail -n +2)"
seen=$((seen + count))
[ -n "$names" ] && merged="$merged$names
"

# A short page is the end of the list. Anything else means there is more, and the cap is the only
# thing standing between this loop and a forge that ignores `page`.
[ "$count" -lt "$PAGE_LIMIT" ] && break
page=$((page + 1))
if [ "$page" -gt "$PAGE_MAX" ]; then
    truncated=" (stopped at the ${PAGE_MAX}-page cap; PRs older than these were NOT searched)"
    break
fi
done

# WRITTEN ONLY ON A COMPLETE WALK. A run that hit the PAGE_MAX cap has NOT seen the older history,
# and caching that partial answer would turn a loud one-run truncation into a quiet three-hour one --
# every branch below the cap reading as unmerged until the TTL expires. `$truncated` is exactly the
# flag for it, so the cap and the cache agree by construction rather than by comment.
#
# TEMP FILE PLUS RENAME, because several sessions merge concurrently on this box and a half-written
# cache read by the next run is the truncated-write case the `-s` test above exists for. `mv` within
# one directory is atomic; a failure at any step leaves the previous cache untouched and the next run
# simply walks again.
if [ -n "$CACHE" ] && [ -n "$merged" ] && [ -z "$truncated" ]; then
    _tmp="$CACHE.$$"
    if printf '%s' "$merged" > "$_tmp" 2>/dev/null; then
        mv -f "$_tmp" "$CACHE" 2>/dev/null || rm -f "$_tmp" 2>/dev/null || true
    else
        rm -f "$_tmp" 2>/dev/null || true
    fi
fi
fi

# hub answered, and no closed PR is merged: nothing can be landed-stale. Silent as a hook; said out
# loud when a caller ASKED, because "the forge lists no merged PR at all" and "your branch is not
# among them" are different answers and only one of them is about your branch.
#
# IT NO LONGER EXITS HERE. It used to, and that would have made the contained-in-main arm
# below fire only when some UNRELATED branch happened to have landed -- a feature quietly
# conditional on data it has nothing to do with. The forge's answer is still reported exactly as
# before; the loop then runs with an empty `$merged`, so every branch takes the not-merged path and
# only the ancestry arm can clear anything.
if [ -z "$merged" ]; then
    if [ "$MODE" != hook ]; then
        echo "$SELF hub answered: NO merged PR among the $seen closed PRs on $REPO_PATH$truncated, so nothing here has landed"
    fi
fi

# The verdict loop. `$BRANCHES` is empty in hook mode, so `${BRANCHES:-$(...)}` is every local
# branch exactly as before; a hand invocation may name branches instead. Unquoted on purpose in
# both arms -- the split into words IS the list.
deleted=""
# shellcheck disable=SC2086
for b in ${BRANCHES:-$(git for-each-ref --format='%(refname:short)' refs/heads/)}; do
    [ "$b" = "main" ] && continue
    if ! printf '%s\n' "$merged" | grep -qxF "$b"; then
        # THE ARM THAT MUST EXIST. A pruner that deletes everything and one that deletes nothing
        # both look successful on a happy path; this is where the second is told from the first.
        #
        # AND IT MAY ONLY CLAIM WHAT IT SEARCHED. "hub lists NO merged PR with this head
        # branch" was a claim about the whole forge made from one page of it. The verdict now cites
        # the number of closed PRs this run actually read, and says so when the cap cut it short --
        # so a KEEP that means "not found in what I looked at" cannot be read as "not merged".
        # THE ONE ARM THAT DOES NOT NEED THE FORGE. See the block above `MAIN_REF`. A branch
        # contained in `main` has every one of its commits on `main`, so deleting the ref loses only
        # the name. **It may now hold real commits** -- the old wording said it "has no commits of
        # its own", which was true only while squash guaranteed it and stopped being true with
        # `Do=merge`. The reason string deliberately does NOT say "merged per hub": hub said nothing
        # about this branch, and a verdict must not claim an authority it did not consult.
        #
        # "no PR" in the message stays accurate: this arm is only reached when `$b` was absent from
        # the merged list, so a branch that DID land through a PR takes the `merged per hub` path
        # below and never arrives here.
        if is_contained_in_main "$b"; then
            esha="$(git rev-parse --short "$b" 2>/dev/null || echo '?')"
            if [ "$MODE" = dry ]; then
                echo "$SELF WOULD DELETE $b($esha) — no PR, but every commit is already on $MAIN_REF (rerun with --delete)"
            elif git branch -D "$b" >/dev/null 2>&1; then
                deleted="$deleted $b($esha)"
            elif [ "$MODE" != hook ]; then
                echo "$SELF COULD NOT DELETE $b($esha) — contained in $MAIN_REF, but git refused (checked out in a worktree?)"
            fi
            continue
        fi
        if [ "$MODE" != hook ]; then
            # NAMES THE REPO IT SEARCHED. Without it this line reads identically whether
            # the search was in the right history or the wrong one, which is why a wrong-repo KEEP
            # took a human reading two logs side by side to notice. The count alone was the only
            # tell -- 823 closed PRs against a repo that has 13 -- and a count is only implausible
            # to someone who already knows which repo it was about.
            echo "$SELF KEEP        $b — no merged PR with this head branch among the $seen closed PRs searched on $REPO_PATH$truncated"
        fi
        continue
    fi
    sha="$(git rev-parse --short "$b" 2>/dev/null || echo '?')"
    if [ "$MODE" = dry ]; then
        echo "$SELF WOULD DELETE $b($sha) — merged per hub (rerun with --delete)"
        continue
    fi
    if git branch -D "$b" >/dev/null 2>&1; then
        deleted="$deleted $b($sha)"
    elif [ "$MODE" != hook ]; then
        # git refuses a branch checked out in ANY worktree, which is the guard this file
        # deliberately does not duplicate. As a hook that refusal is invisible; when asked, say it.
        echo "$SELF COULD NOT DELETE $b($sha) — merged per hub, but git refused (checked out in a worktree?)"
    fi
done

# ORPHANS: merged per hub, still on the remote, and with NO local ref -- so the loop above could not
# see them. Its universe is `refs/heads/`, and THIS TOOL CREATES THE BLIND SPOT ITSELF: once
# it deletes a local branch it prints "remote branch remains", and from the next run onward that
# branch has no local ref and never appears again, in any verdict. `pr-queue.sh` reaches the same
# state by rebasing and merging every branch it admits. So the branches most likely to be orphaned
# on hub are exactly the ones the sweep cannot report.
#
# REPORT ONLY, NEVER DELETE. The note below is why the remote is not touched: git's refusal on a
# branch checked out in any worktree is the guard this file leans on, and it has no remote
# equivalent, so a `push --delete` here could remove a live session's branch with nothing to refuse
# it. Printing cannot, so this needs none of that guard -- and widening `--delete` remains out.
#
# Skipped when branches were NAMED: those were judged above, and the caller already knows them.
if [ -z "$BRANCHES" ] && [ "$MODE" != hook ]; then
    # CANDIDATES FIRST, AND THE NETWORK ONLY IF THERE ARE ANY. `ls-remote` is a real network call,
    # and the first draft made it unconditionally: the prune tests went 12.10s -> 19.21s (+59%)
    # because their fixture's `hub` URL is a deliberately unreachable placeholder that every test
    # then tried to contact. Most runs have no orphan at all, so the common case must cost nothing.
    #
    # `sort -u`: this iterates hub's merged list DIRECTLY rather than local refs, so a head branch
    # listed twice (page overlap, or one branch across two PRs) would report the same orphan twice.
    # Measured 2026-08-22 -- the first draft printed `wiki/session-cases` twice. The verdict loop
    # cannot hit this: its universe is `refs/heads/`, where each name occurs once by construction.
    candidates=""
    # shellcheck disable=SC2086
    for b in $(printf '%s\n' "$merged" | sort -u); do
        [ "$b" = "main" ] && continue
        git show-ref --verify --quiet "refs/heads/$b" && continue
        candidates="$candidates $b"
    done
    if [ -n "$candidates" ]; then
        # GIT_TERMINAL_PROMPT=0 so an auth-requiring remote fails instead of blocking on a prompt --
        # this runs inside `pr-queue.sh`, which may be unattended.
        remote_heads="$(GIT_TERMINAL_PROMPT=0 git ls-remote --heads "$HUB_REMOTE" 2>/dev/null \
                        | sed -n 's|^.*[[:space:]]refs/heads/||p')"
        # shellcheck disable=SC2086
        for b in $candidates; do
            printf '%s\n' "$remote_heads" | grep -qxF "$b" || continue
            echo "$SELF ORPHAN      $b — merged per hub, still on '$HUB_REMOTE', no local ref (not deleted)"
        done
    fi
fi

# NAME THE SIDE. This deletes the LOCAL branch only, and the standing convention it automates is
# "confirm the content landed, then delete remote AND local" -- so an unqualified "deleted landed"
# is read as both being done. Measured 2026-08-21: after `prune-merged --delete fix/162-prune-merged`
# printed that line, `git ls-remote --heads hub` still listed the branch. The printed sha compounds
# it: `git rev-parse` resolves the LOCAL ref, so a branch rebased before its PR (which is what
# `pr-queue.sh` does to every branch it admits) prints a sha the remote never had.
#
# The remote is deliberately NOT deleted here. Git refuses to delete a branch checked out in ANY
# worktree, which is the guard this file relies on instead of carrying its own -- and there is NO
# equivalent protection on the remote side. This box routinely has several sessions with live
# worktrees whose branches appear in this very list, so a `push --delete` in this loop would remove
# another session's remote branch with nothing to refuse it. Widening it needs that guard first.
# THE SHA IS THE REF REMOVED, NOT THE COMMIT THAT LANDED. `git rev-parse` above resolves
# the LOCAL ref, and `pr-queue.sh` rebases every branch it admits, so for an admitted branch the
# local sha is the PRE-rebase commit -- which is an ancestor of nothing and provably never landed.
# Measured 2026-08-22 on `wayfinder/23` after its PR merged: the line printed `ab5d92437e`, while
# `7c707552ee` was what reached `main`. The DELETION was correct; the evidence printed beside it
# was false, and the word `landed` sitting next to the sha is what made it read as evidence.
# The landing verdict comes from the forge (or from ancestry on the no-PR arm), never from this sha.
# The sha is still worth printing -- it is what `git branch <name> <sha>` needs to undo the delete.
[ -n "$deleted" ] && echo "$SELF deleted LOCAL ref (remote branch remains); sha = the ref removed, NOT the commit that landed:$deleted"
exit 0
