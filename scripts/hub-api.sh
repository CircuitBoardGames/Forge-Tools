#!/bin/sh
# Authenticated Forgejo (hub) API access where the token never becomes visible.
#
# hub's Forgejo runs REQUIRE_SIGNIN_VIEW=true, so every anonymous API call returns
# {"message":"Only signed in user is allowed to call APIs."} with HTTP 200 -- a
# successful-looking response carrying no data. Anything reading this API needs a token,
# and a token that passes through a terminal is a token in a transcript.
#
# The containment rule: the token travels hub-stdout -> local file, and is read only by
# curl. It is never in argv (where `ps` sees it), never exported, never printed. The
# minting path emits ONE line -- a sha256 fingerprint -- so a caller can tell WHICH token
# is installed without learning its value.
#
#   hub-api.sh mint [scopes]     mint on hub, install here, revoke superseded, fingerprint
#   hub-api.sh fingerprint       sha256 of the installed token
#   hub-api.sh revoke            revoke this host's superseded tokens, keeping the current
#   hub-api.sh selfcheck         prove the token is confined (see below)
#   hub-api.sh <path> [curl...]  authenticated call, e.g. hub-api.sh /api/v1/user
#   hub-api.sh pr <verb> ...     PR verbs the cut-over gate needs (see the `pr)` case)
#   hub-api.sh issue <verb> ...  wayfinder issue verbs (see the `issue)` case)
#   hub-api.sh repo ci|protect|provision|create|fork ...  forge gate + protection (see the `repo)` case)
#
# THREE WAYS TO LOSE A CALL, each hit independently by two sessions in one shift.
# All three fail in the shape that costs most: a plausible answer to a DIFFERENT question.
#
#   THE OWNER IS THE REPO'S OWNER (below, `example-org`), NOT `claude`. `HUB_USER` below is the
#   account this box AUTHENTICATES as; it authors every commit and every issue, which makes it the
#   natural guess for the owner segment of a path, and it is wrong. `/api/v1/repos/claude/example-repo/pulls`
#   returns a well-formed 404 -- {"message":"The target couldn't be found."} -- which names no
#   owner and reads like a missing endpoint. `git remote -v` is the discriminator, and is the
#   authority for the whole path segment:  origin  git@forge.example.org:example-org/example-repo.git
#
#   A PASSTHROUGH PATH MUST BEGIN `/api/v1`. Given `repos/...`, curl concatenates it onto the
#   host and dies with `Could not resolve host: forge.example.orgrepos` -- a DNS-shaped error that sends
#   the reader to the network, the token and the tunnel before the argument.
#
#   THERE IS NO `pr list` VERB, and `git ls-remote --heads hub` is NOT the fallback: A REF IS
#   NOT A PR. A pushed branch may carry no PR, and a merged PR's branch is retired -- so the
#   refs count answers a neighbouring question and looks like the one asked. List them with:
#       hub-api.sh "/api/v1/repos/example-org/example-repo/pulls?state=open"
#
# REVOCATION DELIBERATELY DOES NOT USE THE WORKING TOKEN. It runs ON HUB over the same ssh path
# `mint` already needs, using the admin password that already lives there. That is the whole
# reason; it is NOT that the working token cannot. An earlier form of this paragraph said the
# token "stays minimal" without write:user, and that was false of every token installed since
# 2026-08-15: the installed token carries write:user, the scope that deletes and mints tokens.
# It was dropped on 2026-09-10 because nothing then called a /user write.
# WRITE:USER IS BACK (2026-09-15): `repo create <user>/<repo>` POSTs
# /user/repos, which the forge refuses without it. The forge stores write:user IN PLACE OF
# read:user (it implies it, and `token-scopes`/`revoke` still list /users/<u>/tokens fine), so the
# declaration below names four scopes, not five -- declaring read:user too never matches a correct
# token. Measured with that token: DELETE .../tokens/{id} answers 401 "auth method not allowed", so
# token deletion still needs the password path above; minting with the token was not measured.
# Each box's token is re-minted to match. Until it is, `token-scopes` there reads a mismatch and
# fails, in either direction. A comment cannot fail; that verb can.
#
# MINTING AND REVOKING NEED ROOT, SO THEY ARE NOT HERE: `mint` and `revoke` delegate to
# the Forge-Token-Admin repo's `forge-token-admin`, reached by command name. Its token namespace,
# ssh mode and password file are its own, configured in /etc/forge-token-admin/config.json. This
# client keeps what needs no root: the installed credential, `token-scopes`, the validity check.

set -eu

# SITE CONFIGURATION: scripts/ft-config.sh, a sibling found through a symlink.
# The forge URL has NO default -- FORGE_TOOLS_FORGE_URL, or the older HUB_URL, which still wins --
# and a call that needs it and finds neither refuses by name in `hub_curl` below. Tests set HUB_URL.
_ft_cfg="$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")/ft-config.sh"
[ -r "$_ft_cfg" ] || { printf '%s\n' "hub-api: cannot read $_ft_cfg -- Forge-Tools' config reader must sit beside this script" >&2; exit 1; }
. "$_ft_cfg"
#
# A box that reaches the forge through Cloudflare Access says so by holding the Access
# pair as a curl config -- two `header = "CF-Access-Client-…"` lines, mode 600 -- at
# $FORGE_TOOLS_CREDENTIALS_DIR/hub-access.conf. An explicit HUB_CURL_INSECURE / HUB_ACCESS_CONFIG
# still wins.
#
# A SECOND --config, never merged into hub-api.conf: valid_config() pins that file to exactly one
# token line, and the Access pair is a different credential with a different lifecycle (re-minted
# at a credential cutover). Not ~/.curlrc either -- that would send the Access
# secret to every host any curl on the box talks to.
if [ -z "${HUB_ACCESS_CONFIG+set}" ] && [ -f "$FORGE_TOOLS_CREDENTIALS_DIR/hub-access.conf" ]; then
    HUB_ACCESS_CONFIG="$FORGE_TOOLS_CREDENTIALS_DIR/hub-access.conf"
fi
HUB_ACCESS_CONFIG="${HUB_ACCESS_CONFIG:-}"
HUB_URL="${HUB_URL:-${FORGE_TOOLS_FORGE_URL:-}}"
HUB_CURL_INSECURE="${HUB_CURL_INSECURE:-0}"
# What the installed token is declared to carry. `token-scopes` compares the live listing to
# this; change it in the same commit as the re-mint, or the verb goes red and says why.
WORKING_TOKEN_SCOPES="write:issue,write:organization,write:repository,write:user"
hub_curl() {
    [ -n "$HUB_URL" ] || ft_need FORGE_URL "the forge's base URL, e.g. https://forge.example.org (HUB_URL overrides it)"
    if [ -n "$HUB_ACCESS_CONFIG" ]; then
        # Named but unusable is a refusal, not a silent fall-through to a request Access will
        # answer with its login page -- a 302 that reads like a forge outage.
        [ -f "$HUB_ACCESS_CONFIG" ] || die "HUB_ACCESS_CONFIG names $HUB_ACCESS_CONFIG, which does not exist"
        _ap=$(stat -c '%a' "$HUB_ACCESS_CONFIG")
        [ "$_ap" = "600" ] || die "$HUB_ACCESS_CONFIG is mode $_ap, refusing to use it (want 600)"
        set -- --config "$HUB_ACCESS_CONFIG" "$@"
    fi
    if [ "$HUB_CURL_INSECURE" = "1" ]; then
        curl -k "$@"
    else
        curl "$@"
    fi
}
# The forge identity this box acts as. `claude` since 2026-08-15: one account per actor,
# so a push, an issue and a commit can be told apart from a human's. A human admin account
# remains for break-glass, reachable with HUB_USER=<name> plus its own password file.
# THIS IS NOT THE REPO OWNER. Substituting it into an API path yields a 404 naming nothing;
# the owner comes from `git remote -v`. See the header.
HUB_USER="${HUB_USER:-claude}"
CFG="${HUB_API_CONFIG:-$FORGE_TOOLS_CREDENTIALS_DIR/hub-api.conf}"

# Never trace: `set -x` anywhere upstream would print the config line, token and all.
set +x

die() { printf '%s\n' "hub-api: $*" >&2; exit 1; }

# A REFUSAL TO MEASURE, WHICH IS NOT A MEASUREMENT THAT FAILED. `die` exits 1 like every
# other outcome of a verb, so a caller could not tell "this input will never work" from "the gate is
# not ready yet"; a poller then treats a permanent error as transient. Measured: a gate poller spun
# every 30s for ~7 minutes on a 10-char sha while the PR it watched went green in about two.
#
# 2 is already the convention here -- the unparseable-scope arm and the Python `die` both use it,
# and `forge.sh` has carried a `refuse` beside its `die` all along. This gives the shell half the
# same verb so the two seam scripts cannot drift apart on the one code a caller branches on.
refuse() { printf '%s\n' "hub-api: REFUSING: $*" >&2; exit 2; }

# workflow_listing <owner/repo> <ref> -- the workflow directory the forge RUNS at <ref>, and its files.
#
# THE FORGE RUNS THE FIRST OF .forgejo/.gitea/.github/workflows THAT EXISTS, workflows in it or not
# (Forgejo 16.0.4 `ListWorkflows`). Judging any other directory reads a gate that never runs,
# or misses the one that does: `pr checks`' parity arm and `pr create`'s CI check each listed two of the
# three and so never saw `.gitea`. ONE definition, for `repo provision` and both of those.
#
# Prints the directory on line 1, then one workflow path per line, and returns 0. Returns 4 when none
# of the three exists. Returns 3 when a listing cannot be read, printing that directory -- an unread
# listing is not an absent one. ABSENT IS ONLY THE FORGE'S OWN "does not exist" OBJECT (the body its
# 404 carries); an error saying anything else stays unread. `|| true` on the request because `api`
# exits 22 on that 404 under `set -eu`, and the body it captured is exactly what is judged.
workflow_listing() {
    for _wl_d in .forgejo/workflows .gitea/workflows .github/workflows; do
        _wl_raw=$(api "/api/v1/repos/$1/contents/$_wl_d?ref=$2" 2>/dev/null) || true
        _wl_out=$(printf '%s' "$_wl_raw" | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
except ValueError:
    sys.exit(3)
if isinstance(v, list):
    print(sys.argv[1])
    for e in v:
        if e.get("type", "file") == "file" and str(e.get("name", "")).endswith((".yml", ".yaml")):
            print(e.get("path") or sys.argv[1] + "/" + e["name"])
elif isinstance(v, dict) and "does not exist" in json.dumps(v):
    sys.exit(4)
else:
    sys.exit(3)' "$_wl_d") && _wl_rc=0 || _wl_rc=$?
        case $_wl_rc in
        0) printf '%s\n' "$_wl_out"; return 0 ;;
        4) continue ;;
        *) printf '%s\n' "$_wl_d"; return 3 ;;
        esac
    done
    return 4
}

# The one shape a valid config may have. Anything else -- an ssh error, a usage message,
# an empty mint -- must not be left on disk looking like a credential.
valid_config() {
    [ -f "$CFG" ] || return 1
    [ "$(wc -l < "$CFG")" -eq 1 ] || return 1
    grep -q '^header = "Authorization: token [A-Za-z0-9]\{32,\}"$' "$CFG"
}

require_config() {
    valid_config || die "no valid token at $CFG -- run: $0 mint"
    # A credential the group or world can read is not confined.
    perms=$(stat -c '%a' "$CFG")
    [ "$perms" = "600" ] || die "$CFG is mode $perms, refusing to use it (want 600)"
}

# Read the token for internal use only. Callers must never echo the result.
_token() { sed -n 's/^header = "Authorization: token \(.*\)"$/\1/p' "$CFG"; }

# Does this argv carry a request body with no content type named? curl defaults a `-d` body to
# `application/x-www-form-urlencoded`, so JSON arrives at the forge as a FORM and every field
# reads as absent -- and what comes back names the wrong cause. Measured on `/pulls/<n>/reviews`:
# `{"message":"review event  requires a body"}`, where the doubled space IS the field that never
# arrived. That message sends a reader to inspect a payload which was correct all along.
#
# Only the `*)` passthrough was exposed. Every internal verb sets the header by hand (:637, :790),
# so the client worked wherever its own subcommands exercised it and failed only on the path used
# for anything without a verb -- which is where new work happens.
#
# DEFAULT, not force: an explicit -H wins, so the passthrough stays the escape hatch for the
# endpoints that want form encoding or an upload. Refusing instead would be louder but would close
# a door that is rare and legitimate, and failing closed is safe only when the closed
# state is rare. `--data-urlencode` is deliberately NOT a trigger: it names form encoding.
_json_ct_needed() {
    _body=0 _ct=0 _prev=""
    for _a in "$@"; do
        case "$_a" in
        -d|--data|--data-raw|--data-binary|--data-ascii) _body=1 ;;
        -d?*|--data=*|--data-raw=*|--data-binary=*|--data-ascii=*) _body=1 ;;
        --json) _ct=1 ;;   # curl's own --json already sets Content-Type
        esac
        # `-H x`, `-Hx` and `--header=x` are one option to curl; a caller may write any of them.
        _h=""
        case "$_prev" in -H|--header) _h="$_a" ;; esac
        case "$_a" in -H?*) _h="${_a#-H}" ;; --header=*) _h="${_a#--header=}" ;; esac
        case "$_h" in [Cc][Oo][Nn][Tt][Ee][Nn][Tt]-[Tt][Yy][Pp][Ee]:*) _ct=1 ;; esac
        _prev="$_a"
    done
    [ "$_body" = 1 ] && [ "$_ct" = 0 ]
}

# One authenticated request. --fail-with-body so an HTTP error is a non-zero exit AND a
# readable reason; --config so the credential never enters argv.
api() {
    path="$1"; shift
    if _json_ct_needed "$@"; then
        set -- -H 'Content-Type: application/json' "$@"
    fi
    hub_curl -sS --fail-with-body --config "$CFG" "$HUB_URL$path" "$@"
}

# Build a JSON object from key=value pairs without shell quoting bugs. A PR title
# containing a quote, a backslash or a newline is ordinary, and hand-rolled JSON breaks on
# all three -- which fails as a confusing 400 rather than as anything that names the cause.
json_obj() { python3 -c '
import json, sys
print(json.dumps(dict(a.split("=", 1) for a in sys.argv[1:])))' "$@"; }

# resolve_pr_body <arg> -- THE ONE READING OF A PR BODY ARGUMENT, shared by `pr create` and
# `pr body` so the two verbs cannot disagree about what a body is (operator, 2026-09-23: "create a
# verb so that writing a PR body is consistent"). Sets `body`; refuses, never defaults. A variable
# and not stdout, because command substitution would strip the body's trailing newlines.
# `--title-only` and `--from-commits` are passed through as themselves: what they mean depends on
# the verb (only `pr create` can open without a body; `--from-commits` needs a PR number).
resolve_pr_body() {
    case "$1" in --title-only|--from-commits) body="$1"; return ;; esac
    # AN EMPTY ARGUMENT IS NOT A DECISION. It is what a missing file under "$(cat f)", an unset
    # $BODY or an empty @file: all expand to, and it used to fall through every arm below and
    # open a bodyless PR at exit 0 -- the silence the body rule exists to end, one spelling along.
    case "$1" in
        *[![:space:]]*) : ;;
        *) refuse "the body argument is EMPTY (or only whitespace). That is what a missing file, an
  unset variable or an empty @file: expands to, not a decision. Say which one you mean:
      <text> | @file:<path>   write it
      --from-commits          build it from the PR's own commit messages (the record you already wrote)
      --title-only            pr create only: the title IS the whole record" ;;
    esac
    case "$1" in
        *" "*|*"	"*|*"
"*) body="$1" ;;
        # A BARE FLAG IS NOT A BODY (measured: 8 of 50 PRs carried the literal body `-F`).
        # Whitespace is checked first, so prose starting with a hyphen still passes.
        -?*) refuse "'$1' is a flag where the BODY goes. The body is POSITIONAL; there is no -F /
  --body-file (that is the \`gh\` spelling, and it posts the flag as your body).
      @file:<path>     the file's contents become the body
      --from-commits   the PR's commit messages become the body
      --title-only     pr create only: the title IS the whole record" ;;
        # A LONE TOKEN NAMING A FILE THAT EXISTS IS NOT A BODY (one PR's body was the
        # 117-character path to the file it meant). Existence, not shape, is the discriminator.
        *) [ -f "$1" ] && refuse "'$1' names a readable file, where the BODY goes: a bare path is posted
  AS the body. Use @file:$1 for its contents. If the argument count is what went wrong, the order
  is: $0 pr create <owner/repo> <head> <base> <title> <body|--title-only|--from-commits>"
           body="$1" ;;
    esac
}

# WHICH TREE DID YOU MEAN? -- the one question this client could not previously express.
#
# Every verb that acts on a commit takes a FULL 40-char sha and refuses anything else, because
# the alternatives do not fail, they answer a question you did not ask. Measured on
# this repo 2026-08-24; each rejected shape gets its own sentence, since the fix differs:
#
#   slashless ref -- RESOLVES, to that ref's CURRENT tip. `pr checks <repo> main` printed
#                    `OK: 7 registered` byte-identically to passing `git rev-parse hub/main`,
#                    naming no commit anywhere in its output. Point it at a branch and the
#                    verdict re-aims itself as the branch moves. A stale green REPLACED by a
#                    fresh one is worse than a stale green: there is no event to notice.
#   slashed ref   -- `/commits/agent/x/status` is a different URL path, the body is not JSON,
#                    and `json.load` dies mid-pipe. Correct outcome, unreadable delivery.
#   short sha     -- resolves and answers correctly, and still cannot be compared against a PR
#                    head later, which is the entire reason a verdict names its commit.
#
# A branch literally named `deadbeef` is indistinguishable from a sha here. That ambiguity is
# inherent to the argument being one positional, and is not worth a flag to resolve.
# was_cancelled <owner/repo> <full-40-sha> -- was this red a CANCELLED run rather than a failed
# one? `pr checks` reads the commit-STATUS API, which renders a cancelled job as `failure`,
# so a red measured through it cannot be told from a test failure without this second question.
# Forgejo cancels the in-flight run whenever a PR head is force-pushed, so the red is routinely
# one the driver caused itself. Measured 2026-08-20 (pr-queue.sh, which has carried this check at
# its own `failure` arm since): force-pushing e1465a7b86 cancelled the run for the prior head
# 6f7abffa30 three seconds later, `pr checks` said `failure`, the run's real `status` was
# `cancelled`.
#
# THE SERVER-SIDE FILTER IS THE ONLY ROUTE. A client-side scan reads `commit_sha`, not `head_sha`,
# and `limit` is ignored -- so it looks exhaustive, matches nothing, and reads as "no runs for this
# commit": a false absence, silently. The full 40 characters are required; a short sha returns 0
# runs, which is indistinguishable from "no such commit".
#
# FOUR ANSWERS, forge.sh's, and `pr await` maps them:
#   0  cancelled, every run finished       -> not a verdict (keep polling, as before)
#   3  cancelled, a re-queue may be coming -> not a verdict (keep polling)
#   1  runs exist, none cancelled          -> the red IS a verdict
#   2  unreadable, zero runs, short sha    -> NOTHING WAS MEASURED. Fail closed: still a verdict.
# Only 0 and 3 may divert. An older copy of this check answered 0 for both, so the mapping is unchanged.
# ponytail: 0 keeps polling to exhaustion although nothing will settle (a spin the drain stops
# early on). Upgrade path: map 0 to its own early "NOT a verdict" exit.
was_cancelled() {
    # THE BODY IS forge.sh's hub_was_cancelled; this client is its $HUB_API.
    # FORGE_HUB_API, when the caller already set one, is honoured: it is the seam's own knob.
    _wc_self=$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")
    [ -r "$(dirname "$_wc_self")/forge.sh" ] || {
        printf 'hub-api: no %s/forge.sh -- cannot tell a cancellation (rc 2, a red stays a red)\n' "$(dirname "$_wc_self")" >&2
        return 2; }
    FORGE_HUB_API="${FORGE_HUB_API:-$_wc_self}" sh "$(dirname "$_wc_self")/forge.sh" was-cancelled "$1" "$2"
}


require_full_sha() {
    arg="$1"; verb="$2"
    case "$arg" in
    *[!0-9a-fA-F]*)
        case "$arg" in
        */*) refuse "$verb: '$arg' is a ref with a slash, not a commit sha. That path 404s into an unreadable traceback. Resolve it first: git rev-parse $arg" ;;
        *)   refuse "$verb: '$arg' is a ref, not a commit sha. A ref SILENTLY resolves to its current tip, so the verdict would re-aim itself as the ref moves. Resolve it first: git rev-parse $arg" ;;
        esac ;;
    esac
    case "${#arg}" in
    40) ;;
    *)  refuse "$verb: '$arg' is a ${#arg}-char short sha, not the full 40. It would resolve, but a verdict that cannot be compared to a PR head later is not worth having. Expand it: git rev-parse $arg" ;;
    esac
}

# The repo whose local branch `pr create` checks: the checkout you run it in. It
# used to be the repo this CLIENT lives in, which was the same tree only while the client lived in
# the repo it serves; installed from Forge-Tools it would look for your branch in Forge-Tools.
# Outside a git tree it falls back to the client's own checkout, the old answer, and a branch that
# is not there still REFUSES below. Worktrees share the ref store, so a sibling's branch is visible.
repo_root() {
    git rev-parse --show-toplevel 2>/dev/null \
        || { CDPATH= cd -- "$(dirname -- "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")/.." && pwd; }
}

# Forge-Token-Admin's command, by name, with a LOUD preflight: a missing tool is a missing repo, not
# a forge failure, and minting is the one verb a box cannot do without.
token_admin() {
    command -v forge-token-admin >/dev/null 2>&1 \
        || die "forge-token-admin is not on PATH -- install the Forge-Token-Admin repo, which provides mint and revoke"
    forge-token-admin "$@"
}

# ==========================================================================================
# IS THIS CLIENT ITSELF CURRENT?
#
# Every verb pins the tree it acts on. A STALE CLIENT ACTING CORRECTLY ON THE
# RIGHT TREE IS STILL WRONG, and that is not hypothetical. On 2026-08-24 five PRs were merged
# with a copy of this file that predated the body fix: `Do=squash` with only
# `MergeTitleField` squashes with an EMPTY BODY, so every merge silently deleted the PR's
# reasoning, its `Closes #N` and its `Co-Authored-By:` trailers. Nothing failed -- HTTP 200,
# branches pruned, tickets auto-closed. `main` refuses force push, so those commits are wrong
# for ever; the bodies exist only as git notes. The variable was isolated: a PR merged from a
# fresh worktree kept its 828-byte body.
#
# WHY NOT `pr-queue.sh`'S CHECK. That one compares a CHECKOUT's position against `hub/main`,
# which is the right idea on the wrong subject: under premise 10 every session works in a
# worktree, so a hardcoded path fires forever, and a checkout's position does not
# answer the question anyway -- what runs is a FILE. This compares the running script's own
# BLOB, so it is correct from any worktree, from an installed checkout, or from a copy.
#
# THREE STATES, AND ONLY ONE OF THEM REFUSES:
#
#   current   blob == the ref's blob for this path            -> proceed, silently
#   stale     blob IS a former version of this path on the ref -> REFUSE. This is the incident.
#   unknown   blob appears nowhere in that path's history      -> proceed, loudly
#
# `unknown` means local edits or a feature branch -- i.e. somebody is WORKING on this client.
# Refusing there would make the client unable to ship its own fix, which is a guard that has to
# be disabled to be useful, and a guard that gets disabled is the guard that was worked around
# on the night of the incident. It is reported rather than silent, because "I am running an
# unreleased client" is exactly what the reader needs to know when a write behaves oddly.
#
# CEILING, STATED: a checkout that is behind AND has local edits to this file reads as
# `unknown`, not `stale`, so it proceeds with a notice. Blob identity cannot distinguish those,
# and the alternative is refusing all development. What IS closed is the incident's own shape:
# an unmodified older release.
#
# READS ARE NOT GUARDED. A stale reader returns stale-shaped data and the caller sees it; a
# stale writer changes the forge permanently and nothing shows. The floor is what writes.
#
# `HUB_API_CURRENCY_REF` names the reference to compare against and SUPPRESSES THE FETCH -- it
# is a redirect, not a skip, so tests exercise this exact logic against a ref they built rather
# than against the network. There is deliberately NO "skip this check" variable.
# ==========================================================================================

# Does this invocation WRITE to the forge? Enumerated rather than inferred: a new verb should
# have to be classified by whoever adds it, and the failure of forgetting is a missing guard
# on a write, so the generic passthrough is covered by METHOD instead of by name.
hub_api_writes() {
    case "${1:-}" in
    revoke) return 0 ;;
    pr)     case "${2:-}" in create|body|merge|hold|unhold) return 0 ;; esac ;;
    repo)   case "${2:-}" in protect|provision|create|fork) return 0 ;; esac ;;
    issue)  case "${2:-}" in
            map-create|child-create|adopt|move|label|block|unblock|claim|unclaim|resolve|body-edit|file|tag|note|close)
                return 0 ;;
            esac ;;
    esac
    # The `*)` passthrough takes raw curl arguments, so the method is the only honest signal.
    prev=""
    for a in "$@"; do
        [ "$prev" = "-X" ] && case "$a" in POST|PATCH|PUT|DELETE) return 0 ;; esac
        case "$a" in --request=*) case "${a#--request=}" in POST|PATCH|PUT|DELETE) return 0 ;; esac ;; esac
        prev="$a"
    done
    return 1
}

# ==========================================================================================
# AN UNKNOWN LABEL IN A `labels=` FILTER RETURNS EVERY ISSUE, NOT NONE.
#
# The verbs are guarded -- `issue list` resolves the name and dies, `pr-queue.sh
# merge_requested` resolves the id and re-reads. THE PASSTHROUGH IS NOT, and it is the path the
# defect was measured through: `labels=wayfinder:map-99999` returned all 122 issues, because
# `issue list` could not express the query at all until it was widened.
#
# FORGEJO DOCUMENTS THIS. Its own spec says "Non existent labels are discarded" on both
# name-taking endpoints, so it is working as specified and will never be fixed upstream. This
# client is the permanent home of the check, not a workaround waiting for a release.
#
# SCOPE IS KEYED ON THE ENDPOINT, NOT ON THE SUBSTRING `labels=`, and that distinction is
# load-bearing. Measured 2026-08-25 by walking `/swagger.v1.json` -- 326 paths, 8 taking a
# `labels` QUERY parameter, with a control (`page` -> 105 params) proving the walk finds them:
#
#   /repos/{o}/{r}/issues                 label NAMES, repo-scoped     <- the only one we can check
#   /repos/issues/search                  label NAMES, CROSS-REPO      <- unresolvable against one repo
#   /repos/{o}/{r}/pulls                  label IDs, array of int64    <- different TYPE, same param name
#   5x .../actions/runners/jobs           RUN JOB labels (ubuntu-latest)  <- different NAMESPACE entirely
#
# A guard keyed on the substring would refuse every valid runner-jobs query and every valid
# `/pulls` call. The `/pulls` case is the sharpest: same parameter name, `array of int64` rather
# than names, and it is NOT the path `pr-queue.sh` uses (that one is `issues?type=pulls&labels=`,
# the issues endpoint filtered to pulls). None of that is deducible from the parameter's name.
#
# THREE OUTCOMES, and only a positive identification refuses -- the shape the currency guard
# below landed on after CI measured that refusing-when-you-cannot-ask turns 35 tests red:
#
#   issues listing + a label the repo does not have  -> REFUSE. The whole point.
#   issues listing + repo not extractable            -> REFUSE. A genuine cannot-determine, and
#                                                       RARE BY CONSTRUCTION: every well-formed
#                                                       call parses, so failing closed here costs
#                                                       nothing and is the arm nothing exercises
#                                                       by accident.
#   /repos/issues/search + labels=                   -> proceed LOUDLY. Cross-repo by design, so
#                                                       the names cannot be resolved against any
#                                                       one repo. Refusing a correctly-formed call
#                                                       because it is the wrong shape for this
#                                                       check is not failing closed, it is
#                                                       breaking a working path.
#   anything else                                    -> silent. Not this guard's subject.
#
# CEILING, STATED, AND IT IS WORSE HERE THAN FOR THE CURRENCY GUARD: a client-side check protects
# only callers running a client that has it. That guard is against a stale client, which by
# definition cannot contain its own guard; this guards ad-hoc callers typing a raw path, who are
# precisely the population most likely to be running whatever copy is in front of them. The
# honest claim is "a passthrough call made through a CURRENT client is checked", not "the
# passthrough is guarded".
#
# READS ARE OTHERWISE UNGUARDED HERE, deliberately, and this is the stated exception. The
# argument above is that a stale reader's output is visible to its caller. THIS read is not: an
# unfiltered 122 rows is indistinguishable from a legitimate broad match, and the caller has
# nothing to notice.
# ==========================================================================================

# Print `owner/repo` for a passthrough path that is the repo-scoped ISSUES LISTING, `search` for
# the cross-repo search endpoint, and nothing at all otherwise. Query string stripped first, so
# a `labels=` value containing a slash cannot be read as a path segment.
hub_api_label_scope() {
    _p=${1%%\?*}
    case "$_p" in
    /api/v1/repos/issues/search) printf 'search'; return 0 ;;
    /api/v1/repos/*/issues) ;;
    *) return 0 ;;                       # not the listing -- silent, print nothing
    esac
    _rest=${_p#/api/v1/repos/}
    _owner=${_rest%%/*}; _rest=${_rest#*/}
    _name=${_rest%%/*}; _tail=${_rest#*/}
    # `owner/repo/issues` exactly. Anything else is a deeper path that merely ends in /issues.
    # SILENT ON PURPOSE, and this is not a fail-open: a path this guard has no opinion about must
    # pass through untouched, or it refuses the five runner-jobs endpoints and /pulls, whose
    # `labels` are a different namespace and a different type. Printing nothing IS the answer.
    [ "$_tail" = "issues" ] || return 0   # silent: a path this guard has no opinion about
    [ -n "$_owner" ] && [ -n "$_name" ] || { printf 'unparseable'; return 0; }
    case "$_owner$_name" in *[!A-Za-z0-9._-]*) printf 'unparseable'; return 0 ;; esac
    printf '%s/%s' "$_owner" "$_name"
}

hub_api_require_known_labels() {
    case "${1:-}" in /*) ;; *) return 0 ;; esac       # passthrough invocations only
    case "$1" in *labels=*) ;; *) return 0 ;; esac

    _scope=$(hub_api_label_scope "$1")
    # SILENT ON PURPOSE. An empty scope is the classifier saying "not one of the endpoints whose
    # `labels` are issue-label NAMES" -- not "I could not tell". The cannot-tell case prints
    # `unparseable` and REFUSES below; this one is a positive identification of somebody else's
    # parameter, and staying out of the way is the correct behaviour rather than a fail-open.
    [ -n "$_scope" ] || return 0   # silent: somebody else's `labels`, not a cannot-tell

    if [ "$_scope" = search ]; then
        printf '%s\n' "hub-api: NOTE -- /repos/issues/search is CROSS-REPO, so a label name here cannot be
  resolved against any one repo and this client is not checking it. Forgejo DISCARDS names it does
  not know (its spec says so), so an unrecognised label widens this result rather than emptying it.
  Read the count with that in mind." >&2
        return 0
    fi
    if [ "$_scope" = unparseable ]; then
        # exit 2, matching the unknown-label arm below and the `REFUSING:` convention the issue
        # verbs already use. `die` would exit 1, and two refusals from one guard answering with
        # two different codes is a contract a caller cannot branch on.
        printf '%s\n' "hub-api: REFUSING: this is the issues listing and it carries labels=, but no owner/repo could be
  read out of '$1'. Forgejo DISCARDS a label it does not know and answers with the WHOLE repo, so a
  filter this client cannot verify is a filter that may not be applied at all. Every well-formed
  call parses; fix the path." >&2
        exit 2
    fi

    require_config
    _want=$(printf '%s' "$1" | python3 -c '
import sys, urllib.parse
q = urllib.parse.urlsplit(sys.stdin.read().strip()).query
out = []
for chunk in urllib.parse.parse_qs(q, keep_blank_values=True).get("labels", []):
    out += [x for x in chunk.split(",") if x]
print("\n".join(out))')
    [ -n "$_want" ] || return 0                      # `labels=` present but empty: no filter asked for

    _known=$(api "/api/v1/repos/$_scope/labels?limit=100" 2>/dev/null) || {
        printf '%s\n' "hub-api: NOTE -- could not read $_scope's labels, so the filter in this call is
  UNVERIFIED. Forgejo discards names it does not know and answers with the whole repo." >&2
        return 0
    }
    printf '%s' "$_known" | python3 -c '
import json, sys
known = {l.get("name") for l in (json.load(sys.stdin) or [])}
want = [w for w in sys.argv[1].split("\n") if w]
bad = [w for w in want if w not in known]
if bad:
    sys.exit("REFUSING: %s has no label %s -- Forgejo DISCARDS a name it does not know and answers\n"
             "  with the WHOLE repo, so this call would read as \"everything matched\" rather than\n"
             "  \"nothing matched\". Measured: labels=wayfinder:map-99999 returned all 122\n"
             "  issues. Known: %s"
             % (sys.argv[2], ", ".join(repr(b) for b in bad),
                ", ".join(sorted(n for n in known if n)) or "(none)"))
' "$_want" "$_scope" || exit 2
}

# CANNOT-TELL PROCEEDS, LOUDLY -- and this is a correction, made because CI measured it.
#
# The first version refused here, by analogy with `pr-queue.sh`, which refuses when it cannot
# establish currency. THE ANALOGY IS WRONG, and the difference is where each runs. `pr-queue.sh`
# runs interactively on this box, where "I cannot ask" really does mean something is broken.
# `hub-api.sh` runs EVERYWHERE -- including CI, where the checkout lives under
# `/var/lib/forgejo-runner/.cache/act/...` and has no `hub/main` ref at all. Measured:
# refusing there turned 35 existing tests red, each reporting `cannot tell whether this client is
# current`, in an environment that structurally cannot answer the question.
#
# So the ONLY refusal is `stale` -- a blob positively identified as a former version. Every other
# state proceeds with a notice naming which one it is. That keeps the guard's teeth exactly where
# the incident was (a real checkout, with a hub remote, behind) and stops it firing in every
# environment that has no forge remote. A guard that fails closed everywhere it cannot ask is a
# guard that gets deleted, which protects nobody.
hub_api_cannot_tell() {
    printf '%s\n' "hub-api: NOTE -- cannot tell whether this client is current ($1).
  Proceeding: only a POSITIVE match against a former version refuses. If a write behaves
  oddly, rule this out first." >&2
}

hub_api_require_current() {
    self=$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")
    dir=$(dirname "$self")

    git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
        hub_api_cannot_tell "$self is not inside a git checkout"; return 0; }

    # The repo ROOT, not the script's directory. A git pathspec resolves against the process
    # CWD, so `git -C scripts/ rev-list -- scripts/hub-api.sh` hunts scripts/scripts/... and
    # matches nothing -- which silently downgrades `stale` to `unknown` and proceeds. That is
    # this guard failing in the accepting direction; it was caught by injecting a real former
    # version, not by reading the code.
    root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) || root="$dir"

    ref="${HUB_API_CURRENCY_REF:-}"
    if [ -z "$ref" ]; then
        ref="$FORGE_TOOLS_REMOTE/main"
        # Quiet and best-effort. A failed fetch does not refuse by itself -- the ref check
        # below decides -- because an unreachable hub means the write is about to fail anyway,
        # and refusing with the wrong reason sends the reader after the wrong problem.
        # --no-write-fetch-head: this runs on EVERY call, in whatever checkout the caller is in, and
        # FETCH_HEAD there may be mid-read by someone else (pr-queue.sh's head read, 2026-09-15).
        git -C "$root" fetch --no-write-fetch-head "$FORGE_TOOLS_REMOTE" main -q 2>/dev/null || true
    fi
    git -C "$root" rev-parse --verify -q "$ref^{commit}" >/dev/null 2>&1 || {
        hub_api_cannot_tell "'$ref' does not resolve in $root"; return 0; }

    rel=$(git -C "$root" ls-files --full-name --error-unmatch -- "$self" 2>/dev/null) || {
        hub_api_cannot_tell "$self is not tracked in its checkout"; return 0; }

    mine=$(git -C "$root" hash-object -- "$self" 2>/dev/null) || {
        hub_api_cannot_tell "could not hash $self"; return 0; }
    theirs=$(git -C "$root" rev-parse --verify -q "$ref:$rel" 2>/dev/null || true)

    [ -n "$theirs" ] && [ "$mine" = "$theirs" ] && return 0     # current -- the normal path

    # Is this blob a FORMER version of this path on that ref? One `cat-file` for the whole
    # history rather than one process per commit.
    if git -C "$root" rev-list "$ref" -- "$rel" 2>/dev/null \
        | awk -v p=":$rel" '{print $0 p}' \
        | git -C "$root" cat-file --batch-check='%(objectname)' 2>/dev/null \
        | grep -qx "$mine"
    then
        die "REFUSING: this client is STALE -- $rel here is $mine, a FORMER version of that file
  on '$ref' (current: ${theirs:-<none>}). It would act on the forge with whatever was wrong with
  it then, and succeed: five PRs were once merged with a client that predated the body fix, each
  returning HTTP 200 while silently discarding the PR body, and \`main\` refuses force push so
  they are wrong for ever. Update this checkout, then re-run."
    fi

    # Unknown: local edits or a feature branch. Loud, not silent, and not fatal.
    printf '%s\n' "hub-api: NOTE -- running an UNRELEASED $rel ($mine); '$ref' has ${theirs:-<none>}.
  Not a former version, so this is local work rather than the staleness shape. Proceeding.
  If a write behaves oddly, this is the first thing to rule out." >&2
    return 0
}

# ONE PLACE WHERE A PASSTHROUGH INVOCATION IS CLASSIFIED, asking two independent questions of
# it: is this client current (keyed on HTTP METHOD, writes only), and is this filter
# real (keyed on the ENDPOINT, reads). Kept adjacent deliberately -- two inspectors on
# the same function that each knew half of it is how they drift apart.
# ==========================================================================================
# `@file:<path>` IN ANY ARGUMENT IS REPLACED BY THAT FILE'S CONTENTS.
#
# WHY THIS EXISTS, and it is a guard-intersection rather than a convenience. Long prose reaches
# this client as an argv string: PR bodies, `issue note`, `issue resolve`. Two guards each fail
# closed on a different unparseable shape, and between them they closed every way to pass one:
#
#   * a harness hook that refuses a command line it cannot tokenise when it mentions the tmux
#     injector -- which a PR body explaining why a mechanism AVOIDS that injector necessarily
#     does. It now names `@file:` as the way out; it used to name `$(cat file)`.
#   * a worktree-isolation check was said to refuse command substitution as too complex to
#     verify, making `$(cat file)` itself denied on a worktree session.
#
# THE SECOND HALF IS STALE AND THIS PARAGRAPH ASSERTED IT FOR MONTHS. Measured 2026-09-09: no
# such check existed. The harness settings carried zero deny and zero ask rules, no hook
# inspected a command for substitution, and `$(...)` ran normally in a worktree session -- dozens
# of times in the session that measured it. Whatever was built either never landed on this path
# or was removed; either way a reader who believes this line will avoid an idiom nothing is
# stopping them from using.
#
# SO WHY `@file:` STILL. The reasons that survive the measurement are the ones below, and they
# are about THIS CLIENT rather than about a guard: a missing file REFUSES instead of posting the
# literal token as a body, and command substitution strips the trailing newline. Those hold
# whatever the hooks do. The lesson worth keeping is the one the stale half teaches: a mechanism
# justified by another mechanism's behaviour rots silently when that other one changes, because
# nothing re-checks a comment. Justify it by what THIS file does.
#
# `@file:` rather than a bare `@path`, because a body legitimately starting with `@` is ordinary
# and a prefix that collides with content is a defect waiting for the first person to write one.
# A missing file REFUSES rather than passing the token through as text: a body silently sent as
# the literal string `@file:/tmp/x.md` is the failure this is meant to prevent, one level down.
#
# Trailing newlines: command substitution strips them, so a body ending in a blank line loses it.
# Immaterial for markdown, stated because this tooling has already been bitten by that exact
# stripping in `handoff.sh`'s read-back, where it mattered and was mis-attributed to the forge.
_n=$#
_i=0
while [ "$_i" -lt "$_n" ]; do
    _a="$1"; shift
    case "$_a" in
        @file:*)
            _p="${_a#@file:}"
            [ -f "$_p" ] || {
                echo "hub-api: no such body file: $_p" >&2
                echo "  \`@file:<path>\` is replaced by that file's contents. Refusing" >&2
                echo "  rather than sending the literal token as your body." >&2
                exit 2
            }
            _a=$(cat "$_p")
            ;;
        # A BARE `@<path>` THAT NAMES A READABLE FILE IS THE CURL SPELLING, AND IT WAS SILENTLY SENT
        # AS TEXT. The block above says it refuses a missing file "rather than sending the literal
        # token as your body" -- but only for tokens it RECOGNISES, and `@x` is not one, so it never
        # reached that refusal. The prefix chosen to avoid colliding with content left the adjacent
        # spelling falling through as content.
        #
        # MEASURED 2026-09-03 on a real PR: `pr create ... "@/tmp/.../body.txt"` opened with a
        # 105-character body that was the PATH. Worse than an empty body, which at least LOOKS
        # empty -- a reader sees a filled-in field and only discovers it resolves to nothing by
        # trying to follow it, and on someone else's box it never resolves at all. `-d @file` is
        # used elsewhere in this same script, which is where the habit comes from.
        #
        # REFUSE, DO NOT EXPAND. `@file:` was chosen over a bare `@` deliberately: "a body
        # legitimately starting with `@` is ordinary and a prefix that collides with content is a
        # defect waiting for the first person to write one." Expanding here would install exactly
        # that defect. The discriminator is that the path RESOLVES: `@claude please review` is
        # ordinary prose and names no file, while `@/tmp/x.md` naming a readable file is a typo for
        # `@file:` essentially every time. A body that really is the text `@<an existing path>` can
        # be passed through `@file:`.
        # `@file:*` is matched above, so it never reaches here -- this sees only the bare spelling.
        #
        # `if` RATHER THAN `[ -f ] && { ... }` FOR LEGIBILITY, AND THE REASON I FIRST WROTE DOWN WAS
        # WRONG. The comment here claimed a trailing false `&&` would abort under `set -eu` and take
        # every ordinary `@mention` with it. Measured before shipping, on this box's busybox ash:
        # both forms print REACHED THE END and exit 0. POSIX `set -e` exempts every command in an
        # `&&` list but the last, so the hazard does not exist and the claim would have been folklore
        # inherited by whoever touched this next. Kept as `if` because it reads better; NOT because
        # the other form is unsafe.
        @?*)
            if [ -f "${_a#@}" ]; then
                echo "hub-api: REFUSING: '$_a' names a readable file, so this is almost" >&2
                echo "  certainly the curl \`-d @file\` spelling. This client uses:" >&2
                echo "" >&2
                echo "      @file:${_a#@}" >&2
                echo "" >&2
                echo "  Sent as-is it would post the PATH as your body -- text that looks like" >&2
                echo "  a body and resolves to nothing on any other box. Measured on a real PR." >&2
                echo "  If you really mean that literal text, put it in a file and pass" >&2
                echo "  that file with \`@file:\`." >&2
                exit 2
            fi
            ;;
    esac
    set -- "$@" "$_a"
    _i=$((_i + 1))
done

if hub_api_writes "$@"; then
    hub_api_require_current
fi
hub_api_require_known_labels "$@"

case "${1:-}" in
mint)
    # Root work, delegated. The client says where its credential lives; the tool mints,
    # validates the shape, installs it at mode 600, and only then revokes the superseded tokens.
    scopes="${2:-read:repository,read:issue,read:user}"
    token_admin mint --install "$CFG" --scopes "$scopes"
    ;;

revoke)
    require_config
    # Identify the LIVE token's name here, with the token -- that needs no root -- and hand the
    # root-only sweep to Forge-Token-Admin. Match on the installed token's last eight characters,
    # which the listing exposes precisely so a caller can identify a token without holding it.
    last8=$(_token | tr -d '\n' | tail -c 8)
    keep=$(hub_curl -sS --fail-with-body --config "$CFG" \
               "$HUB_URL/api/v1/users/$HUB_USER/tokens" | python3 -c "
import json, sys
last8 = sys.argv[1]
for t in json.load(sys.stdin):
    if t.get('token_last_eight') == last8:
        print(t['name']); break
" "$last8")
    [ -n "$keep" ] || die "could not identify the installed token on hub; refusing to revoke blindly"
    token_admin revoke --keep "$keep"
    ;;

token-scopes)
    # The header's posture, made fail-able: the live token's scopes must EQUAL the
    # declared set, so a re-mint that widens or narrows them is a red verb rather than a stale
    # comment. Identifies the installed token the way `revoke` does, by its last eight characters.
    require_config
    last8=$(_token | tr -d '\n' | tail -c 8)
    hub_curl -sS --fail-with-body --config "$CFG" "$HUB_URL/api/v1/users/$HUB_USER/tokens" | python3 -c '
import json, sys
last8, declared = sys.argv[1], sorted(sys.argv[2].split(","))
d = json.load(sys.stdin)
if not isinstance(d, list):
    sys.exit("token listing was not a list (auth failed?): %.200s" % json.dumps(d))
live = next((t for t in d if t.get("token_last_eight") == last8), None)
if live is None:
    sys.exit("could not identify the installed token on hub by its last eight characters")
got = sorted(live.get("scopes") or [])
print("token    : %s" % live.get("name"))
print("declared : %s" % ",".join(declared))
print("live     : %s" % ",".join(got))
if got != declared:
    sys.exit("MISMATCH: scripts/hub-api.sh declares WORKING_TOKEN_SCOPES=%s but the installed token "
             "carries %s -- re-mint to the declaration, or change the declaration to what is true, "
             "in one commit" % (",".join(declared), ",".join(got)))
print("OK: the installed token carries exactly the declared scopes")
' "$last8" "$WORKING_TOKEN_SCOPES"
    ;;

git)
    # Run git against hub with the credential helper wired in, so the token never appears
    # in a remote URL (where it would persist in .git/config, the reflog and every `ps`).
    #   hub-api.sh git push hub main
    require_config
    shift
    HUB_API_CRED_INTENT=1 exec git -c credential.helper="!'$0' git-credential" "$@"
    ;;

git-credential)
    # git's credential-helper protocol. This subcommand necessarily WRITES THE TOKEN TO
    # STDOUT -- that is the protocol -- so it must be unusable by accident.
    #
    # An earlier version guarded with `[ -t 1 ]`, reasoning that git pipes and a human does
    # not. THAT GUARD LEAKED THE TOKEN THE FIRST TIME IT RAN: under any agent harness, CI
    # runner or `$(...)` capture, stdout is ALREADY not a tty, so the condition is false
    # exactly when a human is driving. A guard whose predicate is false in the environment
    # it is meant to protect is not a weak guard, it is an absent one.
    #
    # The intent variable is set only by the `git` subcommand above, so a hand-typed
    # invocation refuses in every environment rather than in some of them.
    [ "${HUB_API_CRED_INTENT:-}" = "1" ] || \
        die "git-credential emits a credential on stdout -- invoke it via '$0 git <args>', never directly"
    require_config
    case "${2:-get}" in
    get) printf 'username=%s\npassword=%s\n' "$HUB_USER" "$(_token)" ;;
    *) : ;;  # store/erase are no-ops: mint and revoke own this credential's lifecycle
    esac
    ;;

repo)
    # hub-api.sh repo protect <owner/repo> <branch> <status-context>...
    #
    # THE FORGE REFUSES WHAT A SESSION MIGHT NOT. Measured 2026-09-08 on a target repo
    # with NO branch protection: `pr merge` landed a PR whose fallow check was red, and two PRs sat
    # open with nothing making the second rebase when the first landed. A gated repo's `main` has
    # required contexts and `block_on_outdated_branch`; this verb puts the same shape on a target's
    # branch -- create if absent, update if present -- and READS IT BACK, refusing to report a
    # protection the forge does not actually hold. Contexts are globs the way Forgejo matches
    # them (`Test /*` covers every job the Test workflow registers). The push whitelist is left
    # alone: this is about what may MERGE, not who may push.
    #
    # hub-api.sh repo provision <owner/repo> (--kind python|rust|node | --no-ci "<reason>")
    # hub-api.sh repo create    <owner/repo> (--kind ... | --no-ci "...") [--private]
    # hub-api.sh repo fork      <clone-url> <owner/repo> (--kind ... | --no-ci "...") [--private]
    #
    # EVERY REPO ON THE FORGE COMES UP GATED. Measured 2026-09-15: the one scripted creation
    # path added no workflow and no protection, every other repo was a hand-typed API call, and the
    # fork recipe turned Actions OFF. `provision` makes an existing repo match a fully gated `main`,
    # idempotently, and REFUSES TO FINISH until it has read back all four:
    #   1. a `.forgejo/workflows` gate on the DEFAULT branch -- committed from scripts/templates when
    #      the directory holds no workflow, never overwritten when it does. A present `.forgejo`
    #      directory is read INSTEAD of `.github/workflows`, which is what makes step 2 safe on a fork.
    #   2. Actions enabled.
    #   3. the protection shape, push whitelist included. THE FORGE SILENTLY DROPS a whitelisted user
    #      who is not a collaborator (HTTP 200, name absent), so both are added as collaborators first.
    #   4. required contexts that are the gate's own (`<workflow name:> /*`): a context no workflow
    #      registers blocks every merge for ever.
    # `--no-ci "<reason>"` protects without a gate and prints the reason. Pull mirrors are refused:
    # read-only, and their Actions stay off. `create` and `fork` (a non-mirror migrate) then provision.
    require_config
    sub="${2:-}"
    # Who may push to a provisioned default branch (space-separated forge user names). No neutral
    # default exists, so provision/create/fork refuse without it, before writing anything.
    REPO_WHITELIST="${FORGE_TOOLS_PUSH_WHITELIST:-}"
    HUB_API_DIR=$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")

    # repo_protect <repo> <branch> <full:0|1> <context>...  -- POST or PATCH, then READ BACK every
    # field sent. A 2xx on the write is a request accepted, not a protection held -- the labels verb
    # learned the same lesson (`did NOT stick`). full=1 is a fully gated main's whole shape.
    repo_protect() {
        _r="$1"; _b="$2"; _full="$3"; shift 3
        _existing=$(api "/api/v1/repos/$_r/branch_protections" 2>/dev/null) || die "repo protect: could not list branch protections on $_r"
        _pbody=$(BRANCH="$_b" FULL="$_full" WL="$REPO_WHITELIST" python3 -c '
import json, os, sys
b = {"rule_name": os.environ["BRANCH"], "branch_name": os.environ["BRANCH"],
     "enable_status_check": bool(sys.argv[1:]), "status_check_contexts": sys.argv[1:],
     "block_on_outdated_branch": True}
if os.environ["FULL"] == "1":
    b.update(enable_push=True, enable_push_whitelist=True, push_whitelist_usernames=os.environ["WL"].split(),
             push_whitelist_deploy_keys=False, enable_merge_whitelist=False, required_approvals=0,
             block_on_rejected_reviews=False, block_on_official_review_requests=False,
             dismiss_stale_approvals=False, ignore_stale_approvals=False, require_signed_commits=False,
             protected_file_patterns="", unprotected_file_patterns="", apply_to_admins=False)
print(json.dumps(b))' "$@")
        _have=$(printf '%s' "$_existing" | BRANCH="$_b" python3 -c '
import json, os, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    sys.exit(2)
print(any((r.get("branch_name") or r.get("rule_name")) == os.environ["BRANCH"] for r in rows) and "yes" or "no")') || die "repo protect: the protection listing on $_r was not JSON"
        # THE RULE NAME IS A PATH SEGMENT, SO IT IS ENCODED. A branch with a slash --
        # `fix/quiet-...`, the pytest-test-categories default -- made `/branch_protections/fix/quiet...`
        # a different URL: the rule was created, and the read-back 404ed into a JSON traceback.
        _bq=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$_b")
        if [ "$_have" = yes ]; then
            api "/api/v1/repos/$_r/branch_protections/$_bq" -X PATCH -d "$_pbody" >/dev/null || die "repo protect: PATCH on $_r/$_b failed"
            _done=updated
        else
            api "/api/v1/repos/$_r/branch_protections" -X POST -d "$_pbody" >/dev/null || die "repo protect: POST on $_r failed"
            _done=created
        fi
        api "/api/v1/repos/$_r/branch_protections/$_bq" | BRANCH="$_b" VERB="$_done" BODY="$_pbody" python3 -c '
import json, os, sys
sent = json.loads(os.environ["BODY"])
r = json.load(sys.stdin)
bad = []
for k, v in sent.items():
    if k in ("rule_name", "branch_name"):
        continue
    g = r.get(k)
    if not (sorted(g or []) == sorted(v) if isinstance(v, list) else g == v):
        bad.append("%s sent %r, read %r" % (k, v, g))
print("%s protection on %s: required contexts %s, block_on_outdated_branch=%s, enable_status_check=%s" % (
    os.environ["VERB"], os.environ["BRANCH"], r.get("status_check_contexts") or [],
    r.get("block_on_outdated_branch"), r.get("enable_status_check")))
if bad:
    print("repo protect: the read-back does NOT match what was sent (%s) -- treat the branch as UNPROTECTED" % "; ".join(bad), file=sys.stderr)
    sys.exit(1)
if "push_whitelist_usernames" in sent:
    print("push whitelist held: %s" % ", ".join(r.get("push_whitelist_usernames")))' || exit 1
    }

    # provision_flags <args...>  -> KIND / NO_CI_REASON / PRIVATE
    provision_flags() {
        KIND=""; NO_CI_REASON=""; PRIVATE=false; EXISTING=""
        while [ $# -gt 0 ]; do
            case "$1" in
            --kind)    [ $# -ge 2 ] || die "repo $sub: --kind needs python|rust|node"; KIND="$2"; shift 2 ;;
            --existing) case "${2:-}" in keep|shadow) EXISTING="$2"; shift 2 ;;
                        *) die "repo $sub: --existing needs keep|shadow (whose .gitea/.github gate is it: this repo's, or upstream's)" ;; esac ;;
            --no-ci)   [ $# -ge 2 ] && [ -n "$2" ] || die "repo $sub: --no-ci needs a reason, which is printed and is the whole record of why this repo has no gate"
                       NO_CI_REASON="$2"; shift 2 ;;
            --private) PRIVATE=true; shift ;;
            *) die "repo $sub: unknown argument '$1'" ;;
            esac
        done
        [ -n "$KIND" ] && [ -n "$NO_CI_REASON" ] && die "repo $sub: --kind and --no-ci are exclusive"
        case "$KIND" in
        python|rust) TEMPLATE="$HUB_API_DIR/templates/forgejo/$KIND.yml" ;;
        node)        TEMPLATE="$HUB_API_DIR/templates/test.yml" ;;   # the gate bootstrap-target-repo.sh places
        "") [ -n "$NO_CI_REASON" ] || die "repo $sub: name --kind python|rust|node, or --no-ci \"<reason>\"" ;;
        *) die "repo $sub: unknown kind '$KIND' (python|rust|node)" ;;
        esac
        ft_need PUSH_WHITELIST "the forge users allowed to push to a provisioned default branch (space-separated)"
        return 0
    }

    repo_provision() {
        _r="$1"
        # THE READ IS CAPTURED ON ITS OWN. Piped into python, `|| die` read python's exit,
        # python parsed the forge's 404 object, and a missing repo was refused as one that "reports no
        # default branch".
        _raw=$(api "/api/v1/repos/$_r" 2>/dev/null) || die "repo provision: could not read $_r"
        _info=$(printf '%s' "$_raw" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("%s %s %s %s" % (d.get("mirror") and "mirror" or "-", d.get("empty") and "empty" or "-",
                       d.get("has_actions"), d.get("default_branch") or ""))') || die "repo provision: could not read $_r"
        set -- $_info
        [ "$1" = mirror ] && refuse "repo provision: $_r is a pull mirror -- mirrors are out of scope (read-only; their Actions stay off)"
        [ "$2" = empty ] && refuse "repo provision: $_r is empty -- there is no default branch to gate or protect. Push an initial commit, or create repos with 'repo create' (auto_init)."
        _actions="$3"; _dflt="${4:-}"
        [ -n "$_dflt" ] || die "repo provision: $_r reports no default branch"
        set --
        if [ -z "$NO_CI_REASON" ]; then
            # THE FORGE RUNS THE FIRST OF THESE DIRECTORIES THAT EXISTS, workflows in it or not
            # (Forgejo 16.0.4 `ListWorkflows`), so the gate is judged there and nowhere else. Listing
            # `.forgejo` alone committed a template over a `.github`-gated repo, which shadows and
            # switches off its CI -- live on a real repo, deploy included. Only
            # the forge's own "object does not exist" moves on; any other answer is unread.
            _wfdir=""; _files=""
            _wl=$(workflow_listing "$_r" "$_dflt") && _wlrc=0 || _wlrc=$?
            case $_wlrc in
            0) _wfdir=$(printf '%s\n' "$_wl" | sed -n 1p); _files=$(printf '%s\n' "$_wl" | sed -n '2,$p') ;;
            4) : ;;
            *) die "repo provision: cannot tell whether $_r has a $_wl gate on $_dflt -- the listing could not be read" ;;
            esac
            # A `.gitea` or `.github` GATE IS EITHER OURS OR UPSTREAM'S, and nothing on the forge says
            # which: one measured repo's is its real CI and deploy, a fresh fork's is upstream's
            # jobs, which once starved the runner. So the caller says, and silence refuses.
            if [ -n "$_files" ] && [ "$_wfdir" != .forgejo/workflows ]; then
                case "$EXISTING" in
                keep)   : ;;
                shadow) _files="" ; _wfdir="" ;;
                *) refuse "repo provision: $_r is gated by $_wfdir ($(printf '%s' "$_files" | tr '\n' ' ')). Say whose it is: --existing keep (this repo's own CI: required contexts come from it, nothing is committed) or --existing shadow (upstream's: commit the $KIND template to .forgejo/workflows, which the forge then reads INSTEAD)" ;;
                esac
            fi
            if [ -z "$_files" ]; then
                # Nothing we keep runs today. A `.forgejo` directory that exists takes the template;
                # so does a fresh one, which shadows whatever was read before.
                _files=".forgejo/workflows/$(basename "$TEMPLATE")"
                _pbody=$(python3 -c '
import base64, json, sys
print(json.dumps({"content": base64.b64encode(open(sys.argv[1], "rb").read()).decode(),
                  "branch": sys.argv[2], "message": sys.argv[3]}))' "$TEMPLATE" "$_dflt" "ci: add the forge gate from scripts/templates (repo provision --kind $KIND)")
                api "/api/v1/repos/$_r/contents/$_files" -X POST -d "$_pbody" >/dev/null || die "repo provision: committing $_files to $_r $_dflt failed"
                _gate="committed $_files from $(basename "$(dirname "$TEMPLATE")")/$(basename "$TEMPLATE")"
            else
                _gate="present, left untouched: $(printf '%s' "$_files" | tr '\n' ' ')"
            fi
            # THE CONTEXTS ARE READ BACK FROM THE BRANCH, not assumed from the template: a gate that
            # is already there names itself.
            _contexts=$(for _f in $_files; do api "/api/v1/repos/$_r/contents/$_f?ref=$_dflt" 2>/dev/null | tr -d '\n' || echo '{}'; echo; done | python3 -c '
import base64, json, re, sys
names = set()
for raw in sys.stdin.read().splitlines():
    if not raw.strip():
        continue
    try:
        text = base64.b64decode(json.loads(raw)["content"]).decode("utf-8", "replace")
    except (ValueError, KeyError, TypeError):
        sys.exit("a gate file could not be read back from the branch")
    # ONLY A WORKFLOW THAT RUNS ON A PR CAN BE A REQUIRED CONTEXT: a push-only one (a
    # deploy) never reports on the PR, so requiring it blocks every merge for ever.
    # ponytail: a text match, not a YAML parse -- `pull_request` in a comment or an `if:` counts as a
    # trigger. That fails LOUD (a context that never reports blocks the merge), so it is left until one does.
    if not re.search(r"\bpull_request", text):
        continue
    m = re.search(r"^name:\s*[\"\x27]?(.+?)[\"\x27]?\s*$", text, re.M)
    if not m:
        sys.exit("a gate workflow has no top-level name:, so its contexts cannot be derived")
    names.add(m.group(1) + " /*")
if not names:
    sys.exit("no gate workflow runs on pull_request, so no required context exists")
print("\n".join(sorted(names)))') || die "repo provision: could not derive required contexts from $_r's gate"
            while IFS= read -r _c; do [ -n "$_c" ] && set -- "$@" "$_c"; done <<EOF
$_contexts
EOF
            api "/api/v1/repos/$_r" -X PATCH -d '{"has_actions": true}' >/dev/null || die "repo provision: enabling Actions on $_r failed"
            _actions=$(api "/api/v1/repos/$_r" 2>/dev/null | python3 -c 'import json, sys; print(json.load(sys.stdin).get("has_actions"))') || _actions=unreadable
            [ "$_actions" = True ] || { printf '%s\n' "hub-api: repo provision: Actions read back as has_actions=$_actions on $_r -- the gate will not run; treat as UNGATED" >&2; exit 1; }
        fi
        for _u in $REPO_WHITELIST; do
            [ "$_u" = "${_r%%/*}" ] && continue
            api "/api/v1/repos/$_r/collaborators/$_u" >/dev/null 2>&1 && continue
            api "/api/v1/repos/$_r/collaborators/$_u" -X PUT -d '{"permission": "admin"}' >/dev/null || die "repo provision: adding $_u as a collaborator on $_r failed"
            echo "collaborator added: $_u (admin) -- a whitelisted non-collaborator is silently dropped"
        done
        repo_protect "$_r" "$_dflt" 1 "$@"
        echo "provisioned $_r (default branch $_dflt)"
        if [ -n "$NO_CI_REASON" ]; then
            echo "  gate: NONE by design -- $NO_CI_REASON"
            echo "  actions: has_actions=$_actions (not changed)"
        else
            echo "  gate: $_gate"
            echo "  required contexts: $*"
            echo "  actions: enabled (read back)"
        fi
    }

    case "$sub" in
    ci)
        # hub-api.sh repo ci <owner/repo> <ref> -- does the forge run CI at <ref>?
        # The ONE question `pr create` asks before opening a PR, as a read verb, so `pr-queue.sh
        # approve` can ask it too instead of waiting 30 minutes for checks no workflow will register.
        # `yes <dir>` exit 0; `none` exit 4 (no workflow directory, or one with no workflow in it);
        # `unreadable <dir>` exit 3 -- an unread listing is not an absent one.
        repo="${3:?usage: $0 repo ci <owner/repo> <ref>}"; ref="${4:?usage: $0 repo ci <owner/repo> <ref>}"
        _wl=$(workflow_listing "$repo" "$ref") && _wlrc=0 || _wlrc=$?
        case $_wlrc in
        0) if [ -n "$(printf '%s\n' "$_wl" | sed -n '2,$p')" ]; then echo "yes $(printf '%s\n' "$_wl" | sed -n 1p)"; exit 0; fi
           echo "none"; exit 4 ;;
        4) echo "none"; exit 4 ;;
        *) echo "unreadable $(printf '%s\n' "$_wl" | sed -n 1p)"; exit 3 ;;
        esac
        ;;
    protect)
        repo="${3:-}"
        branch="${4:?usage: $0 repo protect <owner/repo> <branch> <status-context>...}"
        shift 4
        [ $# -ge 1 ] || die "repo protect: name at least one status-check context (e.g. 'Test /*')"
        repo_protect "$repo" "$branch" 0 "$@"
        ;;
    provision)
        repo="${3:?usage: $0 repo provision <owner/repo> (--kind python|rust|node | --no-ci \"<reason>\")}"
        shift 3; provision_flags "$@"
        repo_provision "$repo"
        ;;
    create)
        repo="${3:?usage: $0 repo create <owner/repo> (--kind ... | --no-ci \"<reason>\") [--private]}"
        shift 3; provision_flags "$@"
        owner="${repo%%/*}"
        if api "/api/v1/orgs/$owner" >/dev/null 2>&1; then
            endpoint="/api/v1/orgs/$owner/repos"
        else
            me=$(api /api/v1/user | python3 -c 'import json, sys; print(json.load(sys.stdin).get("login", ""))')
            [ "$me" = "$owner" ] || die "repo create: '$owner' is neither an org nor the authenticated user ($me)"
            endpoint=/api/v1/user/repos
        fi
        body=$(PRIVATE="$PRIVATE" python3 -c '
import json, os, sys
print(json.dumps({"name": sys.argv[1], "private": os.environ["PRIVATE"] == "true",
                  "auto_init": True, "default_branch": "main"}))' "${repo#*/}")
        api "$endpoint" -X POST -d "$body" >/dev/null || die "repo create: POST $endpoint failed"
        echo "created $repo"
        repo_provision "$repo"
        ;;
    fork)
        url="${3:-}"; repo="${4:-}"
        [ -n "$url" ] && [ -n "$repo" ] || die "usage: $0 repo fork <clone-url> <owner/repo> (--kind ... | --no-ci \"<reason>\") [--private]"
        shift 4; provision_flags "$@"
        # A fresh fork's `.gitea`/`.github` gate is upstream's by definition, so it is shadowed unless
        # the caller says otherwise.
        EXISTING="${EXISTING:-shadow}"
        body=$(PRIVATE="$PRIVATE" python3 -c '
import json, os, sys
o, n = sys.argv[2].split("/", 1)
print(json.dumps({"clone_addr": sys.argv[1], "repo_owner": o, "repo_name": n, "mirror": False,
                  "service": "git", "private": os.environ["PRIVATE"] == "true"}))' "$url" "$repo")
        api /api/v1/repos/migrate -X POST -d "$body" >/dev/null || die "repo fork: migrating $url into $repo failed"
        echo "forked $url -> $repo"
        repo_provision "$repo"
        ;;
    *) die "usage: $0 repo ci|protect|provision|create|fork ... (see the repo) case)" ;;
    esac
    ;;

pr)
    # The `gh pr` surface the cut-over needs, against Forgejo's REST API. Forgejo ships no
    # official client CLI, and the contrib ones (`fj`, `tea`) do not cover check-runs --
    # which is the one verb a gate cannot do without.
    #
    #   hub-api.sh repo protect <owner/repo> <branch> <status-context>...   required checks +
#                                 block_on_outdated_branch on a TARGET repo's branch
#   hub-api.sh pr create <owner/repo> <head> <base> <title> <body|--title-only|--from-commits> [--draft|--no-draft]
    #                                       body REQUIRED, or --title-only to state that the title is
    #                                       the whole record. Silence used to mean the second one.
    #                                       Opens as a DRAFT ('WIP: ') when any other PR is
    #                                       open -- it is not the front of the queue;
    #                                       --draft / --no-draft state it instead. --serial marks it
    #                                       `queue:serial`: it lands alone, never batched.
    #   hub-api.sh pr body   <owner/repo> <number> <text|@file:path|--from-commits>   write/replace, read back
    #   hub-api.sh pr checks <owner/repo> <sha>     FULL 40-char sha only; see require_full_sha
    #   hub-api.sh pr merge  <owner/repo> <number> <subject> [expected-head-sha]
    #                                       refuses while the queue is FROZEN unless
    #                                       HUB_API_FREEZE_OVERRIDE="<reason>"
    #   hub-api.sh pr hold   <owner/repo> <number>   apply the hold the drain actually matches,
    #                                       read it back, and state the window it is honoured in
    #
    # `create` and `checks` both name the commit they acted on (`head <sha>` / `measured <sha>`),
    # because two defects were one defect from two sides: THE CLIENT
    # COULD NOT EXPRESS WHICH TREE YOU MEANT. On the write side the forge resolved a branch name
    # to whatever it pointed at; on the read side a ref silently resolved to a moving tip. Both
    # now pin a sha and say which one, so a verdict carries its own expiry.
    require_config
    sub="${2:-}"; repo="${3:-}"
    [ -n "$sub" ] && [ -n "$repo" ] || die "usage: $0 pr create|body|checks|why-red|await|merge|log|hold|unhold <owner/repo> ..."
    case "$sub" in
    await)
        # WAIT FOR A GATE WITHOUT MISTAKING A REFUSAL FOR PATIENCE -- the second instance.
        #
        # The first measurement: *"a gate poller spun every 30s for ~7 minutes on a 10-char
        # sha while the PR it watched went green in about two."* It answered with `refuse()` exiting
        # 2, so a caller CAN tell "this input will never work" from "not ready yet".
        #
        # IT HAPPENED AGAIN ON 2026-09-02, because the convention only helps a caller that reads the
        # exit code. A hand-rolled loop passed a 12-char sha -- truncated by its own debug print and
        # fed back in -- and matched on STDOUT text for `state='success'`. The refusal goes to
        # STDERR and carries none of those tokens, so eighteen polls in a row measured nothing and
        # the loop reported "not terminal" on a PR that had been green throughout. The filter chose
        # the answer, one layer up from where `refuse()` fixed it.
        #
        # SO THIS BRANCHES ON THE EXIT CODE, never on the text:
        #     0  green   -> done
        #     1  red     -> done, and it is a VERDICT: do not keep waiting for it to improve
        #     2  refused -> NO VERDICT EXISTS. Bounded retries, because "no check-runs yet" is
        #                   briefly true after a push -- but the refusal is PRINTED every time, so
        #                   a permanent one (bad sha, wrong repo) is visible on the first pass
        #                   instead of looking like patience.
        #     4  redundant -> this sha is ALREADY WATCHED and there is a session to wake, so
        #                   blocking buys nothing. Its own code, not 3: a caller must be able to
        #                   tell "go idle, the verdict is coming to you" from "I could not classify
        #                   the gate". Folding them would repeat the rc-6-means-RED mistake that
        #                   once cost a gate cycle -- one number, two causes, opposite fixes.
        #     *  abort   -> an unclassified status is an instrument failure, not a pending gate.
        #
        # AND THE SHA IS VALIDATED BEFORE THE FIRST SLEEP. The permanent case that produced both
        # instances is a short sha; catching it up front costs one call and removes the whole class.
        sha="${4:?usage: $0 pr await <owner/repo> <sha> [max-polls] [interval-s]}"
        require_full_sha "$sha" "pr await"
        max="${5:-20}"; every="${6:-30}"
        # ONE POLL CLASSIFIES; MANY POLLS ARE A SESSION SPINNING, and THIS VERB WAS THE WAY BACK
        # TO THE BEHAVIOUR gate-watch REMOVED. `pr create` registers every non-draft PR
        # with gate-watch, which outlives the session and peer-messages it the verdict, and then
        # prints "go idle, do not poll". Two instruction documents still said the opposite --
        # both recommended this verb "as a harness background task", advice written before
        # gate-watch and never retired when it replaced that. MEASURED 2026-09-21: a session ran
        # six polls of `pr await` on one sha while gate-watch delivered the identical verdict to
        # that same session mid-wait. The docs were fixed too, but a doc cannot refuse -- this can.
        #
        # max=1 IS NEVER REFUSED, AND THAT IS THE DISCRIMINATOR, NOT AN EXEMPTION. `gate-watch.py`
        # measures through `pr await <repo> <sha> 1 0` precisely so the four-way exit contract has
        # ONE definition. Refusing that call would break the watcher this guard points at, and would
        # recurse: the check below shells out to gate-watch, which shells back into this verb.
        # Keying on `max` rather than on a flag is what makes the recursion impossible rather than
        # merely unlikely.
        #
        # REFUSED ONLY WHERE THE WAIT IS ACTUALLY REDUNDANT -- a session to wake AND an open watch
        # on this exact sha. Neither is guaranteed: cron has no FORGE_TOOLS_WAKE_PID, and a batch integration
        # head is nobody's PR and so is never registered. Those still block, because for them
        # blocking is the only instrument there is.
        if [ "$max" -gt 1 ] && [ -z "${HUB_API_AWAIT_BLOCKING:-}" ] && [ -n "${FORGE_TOOLS_WAKE_PID:-}" ]; then
            _aw_dir=$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")
            _aw_sha10=$(printf '%.10s' "$sha")
            _aw_watch=$(python3 "$_aw_dir/gate-watch.py" list 2>/dev/null \
                        | grep -F "  $repo $_aw_sha10 " || true)
            if [ -n "$_aw_watch" ]; then
                printf 'pr await: REFUSING to block -- %s %s is already watched:\n' "$repo" "$_aw_sha10" >&2
                printf '%s\n' "$_aw_watch" >&2
                printf '%s\n' "That watch outlives this session and peer-messages it the verdict, so waiting here
costs a turn and buys nothing. GO IDLE -- you will be woken.
  see the watch : gate-watch list
  one reading   : $0 pr checks $repo $sha
  block anyway  : HUB_API_AWAIT_BLOCKING=1 (for a context with no session to wake)" >&2
                exit 4
            fi
        fi
        i=0
        while [ "$i" -lt "$max" ]; do
            i=$((i + 1))
            # `|| rc=$?` RATHER THAN A BARE ASSIGNMENT: this file runs under `set -eu`, so a
            # command substitution whose command exits non-zero kills the script BEFORE the next
            # line reads `$?`. The first version did that and exited 1 with empty stdout AND empty
            # stderr on a red gate -- the poller dying silently at the exact moment it had an
            # answer, which is the failure this verb exists to remove, one layer down. Caught by the
            # red-path test, which is why that test drives a REAL red rather than asserting on prose.
            rc=0
            out=$("$0" pr checks "$repo" "$sha" 2>&1) || rc=$?
            case "$rc" in
                0)
                   # GREEN WITH THE SUITE SKIPPED IS NOT A VERDICT. A draft skips
                   # `Test / pytest` by design and the commit-status API still says success.
                   # gate-watch measures through this verb, and every queue un-draft registers a
                   # watch on the head about to run, so this arm delivered "GREEN" and
                   # closed on a suite that never ran -- measured on the first such un-draft. Same
                   # clause as pr-queue.sh's wait_for_green, same anchor: the context line starts
                   # with two spaces and ends in the status word.
                   _must="${HUB_MUST_RUN_CONTEXT:-Test / pytest}"
                   if printf '%s\n' "$out" | awk -v p="$_must" 'index($0, "  " p) == 1 && $NF == "skipped" { f = 1 } END { exit !f }'; then
                       printf 'pr await: poll %d/%d, green but %s SKIPPED -- the suite never ran on this head; not a verdict\n' "$i" "$max" "$_must" >&2
                   else
                       printf '%s\n' "$out"; printf 'pr await: GREEN after %d poll(s)\n' "$i"; exit 0
                   fi ;;
                1)
                   # EXIT 1 IS NOT ONE STATE. `pr checks` returns it for ANY state that is not
                   # success -- and `pending` is one of them. The first version of this verb called
                   # that RED and gave up on its first poll against a gate that had barely started:
                   # the mirror of the bug it exists to fix. There a refusal read as patience; here
                   # patience read as a verdict. FOUND BY USING IT, not by testing it -- the fixture
                   # only ever produced `failure` and empty, so no test could have caught it.
                   #
                   # THE STATE IS READ FROM THE CONTRACT LINE, and that is not the text-matching
                   # this verb refuses to do. `total_count=%d state=%r skipped=%d` is a
                   # machine-readable line `pr checks` prints on purpose. The earlier failure was
                   # matching PROSE and treating no-match as `keep waiting`; here an unrecognised
                   # state ABORTS, so a format change stops the wait instead of hiding in it.
                   state=$(printf '%s\n' "$out" | sed -n "s/.*state='\\([a-z]*\\)'.*/\\1/p" | tail -n 1)
                   case "$state" in
                       pending|"") : ;;
                       failure|error)
                           # A RED CAN BE A CANCELLATION THE STATUS API RENDERED AS `failure`.
                           # gate-watch.py reports this verb's rc=1 to a session as a
                           # verdict, so an uncorrected one here becomes "your PR is RED" about a
                           # run the forge cancelled when someone re-pushed the head. Only rc 0
                           # diverts: `not cancelled` and `nothing measured` both stay a verdict,
                           # so a runs API that is down cannot turn a real red into patience.
                           _wc=0; was_cancelled "$repo" "$sha" || _wc=$?
                           if [ "$_wc" = 0 ] || [ "$_wc" = 3 ]; then
                               printf 'pr await: poll %d/%d, %s but a run for this head was CANCELLED, not failed -- not a verdict\n' "$i" "$max" "$state" >&2
                           else
                               printf '%s\n' "$out"
                               printf 'pr await: %s after %d poll(s) -- a verdict, not a delay\n' "$state" "$i" >&2
                               exit 1
                           fi ;;
                       *)
                           printf '%s\n' "$out" >&2
                           printf 'pr await: ABORTING -- unrecognised state %s; classify it before\n' "$state" >&2
                           printf '  trusting any wait built on this output.\n' >&2
                           exit 3 ;;
                   esac
                   printf 'pr await: poll %d/%d, state=%s\n' "$i" "$max" "${state:-unknown}" >&2 ;;
                2) printf 'pr await: poll %d/%d, no verdict yet:
%s
' "$i" "$max" "$out" >&2 ;;
                *) printf '%s
' "$out" >&2
                   printf 'pr await: ABORTING -- `pr checks` exited %d, which is not a gate state.
' "$rc" >&2
                   exit 3 ;;
            esac
            [ "$i" -lt "$max" ] && sleep "$every"
        done
        printf 'pr await: gave up after %d poll(s) at %ss. NOT a verdict -- the gate never produced
' "$max" "$every" >&2
        printf '  one, and the last refusal is above. Do not read this as green.
' >&2
        exit 2
        ;;
    log)
        # READ THE RUN LOG FOR A SHA -- it exists because "read the log rather than
        # re-running locally" was not executable from this seat.
        #
        # THREE INTEGER NAMESPACES, ALL PLAUSIBLE, AND THE WRONG ONE ANSWERS RATHER THAN ERRORS:
        #   /actions/tasks  -> `id`            (4913 for one pytest job)
        #   /actions/runs   -> `id`            (4432)  <- what /actions/runs/{}/jobs wants
        #   the same run    -> `index_in_repo` (3995)  <- what html_url and the UI show
        # A session read the run NUMBER off a commit status `target_url`, passed it where an ID
        # goes, and got a real, well-formed log from TWELVE DAYS EARLIER reporting a green suite.
        # Nothing 404s, because that number is a valid id belonging to an older run. It then spent
        # a full diagnosis on the wrong subject.
        #
        # SO THIS BINDS BEFORE IT PRINTS: runs are matched on `commit_sha`, every line of the
        # header says which run and which sha it is about, and a sha with no run REFUSES rather
        # than falling back to "the newest run" -- that fallback IS the defect.
        sha="${4:?usage: $0 pr log <owner/repo> <sha> [job-name-substring]}"
        require_full_sha "$sha" "pr log"
        SELF="$0" REPO="$repo" SHA="$sha" WANT="${5:-}" python3 - <<'PY2'
import json, os, subprocess, sys

SELF, REPO, SHA, WANT = (os.environ["SELF"], os.environ["REPO"],
                         os.environ["SHA"], os.environ.get("WANT", ""))


def call(path, as_json=True):
    r = subprocess.run(["sh", SELF, path], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        print("pr log: %s failed: %s" % (path, (r.stderr or "").strip()[:200]), file=sys.stderr)
        sys.exit(1)
    return json.loads(r.stdout) if as_json else r.stdout


runs = [r for r in (call("/api/v1/repos/%s/actions/runs?limit=50" % REPO).get("workflow_runs") or [])
        if (r.get("commit_sha") or "") == SHA]
if not runs:
    print("pr log: no run in the last 50 carries commit_sha %s.\n"
          "  NOT falling back to the newest run -- that is exactly how a log for the wrong subject\n"
          "  gets read as yours. If the run is older than 50, say so; if the sha\n"
          "  is wrong, the mismatch is the finding." % SHA, file=sys.stderr)
    sys.exit(1)

printed = 0
for run in runs:
    jobs = call("/api/v1/repos/%s/actions/runs/%s/jobs" % (REPO, run["id"]))
    for job in jobs:
        if WANT and WANT not in (job.get("name") or ""):
            continue
        print("=" * 78)
        print("run id=%s  index_in_repo=%s  workflow=%s" % (run["id"], run.get("index_in_repo"),
                                                            run.get("workflow_id")))
        print("commit_sha=%s" % run.get("commit_sha"))
        print("job id=%s  name=%s  status=%s" % (job["id"], job.get("name"), job.get("status")))
        print("=" * 78)
        if job.get("status") in ("waiting", "running", "blocked"):
            print("  (no log yet -- this job has not finished)")
            continue
        print(call("/api/v1/repos/%s/actions/jobs/%s/logs" % (REPO, job["id"]), as_json=False))
        printed += 1

if not printed:
    print("pr log: %d run(s) matched %s but no FINISHED job matched %r."
          % (len(runs), SHA, WANT or "<any>"), file=sys.stderr)
    sys.exit(1)
PY2
        ;;
    create)
        # EACH NAMES ITS POSITION AND PRINTS THE WHOLE ORDER. These used to say
        # "base branch" and nothing else. A caller that mis-counts here supplies the missing
        # argument and slides every later one along, which is exactly how one PR's body became a
        # filesystem path; the guard for that is in the body slot below, and this is the half
        # that stops the caller needing it.
        head="${4:?missing <head> (argument 2 of 5) -- order: $0 pr create <owner/repo> <head> <base> <title> <body|--title-only>}"
        base="${5:?missing <base> (argument 3 of 5) -- order: $0 pr create <owner/repo> <head> <base> <title> <body|--title-only>}"
        title="${6:?missing <title> (argument 4 of 5) -- order: $0 pr create <owner/repo> <head> <base> <title> <body|--title-only>}"
        # THE BODY IS A DECISION, NOT A DEFAULT. This was `body="${7:-}"`, so omitting
        # the argument opened a bodyless PR silently, exit 0. Measured 2026-09-01: done twice in one
        # evening, hours after merging the skill that states the rule, and one PR had to be CLOSED and
        # reopened as another to carry its reasoning -- `issue body-edit` is a literal exactly-once
        # region replacement, and an empty body has nothing to match, so there is no repair path once
        # the PR exists.
        #
        # NOT MADE MANDATORY, deliberately. The body rule is explicit that a body is not always earned:
        # the median PR here is 55 lines across 2 files and for those a body is ceremony. Requiring
        # one buys filler, which is the failure that skill already measured -- 17 of 25 PRs carrying
        # an identical 122-character body naming the queue rather than the change.
        #
        # So the defect is not the absent body; it is that ABSENCE AND DECISION WERE
        # INDISTINGUISHABLE. `--title-only` says the title is the whole record, and silence is no
        # longer a way to say it.
        if [ $# -lt 7 ]; then
            printf '%s\n' "hub-api: REFUSING: pr create needs a body, or --title-only to say the title is the whole record.

  A BODY IS EARNED by reasoning the diff cannot show: a trade-off, an approach you
  rejected and why, the measurement behind a number, or a ceiling on what the change establishes.
  If the reasoning is already in your commit messages, --from-commits makes THEM the body, so the
  PR does not read as empty while the record sits one click away. If none of those apply,
  --title-only is the honest answer and this refuses nothing.

  LINK THE TICKET IN IT -- 'Closes #N'. Measured at 5 of 25 merged PRs, and the ticket stays open
  after the work lands without it. To write or replace a body LATER:
    $0 pr body <owner/repo> <n> <text|@file:path|--from-commits>

    $0 pr create <owner/repo> <head> <base> <title> <body|--title-only|--from-commits>" >&2
            exit 2
        fi
        # A BARE FLAG IS NOT A BODY. This client takes the body POSITIONALLY and has
        # no `-F`/`--body-file`; that is the `gh` spelling, and typing the `gh` habit here puts
        # the FLAG in the body slot and the filename in the one after it. Both are accepted, the
        # PR opens, exit 0. Measured 2026-09-09: EIGHT of the last fifty PRs on one repo carried a
        # body of the literal two characters `-F`, two of them still open when this was written.
        #
        # It is the `@path` refusal one slot along, and worse in one way: `-F` is not a plausible
        # sentence, so the PR reads as though its author chose to write nothing, which is exactly
        # the distinction `--title-only` exists to make. The repair path is also the narrowest --
        # `issue body-edit` needs an exactly-once region to replace, and two characters is a poor
        # anchor.
        #
        # WHITESPACE FIRST, THEN THE HYPHEN. A body legitimately starting with a hyphen is
        # ordinary prose ("-- and that is the finding"), and the first version of this pattern
        # refused it -- the exact collision rejected when `@file:` was chosen over a bare
        # `@`. A flag has no whitespace in it, so anything containing a space, tab or newline is
        # prose and is taken as-is; only a hyphen-led argument with no whitespace at all is
        # refused. CEILING: a one-word body starting with a hyphen is refused too. That is
        # implausible as a whole PR body, and the message names `@file:` for it.
        #
        # CHECKED HERE AND NOT IN THE ARGUMENT LOOP. The first version of this guard sat in the
        # generic `@file:` loop and refused any bare flag in any slot -- which broke
        # `--title-only`, `--because`, `--delete` and a hook's `--quiet` on the first try. The
        # body slot is the only place a flag is unambiguously wrong, so the check belongs where
        # the body is read and nowhere else.
        resolve_pr_body "$7"
        from_commits=no
        case "$body" in
            --title-only) body="" ;;
            # Written right after the PR exists, by `pr body` -- the forge knows the PR's commits,
            # and one code path builds the body for both verbs.
            --from-commits) from_commits=yes; body="" ;;
        esac
        # A PR CAN BE OPENED AGAINST A HEAD MISSING PART OF THE CHANGE.
        #
        # This verb takes a BRANCH NAME because Forgejo's `head` field is a branch name -- so the
        # forge resolves it to whatever the remote points at NOW, and an unpushed local commit is
        # invisible by construction. Measured live 2026-08-24 while landing a PR: the author
        # committed the CI/docs half and opened the PR without pushing. Three check-runs
        # registered against a tree missing roughly a third of the work, `mergeable` read True,
        # and nothing complained. The runs were not wrong -- they measured the pushed tree
        # accurately. Green was earned and misleading at the same time.
        #
        # A freshness check cannot catch this: it measures the gap to the BASE ("are you
        # behind main"), which is a different question from "is what you are about to ask the
        # forge to gate the same as what you have".
        #
        # So compare the branch's REMOTE tip against its LOCAL ref and refuse on any gap. This
        # keeps the existing call shape -- every current caller passes a branch name and keeps
        # working -- while making the unexpressible question answerable. REFUSES rather than
        # warns: the failure is silent and green, which is the class a session reads past, and
        # there is no PR yet, so a refusal costs a push and nothing else.
        head_remote=$(api "/api/v1/repos/$repo/branches/$head" 2>/dev/null | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("commit", {}).get("id", ""))
except Exception:
    print("")')
        [ -n "$head_remote" ] || die "pr create: '$head' does not exist on $repo. Push it first: git push -u $FORGE_TOOLS_REMOTE $head"
        # WHICH tree you meant, stated or inferred. `HUB_API_EXPECT_HEAD` says it outright --
        # "make the client take a SHA" -- and is the form to prefer when the answer
        # matters. Absent that, infer it from the local branch, which is what a session means
        # in practice. Note the variable SUPPLIES the expected value, it never SKIPS the
        # comparison: an override that could turn the check off would be a fail-open wearing
        # the same output as a verified pass.
        expect="${HUB_API_EXPECT_HEAD:-}"
        if [ -n "$expect" ]; then
            require_full_sha "$expect" "pr create (HUB_API_EXPECT_HEAD)"
            head_local="$expect"; head_src="HUB_API_EXPECT_HEAD"
        else
            head_local=$(git -C "$(repo_root)" rev-parse --verify --quiet "refs/heads/$head" 2>/dev/null || true)
            head_src="local refs/heads/$head"
            # No local ref and no stated expectation means this client cannot tell which tree
            # you meant, which is the whole defect. Refuse rather than fall through to "trust
            # the remote".
            [ -n "$head_local" ] || die "pr create: no local ref refs/heads/$head and no HUB_API_EXPECT_HEAD, so the head cannot be verified against what you have. Fetch it, or state the sha you mean."
        fi
        [ "$head_local" = "$head_remote" ] || die "pr create: REFUSING -- '$head' is $head_remote on $repo but $head_local per $head_src, so the PR would be gated against a tree that is not what you have. Push first: git push $FORGE_TOOLS_REMOTE $head"
        # A PR TO A REPO WITH NO CI IS UNGATED, and nothing says so (operator, 2026-09-14). Measured
        # that day: a repo had no workflow, the forge registered zero checks, and
        # a PR merged and released on a local run alone. So refuse unless the base OR the head
        # carries a workflow the forge reads -- the head arm is what lets the PR that ADDS CI open.
        # Read through `workflow_listing`, as `repo provision` and the gate-parity arm are:
        # CI is a workflow file in the directory the forge RUNS -- a `.gitea`-only repo has CI, and an
        # existing but empty `.forgejo/workflows` switches off a `.github` gate behind it. An unread
        # listing is CANNOT TELL, and that refuses too rather than reading as "has CI" -- unless the
        # other ref settles it. HUB_API_NO_CI_REASON names why a PR opens anyway, and is printed.
        ci_state=none
        for _ref in "$base" "$head_remote"; do
            _wl=$(workflow_listing "$repo" "$_ref") && _wlrc=0 || _wlrc=$?
            case $_wlrc in
            0) if [ -n "$(printf '%s\n' "$_wl" | sed -n '2,$p')" ]; then ci_state=yes; break; fi ;;
            4) : ;;
            *) ci_state=unreadable ;;
            esac
        done
        if [ "$ci_state" != yes ]; then
            if [ -n "${HUB_API_NO_CI_REASON:-}" ]; then
                printf '%s\n' "hub-api: NO CI OVERRIDDEN -- $repo reads '$ci_state' for workflows on $base and $head, and the PR opens because HUB_API_NO_CI_REASON says: $HUB_API_NO_CI_REASON" >&2
            elif [ "$ci_state" = none ]; then
                refuse "pr create: $repo has no CI -- no workflow file in the directory the forge runs (the first of .forgejo, .gitea or .github/workflows that exists) on '$base' or '$head', so the forge would register no checks and this PR would merge ungated. Add a workflow to the branch first (bootstrap-target-repo), or open it anyway with HUB_API_NO_CI_REASON=\"<why>\"."
            else
                refuse "pr create: cannot tell whether $repo has CI -- a workflow listing could not be read, and an unread listing is not a present workflow. Retry, or HUB_API_NO_CI_REASON=\"<why>\" opens it anyway."
            fi
        fi
        # A PR THAT IS NOT AT THE FRONT OF THE QUEUE OPENS AS A DRAFT (operator, 2026-09-15).
        #
        # `block_on_outdated_branch` outdates every open PR each time one lands, so a PR that is not at
        # the front pays full CI on a base that is about to move -- measured that evening: one PR ran
        # green three times before it landed. A `WIP: ` title makes Forgejo mark the PR draft (measured
        # 2026-09-15: a `WIP: probe ...` PR reads `draft=true`, unprefixed PRs read false), and a
        # draft skips the suite, so it waits off the contended runner until the queue reaches
        # it -- one un-drafts and runs CI once, two or more batch.
        #
        # THE RULE IS "NOT THE FRONT", AND THE FRONT IS THE OLDEST OPEN PR (operator, 2026-09-16). So
        # anything else open means this PR is behind it -- including when everything open is a draft:
        # opening ready there would let the newest PR take the runner ahead of drafts already waiting.
        #
        # DECIDED HERE, NOT BY THE CALLER, so no session has to remember the rule -- which is how the
        # hand-kept order issue rotted (22 entries, all merged, none of the week's landings).
        # `--draft` forces it (a burst the caller means to batch); `--no-draft` suppresses it (a caller
        # that has measured it is the front; the queue itself no longer passes it).
        #
        # AN UNREAD LISTING REFUSES, it does not open ready. Read as an empty queue it would open a
        # non-draft PR that takes the runner from whatever is really ahead of it -- the same
        # fail-open the CI arm above refuses. The flags are the way through a listing that cannot be
        # read: the caller then states the answer the client could not measure.
        draft_flag=""
        serial=""
        _i=0
        for _a in "$@"; do
            _i=$((_i + 1)); [ "$_i" -le 7 ] && continue
            case "$_a" in
                --draft) draft_flag=yes ;;
                --no-draft) draft_flag=no ;;
                --serial) serial=yes ;;
                *) die "pr create: unknown option '$_a' after the body -- expected --draft, --no-draft or --serial" ;;
            esac
        done
        if [ -z "$draft_flag" ]; then
            open_ahead=$(api "/api/v1/repos/$repo/pulls?state=open&limit=1" | python3 -c '
import json, sys
try:
    v = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(v, list):      # an error object is not an empty listing
    sys.exit(1)
print(len(v))') || refuse "pr create: cannot tell whether an open PR is ahead of this one on $repo -- the open PR listing could not be read, and an unread listing is not an empty queue. Pass --draft or --no-draft to state it."
            if [ "$open_ahead" -gt 0 ]; then draft_flag=yes; else draft_flag=no; fi
        fi
        if [ "$draft_flag" = yes ]; then
            case "$title" in "WIP: "*) : ;; *) title="WIP: $title" ;; esac
            printf '%s\n' "hub-api: opening as a DRAFT ('WIP: ' title, suite skipped) -- it is not at the front of $repo's queue. The queue un-drafts it at the front, or batches it with other drafts." >&2

        fi
        # Captured rather than piped, for the reason require_full_sha's slashed-ref case names: piping a failed
        # request into `json.load` kills it mid-pipe with a traceback, which is a correct outcome
        # delivered unreadably. Measured on this very verb -- a 422 from a bad base printed
        # `curl: (22)` and then a Python stack. `--fail-with-body` already put the forge's own
        # reason on stderr; this keeps it as the last word instead of burying it.
        # `--serial` KEEPS A PR OUT OF EVERY BATCH (operator, 2026-09-16): "if a PR needs to
        # land by itself then it gets the serial flag". It is a MARK ON THE PR, `queue:serial`, which
        # `drain` matches: the PR still waits its turn (drafted if it is not the front), and at the
        # front it lands alone. The default is the literal `pr-queue.sh` matches, through the same
        # variable; test_late_hold_is_honoured.py asserts the two assignments agree.
        #
        # THE MARK RIDES THE CREATE, not a follow-up `issue tag`: a label added after would leave a
        # window in which the PR is open and unmarked, and a drain could batch it -- and if that
        # second write failed, it would stay unmarked. So the id is resolved FIRST, and an
        # unresolvable label REFUSES with nothing opened, naming the one command that creates it.
        serial_label="${PR_QUEUE_SERIAL_LABEL:-queue:serial}"
        serial_id=""
        if [ "$serial" = yes ]; then
            serial_id=$("$0" issue label-id "$repo" "$serial_label" 2>/dev/null) \
                || refuse "pr create: --serial needs the label '$serial_label' on $repo, and it does not resolve -- nothing was opened, because an unmarked PR can be batched, which is what the flag prevents. Create the label once: $0 /api/v1/repos/$repo/labels -X POST -H 'Content-Type: application/json' --data-binary '{\"name\":\"$serial_label\",\"color\":\"#5319e7\",\"description\":\"pr-queue: lands alone, never in a batch\"}'"
            case "$serial_id" in ''|*[!0-9]*) refuse "pr create: '$serial_label' on $repo resolved to '$serial_id', which is not a label id" ;; esac
        fi
        create_json=$(json_obj "head=$head" "base=$base" "title=$title" "body=$body")
        if [ -n "$serial_id" ]; then
            create_json=$(printf '%s' "$create_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); d["labels"]=[int(sys.argv[1])]; print(json.dumps(d))' "$serial_id")
        fi
        pr_json=$(api "/api/v1/repos/$repo/pulls" -X POST -H "Content-Type: application/json" \
            -d "$create_json") \
            || die "pr create: the forge refused to open the PR (its reason is above). Nothing was created; head $head_remote was verified."
        # NOTHING ON STDOUT UNTIL THE RECORD IS COMPLETE. This verb used to announce
        # "PR #N open" here, then write the body and register the watch. A caller that truncates
        # stdout (`| head -1`, measured four times) closes the
        # pipe after that line, the next print is SIGPIPE, and the script dies with the PR open,
        # body-less and unwatched -- having shown the caller one healthy line. stderr is not the
        # pipe, so the progress lines below stay where they were; stdout comes last, at the end.
        # READ THE MARK BACK. The create's response is the request echoed, not the forge's state
        # (a write that silently drops part of its argument still answers 2xx). A PR
        # that opened unmarked is the failure the flag exists for, so it is named with its repair.
        pr_num=$(printf '%s' "$pr_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["number"])')
        if [ -n "$serial_id" ]; then
            api "/api/v1/repos/$repo/issues/$pr_num/labels" | python3 -c '
import json, sys
try:
    ls = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if any(l.get("name") == sys.argv[1] for l in ls) else 1)' "$serial_label" \
                || die "pr create: #$pr_num is OPEN but '$serial_label' did NOT stick -- until it does, a drain can batch it. Mark it now: $0 issue tag $repo $pr_num $serial_label"
            printf '%s\n' "hub-api: #$pr_num carries '$serial_label' -- the queue lands it alone, never in a batch." >&2
        fi
        # WATCHED BY DEFAULT (operator, 2026-09-16). gate-watch.py outlives the
        # session and wakes it on a verdict, and was measured unused: 8 registrations, all on the day it
        # was built, while sessions hand-rolled `until pr checks; sleep` loops. So the verb that opens
        # the PR registers it, and no session has to remember to. A DRAFT is not watched: its suite is
        # skipped, and `drain` waits on its own un-drafts in-run. Never fatal -- the PR is already open,
        # so a watch that could not be set is SAID, with its repair, and the create still succeeds.
        if [ "$from_commits" = yes ]; then
            "$0" pr body "$repo" "$pr_num" --from-commits 1>&2 \
                || die "pr create: #$pr_num is OPEN but its body was NOT written from its commits (reason above). Repair: $0 pr body $repo $pr_num --from-commits"
        fi
        # SUBSCRIBE FIRST, AND FOR EVERY PR -- draft or not. The watch below covers THIS head only, and
        # only when the PR is not a draft; the subscription covers every head this PR ever gates, no
        # matter who drains it or how the head moves. They are complements: the watch is the fast path
        # for the common case, the subscription is what makes the guarantee hold.
        #
        # THIS SAT INSIDE THE NON-DRAFT ARM BELOW, under that same first sentence, from the day it was
        # written -- so a PR opened as a draft was never subscribed, and anything opened behind the
        # front is a draft. Measured 2026-09-21 on six PRs: every non-draft subscribed at creation,
        # every draft never; an un-drafted PR then took two pushes with nothing watching either head.
        # No session, no subscription: there is nobody to tell, and the arm below says so.
        if [ -n "${FORGE_TOOLS_WAKE_PID:-}" ] && [ -z "${HUB_API_NO_WATCH:-}" ]; then
            python3 "$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")/gate-watch.py" \
                subscribe "$repo" "$pr_num" >/dev/null 2>&1 || true
        fi
        if [ "$draft_flag" = no ]; then
            if [ -n "${HUB_API_NO_WATCH:-}" ]; then
                printf '%s\n' "hub-api: #$pr_num NOT watched -- HUB_API_NO_WATCH: $HUB_API_NO_WATCH" >&2
            elif [ -z "${FORGE_TOOLS_WAKE_PID:-}" ]; then
                printf '%s\n' "hub-api: #$pr_num NOT watched -- no FORGE_TOOLS_WAKE_PID, so there is no session to wake. Wait with: $0 pr await $repo $head_remote" >&2
            elif ! _gw_out=$(python3 "$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")/gate-watch.py" \
                    register "$repo" "$head_remote" "PR #$pr_num" 2>&1); then
                printf '%s\n' "hub-api: #$pr_num NOT watched -- gate-watch register failed: $(printf '%s' "$_gw_out" | tail -1). Repair: gate-watch register $repo $head_remote" >&2
            else
                printf '%s\n' "hub-api: #$pr_num is watched by gate-watch -- this session is peer-messaged on its verdict; go idle, do not poll." >&2
            fi
        fi
        # The announcement, LAST (see above). The head line names the tree that was gated, so a
        # verdict pasted into a ticket carries its own expiry.
        printf '%s' "$pr_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("PR #%s %s mergeable=%s" % (d["number"], d["state"], d.get("mergeable")))'
        printf 'head %s (%s)\n' "$head_remote" "$head"
        ;;
    why-red)
        # WHY A RED VERDICT IS RED, WITHOUT A LOCAL SUITE.
        #
        # `pr checks` reports a job's STATUS and stops there. A session handed `failure` and no
        # failing test name has one local instrument that yields names, and it is the whole suite --
        # which the standing rule forbids, which costs shared CPU the forge, site and agents all
        # want, and which MEASURES A DIFFERENT EXECUTION (sequential here, `-n 4` up there).
        #
        # MEASURED 2026-09-21 on a real PR, and it is the case that justifies the verb rather than an
        # imagined one: the failure was a new-tests differential gate -- "1 of 2 new test(s) PASSED on
        # the merge base and claim no exemption" -- because a control was not named `*__control*`.
        # THE DIFFERENTIAL ONLY EXISTS ON THE RUNNER. Every local run, narrow or whole, was green
        # and always would have been. The suite could not have answered the question it was run to
        # answer, and three of them were run before this verb existed.
        #
        # THE ID CHAIN IS NOT GUESSABLE, which is the other half of why this is a verb and not a
        # line in a runbook. Forgejo exposes THREE different numbers here and they are not
        # interchangeable: the tasks listing's `id` and `run_number`, and the run's own `id`.
        # `actions/jobs/<task-listing-id>/logs` returns SOMEBODY ELSE'S LOG rather than 404 -- it
        # answered with a job from three weeks earlier, which reads exactly like a real answer.
        # The chain that is correct: runs listing -> match `commit_sha` -> `runs/<run id>/jobs` ->
        # the job whose `status` is `failure` -> `actions/jobs/<job id>/logs`.
        sha="${4:?commit sha}"
        require_full_sha "$sha" "pr why-red"
        _pages="${HUB_API_RUN_PAGES:-3}"
        _runs=""
        _p=1
        while [ "$_p" -le "$_pages" ]; do
            _runs="$_runs
$(api "/api/v1/repos/$repo/actions/runs?limit=50&page=$_p" 2>/dev/null)"
            _p=$((_p + 1))
        done
        printf '%s' "$_runs" | REPO="$repo" SHA="$sha" SELF="$0" python3 -c '
import json, os, re, subprocess, sys

repo, sha, self_ = os.environ["REPO"], os.environ["SHA"], os.environ["SELF"]
runs = []
for chunk in sys.stdin.read().splitlines():
    if not chunk.strip():
        continue
    try:
        d = json.loads(chunk)
    except ValueError:
        continue
    runs += d.get("workflow_runs") or []
# THE SHA IS `commit_sha` ON A RUN, not `head_sha` -- that name belongs to the TASKS listing, and
# filtering a run on it silently matches nothing and reads as "no runs for this sha".
mine = [r for r in runs if r.get("commit_sha") == sha]
if not mine:
    sys.stderr.write("no runs found for %s in the pages searched -- raise HUB_API_RUN_PAGES\n"
                     % sha[:10])
    raise SystemExit(3)
failed = []
for r in mine:
    out = subprocess.run(["sh", self_, "/api/v1/repos/%s/actions/runs/%d/jobs" % (repo, r["id"])],
                         capture_output=True, text=True).stdout
    try:
        jobs = json.loads(out)
    except ValueError:
        continue
    failed += [j for j in jobs if j.get("status") == "failure"]
print("%d run(s) for %s; %d failing job(s)" % (len(mine), sha[:10], len(failed)))
if not failed:
    print("  none of them FAILED -- a red `pr checks` with no failing job is a hold or a "
          "cancellation, not a test failure.")
    raise SystemExit(0)
for j in failed:
    print("")
    print("=== %s (job %d)" % (j.get("name"), j["id"]))
    log = subprocess.run(["sh", self_, "/api/v1/repos/%s/actions/jobs/%d/logs" % (repo, j["id"])],
                         capture_output=True, text=True).stdout.splitlines()
    # THE VERDICT LINE IS AS IMPORTANT AS A TRACEBACK. A `FAILED` grep alone missed the measured cause
    # entirely: that job failed on the differential gates verdict, which carries neither the word
    # FAILED nor a traceback.
    marks = ("FAILED", "ERROR ", "verdict:", "NOT PROVEN", "assert", "Error:", "exit status")
    # A TESTS NAME IS NOT A FAILURE. The marks matched anywhere in a line, so every
    # PASSED listing and --durations line for a test named `..._assertion_...` or `..._FAILED_...`
    # printed as if it had failed: ten innocent tests on every red read on 2026-09-22. A mark counts
    # only OUTSIDE node ids and file paths, which pytest status words and tracebacks never are.
    ids = re.compile(r"\S*(?:::|\.py)\S*")
    keep = [l for l in log if any(m in ids.sub("", l) for m in marks)]
    if not keep:
        print("  (no failure marker matched -- last 25 non-empty lines)")
        keep = [l for l in log if l.strip()][-25:]
    for l in keep[-40:]:
        print("  " + (l.split("Z ", 1)[1] if "Z " in l[:32] else l))
'
        ;;
    checks)
        sha="${4:?commit sha}"
        # Refuse anything that is not a full sha BEFORE the call -- see require_full_sha for
        # what each rejected shape does instead of failing.
        require_full_sha "$sha" "pr checks"
        # `--json`: one JSON record per line -- the checks verdict, each gate-parity finding,
        # the refusal -- built by the same code as the text, with the same exit codes.
        checks_json=""; [ "${5:-}" = --json ] && checks_json=1
        # THE PRIMITIVE THIS EXISTS FOR: count the registered checks, do not read a state.
        # Measured on the forge 2026-08-12 -- a commit with no check-runs returns
        # `total_count: 0` and `state: ""` (EMPTY, not "failure"), so every natural test
        # (`state != failure`, `state == ""` treated as neutral, an exit code from a
        # wrapper) calls "nothing ran" a pass. Zero registered checks is the shape of a
        # workflow that never triggered, which is exactly when a gate must refuse.
        # Captured, not piped: `$?` after a pipeline is the LAST command's status, so piping
        # this into anything would throw away the refusal and report the pipe's success. That is
        # the same shape as the bug being fixed, one layer down.
        status_json=$(api "/api/v1/repos/$repo/commits/$sha/status")
        checks_rc=0
        printf '%s' "$status_json" | CHECKS_JSON="$checks_json" CHECKS_SHA="$sha" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
total = d.get("total_count") or 0
state = d.get("state") or ""
sts = d.get("statuses") or []
skipped = sum(1 for s in sts if s.get("status") == "skipped")
if os.environ.get("CHECKS_JSON"):
    verdict = "no_checks" if total == 0 else ("green" if state == "success" else "not_green")
    print(json.dumps({"kind": "checks", "sha": os.environ["CHECKS_SHA"], "total_count": total, "state": state,
                      "skipped": skipped, "verdict": verdict,
                      "statuses": [{"context": s.get("context"), "status": s.get("status")} for s in sts]}))
    raise SystemExit({"no_checks": 2, "green": 0}.get(verdict, 1))
for s in sts:
    print("  %-52s %s" % (s.get("context"), s.get("status")))
print("total_count=%d state=%r skipped=%d" % (total, state, skipped))
if total == 0:
    raise SystemExit(2)          # the caller discriminates WHY -- see below
if state != "success":
    print("REFUSING: state is %r, not success" % state)
    raise SystemExit(1)
# Skipped contexts count toward total_count, so a commit can be "7 checks, success" with
# most of them never having executed. Say so rather than letting the number imply cover.
#
# "ACTUALLY RAN" WAS A CLAIM THIS CANNOT SUPPORT, and it overstated in the direction
# that matters. A job can START, skip every step internally, and report success: measured on
# a real PR head, where the line said "4 actually ran" and the fourth was eslint,
# whose log reads "SKIPPED - 0 of 3 changed file(s) are TS/JS anywhere". This reads the
# COMMIT-STATUS API, which cannot see inside a job, so at this level a full suite and a job
# that skipped itself are the same object.
#
# The count is DROPPED rather than renamed, because the number itself was the overstatement --
# `total - skipped` is "not reported as skipped", which a reader takes as "ran", and any label
# short enough for this line reads that way. It is derivable for anyone who wants it. This is
# what `forge.sh` already does one layer up (`registered=N skipped=K pending=P failed=F`), and
# the claim was dropped here because that seam refused to inherit it from this verb.
print("OK: %d registered, %d skipped" % (total, skipped))
# TWO PRINTS, not one string with an escape: this python is inside a single-quoted shell heredoc,
# where a backslash-n reaches python as a literal backslash and n. Measured -- the first version
# printed the escape.
print("    registered is not RAN: a job can start, skip its steps internally and report success,")
print("    and the commit-status API cannot see inside one")' || checks_rc=$?
        # EVERY VERDICT NAMES ITS COMMIT. Without this line a verdict pasted
        # into a ticket cannot be told apart from one about a head the PR has since left behind.
        [ -n "$checks_json" ] || printf 'measured %s\n' "$sha"

        # A GATE `main` DEFINES AND THIS HEAD LACKS IS INVISIBLE IN EVERY NUMBER ABOVE.
        #
        # Forgejo reads workflow files from the PR's HEAD, not from its base. So a gate that LANDS
        # does not run on any already-open PR: the branch gates without it, silently, and reports a
        # smaller check count that reads as ordinary diff-dependent variation. Measured on one PR
        # across the boundary -- same PR, same content, only the base moved: 7 registered before the
        # rebase, 8 after, with `Hold / admission` appearing. Nothing in `state=success
        # total_count=7` said the PR was unprotected, and the pre-rebase reading is a plausible 7
        # rather than an obvious error.
        #
        # WORSE THAN A MISSING CHECK: on that branch `hold.yml` AND the six admission steps were
        # both absent, so attaching `queue:not-admitted` would have turned NOTHING red -- the whole
        # admission gate missing as a rollout boundary rather than a design flaw.
        #
        # REPORTS, NEVER REFUSES, and that is reasoned rather than timid. `block_on_outdated_branch`
        # already forces a rebase before a merge can land, so by merge time the head necessarily
        # carries `main`'s workflows; refusing here would duplicate a protection the forge enforces
        # and would break every legitimately-behind PR at the moment of merging -- the worst place
        # to discover a signature change, which is the same argument `pr merge` makes for its
        # expected-head argument being optional. The gap this closes is in the READING taken before
        # admission, which is where the silence was.
        #
        # THROUGH THE FORGE, NOT LOCAL GIT: this client has no runtime git dependency (git appears
        # only in its error text, telling a caller what to run), and a comparison that needed the
        # sha fetched locally would fail wherever the client otherwise works.
        #
        # A READ THAT FAILS SAYS SO. An unreadable listing is NOT "no difference" -- that is the
        # fail-safe-looks-like-success shape this tooling keeps re-earning, so the two are printed
        # differently.
        # THE DIRECTORY THE FORGE RUNS, AT EACH REF. This compared `.forgejo` and `.github`
        # by name, so a `.gitea`-gated repo was never compared at all, and a `.github` directory an
        # existing `.forgejo` shadows -- which never runs -- was reported as a gate the head lacked.
        # `workflow_listing` resolves each ref's own directory; when they differ, the report says so.
        _gb=$(workflow_listing "$repo" main) && _gbrc=0 || _gbrc=$?
        _gh=$(workflow_listing "$repo" "$sha") && _ghrc=0 || _ghrc=$?
        printf '%s\n' "$_gb" | GATE_HEAD="$_gh" RC_BASE="$_gbrc" RC_HEAD="$_ghrc" CHECKS_JSON="$checks_json" python3 -c '
import json, os, sys

def read(raw, rc, what):
    lines = [l for l in raw.splitlines() if l]
    if rc == "4":
        return "", set(), None
    if rc != "0":
        return (lines[0] if lines else "workflows"), None, "unreadable %s listing" % what
    return lines[0], {os.path.basename(p) for p in lines[1:]}, None

bdir, base, err_b = read(sys.stdin.read(), os.environ["RC_BASE"], "base")
hdir, head, err_h = read(os.environ.get("GATE_HEAD", ""), os.environ["RC_HEAD"], "head")
as_json = os.environ.get("CHECKS_JSON")
if err_b or err_h:
    d = (bdir if err_b else hdir) or "workflows"
    if as_json:
        print(json.dumps({"kind": "gate_parity", "dir": d, "unchecked": err_b or err_h}))
    else:
        print("  gate parity: %s for %s -- NOT checked (this is not \"no difference\")" % (err_b or err_h, d))
    raise SystemExit(0)
missing = sorted(base - head)
if not missing:
    raise SystemExit(0)
if as_json:
    rec = {"kind": "gate_parity", "dir": bdir, "missing": missing}
    if hdir != bdir:
        rec["head_dir"] = hdir
    print(json.dumps(rec))
else:
    where = bdir if hdir == bdir else "%s (this head runs %s instead)" % (bdir, hdir or "no workflow directory")
    print("  GATE PARITY: main defines %d workflow(s) in %s that this head does NOT carry: %s"
          % (len(missing), where, ", ".join(missing)))
    print("  Forgejo runs workflows from the HEAD, so these did not run here and their absence is")
    print("  invisible in the count above. Rebase onto main before trusting this reading.")
'
        if [ "$checks_rc" = "2" ]; then
            # `total_count=0` CONFLATES TWO CONDITIONS, and the old text asserted the wrong one.
            # It said "no check-runs registered for this commit", which claims the commit exists
            # -- so a typo'd sha read as the registration race, whose correct response is to WAIT.
            # The refusal was right and its reason was wrong, and the wrong reason is the
            # actionable one. `/git/commits/<sha>` separates them: rc 0 for a real commit, 22 for
            # one this repo does not have (measured, both arms).
            if [ -n "$checks_json" ]; then
                if api "/api/v1/repos/$repo/git/commits/$sha" >/dev/null 2>&1; then _why=no_check_runs; else _why=not_a_commit; fi
                printf '{"kind": "refusal", "reason": "%s", "sha": "%s"}\n' "$_why" "$sha"
            elif api "/api/v1/repos/$repo/git/commits/$sha" >/dev/null 2>&1; then
                printf 'REFUSING: %s exists but has NO check-runs registered -- not a pass. If a workflow should have fired, it did not; if it was pushed seconds ago, re-run this.\n' "$sha"
            else
                printf 'REFUSING: %s is not a commit on %s -- check the sha. This is NOT the registration race, so waiting will never change it.\n' "$sha" "$repo"
            fi
            exit 2
        fi
        [ "$checks_rc" = "0" ] || exit "$checks_rc"
        ;;
    merge)
        num="${4:?pr number}"; subject="${5:?merge subject}"
        # WHICH TREE ARE WE MERGING? -- the same question `pr create` above already refuses to
        # guess, asked at the other end of a PR's life.
        #
        # A caller that has verified green on a sha and then POSTs a merge has a GAP between the
        # two, and `main` moving inside it is not hypothetical: `pr-queue.sh`'s merge_queued()
        # carries a 405 repair arm precisely because that race is measured. Its pre-check reads
        # the head, compares, and then acts -- so it is a read-then-act, and no amount of
        # re-reading closes a window that only ends when the write lands.
        #
        # Forgejo takes `head_commit_id` on the merge endpoint (verified against a live forge, not
        # assumed: `swagger.v1.json` -> MergePullRequestOption lists it, on
        # 16.0.2+gitea-1.22.0). Sending it makes the FORGE refuse a merge whose head moved,
        # atomically, instead of this client hoping nothing changed since its last GET. That is
        # a compare-and-swap, and it is the one guarantee `pr-queue.sh`'s `flock` cannot provide:
        # the lock serialises THIS BOX, and a web-UI merge by an operator never takes it.
        #
        # OPTIONAL, AND THAT IS DELIBERATE. Making it required would break every existing caller
        # at the moment of a merge, which is the worst possible place to discover a signature
        # change. Absent, this verb behaves exactly as it did.
        #
        # THE VARIABLE SUPPLIES THE EXPECTED VALUE, IT NEVER SKIPS THE COMPARISON -- the same
        # rule `pr create` states above. There is no form of this argument that turns the check
        # off, because an override that could would be a fail-open wearing the output of a
        # verified pass.
        expect_head="${6:-${HUB_API_EXPECT_HEAD:-}}"
        head_arg=""
        if [ -n "$expect_head" ]; then
            require_full_sha "$expect_head" "pr merge (expected head)"
            head_arg="head_commit_id=$expect_head"
        fi
        # MERGE STYLE -- operator decision 2026-08-26. The DEFAULT is now
        # `fast-forward-only`: `main` lands on the PR head sha exactly, no merge commit, every
        # commit keeps the sha CI tested (measured on a protected main).
        # `block_on_outdated_branch` already forces every PR onto the current base before it can
        # merge, so a fast-forward is always possible at the moment a merge is allowed at all;
        # a diverged head answers 500 `DivergingFastForwardOnly`, which `pr-queue.sh` repairs the
        # same way it repairs a 405. What ff BUYS: sha identity for every landed commit, so every
        # git-side "did this land" query (`--is-ancestor`, `branch -d`) is sound for this era, and
        # a `--first-parent` log is the branch's own commits. What it COSTS: no merge commit, so
        # no MergeTitleField `(#N)` and no trailer -- the PR number lives on the forge's PR record
        # and the merge path becomes a LABEL (below). `(#N)` is DROPPED from main's subjects by
        # that decision; a commit-subject guard (close_ref.py's consumer) still denies a hand-written one, and under ff
        # it is more load-bearing, not less: nothing appends the real number any more, so a typed
        # one would be the only number in history with nothing to contradict it.
        #
        # `HUB_API_MERGE_STYLE=merge` keeps the two-parent form, with everything below
        # about subjects, bodies and trailers exactly as it was. Squash is refused: it was retired.
        # `manually-merged` is batch assembly's verb, not a landing style.
        merge_style="${HUB_API_MERGE_STYLE:-fast-forward-only}"
        case "$merge_style" in
            fast-forward-only|merge) ;;
            squash) die "pr merge: HUB_API_MERGE_STYLE=squash -- retired; it rewrites every sha CI tested" ;;
            *) die "pr merge: HUB_API_MERGE_STYLE '$merge_style' is not a landing style (fast-forward-only | merge)" ;;
        esac
        # Do=merge with MergeTitleField: the subject lands verbatim on the MERGE COMMIT, and
        # Forgejo's default subject is "<title> (#N)" -- the same shape GitHub uses, which is
        # why a commit-subject marker regex needs no forge-specific variant.
        #
        # WAS `Do=squash` UNTIL AN OPERATOR RULING (2026-08-27). Squash rewrote every
        # branch's commits into a new sha, which is the sole reason `git merge-base --is-ancestor`,
        # `branch -d`, `branch --contains` and `main..branch` all answer "not landed" about landed
        # work -- and therefore the sole reason `prune-landed-branches-forgejo.sh` must interrogate
        # the forge at all. Merge commits preserve the exact shas CI tested. The cost is +1 commit
        # per PR (~+20% on a 1238-commit history at the time of the change), which `--no-merges`
        # hides and `--first-parent` turns into one entry per PR.
        #
        # THE NUMBER IS DERIVED HERE, because nothing downstream can add it and nothing
        # upstream can repair it. Sending MergeTitleField means Forgejo's default
        # "<title> (#N)" never applies, so the number exists only if the caller typed it --
        # and `main` refuses force push for admins too, so a subject that lands without one
        # is permanent. There is a measured instance of exactly that. This verb already
        # has the right answer in argv, so it appends rather than trusting the caller.
        #
        # A trailing ref for a DIFFERENT PR is refused, not rewritten: it is a claim about
        # another PR, and only the caller knows whether the wrong half is the number or the
        # subject -- guessing would land a commit pointing at the wrong PR, which is exactly
        # as permanent as the missing number. Refusing costs a retry; there is no commit yet.
        if [ "$merge_style" = merge ]; then
        ref="${subject##*"(#"}"; ref="${ref%")"}"   # digits of a trailing (#N), else junk
        case "$subject" in
        *"(#$num)") ;;                              # already correct -- do not double it
        *) case "$ref" in
           ''|*[!0-9]*) subject="$subject (#$num)" ;;
           *) die "merge subject ends in (#$ref) but this is PR #$num -- fix the subject" ;;
           esac ;;
        esac
        fi
        # PRESERVE THE BODY. Under `Do=squash` this was load-bearing against DATA LOSS:
        # squash with only MergeTitleField produced an EMPTY body, so `Closes #N`,
        # `Co-Authored-By:` and every line of reasoning were DELETED at merge -- silently, since
        # nothing fails and only a diff against the pre-merge commit shows it. Measured on the last
        # 25 commits of main: every `parents=1` squash had a 0-byte body, while the `parents=2`
        # merge-commit era kept 491-3180 B beneath it.
        #
        # UNDER `Do=merge` THE STAKES CHANGE AND THE CODE DOES NOT. The branch's own commits
        # survive as parents carrying their own full messages, so the reasoning can no longer be
        # lost here. What MergeMessageField now buys is the `--first-parent` view: that log shows
        # merge commits ONLY, so a merge commit with an empty body would render the one-entry-per-PR
        # history unreadable while the detail sat one parent away. Keep sending it.
        #
        # The cost is that the body appears twice in a full `git log` -- once on the merge commit,
        # once on the commit beneath it. That is the deliberate trade: duplication in the verbose
        # view, legibility in the summary view.
        #
        # FAILS OPEN TO THE OLD BEHAVIOUR, deliberately: an unreachable endpoint, unparseable JSON
        # or an empty result yields an empty $body and the payload without MergeMessageField -- the
        # exact request sent before. A merge must never be blocked because the prose could not be
        # fetched; losing the body again is the old bug, refusing the merge would be a new one.
        body=""
        [ "$merge_style" = merge ] && body="$(api "/api/v1/repos/$repo/pulls/$num/commits" 2>/dev/null | python3 -c '
import json, sys
try:
    commits = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
if not isinstance(commits, list) or not commits:
    raise SystemExit(0)
parts = []
for c in commits:
    msg = ((c.get("commit") or {}).get("message") or "").strip()
    if not msg:
        continue
    if len(commits) == 1:
        # MergeTitleField already carries the subject, so keep only the body or it lands twice.
        parts.append(msg.split("\n", 1)[1].strip() if "\n" in msg else "")
    else:
        # Several commits: keep each subject, the way a default squash message reads.
        parts.append("* " + msg)
sys.stdout.write("\n\n".join(p for p in parts if p))
' 2>/dev/null || true)"
        # RECORD WHICH PATH THIS MERGE TOOK, because it cannot be recovered afterwards.
        #
        # `merge_queued` carries guarantees a merge outside it does not: the review hold, branch
        # auto-retire, hold-labelling, and the `head_commit_id` CAS. Detecting merges that
        # skipped them is wanted, and BOTH candidate discriminators fail:
        #
        #   subject form   `<title> (#N)` is byte-identical for a queue merge and a hand-called
        #                  `pr merge` -- it HAS to be, since the merge convention requires the
        #                  `(#N)` for any call of this verb. Measured on a real PR.
        #   branch gone    conflates "the queue retired it" with "a human tidied up an hour
        #                  later", and decays toward "retired" for every path as cleanup happens.
        #                  Measured on a PR merged in the web UI and retired by hand.
        #
        # So the answer is to write it down at the moment it is knowable. A trailer on the merge
        # commit is immutable, survives every later cleanup, and is the only option that makes
        # the original question answerable at all.
        #
        # THREE PATHS, DISTINGUISHABLE, and the fourth reports as unknown rather than being
        # silently bucketed -- which is what has to be proven:
        #
        #   Merge-Path: pr-queue        `merge_queued` set it
        #   Merge-Path: hub-api-direct  a hand-called `pr merge` -- this default
        #   no trailer, web-UI subject  Forgejo's template, unambiguous
        #   no trailer, no template     unknown (a pushed merge commit, or history older than the trailer)
        #
        # ponytail: an env var is not a credential. A caller that copies `merge_queued`'s command
        # line stamps `pr-queue` on a merge the queue never made, and nothing here detects that.
        # The ceiling is deliberate -- this records INTENT for an honest caller, which is what
        # detection needs; forgery would need the forge to write the trailer, and it will not.
        # The upgrade path is a queue-only credential, not a better string.
        merge_path="${HUB_API_MERGE_PATH:-hub-api-direct}"
        case "$merge_path" in
            *[!a-z0-9-]*) die "pr merge: HUB_API_MERGE_PATH '$merge_path' is not [a-z0-9-]+" ;;
        esac
        # Blank line first when there is a body, so the trailer is a trailer and not the last
        # paragraph's final line -- `git interpret-trailers` and every reader keys on that.
        if [ "$merge_style" != merge ]; then
            body=""                                  # no merge commit; the record is a label, below
        elif [ -n "$body" ]; then
            body="$body

Merge-Path: $merge_path"
        else
            body="Merge-Path: $merge_path"
        fi
        # `$head_arg` is UNQUOTED on purpose, and it is safe for a reason rather than by luck:
        # empty it expands to no argument at all, and non-empty it is `head_commit_id=<sha>`
        # where the sha has already been through require_full_sha, which admits only 40 hex
        # characters. There is no input to this expansion that can contain a space.
        # shellcheck disable=SC2086
        if [ "$merge_style" = fast-forward-only ]; then
            payload="$(json_obj "Do=fast-forward-only" $head_arg)"
        elif [ -n "$body" ]; then
            payload="$(json_obj "Do=merge" "MergeTitleField=$subject" "MergeMessageField=$body" $head_arg)"
        else
            payload="$(json_obj "Do=merge" "MergeTitleField=$subject" $head_arg)"
        fi
        # THE FORGE'S REASON IS PRINTED, NOT JUST ITS STATUS CODE.
        #
        # This was `-o /dev/null -w 'merged: http=%{http_code}\n'`, so every refusal arrived as
        # three digits and the sentence explaining it went to /dev/null. **405 is returned for
        # several distinct refusals that need OPPOSITE responses** -- `block_on_outdated_branch`
        # means update the branch and retry, failed required checks means do NOT update and go
        # read the log -- and the code alone separates none of them. Measured on a real PR: recovering
        # "outdated branch" took a separate `branch_protections` read, and the CI-gate doc carried a
        # paragraph telling readers not to debug the token or the subject, which is a workaround
        # written because the message was missing. A dependency-blocked merge answers 500 the same
        # way, so this is the drain's most common expected refusal, not an edge case.
        #
        # `merged: http=<code>` IS KEPT VERBATIM AND ON ITS OWN LINE. `forge.sh pr land` parses it
        # (`sed -n 's/.*merged: http=\([0-9]*\).*/\1/p'`) and the seam tests assert on the exact
        # string; the reason is ADDED beside it rather than replacing it.
        #
        # THE BODY IS PRINTED ONLY ON A NON-2xx. On success it is the whole merge object, which
        # would bury the one line callers read.
        #
        # STDERR IS NOT FOLDED IN, and that is a decision rather than an omission. Written with
        # `2>&1` the capture is never empty -- curl's own `curl: (22) The requested URL returned
        # error: 405` lands in it -- so "the forge sent no body" becomes undetectable and curl's
        # diagnosis gets reported as though the forge had said it. Left on stderr, curl still
        # reaches the operator exactly as before and the body below is only ever the forge's.
        #
        # FAILS OPEN ON THE REPORTING PATH, deliberately, and this is the currency guard's lesson applied:
        # failing closed is safe only where the closed state is rare. A merge withheld because its
        # own ERROR TEXT would not parse is a worse bug than the one being fixed, so every step
        # below degrades to printing something -- unparseable JSON prints raw, an empty body prints
        # a sentence saying the forge sent none. The exit status is curl's, unchanged.
        #
        # THE QUEUE FREEZE REACHES EVERY CLIENT MERGE (operator ruling, 2026-09-11): while the wiki page "Queue Freeze" exists, this verb
        # refuses unless HUB_API_FREEZE_OVERRIDE names a reason, which it then prints. `pr-queue.sh`
        # refuses earlier and prints the page's text; this is the check a hand merge cannot walk
        # around, and every queue merge passes through it too, at the last moment before the POST.
        # A web-UI merge never reaches this client, so nothing here can stop one.
        #
        # THREE OUTCOMES, as pr-queue.sh's freeze block states: an unread listing is not an empty
        # one, so it refuses as CANNOT TELL rather than reading as "not frozen".
        #
        # PAGED UNTIL AN EMPTY PAGE, not a short one: the listing's per-page cap is unmeasured
        # (handoff.sh _all_titles), so a page shorter than asked for proves nothing about the next.
        #
        # A WIKI NEVER INITIALISED IS AN EMPTY ONE. Measured 2026-09-15 on a fresh fork:
        # `has_wiki: true`, no page ever written, and the listing answers
        # HTTP 404 `{"message":"The target couldn't be found.",...,"errors":["no such file or
        # directory"]}` -- so every new repo was unmergeable here. EXACTLY that failure (HTTP 404 +
        # that message + that error) reads as absent; any other 404, a 500, or unparseable still
        # refuses. pr-queue.sh `_freeze_sub` carries the same predicate -- change both together.
        freeze_state=$(
            page=1
            while [ "$page" -le 100 ]; do
                listing=$(api "/api/v1/repos/$repo/wiki/pages?limit=50&page=$page" -w '\n%{http_code}' 2>/dev/null) && _fz_rc=0 || _fz_rc=$?
                verdict=$(printf '%s' "$listing" | python3 -c '
import json, re, sys
raw = sys.stdin.read()
body, _, code = raw.rpartition("\n")
if not re.fullmatch(r"\d{3}", code):
    body, code = raw, ("200" if sys.argv[2] == "0" else "")   # a client that printed no status line
try:
    pages = json.loads(body)
except ValueError:
    print("cannot-tell")
    raise SystemExit
if not code.startswith("2"):
    uninit = code == "404" and isinstance(pages, dict) and pages.get("message") == "The target couldn'"'"'t be found." \
        and "no such file or directory" in (pages.get("errors") or [])
    print("absent" if uninit else "cannot-tell")
elif not isinstance(pages, list):
    print("cannot-tell")
elif any(isinstance(p, dict) and p.get("title") == sys.argv[1] for p in pages):
    print("frozen")
elif not pages:
    print("absent")
else:
    print("more")
' "Queue Freeze" "$_fz_rc")
                case "$verdict" in
                    more) page=$((page + 1)) ;;
                    frozen|absent) echo "$verdict"; exit 0 ;;
                    *) echo cannot-tell; exit 0 ;;
                esac
            done
            echo cannot-tell
        )
        if [ "$freeze_state" != absent ]; then
            if [ -n "${HUB_API_FREEZE_OVERRIDE:-}" ]; then
                printf '%s\n' "hub-api: FREEZE OVERRIDDEN -- the queue freeze on $repo reads '$freeze_state', and pr merge #$num proceeds because HUB_API_FREEZE_OVERRIDE says: $HUB_API_FREEZE_OVERRIDE" >&2
            elif [ "$freeze_state" = frozen ]; then
                refuse "pr merge #$num -- the queue is FROZEN: wiki page \"Queue Freeze\" exists on $repo. Lift it with pr-queue thaw, or merge deliberately with HUB_API_FREEZE_OVERRIDE=\"<reason>\"."
            else
                refuse "pr merge #$num -- cannot tell whether the queue is frozen: the wiki listing of $repo could not be read, and an unread listing is not an empty one. HUB_API_FREEZE_OVERRIDE=\"<reason>\" merges anyway."
            fi
        fi
        merge_out=$(api "/api/v1/repos/$repo/pulls/$num/merge" -X POST \
            -H "Content-Type: application/json" -d "$payload" \
            -w '\nmerged: http=%{http_code}\n') && merge_rc=0 || merge_rc=$?
        # `sed`, not `grep`: on this box `grep` is a shell function over ugrep that silently drops
        # matching lines, and a dropped status line here would read as a merge that said
        # nothing.
        merge_status=$(printf '%s\n' "$merge_out" | sed -n 's/^\(merged: http=[0-9][0-9]*\)$/\1/p' | tail -n 1)
        printf '%s\n' "${merge_status:-merged: http=unknown}"
        case "$merge_status" in
        "merged: http=2"*)
            # THE RECORD MOVES TO A LABEL under fast-forward: there is no merge commit
            # to carry the `Merge-Path:` trailer, and the PR object is the only
            # durable thing a fast-forward leaves behind. Same three values, same ceiling (intent,
            # not proof -- a label is editable), same question answered: did this merge take the
            # queue's path. Applied AFTER the merge, so a labelling failure can never withhold a
            # merge; and read back, because a label that does not stick is a known failure shape.
            # `issue tag` is the bare applier (create-then-attach) and prints the label set.
            if [ "$merge_style" = fast-forward-only ]; then
                _lname="merge-path:$merge_path"
                _lpath="/api/v1/repos/$repo/issues/$num/labels"
                _has_label() {   # the readback: is $_lname on the PR now?
                    api "$_lpath" 2>/dev/null | python3 -c '
import json, sys
want = sys.argv[1]
try:
    ls = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
sys.exit(0 if isinstance(ls, list) and any(l.get("name") == want for l in ls) else 1)
' "$_lname"
                }
                # Attach by name (works only for a label that exists); if the readback lacks it,
                # create the label once and attach again. Then read back and REPORT, never assume.
                api "$_lpath" -X POST -H "Content-Type: application/json" -d "{\"labels\":[\"$_lname\"]}" >/dev/null 2>&1 || true
                if ! _has_label; then
                    api "/api/v1/repos/$repo/labels" -X POST -H "Content-Type: application/json" \
                        -d "{\"name\":\"$_lname\",\"color\":\"#ededed\",\"description\":\"which path landed this PR\"}" >/dev/null 2>&1 || true
                    api "$_lpath" -X POST -H "Content-Type: application/json" -d "{\"labels\":[\"$_lname\"]}" >/dev/null 2>&1 || true
                fi
                if _has_label; then
                    printf 'merge-path: %s (label on #%s)\n' "$merge_path" "$num"
                else
                    printf 'merge-path: label %s did NOT stick on #%s -- the merge stands, the record does not\n' "$_lname" "$num"
                fi
            fi ;;
        *)
            printf '%s\n' "$merge_out" | sed '/^merged: http=[0-9][0-9]*$/d' | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    # A refusal with no body is itself worth saying: it distinguishes "the forge explained
    # itself and we dropped it" from "the forge said nothing", which are different bugs.
    print("reason: the forge sent no body with this status")
    raise SystemExit(0)
try:
    d = json.loads(raw)
except Exception:
    d = None
msg = None
if isinstance(d, dict):
    msg = d.get("message") or d.get("error") or d.get("errors")
print("reason: %s" % (msg if msg else raw[:2000]))
' 2>/dev/null || printf 'reason: (unreadable body, printed raw)\n%s\n' "$merge_out"
            ;;
        esac
        [ "$merge_rc" -eq 0 ] || exit "$merge_rc"
        ;;
    body)
        # ONE WAY TO WRITE A PR BODY -- operator, 2026-09-23: "create a verb so that writing a PR body
        # is consistent". Before this the only repair was `issue body-edit`, an exactly-once region
        # replacement that can fill an EMPTY body and nothing else, and every other edit was a
        # hand-typed PATCH. The trigger was four PRs in one evening opened
        # --title-only because "the record lives in the commit message": true, and the PR a reviewer
        # opens still read as empty. --from-commits makes those commit messages the body, so the two
        # records are one. The body argument is read by resolve_pr_body, the same function as
        # `pr create`, so the two verbs agree on what a body is. READ BACK, and refuse on a mismatch:
        # a write answering 2xx is a request accepted, not a body held.
        num="${4:?usage: $0 pr body <owner/repo> <number> <text|@file:path|--from-commits>}"
        case "$num" in ''|*[!0-9]*) die "pr body: '$num' is not a PR number" ;; esac
        [ $# -ge 5 ] || refuse "pr body needs the body: <text>, @file:<path>, or --from-commits"
        resolve_pr_body "$5"
        src="the argument"
        case "$body" in
            --title-only) refuse "pr body: --title-only is how a PR is OPENED without a body; there is no body in it to write" ;;
            --from-commits)
                # The forge lists a PR's commits NEWEST first (measured); a body reads
                # oldest first. Trailers (Co-Authored-By and the like) are dropped: they attribute,
                # they do not explain. One commit: its body. Several: one section per commit.
                # ponytail: one page of 100 commits; a PR that large is refused, not truncated.
                body=$(api "/api/v1/repos/$repo/pulls/$num/commits?limit=100" | python3 -c '
import json, re, sys
cs = json.load(sys.stdin)
if len(cs) >= 100:
    sys.exit("more than one page of commits; write this body by hand")
parts = []
for c in reversed(cs):
    lines = c["commit"]["message"].strip().splitlines()
    rest = [l for l in lines[1:] if not re.match(r"^[A-Z][A-Za-z-]+: .*<[^>]*@[^>]*>\s*$", l)]
    text = "\n".join(rest).strip()
    parts.append((lines[0] if lines else "", text))
if not any(t for _, t in parts):
    sys.exit("no commit on this PR carries a message body; there is nothing to build one from")
if len(parts) == 1:
    print(parts[0][1])
else:
    print("\n\n".join("### %s\n\n%s" % (s, t) if t else "### %s" % s for s, t in parts))') \
                    || refuse "pr body: could not build #$num's body from its commits (reason above). Nothing was written."
                src="its $(api "/api/v1/repos/$repo/pulls/$num/commits?limit=100" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') commit message(s)" ;;
        esac
        api "/api/v1/repos/$repo/pulls/$num" -X PATCH -H "Content-Type: application/json" \
            -d "$(json_obj "body=$body")" >/dev/null \
            || die "pr body: the forge refused to write #$num's body (its reason is above)"
        got=$(api "/api/v1/repos/$repo/pulls/$num" | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin).get("body") or "")') \
            || die "pr body: #$num was written but could not be read back; check it before trusting it"
        [ "$got" = "$(printf '%s' "$body")" ] \
            || die "pr body: #$num's body did NOT stick: wrote ${#body} chars, read back ${#got}"
        printf '%s\n' "pr body: #$num now carries a ${#got}-character body from $src (read back identical)."
        ;;
    hold)
        # ONE OWNER FOR THE STRING THE QUEUE MATCHES.
        #
        # The hold is the opt-out from an automated merge, so its whole value is that it works when
        # nobody is watching. Before this verb existed the documented path was `issue label`, which
        # prefixes `wayfinder:` and therefore produced a hold the drain does not match -- and
        # `ensure_label` CREATED it, so the repo carried plausible evidence of a hold that never
        # held. `issue tag` applies the name bare and is correct, which is not what either name
        # suggests and was written down nowhere.
        #
        # The default here is the same literal `pr-queue.sh` defaults `HELD_LABEL` to, and both read
        # `PR_QUEUE_HELD_LABEL`, so an override moves them together. `scripts/tests/test_late_hold_is_honoured.py`
        # asserts the two files agree -- a shared constant enforced by a test rather than by a
        # config module neither script would otherwise need.
        num="${4:?usage: $0 pr hold <owner/repo> <number>}"
        case "$num" in ''|*[!0-9]*) die "pr hold: '$num' is not a PR number" ;; esac
        held="${PR_QUEUE_HELD_LABEL:-queue:needs-human-review}"
        # `issue tag` is the bare applier and already creates-then-attaches; recursing into it
        # keeps one code path rather than a second copy of ensure_label out here.
        out=$("$0" issue tag "$repo" "$num" "$held") || die "pr hold: could not apply '$held' to #$num"
        # READ BACK AND REFUSE, rather than telling readers to check readbacks. The mismatch this
        # verb exists for was caught exactly once, by an operator who happened to compare the
        # readback against the string passed in; a hold that reports success without sticking
        # reproduces the original defect through a new verb.
        case "$out" in
            *"$held"*) : ;;
            *) die "pr hold: '$held' did NOT stick on #$num -- the forge reported: $out" ;;
        esac
        printf '%s\n' "$out"
        printf '%s\n' "held #$num with '$held'.

  A HOLD IS HONOURED AT READ TIME AND CANNOT STOP A MERGE ALREADY IN FLIGHT.
  The queue reads the hold immediately before it POSTs the merge, so applying one before that
  read stops the merge; applying one after it does not, and the remaining window is the latency
  of the POST itself. That window is narrow and it is NOT zero -- nothing here closes it, because
  only a precondition the forge evaluates atomically could, and Forgejo offers none.

  So this is reliable against a PR waiting its turn or sitting in CI, and it is NOT a way to
  recall a merge you have just watched start. If the queue is mid-merge on this PR, say so to
  whoever is running it -- that is the only mechanism that beats the window."
        ;;
    unhold)
        # THE OTHER HALF OF THE BRAKE. `hold` existed
        # and nothing lifted it: the label means `queue:needs-human-review`, so its whole lifecycle
        # is apply, get a ruling, resume, and only the first third had a verb. The three ways out
        # were merging past it (leaving a merged PR carrying a false needs-human-review artifact),
        # the web UI (needs a human, which defeats holding it from a session), or leaving it held.
        #
        # SCOPE, STATED BECAUSE THE NAME OVERPROMISES: this is HYGIENE, not a safety mechanism.
        # `pr merge` does not read the hold at all -- only `pr-queue.sh`'s drain does -- so lifting
        # one does not make a merge safe and holding one does not stop `pr merge`. Whether
        # `merge-requested` should honour a hold is NOT decided here.
        num="${4:?usage: $0 pr unhold <owner/repo> <number>}"
        case "$num" in ''|*[!0-9]*) die "pr unhold: '$num' is not a PR number" ;; esac
        held="${PR_QUEUE_HELD_LABEL:-queue:needs-human-review}"

        # A NO-OP MUST SAY SO. Without this the verb cannot tell "I lifted it" from "there was
        # nothing there", and those are exactly the two a caller needs to separate: the first
        # means the queue will now admit this PR, the second means someone's model of the queue is
        # wrong. Measured BEFORE the delete rather than inferred from its status, because Forgejo
        # answers 204 for removing a label that was not attached.
        pre=$("$0" "/api/v1/repos/$repo/issues/$num/labels") \
            || die "pr unhold: could not read the labels on #$num"
        if ! printf '%s' "$pre" | grep -qF "\"$held\""; then
            printf 'pr unhold: #%s is NOT HELD -- no %s label to remove, so nothing was lifted.\n' \
                "$num" "$held"
            printf '  This is the desired state, not a success report: if you expected a hold here,\n'
            printf '  the hold you are thinking of was never applied or was already cleared.\n'
            exit 0
        fi

        # NAME, NOT ID, AND THAT IS MEASURED RATHER THAN ASSUMED. The request was that this endpoint takes a
        # label id and to reuse `issue label-id`. Checked against the forge's swagger.v1.json: `/repos/{owner}/{repo}/issues/{index}/labels/{identifier}` documents
        # identifier as "name or id of the label to remove". So the lookup is unnecessary here, and
        # skipping it removes a failure mode -- `label-id` REFUSES when the name is unknown, which
        # would turn "already unheld on a repo that never defined the label" into a hard error.
        # PATH FIRST, THEN CURL ARGS: the passthrough is `hub_curl ... "$HUB_URL$path" "$@"`, so a
        # leading `-X DELETE` is read as the path and curl answers "No host part in the URL".
        "$0" "/api/v1/repos/$repo/issues/$num/labels/$held" -X DELETE >/dev/null \
            || die "pr unhold: the forge refused to remove '$held' from #$num"

        # READ BACK AND REFUSE, symmetric with `hold` and for the mirrored reason. An unhold that
        # reports success while the label is still attached leaves a PR the drain keeps skipping,
        # and the seat that ran it stops watching BECAUSE it ran it.
        post=$("$0" "/api/v1/repos/$repo/issues/$num/labels") \
            || die "pr unhold: removed '$held' from #$num but could not read the labels back"
        if printf '%s' "$post" | grep -qF "\"$held\""; then
            die "pr unhold: '$held' is STILL on #$num after the removal -- the forge accepted the
  request and did not apply it. Do not treat this PR as released."
        fi
        printf 'unheld #%s -- %s removed and confirmed gone.\n' "$num" "$held"
        printf '  The drain will now admit this PR when its turn comes. This did NOT merge it, and\n'
        printf '  it changes nothing about `pr merge`, which never read the hold.\n'
        ;;
    *) die "unknown pr subcommand '$sub' (create|body|checks|why-red|await|merge|log|hold|unhold)" ;;
    esac
    ;;

issue)
    # Wayfinder operations plus generic maintainer verbs (list/file/tag/note/close).
    #
    # Forgejo has NO sub-issues endpoint -- measured 2026-08-12: zero swagger paths matching
    # sub_issue, and /issues/1/sub_issues answers 404 where a bogus sibling path answers the
    # same, so the absence is an absence. Parenthood is therefore body-encoded: the child
    # body opens with `Part of #<map>`, and the MAP body carries the child numbers between
    # HTML-comment markers. The markers are what makes the list machine-rewritable while a
    # human edits prose around it; the ORDER of that list is the only place map order lives,
    # and "first in map order wins" is the whole frontier rule.
    #
    #   hub-api.sh issue map-create   <owner/repo> <title> [body]
    #   hub-api.sh issue child-create <owner/repo> <map#> <title> [body] [type]
    #   hub-api.sh issue adopt        <owner/repo> <n> <map#> [type] --because <why this map>
    #   hub-api.sh issue label        <owner/repo> <n> <type>
    #   hub-api.sh issue block        <owner/repo> <blocked#> <blocker#>
    #   hub-api.sh issue unblock      <owner/repo> <blocked#> <blocker#>   (remove that edge)
    #   hub-api.sh issue frontier     <owner/repo> <map#>
    #   hub-api.sh issue blockers     <owner/repo> <n>
    #   hub-api.sh issue claim        <owner/repo> <n> [user] [--take-over]   refuses a LIVE claim
    #   hub-api.sh issue unclaim      <owner/repo> <n> [user]   leaves a claim, read back
    #   hub-api.sh issue claims       <owner/repo> <session>    open issues whose newest claim-session is it
    #   hub-api.sh issue resolve      <owner/repo> <n> <map#> <comment> [--take-over]   refuses a LIVE claim
    #                                 (refuses a comment that says the work is incomplete and
    #                                 names no follow-up ticket; RESOLVE_PARTIAL_OK=1)
    #   hub-api.sh issue body-edit    <owner/repo> <n> <old-text-file> <new-text-file>
    #   hub-api.sh issue zoom         <owner/repo> <n>
    #   hub-api.sh issue list         <owner/repo> [label] [open|closed|all]
    #   hub-api.sh issue file         <owner/repo> --no-map <title> [body]
    #                                 (the marker is required, because a map-less ticket
    #                                 is legitimate but must be stated, not defaulted)
    #   hub-api.sh issue tag          <owner/repo> <n> <label>
    #   hub-api.sh issue note         <owner/repo> <n> <comment>
    #   hub-api.sh issue close        <owner/repo> <n> [comment]
    require_config
    shift
    [ $# -ge 2 ] || die "usage: $0 issue <verb> <owner/repo> ... (see the issue) case)"

    # python does the work because seven of the nine verbs are read-modify-write over JSON.
    # It never reads $CFG: the credential stays curl's, exactly as everywhere else here.
    py=$(cat <<'PYEOF'
import fcntl, json, os, re, shutil, subprocess, sys, urllib.parse

URL, CFG, ME = sys.argv[1], sys.argv[2], sys.argv[3]
verb, repo, args = sys.argv[4], sys.argv[5], sys.argv[6:]
# `--json`: the verdict as one JSON object on stdout, built by the same code that prints
# the text. Only the read verbs take it -- anywhere else `--json` stays an ordinary argument, since a
# note or a title may legitimately say it. A failure under it is an `{"error": ...}` object with the
# same non-zero exit, so an unreadable listing can never serialise as an empty one.
JSON = verb in ("frontier", "blockers") and "--json" in args
if JSON:
    args = [a for a in args if a != "--json"]

# A child carrying this is OPEN ON PURPOSE and is NOT work: it records something that will matter
# if a decision or a setting changes, and until then there is nothing to do. It is not `wontfix`
# (that is a refusal) and not a blocker (nothing blocks it).
LATENT_LABEL = "wayfinder:latent"
# A child carrying this WAITS ON THE OPERATOR, operator 2026-09-10. It is skipped whatever its
# claim says. It exists because a claim whose holding session has ENDED is now takeable, and some claims
# mean "the agent's part is done, a human's is not" -- two measured tickets were exactly that. Without the label
# the liveness rule would hand an agent a ticket it cannot finish.
WAITING_LABEL = "wayfinder:waiting-operator"
OWNER, NAME = (repo.split("/", 1) + [""])[:2]


def bail(msg):
    if JSON:
        print(json.dumps({"error": msg}))
    print("hub-api issue: " + msg, file=sys.stderr)
    raise SystemExit(1)


def die(msg):
    """Every state this verb cannot DETERMINE ends here, exit 2.

    The frontier's failure mode is a wrong answer that looks authoritative: an unreachable
    dependency call is not "no blockers", and a child that fell out of the listing is not
    "closed". Both would silently hand a caller a takeable ticket.
    """
    if JSON:
        print(json.dumps({"error": msg}))
    print("REFUSING: " + msg, file=sys.stderr)
    raise SystemExit(2)


def text_arg(v, what):
    """Refuse a bare flag where prose goes, and why this is not only in `pr create`.

    That arm catches 8 of the 15 writes lost this way; the other 7 came through `issue note`
    (four here and three on another repo), which has the identical shape: a
    positional body, a `gh` habit spelling it `-F <file>`, and a success line either way. One of
    the lost ones was a "merging this takes the live site down" analysis that existed as two
    characters on the PR while being reported as posted.

    A flag has no whitespace; prose usually does. Anything containing whitespace is taken as-is,
    and only a hyphen-led argument with none is refused. CEILING: a one-word body starting with a
    hyphen is refused too -- implausible here, and the message names `@file:` for it.
    """
    if v and v[0] == "-" and not any(c.isspace() for c in v):
        bail("'%s' is a flag where the %s goes.\n"
             "  This client takes it POSITIONALLY and has no -F/--body-file; that is the `gh`\n"
             "  spelling, and it posts the flag as your text and drops the filename after it.\n"
             "  Measured 2026-09-09: 15 writes on this forge landed with a body of '-F'.\n"
             "      @file:<path>   the file's contents become the argument" % (v, what))
    return v


def api(path, method="GET", data=None):
    # `type=` is MANDATORY and is a FALSE FRIEND: it enums to issues|pulls, NOT to issue
    # types. Unfiltered, hub's own repo lists 3 items, all of them pull requests and none of
    # them an issue -- so a frontier computed without it is computed over the wrong objects.
    if method == "GET" and re.search(r"/issues(\?|$)", path) and "type=issues" not in path:
        die("list call without type=issues would count pull requests: " + path)
    if not URL:   # the shell's `ft_need FORGE_URL`, said the same way
        sys.stderr.write("forge-tools: FORGE_TOOLS_FORGE_URL is not set -- the forge's base URL, e.g. "
                         "https://forge.example.org (HUB_URL overrides it).\n  It has no default. Set it in the "
                         "environment or in %s (see config.example).\n" % os.environ.get("FT_CONFIG_FILE", "the config file"))
        raise SystemExit(1)
    cmd = ["curl", "-sS", "--config", CFG, "-w", "\n%{http_code}", "-X", method, URL + path]
    # THE ACCESS PAIR, the same as hub_curl() sends. Without it every issue verb from a box that
    # reaches the forge through Cloudflare Access got the login page: a 302 on each write, while
    # the passthrough's GETs, which go through hub_curl(), worked (measured 2026-09-11, the day
    # a box's direct path to the forge went away).
    acc = os.environ.get("HUB_ACCESS_CONFIG", "")
    if acc:
        if not os.path.isfile(acc):
            die("HUB_ACCESS_CONFIG names %s, which does not exist" % acc)
        if oct(os.stat(acc).st_mode & 0o777) != "0o600":
            die("%s is mode %s, refusing to use it (want 600)" % (acc, oct(os.stat(acc).st_mode & 0o777)[2:]))
        cmd[1:1] = ["--config", acc]
    if os.environ.get("HUB_CURL_INSECURE") == "1":
        cmd[1:1] = ["-k"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(data)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        die("curl exit %d on %s %s: %s" % (p.returncode, method, path, p.stderr.strip()[:200]))
    body, _, status = p.stdout.rpartition("\n")
    if not 200 <= (int(status) if status.strip().isdigit() else 0) < 300:
        die("%s %s -> HTTP %s: %s" % (method, path, status.strip(), body.strip()[:200]))
    if not body.strip():
        return None
    try:
        out = json.loads(body)
    except ValueError:
        die("%s %s returned non-JSON: %s" % (method, path, body.strip()[:200]))
    # REQUIRE_SIGNIN_VIEW answers an unauthenticated call with HTTP 200 and a message body,
    # which every natural test reads as data.
    if isinstance(out, dict) and out.get("message") and set(out) <= {"message", "url", "errors"}:
        die("the forge answered with a message, not data: " + str(out["message"])[:200])
    return out


def issue(n):
    return api("/api/v1/repos/%s/issues/%d" % (repo, n))


def issue_num(s, what):
    if not re.fullmatch(r"#?\d+", s or ""):
        bail("%s must be an issue number, got %r" % (what, s))
    return int(s.lstrip("#"))


# Forgejo's per-request ceiling for a listing. Named once because `paged` terminates on
# "this page was short", which is only a valid stop condition while the request asked for
# exactly this many.
PAGE = 50


def paged(path):
    """Every item on `path`, not the first page. OWNS `limit` and `page` -- do not pass either.

    A TRUNCATED LIST AND A COMPLETE ONE ARE THE SAME OBJECT. Nothing in the response
    says which you are holding, so a capped read reports a clean subset as the whole set.
    Measured 2026-08-25: a labelling sweep over this repo read 50 of 122 issues and called it
    the repo, and was caught only because a child known to be absent from the result was known
    to exist. That is not a check -- that is luck standing in for one.

    `limit` is appended HERE rather than trusted from the caller, because the stop condition
    below is `len(batch) < PAGE`: a caller that passed a different limit, or none, would make
    that comparison silently wrong in one direction or the other.
    """
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while True:
        batch = api("%s%slimit=%d&page=%d" % (path, sep, PAGE, page)) or []
        out += batch
        if len(batch) < PAGE:
            return out
        page += 1
        if page > 40:
            die("listing did not terminate after %d items: %s" % (40 * PAGE, path))


def list_issues(state):
    return paged("/api/v1/repos/%s/issues?type=issues&state=%s" % (repo, state))


def known_labels(scope=None):
    """{name: id} for a repo's labels. THE ONE FETCH.

    Six call sites independently wrote `/labels?limit=100` before this existed, in three files
    and two languages, because each author hit Forgejo's label fail-open separately and wrote
    their own guard. That is what a codebase looks like when the knowledge lives in people
    rather than in a function -- and the third site was written AFTER the first two guards
    existed and still shipped without one.

    THE POLICY IS DELIBERATELY NOT HERE, and that is the design rather than an omission. The
    callers want three different things from the same fetch and all three are correct:

      * REFUSE          `issue list`, `label-id`, `pr-queue merge-requested` -- an unresolvable
                        name makes Forgejo return the WHOLE repo, so a filter that cannot be
                        verified must not run.
      * FAIL SOFT       `pr-queue`'s hold labelling -- a labelling failure must never block a
                        merge, so it logs and continues (see that file's :364-403).
      * CREATE          `ensure_label` -- the wayfinder verbs create a label the repo lacks.

    A helper that picked one would have to be overridden at two thirds of its call sites, which
    is how a shared guard becomes a shared workaround. So this answers "what exists", and each
    caller keeps its own verdict.

    ponytail: one page. A repo with >100 labels needs paging; hub's have single digits.
    """
    return {l["name"]: l["id"]
            for l in (api("/api/v1/repos/%s/labels?limit=100" % (scope or repo)) or [])
            if l.get("name")}


def ensure_label(name):
    """create-label -> resolve-id -> create-issue.

    CreateIssueOption.labels takes label IDs (int64) and EditIssueOption has no labels field
    at all, so there is no `--label <name>` shortcut here the way gh has one.
    """
    existing = known_labels().get(name)
    if existing is not None:
        return existing
    return api("/api/v1/repos/%s/labels" % repo, "POST",
               {"name": name, "color": "#ededed", "description": "wayfinder"})["id"]


def attach_label(n, name):
    # .../issues/{index}/labels accepts ids OR names, but only for labels that exist.
    ensure_label(name)
    api("/api/v1/repos/%s/issues/%d/labels" % (repo, n), "POST", {"labels": [name]})


BEGIN, END = "<!-- wayfinder:children -->", "<!-- /wayfinder:children -->"


def read_children(body):
    """The map's child numbers, in map order. Conforms to the wayfinder map convention.

    Everything OUTSIDE the markers is prose a human owns and is ignored. Everything INSIDE
    them is data, and anything unparseable there is REFUSED rather than skipped: a skipped
    line drops a ticket out of the frontier, and a frontier that is short by one -- or empty,
    if a marker line was deleted -- is indistinguishable from a map that is complete. Zero
    markers is the one benign case: a fresh map legitimately has no children.

    Leniency survives only where it cannot lose a child: indentation, the bullet character,
    a bare `#12`, blank lines, and trailing title text that is never validated.
    """
    body = body or ""
    opens, closes = body.count(BEGIN), body.count(END)
    if (opens, closes) == (0, 0):
        return []
    if opens != 1 or closes != 1 or body.find(END) < body.find(BEGIN):
        die("the map's child block is malformed: %d opening and %d closing markers%s -- "
            "refusing rather than reporting a frontier from a damaged list"
            % (opens, closes, ", closer first" if 0 < body.find(END) < body.find(BEGIN) else ""))
    out = []
    for line in body[body.find(BEGIN) + len(BEGIN):body.find(END)].splitlines():
        if not line.strip():
            continue
        m = re.match(r"\s*(?:[-*+]\s*)?#(\d+)(?:\s.*)?$", line)
        if not m:
            die("not a child entry, inside the map's child block: %r" % line[:80])
        if int(m.group(1)) in out:
            die("#%s is listed twice in the map's child block -- map order is then ambiguous"
                % m.group(1))
        out.append(int(m.group(1)))
    return out


def write_children(body, nums):
    block = BEGIN + "\n" + "".join("- #%d\n" % n for n in nums) + END
    body = body or ""
    i = body.find(BEGIN)
    if i < 0:
        return (body.rstrip("\n") + "\n\n" if body.strip() else "") + "## Children\n\n" + block + "\n"
    j = body.find(END, i)
    if j < 0:
        die("the map body opens a child-list marker and never closes it")
    return body[:i] + block + body[j + len(END):]


DECISIONS = re.compile(r"^(#{1,6})\s*decisions[ -]so[ -]far\s*$", re.I | re.M)


def append_decision(body, line):
    body = body or ""
    m = DECISIONS.search(body)
    if not m:
        return (body.rstrip("\n") + "\n\n" if body.strip() else "") + \
            "## Decisions so far\n\n" + line + "\n"
    nxt = re.compile(r"^#{1,%d}\s" % len(m.group(1)), re.M).search(body, m.end())
    cut = nxt.start() if nxt else len(body)
    return body[:cut].rstrip("\n") + "\n" + line + "\n" + ("\n" + body[cut:] if nxt else "")


def get_map(n):
    m = issue(n)
    if "wayfinder:map" not in [lab["name"] for lab in m.get("labels") or []]:
        die("#%d is not labelled wayfinder:map -- refusing to treat it as a map" % n)
    return m


def open_maps():
    """Every open wayfinder map with the first line of its Destination.

    THE LISTING CAPS AT 50 AND IGNORES A LARGER `limit` on this forge, which is why this asks for
    exactly that and says so rather than pretending to be exhaustive. Maps are a handful; children
    are the hundreds. If this ever truncates, the refusal below is short by a map rather than
    silently wrong about one.
    """
    rows = api("/api/v1/repos/%s/issues?state=open&type=issues&labels=wayfinder:map&limit=50" % repo) or []
    out = []
    for r in rows:
        dest = ""
        body = r.get("body") or ""
        i = body.lower().find("## destination")
        if i >= 0:
            for line in body[i:].splitlines()[1:]:
                if line.strip():
                    dest = line.strip().lstrip("*").strip()[:110]
                    break
        out.append((r["number"], r["title"][:70], dest))
    return sorted(out)


def routing_refusal(mapno):
    """What a filer must see BEFORE choosing a map (a misfiling, 2026-09-05).

    THE DEFECT THIS EXISTS FOR. Four tickets about the SUCCESSION HANDOFF were filed on the map
    for a forge-workflow rebuild, because that was the map the session had been working and was
    therefore the one in mind. Nothing was wrong with the tickets; they were filed at whatever map
    was salient. `child-create` took the number as free input and validated only that it WAS a map.

    So the refusal is the teaching moment: it prints every open map and what each is FOR. A filer
    that has read this list cannot claim it did not know there was a choice.

    CEILING, STATED: this cannot know the right map, and `--because` is a self-report. What it
    removes is the DEFAULT -- there is no longer a path where a map number is accepted without the
    filer having seen the alternatives and written down why this one. The rationale lands on the
    ticket so a later reader can audit the routing rather than re-derive it.
    """
    lines = ["issue: child-create needs `--because <one line: why THIS map>`.",
             "",
             "  A map number is not a formality. Four succession-handoff tickets were filed on",
             "  the forge-workflow map because that was the map in hand at the time.",
             "  Read these and pick the one whose DESTINATION this ticket serves:",
             ""]
    for num, title, dest in open_maps():
        mark = "  ->" if num == mapno else "    "
        lines.append("%s #%d  %s" % (mark, num, title))
        if dest:
            lines.append("        %s" % dest)
    lines += ["",
              "  (open maps, from the forge; the listing caps at 50)",
              "  If none of them fits, the ticket is not map-bound: use `issue file`.",
              "",
              "  Then:  hub-api issue child-create <owner/repo> %d <title> <body> [type] \\" % mapno,
              "             --because \"serves this map's destination by ...\""]
    die("\n".join(lines))


def edit_body(n, mutate, get=None):
    """Read-modify-write one issue body, with the READ INSIDE a cross-session lock.

    THERE IS NO COMPARE-AND-SET TO FALL BACK ON. Measured against the live forge,
    both arms: the issue GET carries **no `ETag` and no `Last-Modified`** (8 response headers,
    none of them a validator), and `EditIssueOption.updated_at` is a settable timestamp, NOT a
    precondition -- a PATCH carrying a deliberately STALE `updated_at` returned **201** and
    silently clobbered the write that had landed in between. Swagger advertises a `412` on this
    path; it is not reachable that way. So a lost update cannot be REFUSED by the server, and
    the only remedies are to serialise or to detect afterwards. This serialises.

    `child-create` used to carry "make this a compare-and-set on `updated_at` if that stops
    holding". That route does not exist -- which is precisely why this is a lock, and why the
    comment is gone rather than left as advice that cannot be followed.

    THE LOCK PATH IS FIXED, OUTSIDE EVERY CHECKOUT, AND KEYED ON THE REPO, for the reason
    `scripts/pr-queue.sh` sets out at length: locking anything *inside* a tree locks a
    DIFFERENT INODE per worktree, and this client is routinely run from several at once, so
    both holders would acquire happily and the lock would look like it worked. `flock` is held
    by the kernel on the open descriptor, so there is no on-disk state to go stale.

    ponytail: this serialises writers that go through THIS CLIENT. A web-UI edit, another box,
    or a hand-rolled curl takes no lock and can still clobber, and nothing here can detect it
    -- that is the known ceiling, and it is why a web-UI map edit is ruled out. The upgrade
    path is not a better lock: it is the forge growing a conditional update, at which point
    this becomes an `If-Match` and the lock can go.
    """
    getter = get or issue
    path = os.environ.get("HUB_API_LOCK") or ("/tmp/hub-api-body.%s.lock"
                                              % repo.replace("/", "-"))
    # OPENED READ-ONLY, because /tmp is shared by every user on the box. Whoever runs
    # first owns the file, and /tmp is sticky, so the kernel's `protected_regular` refuses an
    # O_CREAT open (`"a"`, `"w"`) of it by anyone else, whatever its mode -- the CI runner's
    # lock made every other user's body-edit a PermissionError. `flock` needs no write access.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY)
    with os.fdopen(fd) as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        cur = getter(n)
        body = cur.get("body") or ""
        new = mutate(body)
        if new == body:
            return cur, False
        got = api("/api/v1/repos/%s/issues/%d" % (repo, n), "PATCH", {"body": new})
        if (got or {}).get("body") != new:
            die("the body of #%d did not take" % n)
        return got, True


def open_blockers(n):
    """Forgejo has NO issue_dependencies_summary; the count has to be enumerated.

    Anything other than a list of dependency records is a determination failure, not zero.
    """
    deps = api("/api/v1/repos/%s/issues/%d/dependencies" % (repo, n))
    if not isinstance(deps, list):
        die("could not read the blockers of #%d -- an unreadable dependency list is not "
            "'no blockers'" % n)
    # Not `== "open"`: a state this build does not know about must count as blocking.
    return [d for d in deps if d.get("state") != "closed"]


_ATTEST_WARNED = []


def _attest_path():
    """`session-attest` BY COMMAND NAME (the Session-Attest repo), or HUB_API_ATTEST.

    Missing, every caller degrades to "unknown" -- the safe direction: a claim whose session cannot
    be checked is treated as live and skipped. It is said once, aloud, so that degradation is never
    mistaken for a measurement."""
    p = os.environ.get("HUB_API_ATTEST") or shutil.which("session-attest") or ""
    if not (p and os.path.isfile(p)) and not _ATTEST_WARNED:
        _ATTEST_WARNED.append(1)
        sys.stderr.write("hub-api: MISSING COMMAND `session-attest` (from the Session-Attest repo) -- "
                         "claim liveness and host keys read as UNKNOWN until it is on PATH.\n")
    return p


def local_host():
    """This box's key from `session-attest host`, or "" when it cannot be read."""
    p = _attest_path()
    if not (p and os.path.isfile(p)):
        return ""
    try:
        r = subprocess.run(["sh", p, "host"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def newest_claim(n):
    """The newest `claim-session` record on #n, as (session, host); (None, None) when there is none.

    Shared by `claim_state` (is that session alive?) and `claims` (which issues does it hold?), so
    the two cannot disagree about whose claim an issue carries.
    """
    cs = api("/api/v1/repos/%s/issues/%d/comments" % (repo, n))
    sid = host = None
    for c in (cs if isinstance(cs, list) else []):
        body = c.get("body") or ""
        m = re.search(r"^claim-session: *([0-9A-Za-z-]{8,})\s*$", body, re.M)
        if m:
            sid = m.group(1)
            h = re.search(r"^claim-host: *(\S+)\s*$", body, re.M)
            host = h.group(1) if h else None
    return sid, host


def claim_state(n):
    """Is the session that claimed #n still alive? Returns ("live"|"ended"|"unknown", session or None).

    Every session is the one `claude` login, so the assignee cannot say whose
    claim it is; `issue claim` records the session in a comment, and this reads the newest one. ENDED
    only when the resolver answers with no live match (exit 1) for a session that was recorded.
    Everything else is UNKNOWN and skipped: no record (every claim older than the record), no resolver
    (the forge runner), an ambiguous match (exit 3), a timeout.

    ANOTHER HOST IS UNKNOWN TOO. The resolver answers for sessions on THIS box, so a claim
    recorded on a different host would read as ended while its holder works elsewhere.

    CEILING: a later claim made by a raw PATCH, with no comment, leaves an older claim-session as the
    newest record, and this answers about THAT session. `issue claim` always records; use it.

    NO ASSIGNEE IS NO CLAIM. `unclaim` clears the assignee and writes no comment, so after
    a release the newest record still names the releaser. While that session idled, `claim` and
    `resolve` refused everyone ("LIVE") and `frontier`, which checks the assignee first, offered the
    ticket as TAKE. The assignee is the claim; the comment only says whose it is.
    """
    sid, host = newest_claim(n)
    if not sid or sid.startswith("unrecorded-"):
        return ("unknown", sid)
    if not (api("/api/v1/repos/%s/issues/%d" % (repo, n)) or {}).get("assignees"):
        return ("released", sid)
    resolver = _attest_path()
    if not (resolver and os.path.isfile(resolver)):
        return ("unknown", sid)
    if host:
        mine = local_host()
        if mine and mine != host:
            return ("unknown", sid)
    try:
        r = subprocess.run(["sh", resolver, "resolve", sid], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ("unknown", sid)
    return ({0: "live", 1: "ended"}.get(r.returncode, "unknown"), sid)


def need(k, usage):
    if len(args) < k:
        bail("usage: hub-api issue " + usage)
    return args


# NO IDENTITY IS NAMED, NOT GUESSED. The core takes the caller's session only as
# FORGE_TOOLS_SESSION_ID, mapped by a harness adapter at session start (adapters/). A session
# started before its adapter was enabled has none until it resumes, clears or compacts -- and it
# was refused as a stranger by its OWN live claim, or recorded a claim frontier skips for ever.
NO_IDENTITY = ("this shell has no FORGE_TOOLS_SESSION_ID, so the core cannot tell whose claim is "
               "whose. Your harness adapter (adapters/) sets it at session start; a session that "
               "started before the adapter was enabled gets it on resume, clear or compact, or run "
               "`export FORGE_TOOLS_SESSION_ID=<this session's id>` and re-run.")

def refuse_live(n, holder, me):
    die("REFUSING: #%d is claimed by session %s, which is LIVE. Ask it, or pass --take-over for "
        "an agreed handover.%s" % (n, holder[:8], ("\n  It may be YOUR claim: " + NO_IDENTITY)
                                   if not me else ""))

if verb == "map-create":
    need(1, "map-create <owner/repo> <title> [body]")
    body = args[1] if len(args) > 1 else ""
    m = api("/api/v1/repos/%s/issues" % repo, "POST",
            {"title": args[0], "body": write_children(body, []),
             "labels": [ensure_label("wayfinder:map"), ensure_label("wayfinder")]})
    # THE PER-MAP LABEL IS ATTACHED AFTER THE POST, NOT WITH IT -- a map cannot know its
    # own number until the forge assigns one, so there is nothing to name in the create call. The
    # map carries `wayfinder:map-<its own number>` so that filtering a map's scope returns the map
    # ALONGSIDE its children, which is what the by-hand sweep produced and what anyone filtering
    # will expect.
    #
    # A failure here leaves a map that exists and is under-labelled rather than no map at all, and
    # that is the right way round: the create is the irreversible part. It is not wrapped in a
    # try/except for the same reason every other write here is not -- a silent partial success is
    # worse than a traceback naming the map that was made.
    attach_label(m["number"], "wayfinder:map-%d" % m["number"])
    print("map #%d %s" % (m["number"], m["html_url"]))

elif verb == "child-create":
    need(2, "child-create <owner/repo> <map#> <title> [body] [type] --because <why this map>")
    because = ""
    if "--because" in args:
        i = args.index("--because")
        if i + 1 >= len(args) or not args[i + 1].strip():
            die("issue: --because needs a reason after it")
        because = args[i + 1].strip()
        args = args[:i] + args[i + 2:]
    mapno = issue_num(args[0], "map#")
    m = get_map(mapno)
    if not because:
        routing_refusal(mapno)
    body = "Part of #%d\n\n%s" % (mapno, args[2] if len(args) > 2 else "")
    # THE ROUTING RATIONALE LIVES ON THE TICKET, not only in the filer's head. A later reader
    # auditing whether this is the right map should not have to re-derive the decision.
    body = body.rstrip("\n") + "\n\n---\nFiled on map #%d because: %s\n" % (mapno, because)
    # THE LABEL IS NOT OPTIONAL. It used to be, and the cost was measurable: 11 of
    # one map's 39 children were filed unlabelled, and the split fell EXACTLY on whether the
    # 4th argument was passed -- not on triage discipline. `frontier` keys on state, assignees
    # and blockers and never reads labels, so nothing downstream noticed; the loss was to the
    # human scanning the forge by the triage-label vocabulary, to whom an
    # unlabelled child is invisible.
    #
    # `task` is the default rather than a refusal because a refusal would break every existing
    # 3-argument caller, and the overwhelmingly common answer is `task` anyway. An explicit 4th
    # argument still overrides, and `ensure_label` creates the label if the repo lacks it.
    #
    # The `and args[3]` is load-bearing, not defensive noise: `args[3] if len(args) > 3 else
    # "task"` would send `wayfinder:` for an explicitly-empty argument, which `ensure_label`
    # would happily CREATE as a real label named `wayfinder:`. Empty and absent mean the same
    # thing here, and both mean `task`.
    # ALL THREE LABELS, ON BOTH BRANCHES OF THE TYPE ARGUMENT.
    #
    # An earlier fix made the TYPE label non-optional and stopped there, so `child-create` attached one label
    # of the three the operator asked for. The other two -- the generic `wayfinder` and the per-map
    # `wayfinder:map-<N>` -- were applied by an ad-hoc script in a coordinator's scratchpad: not in
    # the repo, not in any runbook, and dead when that session ended. Measured 2026-08-25: a child came
    # back carrying `['wayfinder:task']` alone, and **the operator noticed, no check did**. The other
    # 89 children were correct only because the by-hand sweep had been run after every prior filing.
    #
    # TWO WAYS TO GET THIS WRONG, both silent, both tested:
    #
    # 1. Adding the extras only where the type DEFAULTS recreates that exact symptom for the
    #    callers who bothered to be specific -- an explicit type would come back alone, e.g.
    #    `['wayfinder:grilling']`. That is why the list is built once here rather than inside the
    #    conditional: there is no branch for the extras to fall off.
    #
    # 2. Hardcoding `wayfinder:map-268` fails in the ACCEPTING direction. Every child would carry a
    #    wrong-but-present map label, so every check asking "is a map label attached?" answers yes.
    #    `mapno` is already in scope and is the number this child was actually filed under; nothing
    #    here may reference a literal map number.
    labels = [
        ensure_label("wayfinder:" + (args[3] if len(args) > 3 and args[3] else "task")),
        ensure_label("wayfinder"),
        ensure_label("wayfinder:map-%d" % mapno),
    ]
    c = api("/api/v1/repos/%s/issues" % repo, "POST",
            {"title": args[1], "body": body.rstrip("\n") + "\n", "labels": labels})
    # The map is re-read INSIDE edit_body's lock, not reused from the copy fetched above: the
    # create round-trip is a window in which the map body may have changed, and a concurrent
    # `resolve` rewrites this same body to append its decision pointer.
    edit_body(mapno, lambda b: write_children(b, read_children(b) + [c["number"]]), get_map)
    print("child #%d of map #%d %s" % (c["number"], mapno, c["html_url"]))

elif verb == "move":
    # REPARENT A CHILD, operator 2026-09-05. A child is bound to its map in THREE
    # places, and moving it by hand reliably updates two of them:
    #
    #   1. the `Part of #N` line at the top of its body
    #   2. the `wayfinder:map-N` label
    #   3. the map's own children block
    #
    # Miss (3) on either side and the ticket is either invisible to the new map's `frontier` or
    # still counted by the old one -- and BOTH failures look like a clean move from the ticket
    # itself, which shows the right body and the right label. That is why this is a verb rather
    # than a runbook: the step most easily forgotten is the one no reader of the ticket can see.
    need(2, "move <owner/repo> <n> <new-map#> [--because <why this map>]")
    because = ""
    if "--because" in args:
        i = args.index("--because")
        if i + 1 >= len(args) or not args[i + 1].strip():
            die("issue: --because needs a reason after it")
        because = args[i + 1].strip()
        args = args[:i] + args[i + 2:]
    n = issue_num(args[0], "issue number")
    dest_map = issue_num(args[1], "map#")
    get_map(dest_map)                       # refuses a target that is not a map
    child = issue(n)

    labels = [lab["name"] for lab in child.get("labels") or []]
    src = [lab for lab in labels if lab.startswith("wayfinder:map-")]
    if not src:
        die("#%d carries no `wayfinder:map-N` label -- it is not a map child, so there is "
            "nothing to move it FROM. File it with `child-create`." % n)
    if len(src) > 1:
        die("#%d carries %d map labels (%s) -- refusing to guess which parent is real."
            % (n, len(src), ", ".join(sorted(src))))
    src_map = int(src[0].rsplit("-", 1)[1])
    if src_map == dest_map:
        die("#%d is already on map #%d." % (n, dest_map))
    if not because:
        routing_refusal(dest_map)

    # Body first: it is the copy a human reads, and a half-move that leaves it wrong is worse
    # than one that leaves a label wrong.
    def repoint(b):
        b = b or ""
        old = "Part of #%d" % src_map
        if old not in b:
            die("#%d's body does not say %r, so this is not the parent it claims -- refusing "
                "to rewrite a line that is not there." % (n, old))
        return b.replace(old, "Part of #%d" % dest_map, 1).rstrip("\n") + \
            "\n\nMoved from map #%d to #%d because: %s\n" % (src_map, dest_map, because)
    edit_body(n, repoint)

    attach_label(n, "wayfinder:map-%d" % dest_map)
    api("/api/v1/repos/%s/issues/%d/labels/%d" % (repo, n, ensure_label(src[0])), "DELETE")

    # Both maps, each under its own lock. Order matters only for a crash between them, and this
    # order leaves the ticket on BOTH maps rather than on neither -- visible twice beats lost.
    edit_body(dest_map, lambda b: write_children(b, read_children(b) + [n]), get_map)
    edit_body(src_map, lambda b: write_children(b, [c for c in read_children(b) if c != n]), get_map)
    print("moved #%d from map #%d to map #%d" % (n, src_map, dest_map))

elif verb == "adopt":
    # ADOPT AN EXISTING UNMAPPED ISSUE ONTO A MAP. `move` re-parents a child and
    # REFUSES an issue that has no `wayfinder:map-N` label, correctly: there is nothing to move it
    # from. Its refusal advises `child-create`, which CREATES a second issue -- so the honest reading
    # of that advice is "abandon the original and retype it", and the numbered ticket someone already
    # filed becomes a duplicate. `issue file` produces exactly this shape on purpose (it labels
    # nothing), and the weekly maintainer run files seven or eight of them; on 2026-09-14 eighteen
    # accumulated and needed a whole map built to absorb them.
    #
    # THE BODY LINE IS THE PART THAT IS EASY TO MISS, and skipping it is silent. `move` rewrites
    # `Part of #<src>` and DIES if the line is absent, so an issue adopted by label and children
    # block alone can never be moved again -- it is a child that the re-parenting verb refuses to
    # touch. Measured 2026-09-21: seven maintainer tickets adopted by hand that way, none of which
    # said `Part of #N`. This verb writes it, which is what makes adoption reversible.
    #
    # IDEMPOTENT ON EACH PIECE, because half-adopted is the state this exists to repair. Labels,
    # body line and children entry are checked and added independently, and what was already right
    # is reported rather than rewritten.
    need(2, "adopt <owner/repo> <n> <map#> [type] --because <why this map>")
    because = ""
    if "--because" in args:
        i = args.index("--because")
        if i + 1 >= len(args) or not args[i + 1].strip():
            die("issue: --because needs a reason after it")
        because = args[i + 1].strip()
        args = args[:i] + args[i + 2:]
    n = issue_num(args[0], "issue number")
    dest_map = issue_num(args[1], "map#")
    get_map(dest_map)                     # refuses if it is not a map, before anything is written
    if n == dest_map:
        die("#%d cannot be its own child." % n)
    if not because:
        routing_refusal(dest_map)

    cur = [l["name"] for l in (api("/api/v1/repos/%s/issues/%d" % (repo, n)) or {}).get("labels", [])]
    other = [l for l in cur if l.startswith("wayfinder:map-") and l != "wayfinder:map-%d" % dest_map]
    if other:
        die("#%d is already a child of %s -- re-parenting is `move`'s job, not `adopt`'s.\n"
            "  hub-api issue move %s %d %d --because \"...\"" % (n, ", ".join(sorted(other)), repo, n, dest_map))

    kind = args[2] if len(args) > 2 and args[2] else "task"
    did, had = [], []

    # BODY FIRST, as `move` does it and for the same reason: it is the copy a human reads, and a
    # half-adoption that leaves the prose wrong is worse than one that leaves a label wrong.
    def adopt_body(b):
        b = (b or "").rstrip("\n")
        mine = "Part of #%d" % dest_map
        if mine in b:
            had.append("body says %s" % mine)
            return b + "\n"
        return mine + "\n\n" + b + "\n\n---\nAdopted onto map #%d because: %s\n" % (dest_map, because)
    edit_body(n, adopt_body)
    if not had:
        did.append("wrote `Part of #%d` and the rationale" % dest_map)

    for name in ("wayfinder", "wayfinder:map-%d" % dest_map, "wayfinder:" + kind):
        if name in cur:
            had.append(name)
        else:
            attach_label(n, name)
            did.append(name)

    # The children block is what `frontier` actually reads -- it keys on state, assignees and
    # blockers and never reads labels -- so an issue labelled but unlisted is invisible to it.
    def adopt_children(b):
        kids = read_children(b)
        if n in kids:
            had.append("already listed on map #%d" % dest_map)
            return b
        did.append("listed on map #%d" % dest_map)
        return write_children(b, kids + [n])
    edit_body(dest_map, adopt_children, get_map)

    print("adopted #%d onto map #%d as %s" % (n, dest_map, kind))
    if did:
        print("  added:   " + "; ".join(did))
    if had:
        print("  already: " + "; ".join(had))

elif verb == "label":
    need(2, "label <owner/repo> <n> <type>")
    n = issue_num(args[0], "issue number")
    # A NAMESPACED NAME IS NEVER MEANT FOR THIS VERB. `label` prefixes `wayfinder:`
    # unconditionally and `tag` applies the name bare, so `label <n> queue:needs-human-review`
    # produced `wayfinder:queue:needs-human-review`. The drain matches the BARE string, so the
    # hold did not hold, and because `ensure_label` CREATES what it cannot find, the repo was left
    # carrying a real, plausible-looking label that has never held anything and never will.
    #
    # A HOLD THAT SILENTLY DOES NOT HOLD IS WORSE THAN NO HOLD, because the seat that applied it
    # stops watching BECAUSE they applied it. This was caught once, by an operator who happened to
    # read the API's readback and notice it differed from the string passed in; nothing else would
    # have reported it.
    #
    # Refusing on `:` is exact rather than heuristic: every legitimate argument here is a wayfinder
    # TYPE (`task`, `research`, `map-268`) and none contains a colon, while every name that already
    # carries a namespace belongs to `tag` or to a verb that owns the string outright.
    if ":" in args[1]:
        die("`label` prefixes `wayfinder:`, so '%s' would become 'wayfinder:%s' -- a label\n"
            "  nothing matches, which this would CREATE rather than fail on.\n\n"
            "  To apply a name exactly as written:   issue tag %s %d %s\n"
            "  To hold a PR, use the verb that owns the string the queue matches:\n"
            "                                       pr hold %s %d"
            % (args[1], args[1], repo, n, args[1], repo, n))
    attach_label(n, "wayfinder:" + args[1])
    print("#%d labels: %s" % (n, ",".join(lab["name"] for lab in issue(n).get("labels") or [])))

elif verb == "label-id":
    # THE SEAM FOR CALLERS OUTSIDE THIS FILE. `pr-queue.sh` and `forge.sh` each
    # hand-rolled `/labels?limit=100` plus a python one-liner to pick an id out of it, because
    # neither may import this file's internals -- `forge.sh` deliberately WRAPS the client
    # rather than depending on its guts. A verb is the only shared surface they both already
    # have, so the duplication was structural rather than careless.
    #
    # RESOLVE-OR-REFUSE, and the refusal is the point. Forgejo DISCARDS a label name it does not
    # know and answers with the whole repo, so `labels=zz-no-such-label` returned all 28 open
    # PRs on this repo (measured 2026-08-24). A caller that got an empty id and carried on would
    # list everything and act on it. Exit 2 matches the `REFUSING:` convention the other verbs
    # use, so a caller can branch on the code rather than on the text.
    #
    # A caller that wants to FAIL SOFT still can, and `pr-queue.sh`'s hold labelling does: it
    # discards the exit code deliberately, because a labelling failure must never block a merge.
    # That is its judgement to make and this verb does not take it away.
    need(1, "label-id <owner/repo> <name>")
    name = args[0]
    known = known_labels()
    if name not in known:
        die("no label %r on %s -- Forgejo DISCARDS a name it does not know and answers with the\n"
            "  WHOLE repo, so a filter using it reads as \"everything matched\" rather than\n"
            "  \"nothing matched\". Known: %s"
            % (name, repo, ", ".join(sorted(known)) or "(none)"))
    print(known[name])

elif verb == "block":
    need(2, "block <owner/repo> <blocked#> <blocker#>")
    blocked, blocker = issue_num(args[0], "blocked#"), issue_num(args[1], "blocker#")
    # Measured on hub 2026-08-12: Forgejo ACCEPTS a self-dependency, and the issue is then
    # permanently unclosable -- every close answers 412 "still has open dependencies",
    # including the close that would clear it. There is no undo short of deleting the issue.
    if blocked == blocker:
        die("#%d cannot block itself -- Forgejo accepts the edge and the issue can then "
            "never be closed" % blocked)
    # TWO inversions against GitHub, either of which writes a silently backwards graph:
    #   * Forgejo identifies the other issue by NUMBER (index); GitHub wants its DB id.
    #   * POST .../{index}/dependencies means "{index} DEPENDS ON the issue in the body",
    #     so {index} is the BLOCKED ticket and the body names the BLOCKER.
    api("/api/v1/repos/%s/issues/%d/dependencies" % (repo, blocked), "POST",
        {"owner": OWNER, "repo": NAME, "index": blocker})
    deps = api("/api/v1/repos/%s/issues/%d/dependencies" % (repo, blocked))
    if not isinstance(deps, list) or blocker not in [d["number"] for d in deps]:
        die("the edge #%d blocked-by #%d did not land" % (blocked, blocker))
    print("#%d is blocked by #%d" % (blocked, blocker))

elif verb == "unblock":
    # `block` could only ADD, so a graph could be extended but never re-cut: re-ordering a chain
    # means adding the new edge while the old one stands, and Forgejo accepts the CYCLE that makes.
    need(2, "unblock <owner/repo> <blocked#> <blocker#>")
    blocked, blocker = issue_num(args[0], "blocked#"), issue_num(args[1], "blocker#")
    path = "/api/v1/repos/%s/issues/%d/dependencies" % (repo, blocked)
    # Refuse an edge that is not there: a DELETE naming the wrong pair would otherwise report
    # success, and the graph the caller believes it re-cut would be the old one.
    if blocker not in [d["number"] for d in api(path) or []]:
        die("#%d is not blocked by #%d -- nothing to remove (are the two the wrong way round?)"
            % (blocked, blocker))
    api(path, "DELETE", {"owner": OWNER, "repo": NAME, "index": blocker})
    if blocker in [d["number"] for d in api(path) or []]:
        die("the edge #%d blocked-by #%d is still there" % (blocked, blocker))
    print("#%d is no longer blocked by #%d" % (blocked, blocker))

elif verb == "blockers":
    need(1, "blockers <owner/repo> <n>")
    n = issue_num(args[0], "issue number")
    blk = open_blockers(n)
    if JSON:
        print(json.dumps({"number": n, "open_blockers": len(blk), "blockers": [b["number"] for b in blk]}))
    else:
        print("#%d open_blockers=%d %s" % (n, len(blk), ",".join("#%d" % b["number"] for b in blk)))

elif verb == "frontier":
    need(1, "frontier <owner/repo> <map#>")
    mapno = issue_num(args[0], "map#")
    order = read_children(get_map(mapno).get("body"))
    # One listing plus one dependency call per candidate, rather than a GET per child. It is
    # also the only call here that lists, which is what keeps the type=issues guard honest.
    by_num = {i["number"]: i for i in list_issues("all")}
    winner = None
    # One row per examined child, whatever the output: the text lines and the `--json` object are
    # the same verdicts, so a format cannot drift from the other.
    rows = []

    def skip(n, reason):
        rows.append({"number": n, "decision": "skip", "reason": reason})
        if not JSON:
            print("  #%-5d skip   %s" % (n, reason))
    for n in order:
        it = by_num.get(n)
        if it is None:
            die("child #%d is on the map but not in the issue listing -- refusing to compute "
                "a frontier from an incomplete list" % n)
        if it.get("state") != "open":
            skip(n, it.get("state"))
        elif LATENT_LABEL in [l.get("name") for l in (it.get("labels") or [])]:
            # OPEN BY DECISION, AND NOT TAKEABLE. The three skips below all describe
            # something ABOUT the ticket that the forge already knows: it is finished, someone
            # holds it, something blocks it. A ticket deliberately left open -- because it records
            # a decision that will matter if a setting changes, or waits on another map's
            # destination -- is none of those. It is open, unassigned and unblocked, all true, so
            # the frontier called it TAKE.
            #
            # Measured twice on one map, and the second time was worse. On 2026-09-02 it returned
            # the same child forever, the map's largest build, because it was the 28th child and takeable.
            # When that closed the stop moved to a child "latent BY DECISION" since 2026-08-29,
            # its sibling having resolved as keep `required_approvals: 0`, so there is no
            # approval for an admission rebase to invalidate and nothing to build. A successor
            # told to take the frontier without asking opens the one ticket carrying a written
            # decision not to proceed, and the three actionable children are never examined.
            skip(n, "latent by decision (%s) -- the body says why" % LATENT_LABEL)
        elif WAITING_LABEL in [l.get("name") for l in (it.get("labels") or [])]:
            # WAITS ON THE OPERATOR. Checked BEFORE the claim, because this is what keeps
            # such a ticket skipped after its claimant has ended.
            skip(n, "waiting on the operator (%s)" % WAITING_LABEL)
        elif it.get("assignees") and (claim := claim_state(n))[0] != "ended":
            # A CLAIM IS SKIPPED WHILE ITS SESSION IS LIVE OR UNKNOWN. The line says which,
            # so "claimed by claude" stops reading the same for a working session and a dead one.
            skip(n, "claimed by %s%s"
                 % (",".join(a["login"] for a in it["assignees"]),
                    " (session %s, %s)" % (claim[1][:8], claim[0].upper()) if claim[1]
                    else " (no claim-session recorded)"))
        else:
            blk = open_blockers(n)
            if blk:
                skip(n, "blocked by %s" % ",".join("#%d" % b["number"] for b in blk))
            else:
                # A CLAIM WHOSE SESSION ENDED IS TAKEABLE, operator 2026-09-10. Said on the line,
                # so the taker knows it inherits an abandoned claim rather than a free ticket.
                held = ("  [claimed by session %s, which has ENDED -- takeable]" % claim[1][:8]
                        if it.get("assignees") else "")
                rows.append({"number": n, "decision": "take", "reason": ((it.get("title") or "") + held).strip()})
                if not JSON:
                    print("  #%-5d TAKE   %s%s" % (n, (it.get("title") or "")[:60], held))
                winner = n
                break  # first in map order wins; the rest are not the answer
    # The per-child lines above have the SHAPE of an exhaustive survey, and twice a session read
    # them as the whole child set and reported no parallel work while five takeable children sat
    # below the break. The break stays -- it is one dependency call per candidate saved;
    # what changes is that the output stops claiming more than it walked.
    rest = len(order) - order.index(winner) - 1 if winner is not None else 0
    if JSON:
        print(json.dumps({"map": mapno, "children": rows, "not_examined": rest, "frontier": winner}))
    else:
        if rest:
            print("  ... and %d further child(ren) NOT EXAMINED -- the frontier stops at the first "
                  "takeable, so the lines above are not the child set" % rest)
        print("FRONTIER: %s" % ("#%d" % winner if winner else "none"))

elif verb == "claim":
    # A LIVE CLAIM IS NOT TAKEN SILENTLY. `claim` wrote a new claim-session over a LIVE
    # one, exit 0, no warning: on 2026-09-23 one session claimed a ticket two hours after another
    # did, both began editing hub-api.sh, and only a push announce surfaced it. `frontier` already
    # reads the newest claimant with `claim_state`; this reads the same answer before writing.
    # `--take-over` is the handover (a successor adopting its predecessor's ticket).
    take_over = "--take-over" in args
    no_session = "--no-session" in args
    args = [a for a in args if a not in ("--take-over", "--no-session")]
    need(1, "claim <owner/repo> <n> [user] [--take-over] [--no-session]")
    n, who = issue_num(args[0], "issue number"), (args[1] if len(args) > 1 else ME)
    _me = os.environ.get("FORGE_TOOLS_SESSION_ID", "")
    if _me in ("", "unknown") and not no_session:
        # Refused BEFORE any write: an unrecorded claim is skipped by frontier until someone notices.
        # `--no-session` is the deliberate form, for a person claiming from a plain terminal.
        die("REFUSING to claim #%d with no session identity: %s\n  To claim without one on "
            "purpose, pass --no-session." % (n, NO_IDENTITY))
    _state, _holder = claim_state(n)
    if _state == "live" and _holder and _holder != _me and not take_over:
        refuse_live(n, _holder, _me)
    got = api("/api/v1/repos/%s/issues/%d" % (repo, n), "PATCH", {"assignees": [who]})
    # Forgejo drops an assignee it will not accept and still answers 201.
    if who not in [a["login"] for a in (got or {}).get("assignees") or []]:
        die("%s is not assigned to #%d after the write -- the claim did not take" % (who, n))
    print("#%d claimed by %s" % (n, who))
    # RECORD WHOSE CLAIM IT IS. The assignee is `claude` for every session, so
    # without this `frontier` cannot tell a live claim from an abandoned one. A claim with no session
    # is still made; frontier then treats its liveness as unknown and keeps skipping it.
    _sid = os.environ.get("FORGE_TOOLS_SESSION_ID", "")
    if _sid and _sid != "unknown":
        # The host rides in the same comment, so frontier can tell a claim it cannot measure.
        _host = local_host()
        api("/api/v1/repos/%s/issues/%d/comments" % (repo, n), "POST",
            {"body": "Claimed by session %s.\n\nclaim-session: %s%s"
                     % (_sid, _sid, ("\nclaim-host: %s" % _host) if _host else "")})
        print("  claim-session %s recorded" % _sid[:8])
    else:
        # A RECORD IS POSTED ANYWAY. With no comment, the newest record stayed the
        # RELEASER's, and while that session idled `claim`/`resolve` refused everyone including
        # the real holder (an earlier symptom, back). `claim_state` reads this as UNKNOWN.
        api("/api/v1/repos/%s/issues/%d/comments" % (repo, n), "POST",
            {"body": "Claimed with no session id in the environment.\n\nclaim-session: unrecorded-%d"
                     % int(__import__("time").time())})
        print("  no session id in the environment: this claim records no session, so frontier keeps "
              "skipping it even after its holder ends")

elif verb == "claims":
    # A SESSION'S OPEN CLAIMS. A successor inherits its predecessor's claims and nothing
    # listed them, so a claim stayed attached to an ended session until someone noticed. Read-only:
    # open issues assigned to this login, filtered HERE (Forgejo ignores a query parameter it does not
    # know rather than refusing it), each matched on its newest `claim-session` record. Paged to
    # exhaustion, and the trailer names how many were read so a short listing shows.
    need(1, "claims <owner/repo> <session>")
    sid = args[0]
    rows = [it for it in paged("/api/v1/repos/%s/issues?type=issues&state=open" % repo)
            if ME in [a.get("login") for a in it.get("assignees") or []]]
    held = [it for it in rows if newest_claim(it["number"])[0] == sid]
    for it in held:
        print("#%d\t%s" % (it["number"], (it.get("title") or "")[:80]))
    print("%d claim(s) held by session %s, of %d open issue(s) assigned to %s read."
          % (len(held), sid[:8], len(rows), ME))

elif verb == "unclaim":
    # THE VERB THAT LEAVES A CLAIM. Releasing used to be a raw PATCH {"assignees": []}
    # through the passthrough: no verb name, no read-back, no record -- so a peer auditing a
    # DELIBERATE release concluded something outside the tooling had cleared it. Mirrors `claim`:
    # write, read back, fail if the login is still there. Only that login is removed, not every
    # assignee. It does NOT say WHOSE release it was: every session is `claude`.
    take_over = "--take-over" in args
    args = [a for a in args if a != "--take-over"]
    need(1, "unclaim <owner/repo> <n> [user] [--take-over]")
    n, who = issue_num(args[0], "issue number"), (args[1] if len(args) > 1 else ME)
    # ANOTHER SESSION'S LIVE CLAIM IS NOT RELEASED SILENTLY EITHER. Since a change made
    # "no assignee" mean released, `unclaim; claim` was a two-line bypass of the refusal that
    # `claim` and `resolve` carry. Same three lines, same flag.
    _me = os.environ.get("FORGE_TOOLS_SESSION_ID", "")
    _state, _holder = claim_state(n)
    if _state == "live" and _holder and _holder != _me and not take_over:
        refuse_live(n, _holder, _me)
    cur = api("/api/v1/repos/%s/issues/%d" % (repo, n))
    held = [x["login"] for x in (cur or {}).get("assignees") or []]
    if who not in held:
        die("#%d is not claimed by %s (assignees: %s) -- nothing to release"
            % (n, who, ", ".join(held) or "none"))
    got = api("/api/v1/repos/%s/issues/%d" % (repo, n), "PATCH",
              {"assignees": [x for x in held if x != who]})
    # Same trap as `claim`: Forgejo can answer 2xx and keep the assignee.
    if who in [x["login"] for x in (got or {}).get("assignees") or []]:
        die("%s is still assigned to #%d after the write -- the release did not take" % (who, n))
    print("#%d released by %s" % (n, who))

elif verb == "resolve":
    # A LIVE CLAIM IS NOT CLOSED OVER EITHER, the same gap `claim` closed. On
    # 2026-09-23 a session ran `claim` and `resolve` on one ticket in one command; the claim was refused
    # ("claimed by <session>, LIVE") and the resolve closed the ticket anyway. Checked before any
    # write, and `--take-over` is the same agreed handover `claim` accepts.
    take_over = "--take-over" in args
    args = [a for a in args if a != "--take-over"]
    need(3, "resolve <owner/repo> <n> <map#> <comment> [gist] [--take-over]")
    n, mapno, comment = (issue_num(args[0], "issue number"), issue_num(args[1], "map#"),
                         text_arg(args[2], "resolution comment"))
    _state, _holder = claim_state(n)
    _me = os.environ.get("FORGE_TOOLS_SESSION_ID", "")
    if _state == "live" and _holder and _holder != _me and not take_over:
        refuse_live(n, _holder, _me)

    # THE GIST IS THE INDEX, AND DERIVING IT FROM THE FIRST LINE PRODUCED `## Resolution`.
    # The map is an index, not a store -- the wayfinder convention: "one line per closed
    # ticket: enough to judge relevance, then zoom the link for the detail". A pointer reading
    # `## Resolution` carries zero bits, so the reader must zoom every closed ticket or skip them
    # all, which is the cost the index exists to avoid, paid once per future session instead of once.
    #
    # An explicit 5th argument is the shape, because WHICH SENTENCE IS THE DECISION is a human
    # judgement and the caller is the only party holding it. Deriving it is the same
    # "construct a name rather than ask for it" pattern already decided against for page titles.
    # Four-argument callers keep the derivation, so nothing breaks mid-flow.
    gist = args[3].strip() if len(args) > 3 and args[3].strip() else \
        (comment.strip().splitlines() or [""])[0].strip()

    # REFUSED BEFORE ANY WRITE, and that ordering is the whole point: this used to build the line
    # AFTER posting the comment and closing the ticket, so a refusal there would leave the ticket
    # closed and the map unwritten -- a worse state than the bug. Nothing has been sent yet here.
    if not gist:
        die("resolve: the pointer gist is empty -- the map line would carry nothing.\n"
            "  Pass one explicitly: resolve <repo> %d <map#> <comment> \"<gist>\"" % n)
    if gist.startswith("#"):
        die("resolve: the derived gist is a HEADING (%r), which indexes as zero bits on the map.\n"
            "  A comment that opens with a `## Resolution` heading hits this, because the first\n"
            "  line is what gets indexed. Either open the comment with a one-sentence gist, or\n"
            "  pass one: resolve <repo> %d <map#> <comment> \"<gist>\"" % (gist[:40], n))

    # A RESOLUTION THAT SAYS IT IS INCOMPLETE MAY NOT CLOSE THE TICKET. This verb never
    # reads what the ticket said it owed, and cannot: "did you really finish" is not a question a
    # script answers. What it CAN read is the resolver's own comment, and on 2026-09-05 one opened
    # "This closes the MECHANICAL half only -- item 1 of what the ticket says it owes" and closed
    # the ticket anyway. The evidence of incompleteness was inside the closing artifact, written in the
    # same breath as the close, and nothing joined the two. That contradiction is internal, so it
    # needs no knowledge of the work -- only that the words and the action disagree.
    #
    # CEILING, STATED SO IT IS NOT DISCOVERED LATER: this detects a resolution that contradicts
    # ITSELF. It cannot tell a complete resolution from an incomplete one that says nothing, and a
    # resolver who rewords the hedge away defeats it. That is a different failure from the measured
    # one, where the hedge was stated plainly and the close happened anyway.
    #
    # The exit is a follow-up ticket: a comment that names where the remainder went (`#N`, not a
    # `PR #N` -- the measured comment cited its PR and no ticket, and a PR is where the DONE half went)
    # is consistent with closing. `RESOLVE_PARTIAL_OK=1` is for the case the marker is true and the
    # remainder is genuinely nothing ("not done, and it does not need doing because ..."): say so in
    # the comment, and mean it with the variable.
    _marker = re.search(
        r"(?i)\b(?:not (?:yet )?(?:done|built|shipped|delivered|addressed|claimed here)"
        r"|(?:half|part) only|only the \w+ (?:half|part)|remains? open|stays? open|left open"
        r"|still owed|unbuilt|not in this (?:pr|change))\b", comment)
    _followups = {int(m) for m in re.findall(r"(?<![Pp][Rr] )(?<![Pp][Rr]\s)#(\d+)\b", comment)} - {n, mapno}
    if _marker and not _followups and os.environ.get("RESOLVE_PARTIAL_OK") != "1":
        die("resolve: REFUSING to close #%d on a comment that says the work is incomplete (%r)\n"
            "  and names no follow-up ticket. A verdict may not claim more than its own text\n"
            "  measured.\n"
            "  Either cite the ticket carrying the remainder (`#N` -- a `PR #N` does not count,\n"
            "  that is where the done half went), or, if the remainder is genuinely nothing, say\n"
            "  so in the comment and re-run with RESOLVE_PARTIAL_OK=1.\n"
            "  Ceiling: this reads the comment, not the ticket -- it catches a close that\n"
            "  contradicts itself, never one that is silently incomplete." % (n, _marker.group(0)))

    api("/api/v1/repos/%s/issues/%d/comments" % (repo, n), "POST", {"body": comment})
    closed = api("/api/v1/repos/%s/issues/%d" % (repo, n), "PATCH", {"state": "closed"})
    if (closed or {}).get("state") != "closed":
        die("#%d did not close" % n)

    # A MARKDOWN LINK, NOT BARE TEXT -- the second defect in this same string, repaired by hand five
    # times before it was written down. `- #N Title -- gist (url)` reads as a different KIND of
    # entry beside `- [Title](url) -- gist`, so a scanning reader takes it for a stray note rather
    # than a decision; that is how one sat unnoticed on a map for a day. Brackets in the title
    # are escaped because an unescaped `]` silently truncates the link text and the line still
    # renders, which is this ticket's failure mode exactly.
    title = (closed.get("title") or "").replace("[", "\\[").replace("]", "\\]")
    line = "- [%s](%s) -- %s" % (title, closed.get("html_url") or "", gist)
    # Read the map inside the lock rather than before the comment POST and the close PATCH:
    # those are two round trips of window, the widest of any writer here.
    edit_body(mapno, lambda b: append_decision(b, line), get_map)
    print("resolved #%d, pointer appended to map #%d" % (n, mapno))

elif verb == "body-edit":
    # The verb found missing. `child-create` and `resolve` each rewrite ONE machine-owned
    # region; nothing could edit the prose a human wrote, which is why one map's standing
    # decisions 16-20 lived as comments for a day rather than in the numbered list they belong
    # in.
    #
    # The contract is a LITERAL region replacement that must match EXACTLY ONCE, not a regex
    # and not a whole-body overwrite. Both alternatives fail the same way on a 50 KB body: a
    # whole-body PUT silently discards whatever landed since you read it, and a regex that
    # matches twice edits the wrong copy while reporting success. Exactly-once is the
    # precondition that makes a wrong `old` file a REFUSAL rather than a wrong edit.
    #
    # Text comes from FILES, not argv: these bodies are markdown with quotes, backticks and
    # newlines, which is the documented way this client's callers break.
    need(3, "body-edit <owner/repo> <n> <old-text-file> <new-text-file>")
    n = issue_num(args[0], "issue number")
    try:
        old = open(args[1]).read()
        new = open(args[2]).read()
    except OSError as e:
        bail("cannot read %s" % e)
    # AN EMPTY OLD TEXT FILLS AN EMPTY BODY, AND NOTHING ELSE. `pr create --title-only` and a
    # bodyless issue had NO repair path: exactly-once needs a region to match and an empty body has
    # none, so the only remedy was close-and-reopen, losing the number and its history. Measured
    # 2026-09-22 on a privileged-tool change opened with no body. Filling is safe exactly when
    # the body is empty -- there is nothing to overwrite -- and against a NON-empty body an empty old
    # text is still refused below, since it could only mean a silent prepend.
    fill_empty = not old.strip()

    def _replace_once(body):
        if fill_empty:
            if (body or "").strip():
                die("the old-text file is empty -- refusing to 'replace' nothing in #%d, which has a "
                    "body: that would only prepend and report success. An empty old text fills an "
                    "EMPTY body only." % n)
            return new
        hits = body.count(old)
        if hits != 1:
            die("the old text occurs %d times in #%d, not once -- refusing. 0 means the body "
                "moved under you or the text never matched; 2+ means this would edit an "
                "arbitrary one of them." % (hits, n))
        # RETRY GUARD. Exactly-once alone does NOT make this idempotent: when `new` CONTAINS
        # `old` -- appending to a sentence, the commonest edit there is -- the replacement
        # leaves `old` still matching exactly once, so running the same edit twice silently
        # DOUBLES the text and reports success both times. Measured live while
        # building this verb: `B4` -> `B4 - edited ...` applied twice, 61 B then 120 B, rc=0
        # each time. The stub tests could not see it; only the real round trip did.
        # BOTH HALVES ARE REQUIRED, and testing only `new in body` was a real bug.
        #
        # The hazard is narrow: an edit doubles on a second run ONLY when `new` re-contains `old`,
        # because that is the sole way `old` still matches after the replacement. `new in body`
        # alone was a PROXY for it, and it was wrong in two directions:
        #
        #   A DELETION COULD NEVER BE EXPRESSED. `"" in body` is True for every body, so the guard
        #   fired on 100% of deletions and could never be satisfied -- while explaining a doubling
        #   hazard that cannot exist for an empty replacement, where a second run genuinely matches
        #   nothing. Measured 2026-08-30 pruning merged entries out of a queue ticket. The workaround
        #   was to widen `old` until `new` was non-empty, which for a fenced block means swallowing
        #   the fence markers -- so the guard pushed callers toward a strictly MORE dangerous edit
        #   than the one it refused.
        #
        #   AN UNRELATED REPLACEMENT WAS REFUSED whenever `new` happened to appear elsewhere in the
        #   body. `foo` -> `bar` in a body that already says `bar` somewhere is perfectly
        #   idempotent: after it runs, `old` no longer matches and a second run refuses not-found.
        #
        # `old in new` ALONE IS NOT THE FIX EITHER, and the ticket's preferred option would have
        # regressed the commonest edit there is. `B4` -> `B4 - edited` has `old in new`, so testing
        # that alone refuses the FIRST application -- the edit the caller actually wants -- rather
        # than the second. Measured against all three cases before choosing this.
        #
        # Conjoined, the two conditions say what is actually meant: the replacement re-contains the
        # old text (so the edit is not self-cancelling) AND it is already present (so this run is
        # the repeat). Append once, refuse the double, allow every deletion.
        if new != old and old in new and new in body:
            die("#%d already contains the new text -- refusing, because this edit is not "
                "idempotent when the new text contains the old: a second run would double it "
                "rather than match nothing. If this is a deliberate second insertion, widen "
                "the old text to something unique." % n)
        return body.replace(old, new, 1)

    got, changed = edit_body(n, _replace_once)
    print("body-edit #%d: %s (%d B -> %d B, body now %d B)"
          % (n, "replaced" if changed else "no change -- old and new are identical",
             len(old), len(new), len(got.get("body") or "")))

elif verb == "zoom":
    need(1, "zoom <owner/repo> <n>")
    n = issue_num(args[0], "issue number")
    it = issue(n)
    print("#%d %s [%s]" % (n, it.get("title"), it.get("state")))
    print("labels    %s" % ",".join(lab["name"] for lab in it.get("labels") or []))
    print("assignees %s" % ",".join(a["login"] for a in it.get("assignees") or []))
    # Every blocker with its state, not just the open ones: "read one ticket in full" means
    # the history of what held it up, and `blockers` is the verb for the live count.
    deps = api("/api/v1/repos/%s/issues/%d/dependencies" % (repo, n))
    print("blockers  %s" % (",".join("#%d(%s)" % (b["number"], b["state"]) for b in deps) or "-"))
    print("--- body ---")
    print(it.get("body") or "")
    for c in api("/api/v1/repos/%s/issues/%d/comments" % (repo, n)) or []:
        print("--- comment by %s ---" % (c.get("user") or {}).get("login"))
        print(c.get("body") or "")

elif verb == "list":
    # Issues, never PRs. Optional label is an exact Forgejo label name; optional state is
    # open|closed|all and defaults to open.
    #
    # THE STATE AND THE PAGING ARE THE POINT, NOT A CONVENIENCE. This verb is the
    # GUARDED way to filter by label -- and it hardcoded `state=open&limit=50`, so it could not
    # express the query the guard exists to protect. "Pull up a map's scope" is `state=all`
    # across 40+ issues, and the only way to ask it was the raw passthrough, which had no guard
    # at all. So the fail-open was demonstrated on the unguarded path BECAUSE the guarded path
    # could not answer the question. That is the mechanism, not a coincidence: a guard on a verb
    # too narrow to be used is a guard nobody is behind.
    #
    # `limit=50` SILENTLY CAPPED rather than failing. Measured 2026-08-25: a labelling sweep read
    # 50 of 122 issues and reported the clean subset as the whole repo -- caught only because a
    # child known to be missing was not in the list. A truncated list and a complete one are the
    # same object; nothing in the response says which you have. So page to exhaustion instead.
    label = args[0] if args else ""
    state = args[1] if len(args) > 1 and args[1] else "open"
    if state not in ("open", "closed", "all"):
        die("state %r is not open|closed|all. Forgejo does not reject an unknown state, it "
            "IGNORES it and answers with the default -- so the wrong answer would look right."
            % state)
    # No `limit` here: `paged` owns it, so the two cannot drift apart and make its
    # short-page stop condition wrong.
    path = "/api/v1/repos/%s/issues?type=issues&state=%s" % (repo, state)
    if label:
        # AN UNKNOWN LABEL IS NOT AN EMPTY RESULT -- IT IS NO FILTER AT ALL. Measured on hub
        # 2026-08-15: `labels=wayfinder:map` returns 2 of 14 open issues, and both
        # `labels=zzz-nonexistent` and a label with spaces return all 14. So a renamed or
        # deleted label does not fail, and does not return zero: it hands the caller the
        # entire backlog as if every item matched. The weekly run lists `agent:maintainer`
        # to build its queue, which is exactly the caller that must not silently get 14.
        #
        # So resolve the name against the repo's real labels first and refuse if it is not
        # there. Percent-encoding matters too -- a space or `&` reaches Forgejo as a
        # different query rather than an error -- but encoding alone would only make the
        # wrong answer well-formed.
        known = known_labels()          # the one fetch; the verdict below stays here
        if label not in known:
            die("no label %r on %s -- it would return every %s issue unfiltered, not none. "
                "Known: %s"
                % (label, repo, state, ", ".join(sorted(known)) or "(none)"))
        path += "&labels=" + urllib.parse.quote(label, safe="")
    # A MAP CARRIES ITS OWN `wayfinder:map-N` LABEL, so it comes back in its own children's
    # listing. Rows and children then differ by exactly one, and nothing said so.
    #
    # MEASURED COST, and it went wrong in both directions in one afternoon: a coordinator's handoff
    # said "39 children", an auditor counted 40 rows and reported the handoff WRONG, and the
    # correction was then written up as "both right about different questions", which teaches a
    # reader nothing. This file's own prose at :1219 said "11 of a map's 39 children" -- the repo
    # counts children while the client handed back rows.
    #
    # EXCLUDED AND SAID, not silently dropped. A quiet exclusion swaps one wrong count for another
    # and is worse, because the caller cannot tell which question was answered. The trailer states
    # the number and names what was removed, so the row/child distinction is in the output rather
    # than in whoever remembers the incident.
    _self = re.match(r"^wayfinder:map-(\d+)$", label or "")
    _self = int(_self.group(1)) if _self else None
    _rows = paged(path)
    _kept = [it for it in _rows if it.get("number") != _self]
    for it in _kept:
        labs = ",".join(lab["name"] for lab in it.get("labels") or [])
        print("#%-5d %s\t%s" % (it["number"], labs, (it.get("title") or "")[:80]))
    if _self is not None and len(_kept) != len(_rows):
        print("%d child(ren) of map #%d; the map itself carries this label and is excluded."
              % (len(_kept), _self))
    else:
        # STATED ON EVERY PATH. A count that appears only sometimes is one a caller learns to
        # ignore, and its absence would then read as "nothing was excluded" rather than as
        # "this listing has no map in it".
        print("%d row(s)%s." % (len(_kept),
                                "" if _self is None else "; map #%d not present" % _self))

elif verb == "file":
    # A MAP-LESS TICKET IS A DECISION, NOT A DEFAULT.
    #
    # Map membership on this repo is carried by the LABEL, not by a dependency link: `frontier` and
    # `list <map-label>` both key on it. So a ticket filed here was real, numbered, and listed by no
    # map -- and the verb printed a number and a URL, which looks exactly like success. Nothing
    # later says "this is on no map"; the frontier simply never mentions it, which is
    # indistinguishable from resolved, deprioritised or blocked.
    #
    # MEASURED, 2026-08-26: four of eight tickets filed that evening were orphaned, every one via
    # this verb, every one caught by the operator rather than by any mechanism -- twice, an hour
    # apart, the second pair from a session that had no way to know.
    #
    # NOT A FLAT REFUSAL, and the ticket is explicit about why: a genuinely map-less issue is a
    # legitimate thing to file, so refusing the case outright would be wrong. What was wrong is that
    # SILENCE AND DECISION WERE INDISTINGUISHABLE -- the same shape as an empty PR body, fixed in
    # this file the same evening. `--no-map` costs one token and makes the orphan deliberate.
    #
    # IT ALSO CLOSES THE CONVENTION HALF. The ticket's own comment says the recurrence was not the
    # tool's fault but a convention living in one coordinator's head, mitigated by
    # an advisory hook. That hook ADVISES and a session may proceed past it; this makes the
    # outcome independent of whether anyone read it.
    need(1, "file <owner/repo> --no-map <title> [body]")
    if args[0] != "--no-map":
        # `die`, NOT `bail`. Both print a refusal; `bail` exits 1 like every other outcome, and
        # `die` exits 2, which is this file's stated convention for a state it will not act on
        # (see `die` above). A refusal answered with 1 is a known bug elsewhere -- adding a second here
        # would ship it again.
        die("`issue file` applies NO LABELS, and map membership here is carried by the\n"
             "  label -- so this would file a real, numbered ticket that no map lists and no\n"
             "  frontier ever surfaces. Measured: four of eight tickets in one evening.\n"
             "\n"
             "  Map-bound work:  hub-api issue child-create <owner/repo> <map#> <title> [body]\n"
             "  Genuinely none:  hub-api issue file <owner/repo> --no-map <title> [body]\n"
             "\n"
             "  The second is a legitimate thing to file. It just has to be said rather than\n"
             "  arrived at by leaving an argument out.")
    need(2, "file <owner/repo> --no-map <title> [body]")
    created = api("/api/v1/repos/%s/issues" % repo, "POST",
                  {"title": args[1], "body": args[2] if len(args) > 2 else ""})
    print("#%d %s" % (created["number"], created.get("html_url") or ""))
    print("  no labels, no map -- stated with --no-map. `issue tag` adds one if that was wrong.")

elif verb == "tag":
    need(2, "tag <owner/repo> <n> <label>")
    n = issue_num(args[0], "issue number")
    attach_label(n, args[1])
    print("#%d labels: %s" % (n, ",".join(lab["name"] for lab in issue(n).get("labels") or [])))

elif verb == "note":
    need(2, "note <owner/repo> <n> <comment>")
    n = issue_num(args[0], "issue number")
    api("/api/v1/repos/%s/issues/%d/comments" % (repo, n), "POST",
        {"body": text_arg(args[1], "comment")})
    print("commented #%d" % n)

elif verb == "close":
    need(1, "close <owner/repo> <n> [comment]")
    n = issue_num(args[0], "issue number")
    if len(args) > 1:
        api("/api/v1/repos/%s/issues/%d/comments" % (repo, n), "POST",
            {"body": text_arg(args[1], "closing comment")})
    closed = api("/api/v1/repos/%s/issues/%d" % (repo, n), "PATCH", {"state": "closed"})
    if (closed or {}).get("state") != "closed":
        die("#%d did not close" % n)
    print("closed #%d" % n)

else:
    bail("unknown issue verb %r (map-create|child-create|adopt|move|label|label-id|block|unblock|frontier|blockers|"
         "claim|unclaim|resolve|body-edit|zoom|list|file|tag|note|close)" % verb)
PYEOF
)
    # The script's own directory (session-attest is reached by command name).
    # HUB_ACCESS_CONFIG is resolved above but never exported, so api() could not see it.
    # An unset forge URL is refused inside api(), at the first call -- not here, so an argument the
    # verb refuses on its own is still refused for ITS reason, offline.
    FT_CONFIG_FILE="$FT_CONFIG_FILE" HUB_ACCESS_CONFIG="$HUB_ACCESS_CONFIG" HUB_API_DIR=$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")") exec python3 -c "$py" "$HUB_URL" "$CFG" "$HUB_USER" "$@"
    ;;

fingerprint)
    require_config
    printf 'sha256:%s\n' "$(_token | sha256sum | cut -c1-16)"
    ;;

selfcheck)
    # Proves confinement WITHOUT disclosing the value: each probe greps for the real
    # token and reports only a verdict. A leak anywhere prints the location, not the
    # secret. Every probe is paired with a positive control, because a grep that finds
    # nothing because it was aimed wrong is indistinguishable from a clean result.
    require_config
    tok=$(_token)
    rc=0

    printf 'perms %s: %s\n' "$CFG" "$(stat -c '%a' "$CFG")"

    for f in "$HOME/.bash_history" "$HOME/.sh_history" "$HOME/.ash_history"; do
        [ -f "$f" ] || continue
        if grep -qF -- "$tok" "$f" 2>/dev/null; then
            printf 'LEAK  shell history: %s\n' "$f"; rc=1
        else
            printf 'clean shell history: %s\n' "$f"
        fi
    done

    if grep -rqF -- "$tok" "$(ft_checkout)" 2>/dev/null; then
        printf 'LEAK  repo tree contains the token\n'; rc=1
    else
        printf 'clean repo tree\n'
    fi

    # Positive control: the same grep, over a file that DOES contain the token, must
    # report a hit. Without this, every "clean" above is consistent with a broken grep.
    ctl=$(mktemp); printf '%s\n' "$tok" > "$ctl"
    if grep -qF -- "$tok" "$ctl"; then
        printf 'control PASS (grep finds the token when it is present)\n'
    else
        printf 'control FAIL -- the clean results above measured nothing\n'; rc=1
    fi
    rm -f "$ctl"

    exit $rc
    ;;

fingerprint-help|-h|--help|"")
    sed -n '2,27p' "$0"
    ;;

*)
    require_config
    # THROUGH `api()`, not a second copy of it. This arm used to carry its own byte-identical
    # `hub_curl -sS --fail-with-body --config ...` line, which is why a transport bug could be read as a
    # bug in `api()` and fixed there without touching the passthrough -- the one path it was
    # actually about. Two copies of a transport is a fix that lands on one of them.
    #
    # (--config keeps the credential out of argv; `ps` on this box shows only the URL.
    # --fail-with-body is load-bearing, not tidiness: hub answers an unauthenticated call
    # with 403 and a bad token with 401, both carrying a JSON body that reads like an
    # ordinary response. Without it curl exits 0 and a caller testing $? treats
    # "you are not signed in" as data. The body is still printed, so the reason survives.)
    api "$@"
    ;;
esac
