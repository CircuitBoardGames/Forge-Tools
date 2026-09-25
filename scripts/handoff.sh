#!/bin/sh
# handoff.sh — publish the coordinator handoff to the Forgejo repo wiki, and PROVE it landed.
#
# THE FAILURE THIS EXISTS FOR. The handoff lived at $HOME/coordinator-handoff.md with its
# only durable copy an issue comment, kept in step BY HAND. They diverged, and nothing said so:
# the copy that survives the box was the one carrying the errors, and a reader had no way to tell
# which of the two they were holding. Two copies plus a habit is not a mechanism.
#
# WHY THE WIKI, chosen by the operator 2026-08-27. It is a real git repo
# (`<repo>.wiki.git`) on the authoritative forge, and it takes a write DIRECTLY:
#
#   - NO PULL REQUEST. A handoff is written when a shift ends; a surface that needs review
#     before it is readable is a surface that is empty exactly when it is needed.
#   - NO CI. Forgejo Actions read workflows from the CODE repo. The wiki is a different
#     repository, so a wiki write dispatches nothing. Verified, not assumed -- see `selfcheck`.
#   - NOT MIRRORED TO GITHUB, and this is structural rather than a promise:
#     a GitHub mirror of a forge repo lists the CODE repo; `<repo>.wiki.git` is a SEPARATE
#     repository and is not mirrored unless someone lists it. Keep it that way: this surface is
#     meant to stay on the forge.
#   - IT OUTLIVES THE BOX. $HOME is not a durable place to keep the one document whose whole
#     purpose is to survive the seat that wrote it.
#
# WHAT THIS DOES THAT A BARE `curl` DOES NOT. `publish` READS THE PAGE BACK and compares sha256
# against what it sent. A publish that reports success has been verified against the forge's own
# copy; it is not a report that a request was accepted. That read-back is the entire point of the
# script -- without it this is two copies and a habit again, which is the bug.
#
# CEILING, STATED. Nothing makes anyone RUN this, exactly as `owed-status.sh` cannot make anyone
# measure. What it removes is the two-copy problem: after `publish`, the wiki page is the single
# authority and the local file is a draft with no standing. The discoverability half lives in
# `.claude/skills/handoff/SKILL.md`, which is the thing a seat actually invokes.
set -u

# The wiki's repo: HANDOFF_REPO, else FORGE_TOOLS_REPO, else FORGE_TOOLS_OWNER/<remote's name> --
# resolved just before the verb dispatch, once ft-config.sh is loaded. No literal default.
REPO="${HANDOFF_REPO:-}"
# THE TITLE IS NOT THE URL, AND ASSUMING IT WAS COST THE FIRST PUBLISH. Forgejo derives a page's
# `sub_url` from its title with an escaping this script does not get to choose. Publishing the
# title `Coordinator-Handoff` created a page addressable only as `Coordinator-Handoff.-`, and the
# read-back 404'd on the name it had just sent.
#
# THIS NOTE USED TO SAY "spaces become `-`, and a LITERAL hyphen becomes `.-`", WHICH IS INCOMPLETE
# AND MADE THE ESCAPING LOOK LOCAL TO THE HYPHEN. Measured against the live wiki:
#
#   Coordinator Handoff                          -> Coordinator-Handoff
#   Hot Cache                                    -> Hot-Cache
#   Handoff 02aa007f-6a26-40cc-927f-2b05499e42af -> Handoff+02aa007f-...-...af.-
#   Handoff research-audit 2026-08-29            -> Handoff+research-audit+2026-08-29.-
#
# **A hyphen ANYWHERE in the title also flips every space to `+`.** Space-only titles stay clean;
# one hyphen makes the whole URL unreadable. That is why the slot convention is `Handoff CC 1` and
# never `Handoff CC-1`, and why `slot claim` refuses a prefix that is not alphanumeric.
#
# So: titles carry SPACES and no hyphens, and no code below ever constructs a page path --
# `sub_url_for_title` asks the forge, every time.
# SET-BUT-EMPTY IS A FAILED COMMAND, NOT A REQUEST FOR THE DEFAULT. `:-` treats unset and empty
# alike, and that cost a real page: the documented recipe is
#
#     HANDOFF_PAGE="$(sh scripts/session-attest.sh page)" sh scripts/handoff.sh publish <file>
#
# (That recipe is historical: `page` left with session-attest for the Session-Attest repo,
# and the name is now plain `HANDOFF_PAGE="Handoff $FORGE_TOOLS_SESSION_ID"`. The lesson stands.)
#
# and on 2026-08-29 that ran in a worktree whose checkout predated `session-attest.sh`. The
# substitution printed its error to stderr and produced an EMPTY STRING, `:-` silently supplied
# "Coordinator Handoff", and a session handoff overwrote the shared coordinator page -- 16,316 bytes
# replaced by 3,812. Recovered from the wiki's git history, byte-identical, but only because the
# wiki is a repository.
#
# Every guard involved worked and none was positioned to catch it. `session-attest.sh page` refuses
# an unset session id -- but the script was not there to refuse. `publish` verified by read-back --
# and truthfully reported byte-identical, to the wrong page. The defect is in the COMPOSITION: a
# command substitution turns a missing dependency into a fallback.
#
# So: `-` rather than `:-`, and an empty value is refused by name. An UNSET variable still means
# "use the default", which is the ordinary case and is unchanged.
PAGE="${HANDOFF_PAGE-Coordinator Handoff}"
if [ -z "$PAGE" ]; then
    echo "handoff: HANDOFF_PAGE is set but EMPTY -- refusing." >&2
    echo "  An empty page name is almost always a command substitution that failed:" >&2
    echo '    HANDOFF_PAGE="$(...)"   <- the inner command errored and printed nothing' >&2
    echo "  Falling back to the default here would publish your content over the SHARED" >&2
    echo "  coordinator page, which is what happened on 2026-08-29. Check the inner command:" >&2
    echo '    echo "Handoff $FORGE_TOOLS_SESSION_ID"   # empty id = no harness adapter set it' >&2
    exit 2
fi
FILE_DEFAULT="$HOME/coordinator-handoff.md"

# `session-attest` is REACHED BY COMMAND NAME: it lives in the Session-Attest repo.
# Every call below discards its stderr, so a missing command would read as "not live" or "no host"
# -- a wrong answer, silently. This says it by name first, then the caller degrades as before.
_need_attest() {
    command -v session-attest >/dev/null 2>&1 && return 0
    echo "handoff: MISSING COMMAND \`session-attest\` (from the Session-Attest repo) -- not on PATH," >&2
    echo "  so the $1 below cannot run and is treated as unavailable. Install it to restore it." >&2
    return 1
}

HERE=$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")   # real dir: hub-api.sh is a sibling, even through a symlink
API="$HERE/hub-api.sh"
[ -x "$API" ] || [ -f "$API" ] || { echo "handoff: cannot find hub-api.sh next to $0" >&2; exit 1; }
[ -r "$HERE/ft-config.sh" ] || { echo "handoff: cannot read $HERE/ft-config.sh -- Forge-Tools' config reader must sit beside this script" >&2; exit 1; }
. "$HERE/ft-config.sh"

# THE SITE'S PAGE LISTS, comma-separated titles, EMPTY BY DEFAULT: which pages this
# site's harness injects at session start (they get the injector's size ceiling on publish), and
# which pages carry `/trigger` claims (they get the trigger gate). A deployment names its own.
_page_list() { printf '%s\n' "$1" | tr ',' '\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//; /^$/d'; }
INJECTED_PAGES=$(_page_list "${FORGE_TOOLS_INJECTED_PAGES:-}")
TRIGGER_PAGES=$(_page_list "${FORGE_TOOLS_TRIGGER_PAGES:-}")

# EVERY JSON AND HASH OPERATION BELOW SHELLS OUT TO python3, AND ITS ABSENCE IS INDISTINGUISHABLE
# FROM AN ABSENT PAGE UNLESS CHECKED HERE. Measured: with no python3 on PATH, `sub_url_for_title`
# exits non-zero, `fetch` returns 1, and `verify` reports `no wiki page "..." -- nothing to compare`.
# That is "I could not tell" wearing the words "it is not there" -- the false negative this whole
# ticket is about, reproduced inside the tool meant to end it. Found when CI went red on a suite
# that was green here, because the runner's PATH and this box's are not the same measurement.
command -v python3 >/dev/null 2>&1 || {
    echo "handoff: no python3 on PATH -- refusing to run, because every failure below would" >&2
    echo "         otherwise be reported as 'no wiki page' rather than 'cannot check'." >&2
    exit 1
}

usage() {
    cat >&2 <<EOF
usage: handoff.sh <verb>

  publish [file]   upload <file> (default $FILE_DEFAULT) to wiki page "$PAGE",
                   then READ IT BACK and compare sha256. Non-zero unless they match.
  show             print the wiki page's current content to stdout
  verify [file]    compare <file> to the wiki page without writing. Non-zero if they differ.
  url              print the human URL of the page
  delete [--dry-run]
                   DELETE the wiki page, then confirm it is ABSENT from the page listing.
                   Only \`Handoff <id>\` pages -- every other title is refused, including
                   "Coordinator Handoff" (shared: overwriting one has cost real bytes).

Environment:
  HANDOFF_PAGE       the wiki page to act on (default "Coordinator Handoff"). Set but EMPTY is
                     REFUSED, never treated as unset -- a failed \`\$(...)\` must not silently
                     fall back to the shared page.
  HANDOFF_NO_STAMP   set to any value to publish a handoff page WITHOUT this session's
                     attestation. **Use it when RESTORING someone else's page**: \`publish\`
                     otherwise signs handoff pages unconditionally, so a recovery re-attaches
                     YOUR session to THEIR document and the page then asserts you wrote it.
                     Truthful about its author, false about the authorship.
  HANDOFF_NO_MEASURE set to any value to publish a handoff page WITHOUT the re-measured
                     block (branch, ahead/behind, dirty count, open PR, worktree list) that
                     \`publish\` otherwise writes beside the prose. HANDOFF_NO_STAMP
                     implies it: a restore keeps the other session's reading.
  FORGE_TOOLS_INJECTED_PAGES  comma-separated titles the harness injects at session start;
                     publish holds them to the injector's size ceiling (default: none)
  FORGE_TOOLS_TRIGGER_PAGES   comma-separated titles whose \`/trigger\`s must resolve (default: none)
  HANDOFF_CACHE_HOOK the harness hook that injects FORGE_TOOLS_INJECTED_PAGES; publish reads
                     their size ceiling from it. NO DEFAULT: unset REFUSES those pages.
  FORGE_TOOLS_SKILL_ROOTS    where a trigger page's \`/trigger\` may resolve, space-separated and
                     relative to the git top level: <root>/<name>/SKILL.md
                     (default ".agents/skills .claude/skills")
  FORGE_TOOLS_COMMAND_ROOTS  <root>/<name>.md|.toml (default ".claude/commands .agents/commands")
  FORGE_TOOLS_TRIGGER_EXCEPTIONS  tokens that are not triggers, ADDED to
                     scripts/skill_trigger_exceptions.txt

The wiki page is the AUTHORITY. A local file is a draft until \`publish\` confirms it.
EOF
    exit 2
}

# Ask the forge for the page's real `sub_url`, matched on TITLE. Returns 1 if no such page.
# This is the only place a page address comes from; nothing here builds one from $PAGE.
# THREE OUTCOMES, NOT TWO. 0 = found, 1 = the listing was read and the page is not in
# it, 2 = THE LISTING COULD NOT BE READ.
#
# This used to return 1 for both of the last two, and the cost was a FALSE ABSENCE. Measured with a
# control: `HANDOFF_PAGE='Hot Cache' show` returns 4102 bytes, and the same command against an
# unreachable forge printed `no wiki page "Hot Cache"` -- of a page that is injected into every
# session at launch. One expression did it: `json.loads(sys.stdin.read() or "[]")` coerces an empty
# response into an EMPTY PAGE LISTING, from which "your page is not among them" follows correctly.
# The API call's own exit status was then lost to the pipe, so nothing downstream could tell.
#
# THIS FILE ALREADY GUARDS THIS EXACT SHAPE FOR A DIFFERENT CAUSE -- see the python3 check above,
# "'I could not tell' wearing the words 'it is not there'". The network case is the same defect and
# was unguarded, so the principle was enforced against one cause and not the other.
sub_url_for_title() {
    # Captured, not piped: a pipeline reports the LAST command's status, so `| python3` would
    # discard exactly the signal that distinguishes the two failures. JSON in a shell variable is
    # safe -- the trailing-newline hazard in digest() is about page CONTENT, which never goes here.
    # PAGED, like `_all_titles`: one unpaged read is the forge's default 30 pages, so a title
    # past them read as absent. A short page ends the walk; a full one asks for the next.
    _p=1
    while :; do
        _pages=$(sh "$API" "/api/v1/repos/$REPO/wiki/pages?limit=50&page=$_p" 2>/dev/null) || return 2
        [ -n "$_pages" ] || return 2
        _n=$(printf '%s' "$_pages" | python3 -c '
import sys, json
want = sys.argv[1]
try:
    pages = json.loads(sys.stdin.read())
except ValueError:
    sys.exit(2)          # not JSON -- an error page, a proxy, a truncated read. NOT an absence.
if not isinstance(pages, list):
    sys.exit(2)          # an error object, e.g. {"message": ...}. Also not an absence.
for p in pages:
    if p.get("title") == want:
        sys.stdout.write(p.get("sub_url", ""))
        sys.exit(0)
print(len(pages))        # read in full and not here: the caller decides whether to page on.
sys.exit(1)
' "$PAGE")
        case $? in
            0) printf '%s' "$_n"; return 0 ;;
            1) [ "$_n" -ge 50 ] || return 1 ;;      # a short page: the listing is exhausted.
            *) return 2 ;;
        esac
        _p=$((_p + 1))
        [ "$_p" -le 100 ] || return 2              # runaway guard, as in _all_titles
    done
}

# Read the page. Prints content to stdout, or nothing and returns 1 if the page is absent.
# The 404 body is kept OUT of stdout so an absent page can never be mistaken for empty content.
fetch() {
    SUB=$(sub_url_for_title) || return $?      # propagates 1 (absent) and 2 (cannot tell) as-is
    [ -n "$SUB" ] || return 1
    # The listing was readable a moment ago, so a failure HERE is still not an absence -- the page
    # could have been deleted between the two calls, but so could the forge have gone away. The
    # API call's status separates them: non-zero is "cannot tell", a 404-shaped body is "absent".
    #
    # `_body` IS THE JSON ENVELOPE, NOT THE PAGE. digest()'s note forbids putting page CONTENT in a
    # shell variable because command substitution strips trailing newlines -- that hazard is real
    # and is not this: the content travels base64-encoded inside this JSON and is decoded straight
    # to stdout below, so a newline at the end of the page is inside the payload, not at the end of
    # the string being captured. Said out loud because the shape looks exactly like the bug.
    _body=$(sh "$API" "/api/v1/repos/$REPO/wiki/page/$SUB" 2>/dev/null) || return 2
    printf '%s' "$_body" | python3 -c '
import sys, json, base64
raw = sys.stdin.read()
if not raw.strip():
    sys.exit(1)
try:
    d = json.loads(raw)
except ValueError:
    sys.exit(1)
if not isinstance(d, dict) or "content_base64" not in d:
    sys.exit(1)      # an error object, e.g. {"message": "The target could not be found."}
sys.stdout.write(base64.b64decode(d["content_base64"]).decode("utf-8", "replace"))
'
}

# sha256 of stdin, over BYTES -- not over a size or a line count. 13,181 characters and 13,279
# bytes have already been reported as the same number once on this ticket, and they were two
# different quantities.
#
# THIS COMPARISON IS STRICT, AND AN EARLIER VERSION OF IT WAS NOT. That version stripped trailing
# newlines from both sides, justified by a measured one-byte shortfall on read-back and a comment
# asserting "Forgejo stores wiki content without the final newline". **That attribution was wrong.**
# The forge stores all 16,264 bytes, newline included -- verified by decoding the API response in
# Python with no shell in the path. The missing byte was `REMOTE=$(fetch)`: COMMAND SUBSTITUTION
# STRIPS TRAILING NEWLINES, so the read-back was corrupting its own evidence and the "fix" was a
# loosening that made a real check weaker to accommodate a bug in the harness around it.
#
# Hence `read_page_to`, below: the remote copy goes to a FILE and is never carried in a shell
# variable. With the corruption gone the comparison needs no tolerance, and the check is stronger
# than the one that was about to ship -- a trailing-newline change is now caught too.
digest() { sha256sum | cut -d' ' -f1; }

# Fetch the page into $1, byte-exactly. Returns 1 (leaving $1 absent) if there is no page.
# Never assign page content to a shell variable: see the note on digest().
read_page_to() {
    _dest="$1"
    rm -f "$_dest"
    # Propagates fetch's 1-vs-2 rather than flattening both to 1: `publish`'s read-back
    # and `verify`'s compare both treat a failure here as "no page to compare", and an unreachable
    # forge must not be reported that way.
    fetch > "$_dest.part" 2>/dev/null || { _rc=$?; rm -f "$_dest.part"; return "$_rc"; }
    mv "$_dest.part" "$_dest"
}

# Is $PAGE a session's ACCOUNT of its own work, rather than repo state? Only those get signed.
# `Handoff <session id>` is the name a session's account is published under (session-attest.sh's
# `page` verb computed it before attestation moved to its own repo; the naming stayed here); `Coordinator Handoff` is the fixed title of the current seat's.
_page_is_a_handoff() {
    case "$PAGE" in
        "Coordinator Handoff"|"Handoff "*) return 0 ;;
        *) return 1 ;;
    esac
}

# Is $PAGE ONE SESSIONS account, as opposed to a page handed between seats? `Coordinator
# Handoff` is signed like a handoff but is a rolling page whose author legitimately changes, so it
# is deliberately NOT in here -- see the block in `publish` that consults this.
_page_is_a_session_account() {
    case "$PAGE" in
        "Handoff "*) return 0 ;;
        *) return 1 ;;
    esac
}

# EVERY PAGE TITLE ON THE WIKI, PAGED, AND REFUSING RATHER THAN TRUNCATING.
#
# The wiki IS the registry: a live page is a claimed slot number. So a SHORT READ HANDS OUT A
# NUMBER THAT IS ALREADY TAKEN, and the next claim overwrites a live terminal's handoff -- the
# overwrite failure this file's header records, arriving through the registry instead of through a
# typo. The listing cap was left unmeasured and named as the registry's ceiling.
#
# MEASURED 2026-08-31 rather than assumed, and the cap turned out to be the wrong question:
#
#   X-Total-Count: 6   -- present on every response, so TRUNCATION IS DETECTABLE without knowing
#                         the cap. That is the property the registry actually needs.
#   limit=2..6         -- honoured exactly.
#   limit=0 and 1      -- IGNORED, returning all 6. A caller probing with `limit=1` gets the
#                         default page size and builds a wrong model of pagination.
#   limit=7,50,1000    -- return all 6; the true ceiling is unmeasurable at six pages and does
#                         not need measuring, because a short read is caught below.
#
# TWO INDEPENDENT SIGNALS, because either alone can lie: pages are collected until one comes back
# short AND the total collected is compared to the header. A mismatch REFUSES -- an allocator that
# guessed here would hand out a live number, and the failure is silent on both sides.
_all_titles() {
    _t=$(mktemp) || return 2
    _h=$(mktemp) || { rm -f "$_t"; return 2; }
    _b=$(mktemp) || { rm -f "$_t" "$_h"; return 2; }
    _page=1; _got=0; _total=""
    while :; do
        if ! sh "$API" "/api/v1/repos/$REPO/wiki/pages?limit=50&page=$_page" -D "$_h" > "$_b" 2>/dev/null; then
            rm -f "$_t" "$_h" "$_b"; return 2
        fi
        if [ -z "$_total" ]; then
            _total=$(tr -d '\r' < "$_h" | sed -n 's/^[Xx]-[Tt]otal-[Cc]ount: *//p' | head -n 1)
        fi
        _n=$(python3 - "$_b" "$_t" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(-1); raise SystemExit(0)
with open(sys.argv[2], "a") as out:
    for p in d:
        out.write((p.get("title") or "") + "\n")
print(len(d))
PY
)
        [ "$_n" = "-1" ] && { rm -f "$_t" "$_h" "$_b"; return 2; }
        _got=$((_got + _n))
        [ "$_n" -eq 0 ] && break
        [ -n "$_total" ] && [ "$_got" -ge "$_total" ] && break
        _page=$((_page + 1))
        [ "$_page" -gt 100 ] && break        # runaway guard: a paging bug must not loop for ever
    done
    rm -f "$_h" "$_b"
    if [ -z "$_total" ] || [ "$_got" != "$_total" ]; then
        echo "handoff: registry read is SHORT -- collected $_got, the forge says ${_total:-<no header>}." >&2
        echo "  Refusing rather than allocating: a truncated registry read hands out a slot number" >&2
        echo "  that a live terminal already holds, and the next publish overwrites its handoff." >&2
        rm -f "$_t"
        return 3
    fi
    cat "$_t"
    rm -f "$_t"
}

case "${1:-}" in
slot|publish|show|verify|url|delete) [ -n "$REPO" ] || { ft_repo; REPO=$FORGE_TOOLS_REPO; } ;;
esac
[ "${1:-}" != url ] || [ -n "${HUB_URL:-}${FORGE_TOOLS_FORGE_URL:-}" ] || ft_need FORGE_URL "the forge's base URL, which \`url\` prints the page under"
FORGE_TOOLS_FORGE_URL="${HUB_URL:-${FORGE_TOOLS_FORGE_URL:-}}"

case "${1:-}" in
slot)
    # THE REGISTRY, IMPLEMENTED.
    #
    #   a terminal is `CC 1` / `OMP 1`   -- harness prefix plus the lowest unclaimed slot number,
    #                                       reused after death (an earlier `CC-1` became
    #                                       `CC 1`, so slot, page and terminal are ONE string)
    #   its page is `Handoff CC 1`       -- SPACE, NEVER HYPHEN
    #   creating the page IS claiming the number, and deleting it frees the slot
    #
    # WHY THE SPACE IS NOT COSMETIC, measured: a hyphen ANYWHERE in a wiki title flips the
    # forge's space escaping from `-` to `+`, so `Handoff CC-3` becomes `Handoff+CC.-3` and stops
    # being addressable by hand. Keeping the hyphen out of every form means no derivation can
    # reintroduce it -- which is why the prefix is validated rather than trusted.
    #
    # CLAIM BY CREATION, AND THE RACE IS THE FORGE'S TO SETTLE. Two sessions reading "lowest
    # unclaimed" concurrently will pick the same number; nothing on this side can prevent that.
    # `POST /wiki/new` 4xxs on a page that exists (this file's own note at the publish path), so
    # the FORGE decides who won, and the loser re-scans and takes the next slot. An allocator that
    # checked-then-wrote would have a window between the two; this has none.
    case "${2:-}" in
    claim)
        PREFIX="${3:-CC}"
        # THE HOLDER IS AN ARGUMENT BECAUSE THE CLAIMER IS NOT ALWAYS THE HOLDER.
        # `session-succeed start` claims a slot FOR the successor it is about to spawn, and it
        # runs as the PREDECESSOR, so taking the holder from the environment stamped every
        # delegated claim with the wrong session. It did so silently: the successor's
        # `SUCCESSION_SLOT` was correct and only the page it named disagreed.
        #
        # A DELEGATED CLAIM CANNOT NAME ITS HOLDER AT ALL, and this is why the argument is a free
        # string rather than a session id. The harness mints the successor's session id (which its
        # adapter maps onto `FORGE_TOOLS_SESSION_ID`) when the pane starts, which is strictly after
        # the claim -- so there is no value `start`
        # could pass that would be right. It passes a sentence saying so, and the successor stamps
        # itself later with `session-succeed lineage`, which is run BY the holder about its own
        # page and was already the only thing that writes an identity here.
        HOLDER="${4:-${FORGE_TOOLS_SESSION_ID:-unknown}}"
        case "$PREFIX" in
            *[!A-Za-z0-9]*|"")
                echo "handoff: slot prefix must be alphanumeric, got '$PREFIX'." >&2
                echo "  A hyphen or space in the prefix reaches the page title, and a hyphen there" >&2
                echo "  makes the page addressable only as 'Handoff+CC.-3' (measured)." >&2
                exit 2 ;;
        esac
        _attempt=1
        while [ "$_attempt" -le 5 ]; do
            # VIA A FILE, NOT A PIPE. `python3 - <<PY` already uses stdin for the SCRIPT, so a
            # piped-in list is silently discarded and every title reads as absent -- measured: the
            # first version reported "0 other page(s)" against a wiki holding six, which is a
            # plausible-looking number produced by a check that read nothing.
            TFILE=$(mktemp) || exit 3
            _all_titles > "$TFILE" || { rm -f "$TFILE"; exit 1; }
            N=$(PREFIX="$PREFIX" python3 - "$TFILE" <<'PY'
import os, re, sys
pat = re.compile(r"^Handoff %s (\d+)$" % re.escape(os.environ["PREFIX"]))
used = set()
for line in open(sys.argv[1]):
    m = pat.match(line.rstrip("\n"))
    if m:
        used.add(int(m.group(1)))
n = 1
while n in used:
    n += 1
print(n)
PY
)
            rm -f "$TFILE"
            SLOT="$PREFIX $N"
            _hostkey=""
            _need_attest "host key" && _hostkey=$(session-attest host 2>/dev/null || true)
            BODY=$(python3 - "$SLOT" "$HOLDER" "$_hostkey" <<'PY'
import base64, json, sys
slot, sess = sys.argv[1], sys.argv[2]
# WHICH MACHINE CLAIMED IT. A stub has no attest block, so this line is the only thing
# that tells a reaper on another box it cannot judge the slot. Omitted when no key could be read.
host = sys.argv[3] if len(sys.argv) > 3 else ""
host_line = ("- claimed on host: %s\n" % host) if host else ""
# NEAR-EMPTY ON PURPOSE: the page exists to CLAIM the number at startup and is written to
# across the terminal's life. It still says who holds it and what it is, because a registry entry
# nobody can attribute is a slot nobody can safely reap.
#
# `holder session` KEEPS ITS SPACE and must never become `holder-session`. That hyphenated
# key is written by `session-succeed lineage` inside the lineage block, and its reader takes the
# FIRST match in the page (`sed ... | head -n 1`). This line is PROVISIONAL and that one is
# AUTHORITATIVE, so a lookup must never have to choose between them.
#
# TODAY THE ORDER HAPPENS TO FAVOUR THE AUTHORITY, WHICH IS WHY THIS IS A TRAP RATHER THAN A BUG.
# `lineage` splices its block immediately after the first heading, i.e. ABOVE this line, so a
# unified key would currently still resolve correctly -- measured on `Handoff CC 1`, where the
# lineage line is 7 and this one is 24. Nothing CONTRACTS that placement: it is one `re.search` for
# `^#[^\n]*\n` in another script, and a page whose heading moved, or a splice point that changed,
# would silently start answering with a claim-time value that was never an identity. Distinct keys
# make the question unaskable instead of correctly-answered-by-luck.
text = (
    "# Handoff %s\n\n"
    "**Slot claimed, no handoff written yet.** Creating this page is what claims the number "
    "`%s`; deleting it frees the slot.\n\n"
    "- holder session: `%s`\n%s"
    "- PROVISIONAL: the line above is what the CLAIMER said at claim time, and a slot can be "
    "claimed for someone else -- `start` claims one for the successor it is spawning, before that "
    "session exists to be named. The authority is `- holder-session:` in the lineage "
    "block, written by the holder itself via `session-succeed lineage`. Until that has run, "
    "this page attributes the slot no further than the sentence above.\n"
    "- predecessor: none recorded yet -- the predecessor is named INSIDE this page, never in its "
    "title.\n" % (slot, slot, sess, host_line))
print(json.dumps({"title": "Handoff " + slot,
                  "content_base64": base64.b64encode(text.encode()).decode(),
                  "message": "slot: claim %s" % slot}))
PY
)
            # THE CREATE'S OWN WORDS ARE KEPT. This was `>/dev/null 2>&1`, and every failure
            # then printed "taken between the read and the write": a stale client's refused write read
            # as a race and was retried into "could not claim" -- 10 of 10 from the reference tree two
            # commits behind hub/main (2026-09-15), while a current worktree claimed on attempt 1.
            if CREATE_OUT=$(printf '%s' "$BODY" | sh "$API" "/api/v1/repos/$REPO/wiki/new" -X POST --json @- 2>&1); then
                echo "$SLOT"
                echo "  claimed by creating \"Handoff $SLOT\" (attempt $_attempt)." >&2
                echo "  Free it by deleting that page: handoff.sh delete, with HANDOFF_PAGE set." >&2
                # THE CLAIM IS PROVISIONAL UNTIL THE HOLDER STAMPS IT. The page says so
                # in its own text, and nothing told the claimer what to do about it. A hand-claimed
                # slot carries only the space-keyed line above; `- holder-session:` -- the key every
                # reader treats as authoritative, including the publish guard -- is written by
                # `lineage`, which is run BY the holder about its own page. Skipped for a DELEGATED
                # claim, where the holder does not exist yet to run anything.
                if [ -z "${4:-}" ] && [ -n "${FORGE_TOOLS_SESSION_ID:-}" ]; then
                    echo "  NOT YET AUTHORITATIVE. Stamp it, or this page names you only provisionally:" >&2
                    echo "      session-succeed lineage \"Handoff $SLOT\" \"$SLOT\"" >&2
                fi
                exit 0
            fi
            # A FAILED CREATE IS A LOST RACE ONLY IF THE PAGE NOW EXISTS. `POST /wiki/new` refuses an
            # existing title, so losing leaves the winner's page behind; a refused write or a forge that
            # is down leaves nothing, and retrying it only repeats it. So look, and say which.
            TFILE=$(mktemp) || exit 3
            if ! _all_titles > "$TFILE"; then
                rm -f "$TFILE"
                echo "handoff: creating \"Handoff $SLOT\" FAILED, and the registry could not be re-read to tell a lost race from a refusal -- stopping rather than retrying blind. The client said:" >&2
                printf '%s\n' "$CREATE_OUT" | sed 's/^/    /' >&2
                exit 1
            fi
            if grep -qxF "Handoff $SLOT" "$TFILE"; then
                rm -f "$TFILE"
                echo "  slot $SLOT was taken between the read and the write -- re-scanning." >&2
                _attempt=$((_attempt + 1))
                continue
            fi
            rm -f "$TFILE"
            echo "handoff: creating \"Handoff $SLOT\" FAILED and no such page exists, so this is not a lost race and it is not retried. The client said:" >&2
            printf '%s\n' "$CREATE_OUT" | sed 's/^/    /' >&2
            exit 1
        done
        echo "handoff: could not claim a slot in 5 attempts -- another claimer won the number every" >&2
        echo "  time (each failure above left a page). Run 'handoff.sh slot list' and look before retrying." >&2
        exit 1 ;;
    list)
        PREFIX="${3:-}"
        TFILE=$(mktemp) || exit 3
        _all_titles > "$TFILE" || { rm -f "$TFILE"; exit 1; }
        PREFIX="$PREFIX" python3 - "$TFILE" <<'PY'
import os, re, sys
pre = os.environ.get("PREFIX") or ""
pat = re.compile(r"^Handoff (%s\S*) (\d+)$" % (re.escape(pre) if pre else r"[A-Za-z0-9]+"))
rows, other = [], 0
for line in open(sys.argv[1]):
    t = line.rstrip("\n")
    if not t:
        continue
    m = pat.match(t)
    if m:
        rows.append((m.group(1), int(m.group(2))))
    else:
        other += 1
for p, n in sorted(rows, key=lambda r: (r[0], r[1])):
    print("  %s %d   (page: Handoff %s %d)" % (p, n, p, n))
print("%d claimed slot(s); %d other page(s) on the wiki." % (len(rows), other))
if not rows:
    # An empty registry and an unread one look identical, and only one of them means "slot 1 is
    # free". `_all_titles` has already refused a short read, so this is the honest reading.
    print("NO SLOTS CLAIMED. The registry was read in full (the total was cross-checked),")
    print("so this is an absence rather than a failed read.")
PY
        rm -f "$TFILE"
        ;;
    *) echo "handoff: slot <claim [prefix]|list [prefix]>" >&2; exit 2 ;;
    esac
    ;;

publish)
    # `--dry-run`: decide everything, touch nothing.
    #
    # THIS EXISTS BECAUSE A NEGATIVE CONTROL COULD NOT BE WRITTEN SAFELY, and that is a fact about
    # this script rather than about whoever was testing it. The guard above refuses an over-budget
    # cache page; proving it refuses is free, because a refusal is inert. Proving it does NOT refuse
    # an acceptable page could only be observed by letting the write happen -- so the safe half of
    # the test was inexpressible, and the obvious workaround (use the real page, it is only a small
    # file) PUBLISHED A 43-BYTE FIXTURE OVER THE LIVE `Session Cache` on 2026-08-30. 9,461 bytes to
    # 43. Recovered only because a read-back copy happened to be sitting in /tmp.
    #
    # So this is not a convenience flag. It is the missing half of a testable contract: with it,
    # "would this be refused?" is answerable without a write, and nobody has to point a control at
    # production to answer it.
    #
    # IT RUNS EVERY DECISION, WHICH IS THE ONLY VERSION WORTH HAVING. Size ceiling, missing/empty
    # file, attestation report, and create-vs-update all execute exactly as they would on a real
    # publish -- a dry run that skipped a check would answer a different question than the one
    # asked, which is the shape this repo keeps filing tickets about. The single thing it skips is
    # the HTTP call and the read-back that follows it.
    #
    # EXIT STATUS IS THE VERDICT: 0 = would publish, non-zero = would refuse. That makes it usable
    # from a test as an assertion rather than as prose to grep.
    DRY_RUN=0
    if [ "${2:-}" = "--dry-run" ]; then DRY_RUN=1; shift; fi
    FILE="${2:-$FILE_DEFAULT}"
    [ -s "$FILE" ] || { echo "handoff: $FILE is missing or empty -- refusing to publish nothing" >&2; exit 1; }
    # ONE counter for the ceiling AND the report. `wc -m` counts BYTES when no locale is
    # exported, which is the Bash tool's env, so it refused an 8,998-character em-dashed page.
    CHARS=$(python3 -c 'import sys;print(len(open(sys.argv[1],encoding="utf-8").read()))' "$FILE")

    # DO NOT PUBLISH OVER A PAGE A DIFFERENT LIVE SESSION HOLDS.
    #
    # MEASURED 2026-09-21 on `Handoff CC 1`, from the forge's own wiki revisions:
    #     18:06:15  slot: claim CC 1                      edad0f6c claims; page created
    #     22:12:49  handoff: publish                      4826be48 publishes ONTO it
    #     22:12:56  its `predecessor` record, 7s later
    # There is no `slot: claim` between those two. The second session never claimed the slot -- it
    # named the page and published, and the holder learned of it four hours later from `slot list`.
    #
    # WHY NO EXISTING REFUSAL COULD HAVE CAUGHT IT (e78a6cb6's reading, and it is the sharp one):
    # publish refuses a from-scratch draft and a dropped `<!-- x:begin -->` block. Both are shaped
    # for a WRITTEN page. A fresh claim is a near-empty stub carrying NEITHER, so the guard surface
    # was built around a different object entirely and one more content-shaped rule would miss this
    # too. The thing to check is the HOLDER, not the bytes -- which is what `adopt` already does.
    #
    # THREE OUTCOMES, KEPT DISTINGUISHABLE ON PURPOSE. A session must be able to tell "someone else
    # is alive on this page" from "I could not find out", because the two call for opposite actions.
    #
    # THE OWN-PAGE CASE NEVER CONSULTS THE REGISTRY, and that is what makes this safe to put in
    # front of a verb every session runs. If the holder is this session, we return before any
    # resolve -- so a registry that is briefly unable to answer cannot lock a session out of its own
    # page. The resolve happens only when the page names SOMEBODY ELSE.
    #
    # AN UNRESOLVABLE FOREIGN HOLDER PROCEEDS, LOUDLY. `session-attest.sh resolve` says of itself
    # that the registry holds live sessions only, so a miss "does NOT distinguish a session that
    # ENDED from one that never existed". Refusing on that would block every legitimate publish to
    # an inherited page whose original claimer is gone -- the `adopt` case by construction. So it
    # proceeds and says exactly what it checked.
    _holder_of_page() {
        # The AUTHORITATIVE key first (`- holder-session:`, written by `session-succeed
        # lineage`), then the PROVISIONAL one `slot claim` writes (`- holder session:`, a SPACE).
        # They are kept distinct so a reader never has to choose; taking them in order here is
        # that rule applied, not a second opinion about it.
        fetch 2>/dev/null | sed -n \
            -e 's/^- holder-session: *`\{0,1\}\([0-9a-f-][0-9a-f-]*\)`\{0,1\} *$/\1/p' \
            -e 's/^- holder session: *`\{0,1\}\([0-9a-f-][0-9a-f-]*\)`\{0,1\} *$/\1/p' \
            | head -n 1
    }
    if [ "${HANDOFF_ALLOW_FOREIGN_HOLDER:-}" != 1 ]; then
        _pub_holder=$(_holder_of_page)
        _pub_me="${FORGE_TOOLS_SESSION_ID:-}"
        if [ -n "$_pub_holder" ] && [ "$_pub_holder" != "$_pub_me" ]; then
            # Without the command, "does not resolve" would be read as "not live" and a live
            # holder's page overwritten -- the one direction this guard exists to stop. Fail closed.
            if ! _need_attest "liveness check of holder $_pub_holder"; then
                echo "handoff: REFUSING to publish over \"$PAGE\" -- it names holder $_pub_holder and" >&2
                echo "  whether that session is live cannot be checked. HANDOFF_ALLOW_FOREIGN_HOLDER=1 overrides." >&2
                exit 1
            fi
            if session-attest resolve "$_pub_holder" >/dev/null 2>&1; then
                echo "handoff: REFUSING to publish over \"$PAGE\" -- it names holder $_pub_holder," >&2
                echo "  and that session resolves to a LIVE session which is not this one." >&2
                echo "  Publishing would overwrite a page somebody is holding; that is how CC 1 was" >&2
                echo "  taken from a live session on 2026-09-21, unnoticed for four hours." >&2
                echo "  If you mean it, say so: HANDOFF_ALLOW_FOREIGN_HOLDER=1" >&2
                exit 1
            fi
            echo "handoff: NOTE -- \"$PAGE\" names holder $_pub_holder, which is not this session and" >&2
            echo "  does not resolve to a live one. Publishing anyway: the session registry holds LIVE" >&2
            echo "  sessions only, so a miss does not distinguish a session that ENDED from one that" >&2
            echo "  never existed, and refusing here would block every inherited page." >&2
        fi
    fi

    # THE SIZE CEILING, FOR INJECTED PAGES ONLY. Past 10,000 CHARACTERS of one SessionStart
    # hook's stdout, Claude Code persists the hook and injects a ~2KB preview instead, so the cache
    # stops arriving while the session looks entirely healthy (measured).
    #
    # WHY A REFUSAL HERE, when the attestation directly below is deliberately only a REPORT. The two
    # differ in what the caller can do about it. A missing attestation is a judgement -- some pages
    # should carry one and some should not -- so the caller is told and decides. An over-cliff cache
    # has no such reading: the next session loses it silently, and the author is holding the file
    # that fixes it. Refusing costs one trim; accepting costs every session until someone notices,
    # and the failure mode is that nobody does.
    #
    # It also loses nothing on the refusal path. The already-published page stays, and a coherent
    # stale cache beats a silently truncated fresh one -- past the cliff only a ~2KB head arrives, so
    # "published" would mean "the first fifth of it published".
    #
    # NO OVERRIDE FLAG, deliberately. The remedy is to trim the page, which the caller can do in the
    # file already in their hand; an escape hatch here would be reached for at exactly the moment the
    # ceiling matters -- end of session, in a hurry.
    #
    # LIST, NOT A PATTERN, AND `test_handoff.py` PINS IT AGAINST `settings.json`. `publish` is not a
    # cache transport: handoff pages and `TODO` go through it too, and those are not injected, so a
    # blanket ceiling would refuse a legitimate 12KB handoff. A hand-maintained list can drift from
    # what is actually wired, so the test asserts this list equals the pages `settings.json` injects
    # -- adding a third cache without extending this list fails there rather than silently.
    #
    # CHARACTERS, NOT BYTES: measured on em-dashed prose, 8,817 characters is 10,417 bytes, so a byte
    # threshold declares a healthy page over a 10,000-CHARACTER cliff. A check that fires on good
    # input gets switched off.
    #
    # THE LIST IS CONFIGURATION (FORGE_TOOLS_INJECTED_PAGES, set above). Unset, no
    # page is held to a ceiling -- and that is SAID on every publish, because an injected page
    # published without its ceiling looks published and arrives truncated. Set-but-empty is a
    # deployment stating it injects nothing, and is quiet.
    if [ -z "${FORGE_TOOLS_INJECTED_PAGES+set}" ]; then
        echo "handoff: NOTE -- FORGE_TOOLS_INJECTED_PAGES is unset, so no page is held to a session-start" >&2
        echo "  injector's size ceiling. If your harness injects \"$PAGE\", name it there (see config.example)." >&2
    fi
    if [ -n "$INJECTED_PAGES" ] && printf '%s\n' "$INJECTED_PAGES" | grep -qxF "$PAGE"; then
        # THE HOOK IS REQUIRED ONLY HERE, INSIDE THE INJECTED-PAGE BRANCH. A handoff page or `TODO`
        # has no cliff, so demanding the injector for those made every publish depend on a sibling
        # file -- and the tests run a COPY of this script from a temp directory, so that refused 53
        # of them at once. The dependency is real for a cache and absent for everything else.
        #
        # NO DEFAULT PATH. The injector is a HARNESS hook that lives in
        # the consumer repo, not in Forge-Tools, so only the caller can name it. Unset is the same
        # case as unreadable -- the budget cannot be read -- and gets the same REFUSAL, said by name.
        [ -n "${HANDOFF_CACHE_HOOK:-}" ] || {
            echo "handoff: HANDOFF_CACHE_HOOK is not set, so the injector that owns the cache budget" >&2
            echo "  for \"$PAGE\" cannot be read -- REFUSING. Point it at the harness hook that injects" >&2
            echo "  this page (a session-start hook). Publishing without" >&2
            echo "  its ceiling would look published and arrive truncated." >&2
            exit 1
        }
        CACHE_HOOK=$HANDOFF_CACHE_HOOK
        [ -r "$CACHE_HOOK" ] || {
            echo "handoff: cannot read $CACHE_HOOK, which owns the cache budget -- REFUSING." >&2
            echo "  Publishing an injected page without its ceiling is the failure this guard" >&2
            echo "  exists for: it would look published and arrive truncated." >&2
            exit 1
        }
        # THE CEILING IS DERIVED FROM THE INJECTOR, NEVER RESTATED HERE. This used to be a
        # literal 9500 justified by "+375 characters" of wrapper, while the injector's own header
        # measured the notice at +599 -- so the write side permitted a page the read side could not
        # deliver, and a 9,499-character cache passed by one character. Reading both numbers from
        # the hook makes the two sides one number; `test_handoff.py` fails if either goes missing.
        _cliff=$(sed -n 's/^CACHE_CLIFF_CHARS=\([0-9][0-9]*\).*/\1/p' "$CACHE_HOOK" | head -1)
        _reserve=$(sed -n 's/^CACHE_NOTICE_RESERVE_CHARS=\([0-9][0-9]*\).*/\1/p' "$CACHE_HOOK" | head -1)
        case "$_cliff$_reserve" in
            ''|*[!0-9]*)
                echo "handoff: cannot read the cache budget from $CACHE_HOOK -- REFUSING." >&2
                echo "  The ceiling is derived from CACHE_CLIFF_CHARS and CACHE_NOTICE_RESERVE_CHARS" >&2
                echo "  there. If either was renamed, fix this reader rather than restating a number." >&2
                exit 1 ;;
        esac
        if [ "$CHARS" -ge $((_cliff - _reserve)) ]; then
            echo "handoff: REFUSING to publish \"$PAGE\" -- $CHARS characters, ceiling $((_cliff - _reserve))." >&2
            echo "  The SessionStart cliff is 10,000 characters of one hook's stdout, and the" >&2
            echo "  injector adds a wrapper on top of the page (measured +375 on the stale path)." >&2
            echo "  Past it the cache is replaced by a ~2KB preview and stops arriving SILENTLY." >&2
            echo "  This is a cache: trim it. Deleting a line that stopped being current is the" >&2
            echo "  intended maintenance, not an exception -- see the file's own header." >&2
            exit 1
        fi
    fi

    # THE TRIGGER GATE, FOR PAGES THAT CARRY A ROSTER CLAIM. Invariant 23 asserts that
    # every backticked `/trigger` a roster doc names resolves to an installed skill or command. Its
    # corpus is FILES, and `TODO.md` left the repo on 2026-09-03 to become a wiki page --
    # so the backlog stopped being gated by the invariant that exists BECAUSE OF IT. From
    # the invariant's own record: TODO.md declined to vendor `skills-create` because it
    # "overlaps `/writing-great-skills`", and that skill had since been removed, so the rationale had
    # silently become false with all 8 check-runs green.
    #
    # PUBLISH TIME IS THE ONLY MOMENT AVAILABLE, and that is a measurement rather than a preference.
    # A check that FETCHES the page would need a forge credential, and the CI runner has none --
    # measured in the same suite run, a test that reads the forge skips with "unreachable or
    # unauthenticated". A skipped gate reads exactly like a passing one, which is worse than no gate
    # because it claims coverage. Here the bytes are already in hand and no credential is involved.
    #
    # THE HOLE, STATED: this gates the TOOL, not the PAGE. Editing through the forge web UI bypasses
    # it entirely -- the same limitation the injector hook names, which is exactly why the cache
    # ceiling lives at INJECTION time instead. Nothing injects the backlog, so there is no second
    # moment to move to and this is the best available rather than the complete answer.
    #
    # A LIST, PINNED BY A TEST, for the reason `INJECTED_PAGES` above is one: a hand-maintained list
    # drifts from what is actually gated, silently.
    #
    # THE LIST IS CONFIGURATION (FORGE_TOOLS_TRIGGER_PAGES, set above), empty by
    # default: no page is gated until a deployment names the pages that carry roster claims.
    if [ -n "$TRIGGER_PAGES" ] && printf '%s\n' "$TRIGGER_PAGES" | grep -qxF "$PAGE"; then
        # THE ROOTS ARE THE CONSUMER'S, THE EXCEPTIONS ARE FORGE-TOOLS' (skills are
        # harness agnostic). A trigger names a skill installed in the repo this page is
        # published FROM -- the git top level of the cwd, as every other measurement below uses --
        # never in the tree this script happens to live in.
        _trig=$(python3 - "$(git rev-parse --show-toplevel 2>/dev/null)" "$FILE" "$HERE/skill_trigger_exceptions.txt" <<'PY' 2>&1
import os, re, sys
root, path, exc_file = sys.argv[1], sys.argv[2], sys.argv[3]
if not root:
    print("UNCHECKED not inside a git work tree, so there is no repo root to resolve skill roots against")
    raise SystemExit(0)
# THE EXCEPTION LIST SHIPS WITH FORGE-TOOLS (it used to be read out of one consumer's own check
# script -- a file no other consumer has). FORGE_TOOLS_TRIGGER_EXCEPTIONS EXTENDS it; it cannot shrink it.
try:
    EXC = {t for t in (l.split("#", 1)[0].strip() for l in open(exc_file)) if t}
except OSError as exc:                                     # reported, not swallowed
    print("UNCHECKED %s" % exc)
    raise SystemExit(0)
EXC |= set(os.environ.get("FORGE_TOOLS_TRIGGER_EXCEPTIONS", "").split())
# `.split() or DEFAULT`: a set-but-blank root list would resolve NOTHING and refuse every page,
# which is a check firing on good input.
SKILLS = os.environ.get("FORGE_TOOLS_SKILL_ROOTS", "").split() or [".agents/skills", ".claude/skills"]
COMMANDS = os.environ.get("FORGE_TOOLS_COMMAND_ROOTS", "").split() or [".claude/commands", ".agents/commands"]
bad = []
for tok in sorted(set(re.findall(r"`(/[a-z0-9][\w-]*)`", open(path).read()))):
    name = tok.lstrip("/")
    if (any(os.path.isfile(os.path.join(root, r, name, "SKILL.md")) for r in SKILLS)
            or any(os.path.isfile(os.path.join(root, r, name + ext)) for r in COMMANDS for ext in (".md", ".toml"))
            or name in EXC or tok in EXC):
        continue
    bad.append(tok)
print("BAD %s" % " ".join(bad) if bad else "OK")
PY
)
        case "$_trig" in
            # PERMIT ON AN UNLOADABLE EXCEPTION LIST, LOUDLY. Treating a failed import as "no
            # exceptions" would flag `/login` and refuse a page that is fine, and a check that fires
            # on good input is one every reader learns to switch off. Absence of a check is said out
            # loud instead, which is the honest half.
            UNCHECKED*)
                echo "handoff: NOT CHECKED -- \"$PAGE\" carries roster triggers and they could not be" >&2
                echo "  resolved:" >&2
                echo "    ${_trig#UNCHECKED }" >&2
                echo "  Publishing anyway; treat invariant 23 as UNMEASURED for this revision." >&2 ;;
            BAD*)
                echo "handoff: REFUSING to publish \"$PAGE\" -- it names trigger(s) that resolve" >&2
                echo "  to no installed skill or command:" >&2
                echo "    ${_trig#BAD }" >&2
                echo "  Looked for <root>/<name>/SKILL.md under FORGE_TOOLS_SKILL_ROOTS" >&2
                echo "  (\"${FORGE_TOOLS_SKILL_ROOTS:-.agents/skills .claude/skills}\") and <root>/<name>.md|.toml" >&2
                echo "  under FORGE_TOOLS_COMMAND_ROOTS (\"${FORGE_TOOLS_COMMAND_ROOTS:-.claude/commands .agents/commands}\")." >&2
                echo "  This is invariant 23, applied where the page can still be measured. A" >&2
                echo "  decision resting on a dead trigger is not cosmetic: a removed skill once" >&2
                echo "  left the backlog declining to vendor another one BECAUSE of it." >&2
                echo "  Fix the text in the file you are holding, or name the token in" >&2
                echo "  FORGE_TOOLS_TRIGGER_EXCEPTIONS if it is not a trigger at all." >&2
                exit 1 ;;
        esac
    fi

    # IS THE TREE THIS WAS WRITTEN FROM CURRENT?
    #
    # A handoff makes claims about what its reader will have loaded. Written from a stale tree those
    # claims are false, and publish is the one moment that can catch it cheaply. THE MEASURED
    # INSTANCE: a predecessor wrote "guard-bypass-guard.py is live on main ... you are very likely
    # the first session it can refuse" -- true of `hub/main`, false for the successor it had placed
    # in a tree six commits behind that contained neither the hook nor its rule.
    #
    # WHY reference-tree-guard DID NOT CATCH IT, and this is the interesting part: that guard RAN
    # and was CORRECT. It measured `0 behind, 0 ahead` at SessionStart. The tree then drifted six
    # behind DURING the session, from merges that session performed. So this is not a missing
    # instrument -- it is a measurement that was true when taken, carried forward as current by the
    # party who invalidated it. Same family as a freshness claim needing a re-measure at the moment
    # it is WRITTEN, applied to the tree instead of to a page.
    #
    # WHAT IT COVERS, AND THE BOUNDARY IS MEASURED RATHER THAN ASSUMED. Two surfaces sit in the class
    # "the handoff asserts something false about the successor's environment", and they behave in
    # OPPOSITE ways:
    #
    #   PreToolUse hooks and guards  -- follow the TREE, re-read live, mid-session.  COVERED.
    #   SessionStart-injected context (CLAUDE.md, the caches, the handoff itself)
    #                                -- pinned at the session's LAUNCH.              NOT COVERED.
    #
    # Proved by probe, not by reasoning: two sessions launched from a commit whose `settings.json`
    # did not register `guard-bypass-guard` both had it DENYING once the tree carried it. An earlier
    # claim that it "cannot be active for a session launched before it existed" was false, and it was
    # the third of four successive wrong assertions about the same fact -- every one an author-side
    # inference about someone else's environment, and none derivable from the author's side at all.
    #
    # So the warning SAYS WHICH HALF IT COVERS. Silence here must not read as "the environmental
    # claims are sound"; the general case is the two-party audit, and this does not substitute for it.
    #
    # WARN, NEVER REFUSE. A handoff written from a deliberately pinned tree is legitimate and the
    # author is the only party who knows whether the pin was deliberate. Refusing would make this
    # script the judge of a question it cannot answer.
    #
    # HANDOFF PAGES ONLY. `Session Cache`, `Hot Cache` and `TODO` are repo state and carry no claims
    # about a reader's environment; warning on them would be noise on the path of every cache write.
    if _page_is_a_handoff; then
        # FETCHED FIRST, because `HEAD..hub/main` against a remote ref nobody updated measures the
        # distance to a stale idea of main -- a check that reports 0 because it never looked. If the
        # fetch fails the currency is UNMEASURED, and that is said rather than passed over as clean.
        if git fetch -q "$FORGE_TOOLS_REMOTE" main 2>/dev/null; then
            _behind=$(git rev-list --count "HEAD..$FORGE_TOOLS_REMOTE/main" 2>/dev/null || echo "")
            if [ -n "$_behind" ] && [ "$_behind" != 0 ]; then
                echo "handoff: NOTE -- this tree is $_behind commit(s) behind $FORGE_TOOLS_REMOTE/main." >&2
                echo "  A handoff makes claims about what its reader will have loaded, and you are" >&2
                echo "  writing from a tree that is not what a successor cloning main would get." >&2
                echo "  COVERED by this warning: PreToolUse hooks and guards, which follow the tree" >&2
                echo "    live -- a claim that some guard is active is the shape that has been wrong." >&2
                echo "  NOT COVERED: anything SessionStart-injected (CLAUDE.md, the caches, the" >&2
                echo "    handoff itself). Those are pinned at the successor's launch, and no tree" >&2
                echo "    measurement reaches them. Silence on that half is not reassurance." >&2
                echo "  Not a refusal: a deliberately pinned tree is legitimate and only you know." >&2
            fi
        else
            echo "handoff: NOTE -- could not fetch $FORGE_TOOLS_REMOTE/main, so this tree's" >&2
            echo "  currency is UNMEASURED. That is not the same as current: a handoff written from" >&2
            echo "  a stale tree makes false claims about what its reader has loaded." >&2
        fi
    fi

    # WHO WROTE THIS PAGE. A handoff names a session, and git cannot attribute between
    # sessions on one box -- every agent commits as the same identity by design. An earlier change
    # built `session-attest.sh` to separate the signable half from the checkable half, and then
    # NOTHING CALLED IT. Measured 2026-08-30: of six pages on the wiki, ONE carried an attestation
    # block, and it was written by the seat that built the tool. The next handoff published after it
    # carried none, and neither did the coordinator's.
    #
    # WHY A REPORT AND NOT A REFUSAL, which is the opposite of this repo's usual fail-closed habit.
    # `publish` is not a handoff-only transport -- `Session Cache`, `Hot Cache` and `TODO` go through
    # it too, and those are REPO state, not a session's account of itself. Attesting them would say
    # nothing: no session claims authorship of what the box currently is. A blanket refusal would
    # demand a signature on pages where a signature is meaningless, which is the same defect this
    # ticket is about -- a line whose authority does not match its evidence -- introduced in the
    # name of fixing it. So the caller is told, once, and decides.
    #
    # STDERR IS THE RIGHT CHANNEL HERE, and that is not in tension with an earlier finding that
    # measured a
    # HOOK's stderr going undelivered at exit 0; this is a script the caller invoked directly, whose
    # stderr lands in the caller's terminal or tool result. The two are different delivery paths and
    # the distinction is the whole of that finding.
    #
    # The marker is defined by `session-attest.sh` (BEGIN); it is matched literally here rather than
    # shelling out, because `check` answers "is that pid still live", which is a different question
    # from "is this page signed at all" and would fail for an author who has since exited.
    # STAMPING, AND WHY ONLY HANDOFF PAGES. The report above tells a caller to sign; this signs for
    # them, because a one-line fix that nothing performs is what an earlier change already shipped -- the `page`
    # verb of session-attest.sh NAMES THIS CALL SITE IN A COMMENT and nothing ever edited this file.
    # A consumer written in prose and never gated is a known failure shape, on the tool built for this.
    #
    # The scope is the argument above, applied rather than restated: `Session Cache`, `Hot Cache` and
    # `TODO` are repo state and no session authors them, so they are not stamped and the note below
    # still invites a human to decide. A handoff IS a session's account of its own work, which is the
    # one case where a signature says something. The predicate is the naming convention
    # session-attest.sh already owns (`Handoff <id>`), plus the coordinator's fixed title.
    #
    # THE FILE IS REWRITTEN, NOT JUST THE UPLOAD, and that is load-bearing. `publish` proves it
    # worked by digesting the local file and the read-back and demanding they match, and `verify`
    # compares the draft to the page. Stamping only the uploaded bytes would break BOTH: a good
    # publish would report MISMATCH, and `verify` would report DIFFER for ever. Stamping the draft
    # keeps file and page byte-identical, so every existing guarantee survives untouched.
    #
    # An existing block is REPLACED, not appended to: re-publishing a page four times must not leave
    # four signatures, and the last stamp is the true one.
    # REFUSE TO RE-ATTRIBUTE ONE SESSIONS OWN ACCOUNT, narrowed by two existing tests
    # that contract the opposite for the OTHER kind of page. Both kinds ride this transport:
    #
    #   `Coordinator Handoff`  the seats ROLLING page, handed between seats by design. Signing
    #                          REPLACES here, and must: an implementation that skips when a block
    #                          exists keeps the FIRST authors name for ever, so a page edited by a
    #                          second seat still names the first. Measured -- two seats republished
    #                          it in one night. UNCHANGED BY THIS.
    #   `Handoff <id>`         ONE sessions account of its own work. Another session publishing it
    #                          is RESTORING or CORRECTING, and neither makes it their document.
    #
    # `publish` cannot tell "I am the new author" from "I am restoring what someone else wrote" --
    # only the caller knows, which is exactly why HANDOFF_NO_STAMP exists. So the split is on the
    # page KIND, where the answer is knowable, rather than on intent, where it is not.
    #
    # THE OWNER IS READ FROM A TRAILING WELL-FORMED BLOCK ONLY, by rfind, for the same reason the
    # stripper below does: a page ABOUT attestation quotes these markers inside a fence, and a first
    # attempt here matched any `attested-session:` line and refused to publish a note documenting it.
    # AN OPERATOR-INSTRUCTION BLOCK MUST BE VERBATIM, AND VERBATIM IS MEASURED. The line a
    # successor merges under reached it as its predecessor's paraphrase, marked "verbatim in spirit",
    # and nothing on the page could tell a faithful paraphrase from a drifted one. A block rendered
    # by `session-succeed operator-lines --block` carries each line's sha256 prefix; here every
    # quoted line is re-hashed and checked against the publishing session's own transcript. A
    # mismatch is a paraphrase (or a retyping) and is REFUSED before anything is written. Prose
    # quoting the operator outside a block is not checked -- `arrive` tells the successor to read
    # it as recollection. Restores (HANDOFF_NO_STAMP) skip it: the lines are another session's.
    # UNMEASURABLE (no transcript, no session id, no sibling script) is REPORTED, never read as
    # verified -- an omp session has no transcript there and publishes with the note.
    if _page_is_a_handoff && [ -z "${HANDOFF_NO_STAMP:-}" ] && grep -q '<!-- operator-instruction:begin -->' "$FILE" 2>/dev/null; then
        if command -v session-succeed >/dev/null 2>&1; then
            _hashes=$(session-succeed operator-lines --hashes 2>&1) && _hrc=0 || _hrc=$?
        else
            _hashes="MISSING COMMAND \`session-succeed\` (from the Session-Succession repo) -- not on PATH"; _hrc=2
        fi
        if [ "$_hrc" -ne 0 ]; then
            echo "handoff: NOTE -- \"$PAGE\" carries an operator-instruction block and it could NOT be verified" >&2
            echo "  against a transcript: ${_hashes}" >&2
            echo "  Publishing; the block is UNVERIFIED, not verbatim. Say so on the page." >&2
        else
            _bad=$(FILE="$FILE" HASHES="$_hashes" python3 - <<'PY'
import hashlib, os, re
body = open(os.environ["FILE"], encoding="utf-8").read()
body = re.sub(r"^```.*?^```", "", body, flags=re.S | re.M)
known = set(os.environ["HASHES"].split())
bad = []
for blk in re.findall(r"<!-- operator-instruction:begin -->(.*?)<!-- operator-instruction:end -->", body, flags=re.S):
    heads = list(re.finditer(r"^line (\d+) \([^)]*\) sha256:([0-9a-f]{6,64})[ \t]*$", blk, flags=re.M))
    if not heads:
        bad.append("no `line N (...) sha256:...` header inside the block -- render it with operator-lines --block")
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(blk)
        quoted = [l[2:] if l.startswith("> ") else l[1:] for l in blk[h.end():end].split("\n") if l.startswith(">")]
        text = "\n".join(quoted)
        got = hashlib.sha256(text.encode("utf-8")).hexdigest()
        stated = h.group(2)
        if not got.startswith(stated):
            bad.append("line %s: the quoted text hashes to %s, the header says %s -- edited or retyped" % (h.group(1), got[:12], stated))
        elif not any(k.startswith(stated) for k in known):
            bad.append("line %s: sha256:%s is not any line of this session's transcript (%d lines) -- another session's, or invented" % (h.group(1), stated, len(known)))
print("\n".join(bad))
PY
)
            if [ -n "$_bad" ]; then
                echo "handoff: REFUSING to publish \"$PAGE\" -- its operator-instruction block is NOT verbatim:" >&2
                printf '%s\n' "$_bad" | sed 's/^/    /' >&2
                echo "  A paraphrase of the operator is the widest authority on the page arriving unverifiable." >&2
                echo "  Render the block from the transcript instead of typing it:" >&2
                echo "      session-succeed operator-lines            # find the line numbers" >&2
                echo "      session-succeed operator-lines --block N   # paste the output into the page" >&2
                echo "  Nothing has been sent." >&2
                exit 1
            fi
            echo "handoff: operator-instruction block verified verbatim against this session's transcript." >&2
        fi
    fi

    if _page_is_a_session_account && [ -z "${HANDOFF_NO_STAMP:-}" ]; then
        _owner=$(FILE="$FILE" python3 - <<'PY'
import os, re
BEGIN, END = "<!-- session-attest:begin -->", "<!-- session-attest:end -->"
try:
    body = open(os.environ["FILE"], encoding="utf-8").read()
except OSError:
    body = ""
i = body.rfind(BEGIN)
tail = body[i:] if i >= 0 else ""
owner = ""
# Only a block that RUNS TO THE END is this scripts signature. A quoted example sits inside a fence
# with prose after it, and must not be read as one. WHERE the signature sits is this page layout,
# so it is found here; WHAT it says is read by `session-attest parse`, the block format's one owner
# -- this used to be a second copy of that format, as a regex.
if tail and tail.rstrip().endswith(END):
    import subprocess
    try:
        out = subprocess.run(["session-attest", "parse"], input=tail, capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    m = re.search(r"^attested-session:[ \t]*(\S+)[ \t]*$", out, re.M)
    if m:
        owner = m.group(1)
print(owner)
PY
)
        if [ -n "$_owner" ] && [ -n "${FORGE_TOOLS_SESSION_ID:-}" ] &&
           [ "$_owner" != "${FORGE_TOOLS_SESSION_ID:-}" ]; then
            echo "handoff: REFUSING to stamp \"$PAGE\" -- it is one session ACCOUNT of its own work" >&2
            echo "  and it is already attested to a DIFFERENT session. Publishing would replace" >&2
            echo "  that signature with yours." >&2
            echo "    page attests : $_owner" >&2
            echo "    you are      : ${FORGE_TOOLS_SESSION_ID:-unknown}" >&2
            echo "  If you are RESTORING or correcting their page, keep their signature:" >&2
            echo "    HANDOFF_NO_STAMP=1 $0 publish $FILE" >&2
            echo "  If this page really is yours, remove the block above and publish again." >&2
            exit 1
        fi
    fi
    # RE-MEASURED AT PUBLISH, IN A BLOCK THE AUTHOR CANNOT WRITE BY HAND. A handoff is
    # mostly claims about state -- what landed, what is clean, what is in flight -- and every one
    # of them decays. Measured 2026-09-05: a page said "all branches/worktrees cleaned except one"
    # while `git worktree list` showed eleven, and the reader it was written for is exactly the one
    # who cannot re-derive the author's scope. That sentence invites `worktree-reap --delete` on
    # other sessions' work. `publish` verifies the BYTES it sent; this is the one moment a cheap
    # re-measure is both free and exactly timed, so the readings go on the page beside the prose
    # and a reader can see them disagree.
    #
    # CEILING, IN THE BLOCK ITSELF: it describes the PUBLISHING box only. Three git commands plus one
    # forge read (the open PR for this branch), each failing soft to UNMEASURED rather than to a
    # value -- an unmeasured line that read as "clean" would be this ticket's defect re-created.
    # Handoff pages only, for the reason the attestation is: the caches are repo state and carry no
    # author whose claims a measurement could contradict. Skipped under HANDOFF_NO_STAMP (a RESTORE
    # of another session's page must not overwrite what THEY measured) and HANDOFF_NO_MEASURE.
    #
    # PLACED AFTER THE OWNER REFUSAL AND BEFORE THE SIGNATURE. A refused publish must not have
    # rewritten the draft (a test says so), so nothing is written into the file until the
    # other-session check above has passed. The stamp below strips-and-replaces an attestation block only
    # when it sits at EOF, so anything appended after it would make every republish accumulate
    # signatures. The block goes in front of a trailing attestation and replaces its own previous
    # copy, so a republish carries exactly one of each.
    if _page_is_a_handoff && [ -z "${HANDOFF_NO_STAMP:-}" ] && [ -z "${HANDOFF_NO_MEASURE:-}" ]; then
        _mtmp=$(mktemp) || { echo "handoff: mktemp failed" >&2; exit 1; }
        _remote="$FORGE_TOOLS_REMOTE"
        _cwd=$(pwd -P 2>/dev/null || pwd)
        _br=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || _br=""
        _ab=$(git rev-list --left-right --count "HEAD...$_remote/main" 2>/dev/null | tr '\t' '/') || _ab=""
        _dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ') || _dirty=""
        _wts=$(git worktree list 2>/dev/null) || _wts=""
        # WHO OWNS EACH TREE, BECAUSE THE LIST ALONE INVITES THE ACTION IT WAS ADDED TO PREVENT, as
        # an audit found. The block above lists every worktree so a page cannot claim "all cleaned"
        # against eleven of them; it then hands the reader seventeen paths and no way to tell which
        # are abandoned, which is the one fact the next action needs. Reaping a peer's tree on
        # inference has already cost this box a session's scratchpad (2026-09-16).
        #
        # OWNERSHIP IS CWD AND THE DOCTRINE IS REUSED, NOT REDONE: `agent_comms.AGENT_COMMS` decides
        # which processes are agents, read from `comm` rather than matched against command text --
        # the same source `worktree-reap.sh`, `pr-queue.sh`'s `_owner_pid`, `stash-guard` and
        # `launch-provenance-guard` all read. A tree is owned if an agent's cwd IS it or is under it,
        # which is `worktree-reap.sh`'s rule and deliberately wider than a toplevel-only match
        # (toplevel only): a session sitting in a subdirectory of its own tree must not read as
        # absent, because absent is the answer that gets a tree deleted.
        #
        # NOT the reaper itself, though it answers a neighbouring question and was tried first: it
        # refuses EVERY verdict while any agent's cwd is unreadable, which is the live
        # state of this box -- measured 2026-09-22, one foreign `claude` (pid 419379, EACCES) and the
        # whole report collapses to one refusal line. That refusal is right for a verb that DELETES
        # and useless on a page: positive attribution does not need the unaccounted pid resolved, so
        # this reports what it can see and names what it cannot beside it.
        #
        # FAILS SOFT TO `UNMEASURED`, NEVER TO `NONE FOUND` -- the whole block's doctrine. An absent
        # owner is the dangerous direction here, so a failed import, an unreadable /proc or a list
        # that would not pair each say so per line rather than resolving to "unowned".
        _wts_owned=$(WTS="$_wts" \
            WTP="$(git worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p')" \
            HERE="$HERE" python3 - <<'PY' 2>/dev/null
import os, sys

lines = os.environ.get("WTS", "").splitlines()
paths = os.environ.get("WTP", "").splitlines()
if not lines:
    raise SystemExit(1)                       # nothing measured: the caller prints UNMEASURED


def bail(why):
    # PER LINE, SO NO TREE READS AS UNOWNED -- but the reason once, not eighteen times.
    for line in lines:
        print("  %s  owner: UNMEASURED" % line)
    print("  owner UNMEASURED for every tree above: %s" % why)
    raise SystemExit(0)


if len(paths) != len(lines):
    bail("the plain and porcelain worktree lists did not pair")
sys.path.insert(0, os.environ.get("HERE", ""))
try:
    from agent_comms import AGENT_COMMS, foreign
except Exception as e:                        # noqa: BLE001 -- reported per line, never a value
    bail("cannot import agent_comms.AGENT_COMMS: %s" % e)
proc = os.environ.get("COSESSION_PROC") or os.environ.get("GIT_GUARD_PROC") or "/proc"
try:
    entries = os.listdir(proc)
except OSError as e:
    bail("%s is unreadable: %s" % (proc, e))

owners, blind = {}, []
for e in entries:
    if not e.isdigit():
        continue
    try:
        with open(os.path.join(proc, e, "comm")) as fh:
            comm = fh.read().strip()
    except OSError:
        continue                              # exited between listdir and read: not an agent we saw
    if comm not in AGENT_COMMS:
        continue
    try:
        cwd = os.path.realpath(os.readlink(os.path.join(proc, e, "cwd")))
    except OSError:
        if foreign(e, proc):
            continue                          # another uid's agent is not ours
        blind.append("%s(%s)" % (e, comm))     # an agent we cannot place
        continue
    owners.setdefault(cwd, []).append((int(e), comm))

for line, path in zip(lines, paths):
    real = os.path.realpath(path)
    who = sorted(p for cwd, ps in owners.items()
                 if cwd == real or cwd.startswith(real + os.sep) for p in ps)
    print("  %s  owner: %s" % (line, ", ".join("%s pid %d" % (c, p) for p, c in who) or "NONE FOUND"))
print("  (owner = an agent session whose cwd is that tree or under it, by `agent_comms.AGENT_COMMS`.")
print("   NONE FOUND IS NOT PROOF OF ABANDONMENT -- a session working outside its tree, a respawn,")
print("   or a cwd owned by another user all read as absent.)")
if blind:
    print("  UNACCOUNTED: %d live agent(s) whose cwd cannot be read (%s). Any NONE FOUND above may"
          % (len(blind), ", ".join(sorted(blind))))
    print("   be theirs, and this is the condition on which `worktree-reap.sh` refuses every")
    print("   verdict and removes nothing. Ask the live sessions before acting on a tree.")
PY
) || _wts_owned=""
        _pr="UNMEASURED: hub-api.sh not beside this script, or the forge did not answer"
        if [ -n "$_br" ] && [ -x "$HERE/hub-api.sh" ] &&
           _prs=$(sh "$HERE/hub-api.sh" "/api/v1/repos/$REPO/pulls?state=open&limit=50" 2>/dev/null); then
            _pr=$(printf '%s' "$_prs" | BR="$_br" python3 -c '
import json, os, sys
try:
    prs = json.load(sys.stdin)
    hits = [p for p in prs if (p.get("head") or {}).get("ref") == os.environ["BR"]]
except Exception:                                    # noqa: BLE001 -- reported, not a value
    print("UNMEASURED: the forge answer could not be read"); raise SystemExit
print("; ".join("#%s (%s -> %s)" % (p["number"], os.environ["BR"], (p.get("base") or {}).get("ref"))
               for p in hits) if hits else "none open for this branch (of %d open PRs read)" % len(prs))
' 2>/dev/null) || _pr="UNMEASURED: the forge answer could not be read"
        fi
        # EVERY LISTED WORKTREE'S BRANCH, NOT ONLY THIS TREE'S. `Handoff CC 1` handed over
        # `agent/274-probe-doc` as "pushed if the push below succeeded" while this block measured only
        # the publishing branch, so the successor ran `ls-remote` itself. One `ls-remote` and the PR
        # list already read above; each field fails soft to UNMEASURED, never to "not on hub".
        _heads=$(git ls-remote --heads "$_remote" 2>/dev/null) && _hrc=0 || _hrc=1
        _branches=$(HEADS="$_heads" HRC="$_hrc" PRS="${_prs:-}" REMOTE="$_remote" python3 - <<'PY' 2>/dev/null
import json, os, subprocess
remote, heads = os.environ["REMOTE"], {}
for l in os.environ["HEADS"].splitlines():
    sha, _, ref = l.partition("\t")
    if ref.startswith("refs/heads/"):
        heads[ref[len("refs/heads/"):]] = sha
try:
    prs = json.loads(os.environ["PRS"]) if os.environ["PRS"] else None
except ValueError:
    prs = None
out = subprocess.run(["git", "worktree", "list", "--porcelain"], capture_output=True, text=True)
if out.returncode != 0:
    raise SystemExit(1)
for chunk in out.stdout.split("\n\n"):
    f = {}
    for l in chunk.splitlines():
        k, _, v = l.partition(" ")
        f[k] = v
    if "worktree" not in f:
        continue
    br = f.get("branch", "")[len("refs/heads/"):] if f.get("branch", "").startswith("refs/heads/") else ""
    if not br:
        print("  %s  (detached HEAD, no branch to measure)" % f["worktree"])
        continue
    on = "UNMEASURED" if os.environ["HRC"] != "0" else (heads[br][:10] if br in heads else "not on %s" % remote)
    ab = subprocess.run(["git", "rev-list", "--left-right", "--count", "refs/heads/%s...%s/main" % (br, remote)],
                        capture_output=True, text=True)
    ab = ab.stdout.strip().replace("\t", "/") if ab.returncode == 0 else "UNMEASURED"
    pr = "UNMEASURED" if prs is None else (", ".join("#%s" % p["number"] for p in prs
                                                     if (p.get("head") or {}).get("ref") == br)
                                           or "none of %d open read" % len(prs))
    print("  %s  on %s: %s  ahead/behind %s/main: %s  open-pr: %s" % (br, remote, on, remote, ab, pr))
PY
) || _branches=""
        {
            echo "<!-- handoff-measured:begin -->"
            echo "measured-at: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
            echo "measured-on: $(hostname 2>/dev/null || echo UNKNOWN) -- the PUBLISHING box only; a successor elsewhere re-measures"
            echo "measured-in: $_cwd"
            echo "branch: ${_br:-UNMEASURED: not a git tree}  ahead/behind $_remote/main: ${_ab:-UNMEASURED: no $_remote/main ref}  dirty files: ${_dirty:-UNMEASURED}"
            echo "open-pr: $_pr"
            echo "worktrees:"
            if [ -n "$_wts_owned" ]; then printf '%s\n' "$_wts_owned"
            elif [ -n "$_wts" ]; then printf '%s\n' "$_wts" | sed 's/^/  /'
                 echo "  owner: UNMEASURED for every tree above -- the annotator did not run"
            else echo "  UNMEASURED: git worktree list failed"; fi
            echo "branches (every listed worktree):"
            if [ -n "$_branches" ]; then printf '%s\n' "$_branches"; else echo "  UNMEASURED: could not read the worktree list"; fi
            echo "measured-by: handoff.sh publish, not the author. Where the prose above disagrees with"
            echo "  these lines, the prose is the claim to distrust."
            echo "<!-- handoff-measured:end -->"
        } > "$_mtmp"
        python3 - "$FILE" "$_mtmp" <<'PY' || { echo "handoff: could not write the measured block into $FILE" >&2; rm -f "$_mtmp"; exit 1; }
import re, sys
page, blk = sys.argv[1], sys.argv[2]
body = open(page, encoding="utf-8").read()
block = open(blk, encoding="utf-8").read().strip("\n")
AB, AE = "<!-- session-attest:begin -->", "<!-- session-attest:end -->"
MB, ME = "<!-- handoff-measured:begin -->", "<!-- handoff-measured:end -->"
# A trailing signature is kept trailing: split it off, insert in front of it.
i = body.rfind(AB)
head, tail = (body[:i], body[i:]) if i != -1 and re.match(re.escape(AB) + r".*?" + re.escape(AE) + r"[ \t\n]*\Z", body[i:], flags=re.S) else (body, "")
# Replace this script's own previous reading -- the one it appended, which is the TRAILING block.
# A block quoted mid-document (a page explaining the mechanism) is not trailing and survives; the
# unanchored form ate exactly that on the attestation stamp once (case D).
j = head.rfind(MB)
if j != -1 and re.match(re.escape(MB) + r".*?" + re.escape(ME) + r"[ \t\n]*\Z", head[j:], flags=re.S):
    head = head[:j]
out = head.rstrip("\n") + "\n\n" + block + "\n"
if tail:
    out += "\n" + tail
open(page, "w", encoding="utf-8").write(out)
PY
        rm -f "$_mtmp"
        echo "handoff: re-measured this tree into \"$PAGE\": branch, ahead/behind, dirty count," >&2
        echo "  open PR and worktree list, in a block beside the prose. It describes THIS box only." >&2
    fi

    if _page_is_a_handoff && [ -z "${HANDOFF_NO_STAMP:-}" ]; then
        if _need_attest "signature" &&
           _stamp=$(session-attest stamp --session "${FORGE_TOOLS_SESSION_ID:-}" --pid "${FORGE_TOOLS_WAKE_PID:-}" 2>/dev/null) &&
           [ -n "$_stamp" ]; then
            _tmp_stamp=$(mktemp) || { echo "handoff: mktemp failed" >&2; exit 1; }
            printf '%s\n' "$_stamp" > "$_tmp_stamp"
            python3 -c '
import re, sys
page, stamp = sys.argv[1], sys.argv[2]
body = open(page, encoding="utf-8").read()
block = open(stamp, encoding="utf-8").read().strip("\n")
BEGIN, END = "<!-- session-attest:begin -->", "<!-- session-attest:end -->"

# DROP ONLY A SIGNATURE THIS SCRIPT WROTE, WHICH IS ONLY EVER THE TRAILING ONE.
#
# A page ABOUT attestation quotes these markers -- a wiki note on the mechanism, or a handoff explaining
# the mechanism to a successor. An unanchored `re.sub` deletes that quoted example out of the middle
# of the document, and THE READ-BACK CANNOT CATCH IT: the strip happens before the digest, so
# `publish` verifies the damaged text as byte-perfect and reports VERIFIED. A check confirming the
# wrong thing, inside the fix for a ticket about exactly that.
#
# ANCHORING AT EOF IS NOT ENOUGH, and this is the case that makes rfind necessary rather than
# tidier. With both a quoted example and a real trailing signature present -- the normal state of
# such a page once it has been signed once -- a lazy `.*?` anchored with \Z starts at the QUOTED
# begin marker and expands to the REAL end marker, deleting the example and everything between.
# Measured: the \Z form loses the example in exactly that case.
#
# So: seek the LAST begin marker and strip only if everything after it is one well-formed block.
# A document whose final bytes are a quoted example with nothing following is indistinguishable from
# a signed one, and is stripped -- an accepted limit, stated rather than discovered.
i = body.rfind(BEGIN)
if i != -1 and re.match(BEGIN + r".*?" + END + r"[ \t\n]*\Z", body[i:], flags=re.S):
    body = body[:i]
open(page, "w", encoding="utf-8").write(body.rstrip("\n") + "\n\n" + block + "\n")
' "$FILE" "$_tmp_stamp" || { echo "handoff: could not stamp $FILE" >&2; rm -f "$_tmp_stamp"; exit 1; }
            rm -f "$_tmp_stamp"
            echo "handoff: stamped $FILE with this session's attestation." >&2
            echo "  It names an author and a checkable pid. It is NOT verifiable from the repo," >&2
            echo "  and it does not make \"I merged it\" checkable -- see the ceiling in the Session-Attest README." >&2
        else
            # THE REFUSAL IS EXPECTED, NOT AN ERROR. FORGE_TOOLS_SESSION_ID exists only where a harness
            # adapter sets it -- the Claude Code adapter maps CLAUDE_CODE_SESSION_ID onto it for
            # Bash-tool child environments, so cron, CI and a bare shell cannot stamp. Refusing the
            # publish there would make this transport unusable for a provenance nicety; the caller is
            # told and the publish proceeds, which is the report arm above by another route.
            if command -v session-attest >/dev/null 2>&1; then
                echo "handoff: NOTE -- cannot stamp \"$PAGE\": no FORGE_TOOLS_SESSION_ID in this environment." >&2
                echo "  It is set only by a harness adapter (adapters/claude-code, for Bash tool calls), so" >&2
                echo "  cron, CI and a bare shell cannot sign. Publishing UNSTAMPED." >&2
            else
                echo "handoff: NOTE -- cannot stamp \"$PAGE\": the session-attest command is missing (above). Publishing UNSTAMPED." >&2
            fi
        fi
    fi

    if ! grep -q '<!-- session-attest:begin -->' "$FILE" 2>/dev/null; then
        echo "handoff: NOTE -- \"$PAGE\" carries no attestation block, so it does not say which session wrote it." >&2
        echo "  If this page is a session's ACCOUNT of its own work, sign it:" >&2
        echo '      session-attest stamp --session "$FORGE_TOOLS_SESSION_ID" --pid "$FORGE_TOOLS_WAKE_PID" >> '"$FILE" >&2
        echo "  If it is repo state rather than a session's account -- a cache or backlog page -- ignore this." >&2
        echo "  A signature is not verifiable from the repo; it names an author and a checkable pid. Publishing anyway." >&2
    fi

    # THE LINEAGE HOLDER MUST BE THE SESSION THAT SIGNS. Measured 2026-09-15: "Handoff
    # CC 1" went out with `holder-session: d9285a57`, the previous generation's block pasted back to
    # get past the dropped-block refusal below, under `attested-session: 20eaea3f`. The SessionStart
    # hook hands that block to the successor as the chain's identity, so the chain skipped a
    # generation, and only a reader caught it. Both lines are on the draft by now: compared, not
    # trusted. Either line absent means there is nothing to compare, so this stays silent.
    _HOLDER_MISMATCH=$(python3 - "$FILE" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)   # a quoted block is documentation
lin = re.search(r"<!--\s*lineage:begin\s*-->(.*?)<!--\s*lineage:end\s*-->", text, re.S)
att = text.rfind("<!-- session-attest:begin -->")
holder = re.search(r"^- holder-session:[ \t]*([0-9a-fA-F-]{8,})", lin.group(1), re.M) if lin else None
signer = re.search(r"^attested-session:[ \t]*(\S+)", text[att:], re.M) if att != -1 else None
if holder and signer and holder.group(1) != signer.group(1):
    print("%s %s" % (holder.group(1), signer.group(1)))
PY
) || _HOLDER_MISMATCH=""
    if [ -n "$_HOLDER_MISMATCH" ]; then
        echo "handoff: REFUSING to publish \"$PAGE\" -- its lineage block names another session as holder:" >&2
        echo "    holder-session:   ${_HOLDER_MISMATCH% *}" >&2
        echo "    attested-session: ${_HOLDER_MISMATCH#* }" >&2
        echo "  A successor inherits that block as the chain's identity. Nothing has been sent." >&2
        echo "  Regenerate the block as the signing session, never paste an old one back:" >&2
        echo "      session-succeed lineage \"$PAGE\" <slot> [predecessor-page]" >&2
        exit 1
    fi

    # A STAMP THAT IS NO LONGER TRAILING IS STALE AND NOTHING SAYS SO.
    #
    # `publish` replaces only the TRAILING block, anchored at EOF, and that is deliberate: an
    # unanchored strip once deleted a QUOTED example out of the middle of a page documenting this
    # mechanism, and the read-back verified the damage as byte-perfect (case D). So prose
    # written BELOW a stamp does not update that stamp -- it buries it, and a new one is appended
    # after the prose. The page then carries a stale measurement ABOVE the live one, and a reader
    # going top-down meets the stale one first. Measured on "Handoff CC 1": the buried block still
    # named `agent/918-secrets`, a branch that no longer existed, and listed eight worktrees.
    #
    # REPORTED, NEVER STRIPPED. Removing mid-document blocks deletes quoted examples, and this
    # script cannot tell a buried stamp from a quoted example -- both are well-formed blocks that
    # are not at EOF. So the author is told and decides; the bytes are not touched.
    #
    # AN ARCHIVED PREDECESSOR HANDOFF IS NOT AN ORPHAN. `slot claim` copies the predecessor page in
    # verbatim between `predecessor-handoff` fences, stamps and all, and those are correctly marked
    # as somebody else's by the fence they sit in. Warning about them would fire on every succession
    # -- the common case -- which is how a warning teaches people to ignore it.
    ORPHANS=$(PAGEFILE="$FILE" python3 - <<'ORPHAN_EOF' || true
import os, re
body = open(os.environ["PAGEFILE"], encoding="utf-8").read()
MB, ME = "<!-- handoff-measured:begin -->", "<!-- handoff-measured:end -->"
AB, AE = "<!-- session-attest:begin -->", "<!-- session-attest:end -->"
PB, PE = "<!-- predecessor-handoff:begin -->", "<!-- predecessor-handoff:end -->"
fences = []
for m in re.finditer(re.escape(PB), body):
    e = body.find(PE, m.start())
    if e != -1:
        fences.append((m.start(), e))
# Where the trailing run starts -- computed exactly as the stamp steps above compute it.
i = body.rfind(AB)
trail = i if i != -1 and re.match(re.escape(AB) + r".*?" + re.escape(AE) + r"[ \t\n]*\Z",
                                 body[i:], flags=re.S) else len(body)
head = body[:trail]
j = head.rfind(MB)
if j != -1 and re.match(re.escape(MB) + r".*?" + re.escape(ME) + r"[ \t\n]*\Z",
                        head[j:], flags=re.S):
    trail = j
for mark, label in ((MB, "measured"), (AB, "attest")):
    for m in re.finditer(re.escape(mark), body):
        at = m.start()
        if at >= trail or any(a <= at <= b for a, b in fences):
            continue
        print("line %d: a %s block, buried above the live one" % (body[:at].count("\n") + 1, label))
ORPHAN_EOF
)
    if [ -n "$ORPHANS" ]; then
        echo "handoff: NOTE -- \"$PAGE\" carries stamped block(s) that are no longer trailing:" >&2
        printf '%s\n' "$ORPHANS" | sed 's/^/    /' >&2
        echo "  Prose was written BELOW a stamp, so publish appended a new one after it rather than" >&2
        echo "  updating it. The buried block is a MEASUREMENT THAT IS NO LONGER TRUE, sitting above" >&2
        echo "  the live one, and a reader going top-down meets it first." >&2
        echo "  NOT STRIPPED, deliberately: this script cannot tell a buried stamp from a quoted" >&2
        echo "  example, and deleting the latter destroys content. Delete it yourself, or move your" >&2
        echo "  prose ABOVE the stamp so the next publish updates it." >&2
    fi

    LOCAL_SHA=$(digest < "$FILE")
    BYTES=$(wc -c < "$FILE" | tr -d ' ')

    # create vs update: the forge has no upsert, and POSTing over an existing page 4xxs.
    # The update path uses the forge's OWN sub_url, never a path built from the title.
    # rc 2 is "could not read the listing", and it used to take the POST branch -- past the lock,
    # the block guard and the stale-base check, which all live under PATCH.
    SUB=$(sub_url_for_title); _rc=$?
    if [ "$_rc" = 2 ]; then
        echo "handoff: CANNOT TELL whether \"$PAGE\" exists on $REPO -- the forge did not answer." >&2
        echo "         This is NOT an absence, and a create here would skip every guard an update" >&2
        echo "         carries. Retry when the forge answers." >&2
        exit 2
    elif [ "$_rc" = 0 ] && [ -n "$SUB" ]; then
        METHOD=PATCH; PATH_="/api/v1/repos/$REPO/wiki/page/$SUB"; WHAT="updated"
    else
        METHOD=POST;  PATH_="/api/v1/repos/$REPO/wiki/new";       WHAT="created"
        if [ -n "${HANDOFF_EXPECT_SHA:-}" ]; then
            echo "handoff: REFUSING -- HANDOFF_EXPECT_SHA names a base, but \"$PAGE\" resolved as" >&2
            echo "         absent on $REPO, so there is no base to compare and the stale-base" >&2
            echo "         check would silently not run." >&2
            exit 1
        fi
    fi

    # SERIALISE THE READ-MODIFY-WRITE, and it is `hub-api.sh:edit_body`'s pattern rather
    # than a new invention. That function has serialised every ISSUE body write for
    # exactly this reason; the wiki path was the weaker of the two and the remedy was one file over.
    #
    # WHAT THIS CLOSES THAT AN EXPECTED-SHA CHECK COULD NOT. `HANDOFF_EXPECT_SHA` compares a base and then writes, so
    # a publish landing between that comparison and the PATCH is still lost. An earlier resolution said
    # only the server could close that gap; THAT WAS WRONG, and this is the correction. Re-reading
    # cannot close it -- locking can, for every writer going through this client. On this box that
    # is the realistic population: several sessions on one machine, each republishing at its end,
    # and the incident on record (9,461 bytes to 43 on the live `Session Cache`) was a same-box
    # collision.
    #
    # THE PATH IS FIXED, OUTSIDE EVERY CHECKOUT, AND KEYED ON THE REPO, for the reason `edit_body`
    # and `pr-queue.sh` both set out at length: locking anything INSIDE a tree locks a DIFFERENT
    # INODE per worktree, and this script is routinely run from several at once -- so both holders
    # would acquire happily and the lock would look like it worked. `flock` is held by the kernel on
    # the open descriptor, so there is no on-disk state to go stale.
    #
    # HELD ACROSS THE PRE-READ, THE WRITE AND THE READ-BACK -- never across the caller's editing,
    # which may be session-long and must not block anyone. That is the whole reason this is a lock
    # around a short critical section rather than a checkout-style lease.
    #
    # PATCH ONLY. A create cannot lose an update: two POSTs to /wiki/new race into a 4xx for the
    # loser, which is a visible failure rather than a silent overwrite.
    #
    # CEILING, STATED HERE AND INHERITED VERBATIM FROM `edit_body`: this serialises writers that go
    # through THIS CLIENT. A web-UI edit, another box, or a hand-rolled curl takes no lock and can
    # still clobber, and nothing here can detect it. A real narrowing, not an atomic guarantee --
    # the forge offers no compare-and-swap for wiki pages (measured).
    if [ "$METHOD" = PATCH ]; then
        _LOCKF="${HANDOFF_LOCK:-/tmp/handoff-publish.$(printf '%s' "$REPO" | tr '/' '-').lock}"
        if command -v flock >/dev/null 2>&1; then
            # 9 is the descriptor `pr-queue.sh` uses for the same job; the kernel releases it when
            # this process exits, on every path including the refusals below.
            # THE LOCK FILE IS SHARED ACROSS USERS, SO ITS MODE IS DECIDED HERE, NOT BY THE FIRST
            # CALLER'S UMASK. Measured 2026-09-12 on the consolidated server: a 644 file left by the
            # forge runner's tests gave the interactive user `cannot create ...: Permission denied` and a
            # REFUSING that read as contention; fixed by hand, that user's 644 file then
            # failed the runner the same way, 58 times in one CI run. Create it 0666 if
            # absent; an existing file another user owns cannot be widened from here, so name it.
            # AND OPEN IT READ-ONLY (a rule that had missed this site): `flock` needs no write
            # access, and in sticky /tmp the kernel's `protected_regular` refuses any O_CREAT open
            # (`>>`) of a file another user owns WHATEVER ITS MODE -- measured 2026-09-22: a runner-
            # owned 0666 lock passed `-w` and then killed the shell with "cannot create".
            # Tested BEFORE `exec`, not with `exec ... ||`: a failed redirection on `exec` is fatal in
            # dash and busybox ash (special builtin), so an `||` after it never runs -- measured here
            # 2026-09-12 with a root-owned 644 lock: the shell died with the raw "cannot create" line.
            [ -e "$_LOCKF" ] || ( umask 000; : >>"$_LOCKF" ) 2>/dev/null
            if [ ! -e "$_LOCKF" ] || [ ! -r "$_LOCKF" ]; then
                echo "handoff: REFUSING to publish \"$PAGE\" -- cannot open the publish lock $_LOCKF" >&2
                echo "  $(ls -l "$_LOCKF" 2>/dev/null || echo 'it does not exist and could not be created')" >&2
                echo "  Another user created it with a private mode. As its owner: chmod a+r $_LOCKF;" >&2
                echo "  or set HANDOFF_LOCK to a path this user can open. Nothing has been sent." >&2
                exit 1
            fi
            exec 9<"$_LOCKF"
            # BLOCK, WITH A BOUND. `edit_body` blocks outright; a publish is seconds, so waiting is
            # right and refusing instantly would turn ordinary contention into a failed handoff at
            # the moment a shift ends. The timeout exists so a wedged holder cannot hang a session
            # for ever -- and it SAYS SO rather than proceeding as though it had the lock.
            # OVERRIDABLE SO THE REFUSAL IS OBSERVABLE AT ALL. A negative control here can only be
            # produced by actually holding the lock, and at 60s the test would take a minute -- so
            # the branch would go untested, which is how a refusal path rots. Its failure mode is
            # VISIBLE (an early refusal), unlike a `from-mode` override since retired.
            _LOCKW="${HANDOFF_LOCK_WAIT:-60}"
            if ! flock -w "$_LOCKW" 9; then
                echo "handoff: REFUSING to publish \"$PAGE\" -- could not take the publish lock" >&2
                echo "  $_LOCKF within ${_LOCKW}s. Another publisher holds it, or a wedged process does." >&2
                echo "  Nothing has been sent. Re-run; if it persists, find the holder:" >&2
                echo "      fuser -v $_LOCKF" >&2
                exit 1
            fi
        else
            # LOUD, because a silent unserialised publish is indistinguishable from a serialised one
            # right up until it destroys somebody's work.
            echo "handoff: NOTE -- no flock(1) on PATH, so this publish is NOT serialised against" >&2
            echo "  other sessions on this box. It proceeds; a concurrent publish can" >&2
            echo "  still be lost silently." >&2
        fi
    fi

    # REFUSE TO DROP A DELIMITED BLOCK THE PAGE ALREADY CARRIES.
    #
    # `publish` uploads the DRAFT and nothing else, rewriting only the attest block. So a block that
    # lives on the PAGE but not in the draft is destroyed, and both tools report success. Measured:
    # `session-succeed lineage` splices `<!-- lineage:begin -->` and
    # `<!-- predecessor-handoff:begin -->` onto a handoff page; a session ran `lineage`, published
    # its handoff over the top hours later, and the block existed and then silently did not. Its
    # successor measured ZERO lineage blocks on the fetched page during the audit that followed,
    # which is what made that page's registry entry record "its holder-session could not be read".
    #
    # WHY THE READ-BACK DID NOT CATCH IT, and cannot. It proves the forge's copy is byte-identical
    # to the draft -- *what I sent is what is there*, never *what was there is still there*. A good
    # publish and a destructive one produce identical output.
    #
    # REFUSE RATHER THAN MERGE, operator decision 2026-09-03. Carrying the block forward would make
    # `publish` no longer a pure transport, and byte-identity between draft and page is what
    # `publish`'s own verification and `verify`'s compare both rest on -- preserving a block would
    # make that false BY DESIGN and quietly cost two existing guarantees to buy one. Refusing keeps
    # the transport contract intact and costs a fetch and a retry.
    #
    # GENERIC IN THE MARKER, deliberately: any `<!-- name:begin -->` counts, so a future splicer is
    # protected without editing this file. `session-attest` is the one exclusion, because replacing
    # it is this script's documented job rather than an accident.
    #
    # FENCES ARE STRIPPED FIRST. A page ABOUT this mechanism quotes these markers inside a code
    # fence -- the same trap that made an earlier attestation check refuse to publish the
    # note documenting it. Scanning raw text would refuse to publish the documentation of the thing it protects.
    if [ "$METHOD" = PATCH ]; then
        _CURP=$(mktemp) || { echo "handoff: mktemp failed" >&2; exit 1; }
        # STATUS CAPTURED EXPLICITLY. `$?` inside an `else` is the condition's status only until
        # anything else runs, and this branch prints -- the bug class this file already documents
        # for pipelines, arriving through a different door.
        #
        # ONE READ SERVES BOTH CHECKS -- the block guard below and the stale-base guard.
        # Fetching twice would cost a request and, worse, give the two checks DIFFERENT pages to
        # reason about, so they could disagree about what is on the forge.
        read_page_to "$_CURP" && _RPRC=0 || _RPRC=$?

        # THE STALE-BASE GUARD. `HANDOFF_EXPECT_SHA` is the sha256 of the page your draft
        # was built from; a mismatch means someone republished in between and your draft would erase
        # their work.
        #
        # THE FORGE OFFERS NO COMPARE-AND-SWAP, MEASURED RATHER THAN ASSUMED. This hub's own
        # `swagger.v1.json` gives PATCH /wiki/page/{pageName} the body `CreateWikiPageOptions`
        # (`content_base64`, `message`, `title`) and NO header parameters -- so there is no
        # `head_commit_id` as on the merge endpoint, and no `If-Match` either. An atomic refusal is
        # therefore not available at any price here, and this is the best a client can do.
        #
        # STATE THE CEILING, because this check is easy to read as more than it is. It closes the
        # WIDE window -- a draft built from a fetch minutes or hours old, which is the failure that
        # actually happened (9,461 bytes to 43 on the live `Session Cache`, 2026-08-30). It CANNOT
        # close the narrow one between this read and the PATCH below. A write landing in that gap is
        # still lost silently, and no amount of re-reading fixes it; only the server could.
        #
        # OPTIONAL, for the reason `pr merge` states about `head_commit_id`: making it required would
        # break every caller at the moment of a publish, which is the worst place to learn of a
        # signature change. The variable SUPPLIES the expected value and never skips the comparison
        # -- there is no form of it that turns the check off.
        if [ -n "${HANDOFF_EXPECT_SHA:-}" ] && [ "$_RPRC" -eq 0 ]; then
            _NOW_SHA=$(digest < "$_CURP")
            if [ "$_NOW_SHA" != "$HANDOFF_EXPECT_SHA" ]; then
                echo "handoff: REFUSING to publish \"$PAGE\" -- the page moved since your draft's base." >&2
                echo "    expected $HANDOFF_EXPECT_SHA" >&2
                echo "    on forge $_NOW_SHA" >&2
                echo "  Someone republished while you were editing. Publishing your draft would" >&2
                echo "  erase their work, and the read-back would report VERIFIED for it." >&2
                echo "  Nothing has been sent." >&2
                echo "" >&2
                echo "  Re-fetch, re-apply your edit on top, and publish that:" >&2
                echo "      HANDOFF_PAGE=\"$PAGE\" sh $0 show > /tmp/page.md" >&2
                rm -f "$_CURP"
                exit 1
            fi
        elif [ -n "${HANDOFF_EXPECT_SHA:-}" ]; then
            echo "handoff: NOTE -- HANDOFF_EXPECT_SHA was set but the page could not be read, so the" >&2
            echo "  stale-base check did NOT run. That is 'not measured', never 'base unchanged'." >&2
        fi

        if [ "$_RPRC" -eq 0 ] && [ -z "${HANDOFF_DROP_BLOCKS:-}" ]; then
            _LOST=$(FILE="$FILE" CURP="$_CURP" python3 - <<'PY'
import os, re

def blocks(path):
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return set()
    # Drop fenced regions before scanning: a quoted marker is documentation, not a block.
    text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
    return set(re.findall(r"<!--\s*([A-Za-z0-9_-]+):begin\s*-->", text))

# `session-attest` is REPLACED by this script on purpose; every other block is someone else's.
print(" ".join(sorted((blocks(os.environ["CURP"]) - blocks(os.environ["FILE"]))
                      - {"session-attest", "handoff-measured"})))
PY
) || _LOST=""
            if [ -n "$_LOST" ]; then
                echo "handoff: REFUSING to publish \"$PAGE\" -- the page carries a block your draft does not:" >&2
                for _b in $_LOST; do echo "    <!-- $_b:begin -->" >&2; done
                echo '  Publishing would DESTROY it. `publish` sends your draft verbatim; it does not' >&2
                echo "  merge, and its read-back cannot tell a good publish from a destructive one." >&2
                echo "  Nothing has been sent." >&2
                echo "" >&2
                echo "  Your draft is almost certainly built from a stale copy. Fetch, re-apply your" >&2
                echo "  edit, and publish that:" >&2
                echo "      HANDOFF_PAGE=\"$PAGE\" sh $0 show > /tmp/page.md   # then edit /tmp/page.md" >&2
                echo "  A LINEAGE block is regenerated, never pasted back from an older copy: a pasted" >&2
                echo "  block names the previous holder, and publish refuses that too:" >&2
                echo "      session-succeed lineage \"$PAGE\" <slot> [predecessor-page]" >&2
                echo "" >&2
                echo "  If you MEAN to drop it -- restoring a page, or retiring a stale archive --" >&2
                echo "  say so: HANDOFF_DROP_BLOCKS=1" >&2
                rm -f "$_CURP"
                exit 1
            fi
        fi

        # 1 = no page (SUB resolved, so this is a race and there is nothing to lose);
        # 2 = could not look. SAY WHICH, and never let "could not look" read as "nothing there".
        # Standalone rather than an `else`, because the branch above now also declines when the
        # caller passed HANDOFF_DROP_BLOCKS -- and "you asked to drop blocks" must not render as
        # "the forge was unreachable".
        if [ "$_RPRC" -eq 2 ] && [ -z "${HANDOFF_DROP_BLOCKS:-}" ]; then
            echo "handoff: NOTE -- could not read \"$PAGE\" before writing, so the lost-block check" >&2
            echo "  did NOT run. This is 'not measured', not 'nothing to lose': if the page" >&2
            echo "  carries a lineage or archive block your draft lacks, this publish destroys it." >&2
        fi
        rm -f "$_CURP"
    fi

    # THE LAST MOMENT BEFORE ANYTHING LEAVES THIS BOX. Every decision above has run; only the HTTP
    # call and its read-back are skipped. `$WHAT` is resolved from the forge's own page list, so a
    # dry run distinguishes creating a NEW page from overwriting an existing one -- which is exactly
    # the distinction an earlier dry run needed and did not have.
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "handoff: DRY RUN -- nothing was sent."
        echo "  would have $WHAT wiki page \"$PAGE\" on $REPO"
        echo "  from $FILE ($CHARS chars, $BYTES bytes)"
        echo "  sha256 $LOCAL_SHA"
        exit 0
    fi

    BODY=$(python3 -c '
import sys, json, base64
p = sys.argv[1]
data = open(p, "rb").read()
print(json.dumps({
    "title": sys.argv[2],
    "content_base64": base64.b64encode(data).decode(),
    "message": sys.argv[3],
}))
' "$FILE" "$PAGE" "handoff: publish $(date -u +%Y-%m-%dT%H:%M:%SZ)")

    printf '%s' "$BODY" | sh "$API" "$PATH_" -X "$METHOD" --json @- >/dev/null || {
        echo "handoff: $METHOD $PATH_ FAILED -- page NOT $WHAT" >&2; exit 1; }

    # THE READ-BACK. Everything above only proves a request was accepted.
    TMP=$(mktemp) || { echo "handoff: mktemp failed" >&2; exit 1; }
    trap 'rm -f "$TMP" "$TMP.part"' EXIT
    read_page_to "$TMP" || { echo "handoff: published but the page cannot be read back -- treat as UNPUBLISHED" >&2; exit 1; }
    REMOTE_SHA=$(digest < "$TMP")
    REMOTE_N=$(wc -c < "$TMP" | tr -d ' ')

    echo "handoff: $WHAT wiki page \"$PAGE\" on $REPO"
    echo "  local   $LOCAL_SHA  ($CHARS chars, $BYTES bytes)"
    echo "  remote  $REMOTE_SHA  ($REMOTE_N bytes)"
    if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
        echo "  VERIFIED: the forge's copy is byte-identical to $FILE"
        exit 0
    fi
    echo "  MISMATCH IN CONTENT: local $BYTES bytes vs remote $REMOTE_N bytes." >&2
    echo "  The wiki page is NOT what you wrote. Do not hand over on it." >&2
    exit 1
    ;;

show)
    # THE TWO FAILURES GET DIFFERENT SENTENCES AND DIFFERENT EXIT CODES. Saying "it is
    # not there" when the truth is "I could not look" is the false negative this file exists to
    # end, and a caller that treats absence as a decision -- `session-succeed` refuses to start
    # a successor on it -- acts on the wrong one.
    fetch
    case $? in
        0) ;;
        2) echo "handoff: CANNOT TELL whether \"$PAGE\" exists on $REPO -- the forge did not" >&2
           echo "         answer. This is NOT an absence: do not act as if the page is gone." >&2
           exit 2 ;;
        *) echo "handoff: no wiki page \"$PAGE\" on $REPO" >&2; exit 1 ;;
    esac
    ;;

verify)
    FILE="${2:-$FILE_DEFAULT}"
    [ -s "$FILE" ] || { echo "handoff: $FILE is missing or empty" >&2; exit 1; }
    TMP=$(mktemp) || { echo "handoff: mktemp failed" >&2; exit 1; }
    trap 'rm -f "$TMP" "$TMP.part"' EXIT
    read_page_to "$TMP"
    case $? in
        0) ;;
        2) echo "handoff: CANNOT TELL what \"$PAGE\" holds -- the forge did not answer. This is" >&2
           echo "         not a comparison result; the file may be current or stale." >&2
           exit 2 ;;
        *) echo "handoff: no wiki page \"$PAGE\" -- nothing to compare" >&2; exit 1 ;;
    esac
    L=$(digest < "$FILE"); R=$(digest < "$TMP")
    echo "  local   $L  $FILE"
    echo "  remote  $R  $REPO wiki/$PAGE"
    [ "$L" = "$R" ] && { echo "  IDENTICAL"; exit 0; }
    echo "  DIFFER -- the wiki page is the authority; this file is a stale draft." >&2
    exit 1
    ;;

url)
    # The forge's sub_url when the page exists; otherwise say so rather than print a guess that
    # 404s. A URL nobody has checked is exactly the artefact this ticket is about.
    SUB=$(sub_url_for_title); _rc=$?
    if [ "$_rc" = 0 ] && [ -n "$SUB" ]; then
        echo "${FORGE_TOOLS_FORGE_URL%/}/$REPO/wiki/$SUB"
    elif [ "$_rc" = 2 ]; then
        echo "handoff: CANNOT TELL whether \"$PAGE\" exists -- the forge did not answer, so this" >&2
        echo "         is not evidence the page is missing." >&2
        exit 2
    else
        echo "handoff: no wiki page titled \"$PAGE\" on $REPO yet -- run \`handoff.sh publish\`" >&2
        exit 1
    fi
    ;;

delete)
    # WHY THIS VERB EXISTS. The wiki page list IS the registry of claimed
    # terminal numbers, so a page nothing can remove is a slot nothing can free. Before this, the
    # wiki held 10 pages and 6 were dead handoffs -- the endless log the registry exists to prevent, with
    # no way to shorten it from the repo.
    #
    # WHY A NARROWER PREDICATE THAN `_page_is_a_handoff`. That helper decides what gets SIGNED and
    # deliberately includes "Coordinator Handoff", the shared seat page. Signing the shared page is
    # right; deleting it is the 16,316-byte loss recorded at the top of this file, and overwriting a
    # session's page was the second time this family cost real bytes. Deletion is the one operation with no undo on the
    # forge, so it gets its own, stricter, test rather than reusing a predicate written for
    # another question.
    DRY_RUN=0
    [ "${2:-}" = "--dry-run" ] && DRY_RUN=1

    case "$PAGE" in
        "Handoff "*) ;;
        *)
            echo "handoff: REFUSING to delete \"$PAGE\" -- only \`Handoff <id>\` pages are deletable." >&2
            echo "  Repo-state pages (caches, backlogs) and the shared" >&2
            echo "  \"Coordinator Handoff\" are excluded by title, on purpose." >&2
            exit 1
            ;;
    esac

    # The forge's OWN sub_url, never a path built from the title -- and this verb is the one most
    # exposed to getting that wrong, because it addresses the page by path. The escaping is not
    # what the note at the top of this file says: a hyphen ANYWHERE in the title also flips spaces
    # to `+`, so `Handoff research-audit 2026-08-29` is `Handoff+research-audit+2026-08-29.-`
    # while space-only titles stay clean. Measured against the live wiki 2026-08-31.
    SUB=$(sub_url_for_title); _rc=$?
    if [ "$_rc" = 2 ]; then
        echo "handoff: CANNOT TELL whether \"$PAGE\" exists on $REPO -- the forge did not answer," >&2
        echo "         so this is not evidence the page is gone." >&2
        exit 2
    elif [ "$_rc" != 0 ] || [ -z "$SUB" ]; then
        echo "handoff: no wiki page \"$PAGE\" on $REPO -- nothing to delete" >&2
        exit 1
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "handoff: DRY RUN -- nothing was sent."
        echo "  would have deleted wiki page \"$PAGE\" ($REPO wiki/$SUB)"
        exit 0
    fi

    sh "$API" "/api/v1/repos/$REPO/wiki/page/$SUB" -X DELETE >/dev/null || {
        echo "handoff: DELETE /wiki/page/$SUB FAILED -- page NOT deleted" >&2; exit 1; }

    # THE READ-BACK, for the same reason `publish` has one: the call above only proves a request
    # was accepted. An absent page is the assertion, not a 2xx. Asking the LISTING rather than the
    # page endpoint is deliberate -- the listing is what the registry reads, so this checks the
    # thing that actually has to have changed.
    if SUB2=$(sub_url_for_title) && [ -n "$SUB2" ]; then
        echo "handoff: DELETE succeeded but \"$PAGE\" is STILL LISTED as $SUB2." >&2
        echo "  Treat the page as NOT deleted and its number as still claimed." >&2
        exit 1
    fi

    echo "handoff: deleted wiki page \"$PAGE\" on $REPO"
    echo "  VERIFIED: absent from $REPO's wiki page listing"
    ;;

*) usage ;;
esac
