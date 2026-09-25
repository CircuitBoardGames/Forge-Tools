#!/bin/sh
# The client seam.
#
# ONE forge-agnostic surface over two clients, so nothing above this file branches on which
# forge it is talking to. `scripts/hub-api.sh` is behind the hub arm; `gh` is behind the
# GitHub arm. A skill, a queue driver or a session calls the verbs below and never learns
# which one answered.
#
#   forge.sh where                                  what this tree resolved to, and why
#   forge.sh pr open  [--upstream] [--draft|--no-draft] [--serial] <head> <base> <title> [body-file]
#                                                   hub arm drafts a PR that is not the front of
#                                                   the queue itself; the flags state it
#   forge.sh pr gate  <full-40-sha> [--pr <n>]      read the gate; exit code IS the verdict
#                                                   --pr refuses if <n>'s head has left that sha
#   forge.sh pr land  <n> <subject>                 land this PR with this subject
#   forge.sh pr list                                the open queue
#   forge.sh label add|rm <n> <label>
#   forge.sh review request <n> <user>
#   forge.sh review read <n>
#   forge.sh branch rm <branch>
#   forge.sh was-cancelled <owner/repo> <full-40-sha>
#                                                   hub only, no git tree needed: exit 0/1/2/3,
#                                                   the one cancellation probe
#
# GITHUB'S HALF IS FOR FORKS OF UPSTREAM REPOS WE DO NOT OWN. A repo whose GitHub copy is only a
# mirror stays on the forge, and that falls out of resolution rather than being a rule someone has
# to remember: such a checkout has both a hub remote and a github remote, and the hub arm wins.
#
# ------------------------------------------------------------------------------------------
# THE THREE DIFFERENCES THIS SEAM ABSORBS. Each is a place where a caller that "just used the
# other client's flags" would be silently wrong rather than broken.
#
# 1. MERGE SUBJECT RULES ARE OPPOSITE, so `pr land` owns the `(#N)` and no caller writes one.
#    Forgejo squashes with `MergeTitleField`, which lands the subject VERBATIM and suppresses
#    Forgejo's own `<title> (#N)` -- so the number exists only if the client appends it, which
#    `hub-api.sh pr merge` does. GitHub appends `(#N)` ITSELF when a PR has two or
#    more non-merge commits, and `gh pr merge --subject` SUPPRESSES that append -- so passing a
#    bare subject there is what CAUSES a missing number.
#    RESOLUTION: both arms append. The hub arm hands the raw subject to `hub-api.sh`, which
#    appends; the GitHub arm appends here and always passes `--subject`, which makes the commit
#    count irrelevant instead of load-bearing. Callers pass a subject with no number, on both
#    forges, always. `main` refuses force-push on hub, so a missing number is permanent -- this
#    is not a tidiness rule.
#
# 2. THE GATE LIVES ON A DIFFERENT ENDPOINT ON EACH FORGE, and reading the wrong one answers
#    confidently. Measured 2026-08-24 on `cli/cli@5d3c4817f1`, a commit with 1251 check-runs:
#    GitHub's COMBINED STATUS API returns `state: "pending"`, `total_count: 0` -- because
#    GitHub Actions writes check-RUNS and not commit statuses. Forgejo Actions writes commit
#    STATUSES and has no check-runs endpoint at all. So the seam reads
#    `/commits/<sha>/status` on hub and `/commits/<sha>/check-runs` on GitHub, and a caller
#    that generalised from either one would have measured nothing on the other.
#
#    ZERO REGISTERED CHECKS REFUSES ON BOTH SIDES, and both sides need the refusal built by
#    hand. Forgejo answers a commit with no runs `total_count: 0, state: ""` (empty, not
#    "failure"). GitHub answers `total_count: 0` with HTTP 200. Measured on
#    a vendored fork's commit `94732e1646`: `gh api .../check-runs` exits 0 with
#    `total_count: 0`, and `gh pr checks` prints `no checks reported` and also exits 0. On both
#    forges the natural test calls "nothing ran" a pass.
#
#    GITHUB NEEDS A DEDUPE FORGEJO DOES NOT. Every re-run and every scheduled workflow adds
#    MORE check-runs to the same sha rather than replacing them -- that is where 1251 comes
#    from, across dozens of check_suites. Counting them raw would report a stale failed rerun
#    forever. So the GitHub arm keeps the LATEST run per check-run NAME and evaluates those.
#
#    THIS VERB NEVER CLAIMS "ACTUALLY RAN". A started-and-internally-skipped job
#    reports `success` and is indistinguishable, at this level, from one that ran a full suite:
#    neither the status API nor the check-runs API can see inside a job. The verdict states
#    registered / skipped / pending / failed and stops there. `hub-api.sh pr checks` is weaker
#    here: its own summary line still says it; this seam does not inherit
#    the claim by wrapping it, because it does not wrap that verb -- it reads the API directly
#    through `hub-api.sh`'s generic passthrough.
#
# 3. A RED ON HUB CAN BE A CANCELLATION THIS SESSION CAUSED. Forgejo renders a cancelled run as
#    `failure` in the commit-status API, and supersedes the previous run on a push (the
#    supersede is per-REF, wider than the PR-head scoping some docs still carry).
#    A driver that rebases at admission then reads the gate sees a red it caused itself. So a
#    hub RED is checked against `/actions/runs?head_sha=` and reported as CANCELLED, which is
#    its own exit code and not a test failure. GitHub reports `conclusion: "cancelled"` on the
#    run itself and needs no second call -- same verdict, different cost.
#
# ------------------------------------------------------------------------------------------
# EXIT CODES OF `pr gate` ARE THE CONTRACT. `hub-api.sh pr checks` returns 1 for `pending` and
# 1 for `failure` alike, which is why `pr-queue.sh` parses its printed state instead of its
# status -- it still does, though that verb does now at least separate a REFUSAL
# to measure (2) from any measurement it took (1). This verb discriminates fully, so a
# driver can branch on `$?`:
#
#   0  GREEN      every registered check finished acceptably
#   1  RED        a real failure -- stop, a human should look
#   2  REFUSED    no verdict was taken: zero registered checks, the sha is not a commit on
#                 this repo, or the argument was not a full 40-char sha (a
#                 refusal to MEASURE, which is why it is not 1; it belongs in this code
#                 rather than a new one because it is the same category as the other two,
#                 not a third unrelated meaning of the kind the `stale` note below warns
#                 against)
#   3  PENDING    still running -- waiting is the correct response
#   4  CANCELLED  red, but a run was cancelled/superseded -- NOT a test failure
#   5  STALE      --pr was given and that PR's head has moved off the sha you asked about;
#                 no gate was read, so nothing green was printed
#
# ------------------------------------------------------------------------------------------
# WHAT IS NOT PROVEN. The GitHub arm's WRITE paths -- `pr open`, `pr land`, `label`, `review
# request`, `branch rm` -- are exercised against a `gh` stub with their argv asserted, and have
# NEVER been run against github.com. The token on this box is read-only on third-party repos:
# every read succeeds and every write 403s, and only the write attempt reveals it. Clearance is
# not capability. Do not read the hub arm's green as covering both halves.
#
# ponytail: the fork arm targets a PR at the fork's parent when asked, and stops there. It does
# not sync a fork with its upstream, and it does not create the fork. Upgrade path: a `fork
# sync` verb, once there is a caller that needs one.

set -eu

# Never trace: `set -x` upstream would print `hub-api.sh`'s config line, token and all.
set +x

# The REAL directory: installed as a symlink on PATH, `dirname $0` is the link's, not ours.
SELF_DIR=$(dirname -- "$(readlink -f -- "$0" 2>/dev/null || printf '%s' "$0")")
HUB_API="${FORGE_HUB_API:-$SELF_DIR/hub-api.sh}"
# Site configuration -- which hosts are the forge, which remote is its.
[ -r "$SELF_DIR/ft-config.sh" ] || { printf 'forge: cannot read %s/ft-config.sh -- the config reader must sit beside this script\n' "$SELF_DIR" >&2; exit 1; }
. "$SELF_DIR/ft-config.sh"
GH="${FORGE_GH:-gh}"

die()    { printf 'forge: %s\n' "$*" >&2; exit 1; }
refuse() { printf 'REFUSING: %s\n' "$*" >&2; exit 2; }
# Its OWN status, not refuse()'s 2. A stale head is the one refusal a driver can
# recover from without a human: re-gate the head the PR actually has. Folding it into 2 would
# make REFUSED mean three unrelated things, and this file's whole claim is that the exit code
# discriminates. Same `REFUSING:` prefix, because to a human reader it IS a refusal.
stale()  { printf 'REFUSING: %s\n' "$*" >&2; exit 5; }

# --------------------------------------------------------------------------------------
# RESOLUTION -- which forge, which repo, and which remote to verify a push against.
#
# Resolved from the GIT WORK TREE THIS RUNS IN, not from the script's own path: the seam lives
# in the hub repo but must be able to act on a fork checked out somewhere else entirely. That
# is the opposite of `hub-api.sh`'s `repo_root()`, deliberately.
# --------------------------------------------------------------------------------------

# The AUTHORITY of a remote URL -- the host alone, with userinfo and port removed. Empty when
# the URL names no host, which is the ordinary answer for a local-path remote (a `mirror`
# remote such as `/opt/app-src`).
#
# WHY THIS IS A SEPARATE PARSE. `remote_kind` used to glob the WHOLE URL, so a token
# appearing anywhere in it -- including in the PATH -- named the forge, while git dialled the
# host. Measured with `forge.sh where`: `ssh://git@attacker.invalid/github.com/repo.git`
# resolved to `forge github` / `repo github.com/repo`. Found by checking worktrunk's 0.74.0
# `ssh://` fix against this file: theirs searched the whole remainder for the userinfo `@`, so
# `ssh://git@attacker.example/owner/repo@github.com/org/repo.git` resolved to `github.com` and
# borrowed that repository's approvals. Same shape -- a parse searching a whole URL for
# something only the authority is allowed to say.
#
# THE HALF THAT WAS NOT OBSERVABLY BROKEN, and the reason the fix belongs HERE rather than in
# `remote_slug`: the hub-token case, `https://attacker.invalid/forge.example.org/owner/repo.git`,
# WAS classified `hub` and then refused -- by `remote_slug`'s arity check, which is about the
# SHAPE of a slug and knows nothing about hosts. That is not a guard holding; it is a guard
# absent, standing next to something that happens to fail first. **It is not a control and must
# not be cited as one** -- refactor the slug check and the misclassification becomes live with
# nothing left to catch it.
remote_authority() {
    a=$1
    case "$a" in
    *://*) a=${a#*://}; a=${a%%/*} ;;   # scheme://[user@]host[:port]/path
    *:*)   a=${a%%:*} ;;                # [user@]host:path -- the scp form
    *)     printf ''; return 0 ;;       # a local path: no host, so no forge
    esac
    a=${a##*@}                          # userinfo; longest match, so an `@` inside it loses
    printf '%s' "${a%%:*}"              # port
}

# The one place a URL becomes a forge. Anything unrecognised returns empty, and an empty kind
# refuses later -- it never falls through to a default, because guessing the forge is exactly
# the branch this file exists to remove.
#
# Matched against the authority, never the whole URL. Every token here is a HOST, which is what
# makes that a one-line change rather than a migration -- if a token is ever added that is not a
# host, this function is the wrong place for it.
#
# THE HUB'S HOSTS ARE CONFIGURATION: the host of FORGE_TOOLS_FORGE_URL plus every
# name in FORGE_TOOLS_FORGE_HOSTS (an ssh alias, a second address). Unconfigured, nothing is `hub`.
remote_kind() {
    _auth=$(remote_authority "$1")
    case "$_auth" in
    github.com|*.github.com) printf 'github'; return 0 ;;
    '')                      printf ''; return 0 ;;
    esac
    for _h in $(ft_forge_hosts); do
        [ "$_auth" = "$_h" ] && { printf 'hub'; return 0; }
    done
    printf ''
}

# owner/repo out of either URL form. Refuses rather than returning a plausible fragment: a
# wrong slug does not fail, it acts on somebody else's repo.
remote_slug() {
    s=${1%.git}
    case "$s" in
    *://*) s=${s#*://}; s=${s#*@}; s=${s#*/} ;;   # scheme://[user@]host/owner/repo
    *:*)   s=${s##*:} ;;                          # [user@]host:owner/repo
    *)     die "cannot read owner/repo out of remote URL '$1'" ;;
    esac
    s=${s#/}
    case "$s" in
    */*/*|*/|/*) die "remote URL '$1' does not name a plain owner/repo (got '$s')" ;;
    */*)         ;;
    *)           die "remote URL '$1' does not name a plain owner/repo (got '$s')" ;;
    esac
    printf '%s' "$s"
}

KIND=""; REPO=""; PUSH_REMOTE=""; ROOT=""; BOTH=""
resolve() {
    ROOT=$(git rev-parse --show-toplevel 2>/dev/null) \
        || die "not inside a git work tree -- the seam resolves its forge from the tree it runs in"
    KIND="${FORGE_KIND:-}"; REPO="${FORGE_REPO:-}"; PUSH_REMOTE="${FORGE_REMOTE:-}"

    hub_r=""; gh_r=""
    # `hub` and `origin` first so that, within a kind, the canonical remote wins over an
    # incidental one. Order matters for GitHub, where `origin` is our fork and `upstream` is
    # the repo we do not own -- picking the wrong one would aim every write at the upstream.
    for r in "$FORGE_TOOLS_REMOTE" origin upstream $(git -C "$ROOT" remote); do
        git -C "$ROOT" remote get-url "$r" >/dev/null 2>&1 || continue
        u=$(git -C "$ROOT" remote get-url "$r")
        case "$(remote_kind "$u")" in
        hub)    [ -n "$hub_r" ] || hub_r="$r" ;;
        github) [ -n "$gh_r" ]  || gh_r="$r" ;;
        esac
    done

    # THE FORGE REMOTE AND THE PUSH REMOTE ARE TWO THINGS, and conflating them is how the slug
    # ends up read off a remote that has no owner/repo in it at all. They coincide in every
    # ordinary case; `FORGE_REMOTE` overrides only where the branch is PUSHED, never which repo
    # the verbs act on. `FORGE_REPO` is the override for that.
    slug_r=""
    if [ -z "$KIND" ]; then
        # PREMISE 14 IS ENFORCED HERE AND NOWHERE ELSE. A tree with both remotes is this repo,
        # whose GitHub side is a mirror -- so hub wins, and no caller above ever has to know.
        if [ -n "$hub_r" ]; then
            KIND=hub; slug_r="$hub_r"; BOTH="$gh_r"
        elif [ -n "$gh_r" ]; then
            KIND=github; slug_r="$gh_r"
        else
            die "no remote on $ROOT resolves to a forge this seam knows (looked for github.com and the hub's hosts: $(ft_forge_hosts | tr '\n' ' ')-- FORGE_TOOLS_FORGE_URL / FORGE_TOOLS_FORGE_HOSTS). Set FORGE_KIND and FORGE_REPO to state it."
        fi
    else
        slug_r=$([ "$KIND" = hub ] && printf '%s' "$hub_r" || printf '%s' "$gh_r")
    fi
    [ -n "$PUSH_REMOTE" ] || PUSH_REMOTE="${slug_r:-origin}"
    if [ -z "$REPO" ]; then
        [ -n "$slug_r" ] || die "cannot tell which repo to act on -- no remote resolves to $KIND. Set FORGE_REPO."
        REPO=$(remote_slug "$(git -C "$ROOT" remote get-url "$slug_r")")
    fi
    case "$KIND" in hub|github) ;; *) die "FORGE_KIND='$KIND' is not hub or github" ;; esac
}

# The base repo a PR targets. Same as REPO unless --upstream was asked for, in which case it is
# the fork's parent -- the half of the seam the hub side has no analogue for.
base_repo() {
    if [ "${WANT_UPSTREAM:-}" = 1 ]; then
        [ "$KIND" = github ] || die "--upstream is a fork concept; $KIND has no parent repo for $REPO"
        up=$(git -C "$ROOT" remote get-url upstream 2>/dev/null || true)
        if [ -n "$up" ]; then
            remote_slug "$up"
        else
            out=$("$GH" api "repos/$REPO" --jq '.parent.full_name' 2>&1) || die "cannot read $REPO's parent: $out"
            [ -n "$out" ] && [ "$out" != "null" ] || die "$REPO has no parent repo -- it is not a fork"
            printf '%s' "$out"
        fi
    else
        printf '%s' "$REPO"
    fi
}

# REFUSES WITH 2, NOT 1. This is a refusal to MEASURE, not a measurement that failed,
# and `refuse` (defined beside `die` above) is this file's code for that. With both at 1 a caller
# could not tell "this input will never work" from "the gate is not ready yet", and a poller then
# treats a permanent error as a transient one. Measured: a gate poller spun every 30s for ~7
# minutes on a 10-char sha while the PR it was watching went green in about two.
require_full_sha() {
    case "$1" in
    *[!0-9a-fA-F]*) refuse "'$1' is a ref, not a commit sha. A ref silently resolves to its current tip, so the verdict would re-aim itself as the ref moves. Resolve it: git rev-parse $1" ;;
    esac
    [ "${#1}" -eq 40 ] || refuse "'$1' is a ${#1}-char short sha, not the full 40. It would resolve, and could not be compared to a PR head later. Expand it: git rev-parse $1"
}

# One authenticated GET, whichever forge. hub goes through hub-api.sh so the credential stays
# curl's and never becomes a shell variable here; GitHub goes through gh for the same reason.
# Captured by the caller, never piped: `$?` after a pipeline is the PIPE's status, which is how
# a refusal becomes a silent pass.
api_get() {
    case "$KIND" in
    hub)    "$HUB_API" "/api/v1/repos/$1" ;;
    github) "$GH" api "repos/$1" ;;
    esac
}

# --------------------------------------------------------------------------------------
# GATE -- the verb the whole seam exists for.
# --------------------------------------------------------------------------------------

# Shared verdict formatter. Takes the normalised counts on stdin as JSON so both arms print
# byte-identical lines: a caller must not be able to tell the forges apart from the output.
# Rows are sorted HERE rather than in either arm. Forgejo hands back its statuses in API order
# and GitHub's are grouped by check_suite, so leaving the order to the arms is a way a caller
# could tell the forges apart from the output -- which is how the one-surface premise gets lost one `case`
# statement at a time.
VERDICT_PY='
import json, sys
d = json.load(sys.stdin)
for name, state in sorted(d["rows"]):
    print("  %-52s %s" % (name[:52], state))
print("gate: %s  registered=%d skipped=%d pending=%d failed=%d"
      % (d["verdict"], d["registered"], d["skipped"], d["pending"], d["failed"]))
'

gate_hub() {
    sha=$1
    status_json=$("$HUB_API" "/api/v1/repos/$REPO/commits/$sha/status") \
        || die "could not read the gate for $sha on $REPO (the forge's reason is above)"
    norm=$(printf '%s' "$status_json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
sts = d.get("statuses") or []
total = d.get("total_count") or 0
rows, skipped, pending, failed = [], 0, 0, 0
for s in sts:
    st = s.get("status") or ""
    rows.append([s.get("context") or "?", st])
    if st == "skipped":                  skipped += 1
    elif st == "pending":                pending += 1
    elif st in ("failure", "error"):     failed += 1
# Counted off the statuses list rather than trusted from `state`, because `state` is "" -- not
# "failure" -- when nothing registered, and an empty string is what every natural test reads
# as neutral.
if total == 0 or not sts:
    verdict = "NONE"
elif failed:                             verdict = "RED"
elif pending:                            verdict = "PENDING"
else:                                    verdict = "GREEN"
json.dump({"rows": rows, "registered": total, "skipped": skipped,
           "pending": pending, "failed": failed, "verdict": verdict}, sys.stdout)
') || die "the gate response for $sha was not readable JSON"
    printf '%s' "$norm"
}

gate_github() {
    sha=$1
    # --paginate because a busy upstream accumulates check-runs on one sha without bound
    # (measured: 1251 on cli/cli@5d3c4817f1); --slurp so the concatenated pages arrive as one
    # JSON array instead of several objects `json.load` would die on halfway through.
    runs_json=$("$GH" api "repos/$REPO/commits/$sha/check-runs?per_page=100" --paginate --slurp 2>&1) || {
        case "$runs_json" in
        *"No commit found for SHA"*)
            printf 'REFUSING: %s is not a commit on %s -- check the sha. This is NOT the registration race, so waiting will never change it.\n' "$sha" "$REPO" >&2
            exit 2 ;;
        esac
        die "could not read check-runs for $sha on $REPO: $runs_json"
    }
    norm=$(printf '%s' "$runs_json" | python3 -c '
import json, sys
pages = json.load(sys.stdin)
runs = [r for p in pages for r in (p.get("check_runs") or [])]
total = sum(p.get("total_count") or 0 for p in pages[:1]) if pages else 0
# LATEST RUN PER NAME. GitHub APPENDS a check-run for every re-run and every scheduled
# workflow on the same sha rather than replacing the old one, so the raw list carries dead
# results that would pin a verdict red forever. Forgejo has no equivalent: its statuses are
# already one per context.
latest = {}
for r in runs:
    k = r.get("name") or "?"
    key = (r.get("started_at") or "", r.get("id") or 0)
    if k not in latest or key > latest[k][0]:
        latest[k] = (key, r)
rows, skipped, pending, failed, cancelled = [], 0, 0, 0, 0
for name in sorted(latest):
    r = latest[name][1]
    st, concl = r.get("status") or "", r.get("conclusion") or ""
    rows.append([name, concl or st])
    if st != "completed":                                        pending += 1
    elif concl == "skipped":                                     skipped += 1
    elif concl == "cancelled":                                   cancelled += 1
    elif concl in ("success", "neutral"):                        pass
    else:                                                        failed += 1
if not rows:
    verdict = "NONE"
elif failed:                                                     verdict = "RED"
elif cancelled:                                                  verdict = "CANCELLED"
elif pending:                                                    verdict = "PENDING"
else:                                                            verdict = "GREEN"
json.dump({"rows": rows, "registered": len(rows), "skipped": skipped, "pending": pending,
           "failed": failed + cancelled, "verdict": verdict}, sys.stdout)
') || die "the check-runs response for $sha was not readable JSON"
    printf '%s' "$norm"
}

# hub only: was this red a cancellation rather than a failure? THE ONE COPY.
#
# `hub-api.sh pr await`, `pr-queue.sh`'s drain and `pr gate` below each carried their own copy,
# and they had drifted: on runs `cancelled,running` they answered 0 / 3 / 0. All three now reach
# this function -- the other two through the `was-cancelled` verb, which needs no git tree -- and
# each MAPS the four answers to what it needs; none of them re-derives an answer.
#
# hub_was_cancelled <full-40-sha>   (reads $REPO and $HUB_API)
#   0  a run was cancelled AND the head is settled: every run is terminal and at least one
#      reached a verdict (success/failure) -- nothing will ever settle here again
#   1  runs exist and none was cancelled -- the red IS a verdict
#   2  unreadable, zero runs, or not a full 40-char sha -- NOTHING WAS MEASURED, which is not
#      "not cancelled"
#   3  a run was cancelled and a re-queue may still be coming -- waiting is correct
#
# The measurements are load-bearing: only the SERVER-side `head_sha=` filter works (a client-side
# scan matches nothing, because the field is `commit_sha` and `limit` is ignored), and it needs
# the full 40 characters -- a short sha returns zero runs, indistinguishable from "no such commit".
# The narrowing of 0 and the reasons behind TERMINAL/SETTLED are pr-queue.sh's, above its
# `wait_for_green`; this is that body, moved.
hub_was_cancelled() {
    case "$1" in
        ????????????????????????????????????????) : ;;   # exactly 40
        *) printf "forge: was_cancelled: '%s' is not a full 40-char sha -- refusing (a short sha returns 0 runs and reads as 'no runs')\n" "$1" >&2
           return 2 ;;
    esac
    "$HUB_API" "/api/v1/repos/$REPO/actions/runs?head_sha=$1" 2>/dev/null | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin).get("workflow_runs") or []
except Exception:
    raise SystemExit(2)          # unreadable is NOT "not cancelled"
if not runs:
    raise SystemExit(2)          # zero runs is NOT "not cancelled" either -- say so
if not any(r.get("status") == "cancelled" for r in runs):
    raise SystemExit(1)
# TERMINAL IS NAMED POSITIVELY: an unknown status reads as IN FLIGHT, so it keeps a wait going
# rather than ending one. A LONE cancellation is the re-queue window, so settling also needs a
# run that reached a verdict.
TERMINAL = {"cancelled", "success", "failure", "skipped", "error"}
SETTLED = {"success", "failure"}
if all(r.get("status") in TERMINAL for r in runs) and any(r.get("status") in SETTLED for r in runs):
    raise SystemExit(0)          # cancelled, work finished, nothing will ever settle here
raise SystemExit(3)              # a re-queue may still be coming -- waiting is correct
'
}

# --------------------------------------------------------------------------------------

usage() {
    sed -n '2,30p' "$0"
    exit "${1:-0}"
}

case "${1:-}" in
""|-h|--help|help) usage 0 ;;
esac

verb=$1; shift

# `was-cancelled <owner/repo> <full-sha>` -- the answer above as an exit code, for the two callers
# outside this file. BEFORE `resolve` on purpose: the repo is stated, so no git tree is needed.
if [ "$verb" = was-cancelled ]; then
    [ $# -eq 2 ] || die "usage: was-cancelled <owner/repo> <full-40-sha>"
    REPO=$1
    hub_was_cancelled "$2" && exit 0 || exit $?
fi

resolve

case "$verb" in

where)
    printf 'forge %s\nrepo  %s\ntree  %s\npush  %s\n' "$KIND" "$REPO" "$ROOT" "$PUSH_REMOTE"
    # Say the thing that was decided rather than only its result: a tree carrying both remotes
    # is the mirror-only case, and a reader should be able to see that it was noticed.
    [ -n "$BOTH" ] && printf 'note  remote %s is github and was NOT chosen -- GitHub is mirror-only for this repo\n' "$BOTH"
    exit 0 ;;

pr)
    sub="${1:?usage: pr open|gate|land|list ...}"; shift
    case "$sub" in

    open)
        # `--upstream` before the positionals, so the fork case is stated and never inferred.
        #
        # `--draft` / `--no-draft` override the hub client's own decision: a PR that is
        # not at the front of the queue opens as a draft there, and a caller opening a BURST of PRs
        # it means to batch drafts the first one too, on an empty queue. Passed through only when
        # given, so the client still decides for every caller that says nothing.
        # `--serial` marks the PR so the hub queue never batches it: it lands alone.
        WANT_UPSTREAM=0
        DRAFT_ARG=""
        SERIAL_ARG=""
        while [ $# -gt 0 ]; do
            case "$1" in
            --upstream) WANT_UPSTREAM=1; shift ;;
            --draft|--no-draft) DRAFT_ARG=$1; shift ;;
            --serial) SERIAL_ARG=--serial; shift ;;
            *) break ;;
            esac
        done
        head="${1:?head branch}"; base="${2:?base branch}"; title="${3:?title}"; bodyfile="${4:-}"
        body=""
        if [ -n "$bodyfile" ]; then
            # A FILE, NOT A STRING, and that is the whole reason this argument has this shape.
            # Backticks inside a double-quoted shell argument EXECUTE, so a PR body pasted into
            # `"..."` runs whatever a code span contains. Taking a path removes the hazard from
            # every caller at once instead of asking each of them to remember `"$(cat file)"`.
            [ -f "$bodyfile" ] || die "pr open: no such body file '$bodyfile'"
            body=$(cat "$bodyfile")
        fi
        BASE_REPO=$(base_repo)

        # A PR CAN BE OPENED AGAINST A HEAD MISSING PART OF THE CHANGE. Checked HERE, above the
        # split, so both forges refuse it -- `hub-api.sh pr create` has this check and `gh pr
        # create` does not, and a difference in strictness between the arms is exactly the kind
        # of thing a caller would discover by losing a third of a change to a green run.
        local_sha=$(git -C "$ROOT" rev-parse --verify --quiet "refs/heads/$head" || true)
        [ -n "$local_sha" ] || die "pr open: no local ref refs/heads/$head -- this client cannot tell which tree you meant"
        ls_out=$(git -C "$ROOT" ls-remote "$PUSH_REMOTE" "refs/heads/$head" 2>&1) || \
            die "pr open: cannot read $PUSH_REMOTE (its reason: $ls_out)"
        remote_sha=${ls_out%%	*}
        [ -n "$remote_sha" ] || die "pr open: '$head' is not on $PUSH_REMOTE. Push it first: git push -u $PUSH_REMOTE $head"
        [ "$local_sha" = "$remote_sha" ] || \
            die "pr open: REFUSING -- '$head' is $remote_sha on $PUSH_REMOTE but $local_sha locally, so the PR would be gated against a tree that is not what you have. Push first: git push $PUSH_REMOTE $head"

        case "$KIND" in
        hub)
            # hub-api.sh re-verifies the head its own way; that redundancy is deliberate.
            #
            # Its own `head <sha> (<branch>)` line is DROPPED, because the seam prints one below
            # for BOTH arms and `gh` prints none -- so passing it through makes the hub arm emit
            # the line twice and the GitHub arm once. Found by opening this change's own PR with
            # this verb. A caller that can COUNT LINES can tell the forges apart, which is the
            # property this file exists to remove; the same edit was already needed on `label
            # add`, where `issue tag` prints its own `#N labels:`.
            #
            # `sed` as a filter rather than `grep -v`: this box's interactive `grep` is a shell
            # function over ugrep that silently drops matching lines. A script does not
            # source that profile, but the habit costs less than the exception.
            out=$("$HUB_API" pr create "$REPO" "$head" "$base" "$title" "$body" ${DRAFT_ARG:+"$DRAFT_ARG"} ${SERIAL_ARG:+"$SERIAL_ARG"}) || \
                die "pr open: the client refused to open the PR (its reason is above)"
            printf '%s\n' "$out" | sed '/^head /d'
            ;;
        github)
            # A fork's PR to its parent names the head as `owner:branch`; a same-repo PR must
            # NOT, or GitHub 422s on a head it reads as a branch literally containing a colon.
            headspec="$head"
            [ "$BASE_REPO" = "$REPO" ] || headspec="${REPO%%/*}:$head"
            tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
            printf '%s' "$body" > "$tmp"
            # `--draft` maps to gh's own flag, so a burst is drafted on both forges. `--no-draft` is
            # gh's default and needs nothing -- GitHub has no queue deciding for the caller.
            # `--serial` IS REFUSED, not dropped: GitHub has no queue, so there is no batch to keep the PR
            # out of, and a caller who asked for it and got nothing would believe it held.
            [ -z "$SERIAL_ARG" ] || die "pr open: --serial marks a PR for the hub queue, and GitHub has no queue -- there is no batch to keep it out of"
            GH_DRAFT=""; [ "$DRAFT_ARG" = --draft ] && GH_DRAFT=--draft
            "$GH" pr create -R "$BASE_REPO" --head "$headspec" --base "$base" \
                  --title "$title" --body-file "$tmp" ${GH_DRAFT:+"$GH_DRAFT"}
            rm -f "$tmp"; trap - EXIT
            ;;
        esac
        # EVERY VERB THAT ACTS ON A COMMIT NAMES IT, so a verdict pasted into a ticket carries
        # its own expiry.
        printf 'head %s (%s -> %s on %s)\n' "$remote_sha" "$head" "$base" "$BASE_REPO"
        ;;

    gate)
        sha="${1:?full 40-char commit sha}"
        require_full_sha "$sha"
        shift

        # `--pr <n>`. Parsed here rather than positionally so the sha stays argument
        # one and every existing caller is unaffected.
        gate_pr=""
        while [ $# -gt 0 ]; do
            case "$1" in
            # `${2:?...}` would exit 2 here, colliding with REFUSED, and print a raw shell
            # error naming a line number rather than a fix. Checked by hand so a missing value
            # dies like every other usage mistake in this file.
            --pr) [ $# -ge 2 ] || die "pr gate: --pr needs a PR number"
                  gate_pr="$2"; shift 2 ;;
            --pr=*) gate_pr="${1#--pr=}"; shift ;;
            *) die "pr gate: unknown option '$1' (expected --pr <n>)" ;;
            esac
        done

        # THE STALE-HEAD REFUSAL, AND WHY IT IS BEFORE THE VERDICT IS PRINTED.
        #
        # Every verdict names its sha, so a verdict pasted into a ticket carries
        # its own expiry. What it cannot do is NOTICE it has expired: `gate` is handed a sha and no
        # PR, so it has nothing to compare against. The failure is not that the sha is wrong -- the
        # verdict is perfectly accurate about that commit. It is that a correct verdict about sha X
        # is READ as a verdict about the PR, and the two diverge silently the moment anything
        # rebases. Measured live: on 2026-08-24 a session posted "#89 green, head 9c1c89de57", the
        # drain updated the PR to bb79699651, and the comment stayed true of a commit nobody would
        # merge. On a busy drain this is once per rebase, i.e. roughly once per PR.
        #
        # REFUSAL, NOT A NOTE, and that is the whole design decision (the ticket recommends it and
        # the coordinator confirmed it from four occurrences in one night). A warning printed above
        # a `gate: GREEN` line is a warning that gets read past -- it already was, four times. The
        # exit code is this verb's contract, so a disagreement has to reach the exit code.
        #
        # And it happens BEFORE the gate is read, for the same reason the cancellation check sits
        # before printing: no GREEN line may ever exist for someone to paste. Printing the verdict
        # and then refusing would leave the paste-able text on the terminal, which is the entire
        # failure being fixed. It also costs one API call instead of two on the common path.
        if [ -n "$gate_pr" ]; then
            case "$gate_pr" in
            ''|*[!0-9]*) die "pr gate: --pr wants a PR number, got '$gate_pr'" ;;
            esac
            # `base_repo` rather than `$REPO`: a PR on a fork lives on the PARENT, which is the
            # same resolution `pr open` uses. `$BASE_REPO` is assigned only in that arm, so this
            # calls the helper instead of reading a variable another verb happens to set.
            gate_base=$(base_repo)
            pr_json=$(api_get "$gate_base/pulls/$gate_pr") || \
                die "pr gate: cannot read PR #$gate_pr on $gate_base (the forge's reason is above)"
            # `.head.sha` is the same path on both forges, so this needs no arm of its own.
            pr_head=$(printf '%s' "$pr_json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
h = (d.get("head") or {}).get("sha") or ""
print(h)
') || die "pr gate: the PR response for #$gate_pr was not readable JSON"
            [ -n "$pr_head" ] || \
                die "pr gate: #$gate_pr on $gate_base reports no head sha -- cannot confirm the measured sha is current, so this refuses rather than guess"
            if [ "$pr_head" != "$sha" ]; then
                stale "STALE HEAD -- #$gate_pr is now at $pr_head, but you asked about $sha. Any verdict about $sha would be accurate and would NOT be about this PR: the head moved (a rebase, an update, or a push), which detaches the old check-runs. Re-gate the current head: $0 pr gate $pr_head --pr $gate_pr"
            fi
        fi

        case "$KIND" in
        hub)    norm=$(gate_hub "$sha") ;;
        github) norm=$(gate_github "$sha") ;;
        esac
        verdict=$(printf '%s' "$norm" | python3 -c 'import json,sys; print(json.load(sys.stdin)["verdict"])')

        # A hub RED may be a cancellation the caller caused itself. Resolved BEFORE printing, so
        # the verdict line and the exit code never disagree.
        if [ "$KIND" = hub ] && [ "$verdict" = RED ]; then
            # 0 (settled) and 3 (a re-queue may come) are both CANCELLED here: neither is a test
            # failure, and the gate reports what it read rather than deciding whether to wait.
            hub_was_cancelled "$sha" && _wc=0 || _wc=$?
            if [ "$_wc" = 0 ] || [ "$_wc" = 3 ]; then
                verdict=CANCELLED
                norm=$(printf '%s' "$norm" | python3 -c 'import json,sys; d=json.load(sys.stdin); d["verdict"]="CANCELLED"; json.dump(d,sys.stdout)')
            fi
        fi

        printf '%s' "$norm" | python3 -c "$VERDICT_PY"
        printf 'measured %s\n' "$sha"
        # WHAT THE COMPARISON WAS AGAINST, when there was one. Without this a `--pr`
        # run is byte-identical to a bare one, so the stronger check leaves no trace: a reader
        # cannot tell a verdict whose head was confirmed from one where nobody looked.
        #
        # DELIBERATELY PHRASED IN THE PAST TENSE, and it does NOT say "current". Saying so would
        # reproduce this ticket's own bug one level up — a "confirmed" line is itself a
        # measurement that goes stale the moment the next rebase lands, and a reader who trusts
        # it is doing exactly what the `measured <sha>` line already failed to stop. This states
        # which claim was checked and against what, so the next reader can re-run it, and makes
        # no promise about now.
        [ -n "$gate_pr" ] && printf 'was #%s head at read time (re-check: %s pr gate <head> --pr %s)\n' \
            "$gate_pr" "$0" "$gate_pr"
        printf 'forge %s %s\n' "$KIND" "$REPO"

        case "$verdict" in
        GREEN)     exit 0 ;;
        RED)       exit 1 ;;
        PENDING)   exit 3 ;;
        CANCELLED) exit 4 ;;
        NONE)
            # `registered=0` CONFLATES TWO CONDITIONS and the wrong one is the actionable one:
            # a typo'd sha reads as the registration race, whose correct response is to WAIT
            # forever. Separate them -- the commit-object endpoint answers on both forges.
            if api_get "$REPO/git/commits/$sha" >/dev/null 2>&1; then
                refuse "$sha exists on $REPO but has NO checks registered -- not a pass. If a workflow should have fired, it did not; if it was pushed seconds ago, re-run this."
            else
                refuse "$sha is not a commit on $REPO -- check the sha. This is NOT the registration race, so waiting will never change it."
            fi ;;
        *) die "unhandled verdict '$verdict'" ;;
        esac
        ;;

    land)
        num="${1:?pr number}"; subject="${2:?merge subject}"
        case "$num" in *[!0-9]*|'') die "pr land: '$num' is not a PR number" ;; esac
        # THE CALLER NEVER WRITES `(#N)`. See difference 1 in the header: the number is derived
        # from argv on both forges, because nothing downstream can add it and, on a `main` that
        # refuses force-push, nothing upstream can repair it.
        case "$KIND" in
        hub)
            # `hub-api.sh pr merge` appends the number and refuses a trailing ref naming a
            # DIFFERENT PR. Passed through unchanged so that rule keeps living in one place.
            #
            # BUT IT PRINTS THE HTTP CODE AND EXITS 0 REGARDLESS, so a caller gating on its
            # status reads a REFUSAL as a merge -- 405 is `block_on_outdated_branch` firing
            # because something landed while our checks ran, and that is the single most likely
            # non-2xx here. `pr-queue.sh` parses the code for this reason; the seam must too, or
            # it hands every future caller the trap `pr-queue.sh` had to learn about.
            # Captured, not piped: `$?` after a pipeline is the PIPE's status.
            out=$("$HUB_API" pr merge "$REPO" "$num" "$subject" 2>&1); rc=$?
            printf '%s\n' "$out"
            [ "$rc" = 0 ] || die "pr land: the client failed before the merge (see above)"
            code=$(printf '%s' "$out" | sed -n 's/.*merged: http=\([0-9]*\).*/\1/p')
            case "$code" in
            2??) ;;
            '')  die "pr land: no HTTP code in the client's reply, so whether #$num merged is UNKNOWN -- read the PR before retrying" ;;
            *)   die "pr land: REFUSED with http=$code -- #$num did NOT merge. 405 is block_on_outdated_branch: something landed while the checks ran, so rebase and re-gate." ;;
            esac
            ;;
        github)
            case "$subject" in
            *"(#$num)") ;;                                  # already correct -- do not double it
            *"(#"*")")
                ref=${subject##*"(#"}; ref=${ref%")"}
                case "$ref" in
                ''|*[!0-9]*) subject="$subject (#$num)" ;;
                *) die "pr land: subject ends in (#$ref) but this is PR #$num -- fix the subject" ;;
                esac ;;
            *) subject="$subject (#$num)" ;;
            esac
            "$GH" pr merge -R "$REPO" "$num" --squash --subject "$subject"
            ;;
        esac
        # The landing STYLE is squash on both arms today and is stated in one place, so
        # replacing it with fast-forward-plus-amended-subject is a change here and nowhere else.
        printf 'landed #%s on %s %s as: %s\n' "$num" "$KIND" "$REPO" "$subject"
        ;;

    list)
        case "$KIND" in
        hub)
            "$HUB_API" "/api/v1/repos/$REPO/pulls?state=open&limit=50" | python3 -c '
import json, sys
for p in json.load(sys.stdin) or []:
    print("#%-5d %-10s %-28s -> %-10s %s"
          % (p["number"], p.get("state") or "?", (p.get("head") or {}).get("ref") or "?",
             (p.get("base") or {}).get("ref") or "?", (p.get("title") or "")[:60]))'
            ;;
        github)
            "$GH" api "repos/$REPO/pulls?state=open&per_page=50" | python3 -c '
import json, sys
for p in json.load(sys.stdin) or []:
    print("#%-5d %-10s %-28s -> %-10s %s"
          % (p["number"], p.get("state") or "?", (p.get("head") or {}).get("ref") or "?",
             (p.get("base") or {}).get("ref") or "?", (p.get("title") or "")[:60]))'
            ;;
        esac
        ;;

    *) die "unknown pr verb '$sub' (open|gate|land|list)" ;;
    esac
    ;;

label)
    act="${1:?usage: label add|rm <n> <label>}"; num="${2:?issue or pr number}"; lab="${3:?label}"
    case "$num" in *[!0-9]*|'') die "label: '$num' is not a number" ;; esac
    case "$KIND" in
    hub)
        case "$act" in
        add)
            # `issue tag` creates the label if the repo lacks it, which is what the queue's
            # `queue:needs-human-review` labels need on a fresh repo. PRs are issues on Forgejo, so this
            # verb reaches a PR number unchanged.
            # Output suppressed: `issue tag` prints its own `#N labels:` line, and the read-back
            # below prints the same line for both forges. Two of them would be one arm showing
            # through the seam -- and a caller could tell the forges apart by counting lines.
            "$HUB_API" issue tag "$REPO" "$num" "$lab" >/dev/null ;;
        rm)
            # Forgejo deletes a label by ID, not by name, and answers 204 for an ID the issue
            # never carried -- so resolve the name first and refuse an unknown one rather than
            # reporting a no-op as a removal.
            # Through the client's `issue label-id` verb, not a hand-rolled fetch.
            # This file WRAPS `hub-api.sh` and deliberately does not import its internals, so a
            # verb is the only seam the two share -- which is exactly why this site was written
            # later than two existing guards and still shipped without one. The verb refuses an
            # unknown name itself; the check below stays because a refusal must not become a
            # DELETE against an empty id.
            # `|| true` IS LOAD-BEARING UNDER `set -eu`, and its absence was measured rather than
            # reasoned about: without it, `label-id`'s exit 2 aborts this script before the check
            # below, so a refusal that has a careful message becomes a silent `exit 1`. The verb
            # refusing is the EXPECTED path here, not an error, so its status is consumed and the
            # verdict is issued by the check that owns the wording.
            id=$("$HUB_API" issue label-id "$REPO" "$lab" 2>/dev/null || true)
            [ -n "$id" ] || die "label rm: $REPO has no label named '$lab' -- nothing was changed"
            "$HUB_API" "/api/v1/repos/$REPO/issues/$num/labels/$id" -X DELETE -o /dev/null \
                -w 'label rm: http=%{http_code}\n' ;;
        *) die "unknown label verb '$act' (add|rm)" ;;
        esac ;;
    github)
        case "$act" in
        add) "$GH" api "repos/$REPO/issues/$num/labels" -X POST -f "labels[]=$lab" >/dev/null ;;
        rm)  "$GH" api "repos/$REPO/issues/$num/labels/$lab" -X DELETE >/dev/null ;;
        *)   die "unknown label verb '$act' (add|rm)" ;;
        esac ;;
    esac
    # Read back rather than trusting the write: Forgejo drops a label it will not accept and
    # still answers 2xx, which is the same shape as `claim` in hub-api.sh.
    printf '#%s labels: ' "$num"
    api_get "$REPO/issues/$num" | python3 -c \
        'import json,sys; print(",".join(l["name"] for l in (json.load(sys.stdin).get("labels") or [])) or "(none)")'
    ;;

review)
    act="${1:?usage: review request|read <n> ...}"; num="${2:?pr number}"
    case "$num" in *[!0-9]*|'') die "review: '$num' is not a PR number" ;; esac
    case "$act" in
    request)
        who="${3:?reviewer login}"
        case "$KIND" in
        hub)    "$HUB_API" "/api/v1/repos/$REPO/pulls/$num/requested_reviewers" -X POST \
                    -H 'Content-Type: application/json' -d "{\"reviewers\":[\"$who\"]}" -o /dev/null \
                    -w 'review requested: http=%{http_code}\n' ;;
        github) "$GH" api "repos/$REPO/pulls/$num/requested_reviewers" -X POST -f "reviewers[]=$who" >/dev/null
                printf 'review requested: %s on #%s\n' "$who" "$num" ;;
        esac ;;
    read)
        # Premise 19: a native review is the AUTHORITY; the label is only the request. So this
        # prints the reviews themselves and never infers approval from a label.
        api_get "$REPO/pulls/$num/reviews" | python3 -c '
import json, sys
rs = json.load(sys.stdin) or []
for r in rs:
    print("  %-16s %s" % ((r.get("user") or {}).get("login") or "?", r.get("state") or "?"))
print("reviews: %d approved=%d" % (len(rs), sum(1 for r in rs if r.get("state") == "APPROVED")))
'
        ;;
    *) die "unknown review verb '$act' (request|read)" ;;
    esac ;;

branch)
    act="${1:?usage: branch rm <branch>}"; br="${2:?branch name}"
    [ "$act" = rm ] || die "unknown branch verb '$act' (rm)"
    case "$KIND" in
    hub)    "$HUB_API" "/api/v1/repos/$REPO/branches/$br" -X DELETE -o /dev/null \
                -w 'branch rm: http=%{http_code}\n' ;;
    github) "$GH" api "repos/$REPO/git/refs/heads/$br" -X DELETE >/dev/null
            printf 'branch rm: deleted %s on %s\n' "$br" "$REPO" ;;
    esac ;;

*) die "unknown verb '$verb' (where|pr|label|review|branch)" ;;
esac
