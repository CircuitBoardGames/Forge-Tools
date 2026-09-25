#!/bin/sh
# Create a worktree that actually works, and prove it before handing it off.
#
#   worktree-create.sh <slug> [branch]   create ../CC-<slug> on `hub/main`, apply .worktreeinclude
#   worktree-create.sh --check [path]    verify an EXISTING worktree; exit 1 with reasons if not
#
# Adapted from coleam00's `worktree-create` checklist (references/worktree-setup.md). Three of its
# items are corrected for this box, each with the measurement in the place it applies: worktrees are
# SIBLINGS not nested (below), the copy list is an explicit `.worktreeinclude` not a pattern
# intersection (that file), and `node_modules` is SYMLINKED by default (that file).
#
# SIBLINGS, NOT `worktrees/<branch>` INSIDE THE REPO. coleam00 nests worktrees under a gitignored
# root so they never show as untracked in the main checkout. Here that is backwards: the reference
# tree is the one nobody may write to, and hooks, `fallow dead-code --root`, `wiki-auto-commit.sh`
# and the vault scanners all walk it. Nesting would put every agent's worktree inside it. (There IS
# a `.claude/worktrees/` directory, gitignored and empty — the nested layout, provisioned and never
# used. Nothing here writes to it.)
#
# THE NAME IS NOT LOAD-BEARING, AND THAT IS A CORRECTION. An early design wanted the path to
# encode the owning session so orphan detection could be a filename comparison. That needs a session
# identity that is observable from outside and stable for the worktree's life. Measured 2026-08-24,
# neither candidate is:
#
#   - `CLAUDE_CODE_SESSION_ID` is absent from the environ of all four live `claude` pids and the
#     `omp` pid, while being PRESENT on a child pid (12423, alongside `CLAUDE_CODE_CHILD_SESSION`
#     and `CLAUDE_PID`). That child is the positive control: the probe demonstrably can find the
#     var, so the absence on the session processes is a real absence and not a broken read. It is
#     therefore not "invisible from outside" — it is readable, on children, unreliably present, and
#     it is not resume-stable. That is what kills the mechanism.
#   - The pid IS reliably observable (`/tmp/cc-socks/<pid>.sock`, one per session). But
#     `claude-session-recycle.sh` respawns a session under a NEW pid while the worktree keeps its
#     name, so a reaper keyed on the pid reads LIVE work as an orphan.
#
# So `worktree-reap.sh` keys on CWD instead, and the slug below is legibility only. See that
# script's header. The design was rewritten to match.
#
# What that buys: the reassignment defect measured on 2026-08-24 — three worktrees named for a
# ticket, all three holding a different ticket's branch within hours — stops mattering. The name
# goes stale; the attribution does not.
set -eu

SELF="[worktree-create]"
die() { echo "$SELF $*" >&2; exit 1; }
# Site configuration: the forge remote a worktree is cut from is FORGE_TOOLS_REMOTE.
_ft_cfg="$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")/ft-config.sh"
[ -r "$_ft_cfg" ] || die "cannot read $_ft_cfg -- Forge-Tools' config reader must sit beside this script"
. "$_ft_cfg"
FT_REMOTE=$FORGE_TOOLS_REMOTE

# ---------------------------------------------------------------------------------------------
# The reference tree is the MAIN worktree, read from git rather than hardcoded: this script runs
# from inside a worktree as often as from the reference tree, and `$PWD` is not the answer in either
# case. `git worktree list --porcelain` puts the main worktree first, by definition.
ref_tree() {
    git worktree list --porcelain | sed -n '1s/^worktree //p'
}

# ---------------------------------------------------------------------------------------------
# `.worktreeinclude` application, shared by create and --check so the two cannot drift. Prints one
# `<mode> <path>` line per entry.
#
# ALWAYS READ FROM THE REFERENCE TREE, never from the worktree being checked. The reference tree is
# the authority on what a worktree needs; a worktree's own copy is whatever its base commit happened
# to carry, so `--check` on a worktree cut before this file existed would find nothing and have to
# choose between a fail-open and a false alarm. One source, no fallback, no transition case.
#
# IT DOES NOT `die`. This is called inside command substitutions, where `exit 1` leaves the SUBSHELL
# and the caller carries on — a fail-open that produced a real bug on the first end-to-end run: a
# missing `.worktreeinclude` printed its refusal and the run continued to the next check anyway. It
# prints `ERROR <msg>` lines instead, and every caller must check for them.
include_entries() {
    # FROM `hub/main`, NOT FROM THE CHECKOUT. `.worktreeinclude` is tracked, so the tip the
    # worktree is being cut from is the authority on it — and reading the checkout instead made this
    # script depend on a tree nobody is responsible for advancing. `git show` needs only the object
    # store, which the reference tree owns and which `fetch` has just made current.
    if ! inc=$(git -C "$1" show "$FT_REMOTE/main:.worktreeinclude" 2>&1); then
        echo "ERROR cannot read .worktreeinclude from $FT_REMOTE/main: $inc"
        return 0
    fi
    if [ -z "$inc" ]; then
        echo "ERROR .worktreeinclude is empty on $FT_REMOTE/main — refusing to act on a worktree whose setup is unspecified"
        return 0
    fi
    # Comments and blanks out; everything else must be exactly two fields with a known mode.
    printf '%s\n' "$inc" | while IFS= read -r line; do
        case "$line" in ''|'#'*) continue ;; esac
        mode=${line%% *}
        path=${line#* }
        case "$mode" in
            link|copy) : ;;
            *) echo "ERROR .worktreeinclude: unknown mode '$mode' in: $line"; continue ;;
        esac
        if [ "$path" = "$line" ]; then
            echo "ERROR .worktreeinclude: no path in: $line"
            continue
        fi
        printf '%s %s\n' "$mode" "$path"
    done
}

# Every caller's first act on an include listing. Kept as one function so no caller can forget it.
entries_or_die() {
    _e=$(include_entries "$1")
    case "$_e" in
        *ERROR*) printf '%s\n' "$_e" | sed -n 's/^ERROR /'"$SELF"' /p' >&2; exit 1 ;;
    esac
    printf '%s\n' "$_e"
}

# ---------------------------------------------------------------------------------------------
# --check: the health check that must be able to fail.
#
# A check must be able to fail: the failure this exists to catch is a worktree that LOOKS
# fine — the checkout is complete, `git status` is clean, the session starts — and is missing the
# machine-local state, so the breakage surfaces later as a module resolution error or a permission
# prompt storm. A check that passes on a worktree with no `settings.local.json` and a dangling
# `node_modules` symlink certifies exactly the breakage it was added to catch.
#
# Every condition the verdict names is a value this printed. Nothing is bundled: a missing copy and
# a dangling link are separate lines, because the repair differs.
check_worktree() {
    wt=$(cd "$1" && pwd -P)
    ref=$(cd "$wt" && ref_tree)
    entries=$(entries_or_die "$ref")
    problems=0

    # A subshell in a pipeline cannot increment `problems`, so collect first and judge after.
    findings=$(
        printf '%s\n' "$entries" | while IFS=' ' read -r mode path; do
            [ -n "$mode" ] || continue
            target="$wt/$path"
            if [ "$mode" = link ]; then
                if [ ! -L "$target" ]; then
                    if [ -e "$target" ]; then
                        echo "PRESENT-NOT-LINK $path — a real copy where .worktreeinclude says link"
                    else
                        echo "MISSING $path — .worktreeinclude says link, nothing is there"
                    fi
                elif [ ! -e "$target" ]; then
                    # -L true, -e false: the symlink exists and its target does not. This is the
                    # dangling-link fault, and it is invisible to `ls` and to `git status`.
                    echo "DANGLING $path -> $(readlink "$target") — symlink resolves to nothing"
                fi
            else
                [ -e "$target" ] || echo "MISSING $path — .worktreeinclude says copy, nothing is there"
            fi
        done
    )
    if [ -n "$findings" ]; then
        printf '%s\n' "$findings" | while IFS= read -r f; do echo "$SELF FAIL $f"; done
        problems=$(printf '%s\n' "$findings" | wc -l)
    fi

    # THE SYMLINK-DIVERGENCE SEAM. `.worktreeinclude` shares `node_modules` by symlink, which is
    # correct until this branch changes what should be installed. Then the shared tree is answering
    # for a dependency set this branch does not have, and the wrongness is silent — the install
    # resolves, to the wrong versions. Detectable from the diff, so detect it.
    if [ -L "$wt/node_modules" ]; then
        manifests=$(cd "$wt" && git diff --name-only "$FT_REMOTE/main...HEAD" 2>/dev/null \
                    | grep -E '(^|/)(package\.json|package-lock\.json|pnpm-lock\.yaml|npm-shrinkwrap\.json)$' \
                    | grep -v '^\.claude/vendor/' || true)
        if [ -n "$manifests" ]; then
            echo "$SELF FAIL SHARED-DEPS-DIVERGED — this branch changes a manifest while node_modules is still"
            echo "$SELF      a symlink to $(readlink "$wt/node_modules"). Changed: $(printf '%s' "$manifests" | tr '\n' ' ')"
            echo "$SELF      Break the link and install for real: rm node_modules && npm ci   (workspace-aware:"
            echo "$SELF      package.json declares packages/* and mcp/lsp-server. Vendored trees under"
            echo "$SELF      .claude/vendor/ carry their own manifests and are NOT ours to install — excluded above.)"
            problems=$((problems + 1))
        fi
    fi

    # THE TOOLCHAIN CHECK IS ADVISORY, AND THAT IS DELIBERATE. `bootstrap-dev-toolchain.sh --check`
    # is the right instrument and is not reimplemented here — it verifies the CI gate is reproducible
    # by INVOKING the tools. But what it measures is THE BOX, not this worktree: on the first
    # end-to-end run it reported INCOMPLETE ("root npm ci not done", "eslint not runnable") for a
    # worktree whose own setup was perfect. Making that fatal would (a) fail a good worktree whenever
    # the box's toolchain drifts, and (b) reintroduce the local-gate mandate that was
    # deliberately removed — no session is ever required to run the gates locally; hub judges the
    # branch. So it is run, and reported, and does not decide the verdict.
    if [ "${WORKTREE_CHECK_TOOLCHAIN:-1}" = 1 ] && [ -f "$wt/scripts/bootstrap-dev-toolchain.sh" ]; then
        # --offline: the fallow arm is `npx fallow@3`, 142 s warm on this box, and this
        # call sits on `session-succeed start`'s path -- a tree does not need fallow proven to be
        # usable, and a human was waiting up to five minutes for a verdict about the box.
        if ! out=$(cd "$wt" && sh scripts/bootstrap-dev-toolchain.sh --check --offline 2>&1); then
            echo "$SELF note  the BOX's gate toolchain is incomplete (not a worktree fault, not fatal):"
            printf '%s\n' "$out" | sed -n 's/^ *\(MISS\|INCOMPLETE\)/&/p' | sed "s/^/$SELF        /"
            echo "$SELF        full report: sh scripts/bootstrap-dev-toolchain.sh --check   (add --offline to skip the fallow fetch)"
        fi
    fi

    if [ "$problems" -gt 0 ]; then
        echo "$SELF $wt is NOT ready: $problems problem(s) above."
        return 1
    fi
    echo "$SELF $wt is ready (ref tree $ref; every .worktreeinclude entry present and resolving)."
    return 0
}

# ---------------------------------------------------------------------------------------------
# Branch naming. Two rules, both from measurement, both cheap to enforce and expensive to hit.
validate_branch() {
    b="$1"; ref="$2"

    # 1. A BARE PREFIX MAY NEVER BE A BRANCH NAME. Git refs are paths, so a name is either a file or
    #    a directory and not both: `feat` and `feat/x` cannot coexist, in either creation order
    #    (`fatal: cannot lock ref 'refs/heads/feat/x': 'refs/heads/feat' exists`). Requiring at
    #    least one `/` means this script can never be the one that creates the blocking bare ref.
    case "$b" in
        */*) : ;;
        *) die "branch '$b' has no '/'. A bare prefix may never be a branch name here: git refs are paths, so creating 'agent' would make every 'agent/<x>' uncreatable for ever. Use <kind>/<slug>." ;;
    esac
    case "$b" in
        */) die "branch '$b' ends in '/'" ;;
        /*) die "branch '$b' starts with '/'" ;;
    esac

    # Lowercase, hyphens, digits, dots — the convention every recovered branch name already follows
    # (measured 2026-08-24 from merge-commit subjects: wiki/ x5, fix/ x3, feat/ x3, wayfinder/,
    # agent/). Written down rather than redesigned.
    printf '%s' "$b" | grep -qE '^[a-z0-9]+(/[a-z0-9][a-z0-9._-]*)+$' \
        || die "branch '$b' is not <kind>/<slug> in lowercase with hyphens (e.g. agent/272-worktree-lifecycle)"

    # 2. A BRANCH NAME MUST NOT COLLIDE WITH A REPO-ROOT DIRECTORY NAME. That is a real
    #    trap — a checker reads a branch name as a path and finds one. `docs/whatever` resolves
    #    against the repo root and is indistinguishable from a path there; `agent/whatever` is not.
    #
    #    ROOT ENTRIES COME FROM `hub/main`, NOT FROM THE CHECKOUT. This used to be
    #    `[ -e "$ref/$kind" ]`, which reads the reference tree's working directory — and **this is
    #    the site that fails in the ACCEPTING direction.** A stale tree missing a root directory
    #    that `hub/main` has lets the colliding branch name straight through, with no message,
    #    producing exactly the shape this check exists to prevent. The other two stale-tree
    #    sites refuse and at least say something; this one is silent, which is why it is the one to
    #    fix first even though the include-list read is the one that announced itself.
    #
    #    `git ls-tree` lists the tip's root entries; the grep is exact-match on the whole line so a
    #    `kind` that is a prefix of a real entry (`doc` vs `docs`) does not collide falsely.
    kind=${b%%/*}
    roots=$(git -C "$ref" ls-tree --name-only "$FT_REMOTE/main" 2>&1) \
        || die "cannot list $FT_REMOTE/main's root entries ($roots) — refusing to judge '$b' against a tree I could not read"
    if printf '%s\n' "$roots" | grep -qxF "$kind"; then
        die "branch '$b' starts with '$kind/', and '$kind' is a repo-root entry. A branch name that resolves as a path is a known trap — a checker reads it as one. Pick a kind that is not a directory here (agent/, feat/, fix/, wiki/, wayfinder/)."
    fi
}

# ---------------------------------------------------------------------------------------------
case "${1:-}" in
    --check)
        check_worktree "${2:-$PWD}"
        exit $?
        ;;
    ''|-*)
        echo "usage: $0 <slug> [branch]        create ../CC-<slug> on $FT_REMOTE/main" >&2
        echo "       $0 --check [path]         verify an existing worktree" >&2
        exit 2
        ;;
esac

SLUG="$1"
printf '%s' "$SLUG" | grep -qE '^[a-z0-9][a-z0-9-]*$' \
    || die "slug '$SLUG' must be lowercase alphanumerics and hyphens"
BRANCH="${2:-agent/$SLUG}"

REF=$(ref_tree)
[ -n "$REF" ] || die "cannot find the main worktree — is this a git repo?"

# ============================================================================================
# THE FETCH MOVED TO THE TOP, AND THAT IS THE WHOLE FIX. Every read of TRACKED content now
# comes from `hub/main` rather than from the reference tree's checkout, so this script no longer
# depends on anyone keeping that checkout current — which nobody is responsible for doing (the
# tree was made read-only and deliberately gave it no updater).
#
# Measured on 2026-08-25 by a session: the reference tree was 5 commits behind `hub/main`
# and predated `.worktreeinclude` entirely, so this script refused at the moment that session was
# starting work, naming a missing file rather than a stale tree.
#
# THREE SITES READ TRACKED CONTENT, not one. Enumerated over all twelve `$REF` dereferences:
#
#   1. `.worktreeinclude` itself             — the measured failure. REFUSES, so it announces itself.
#   2. the repo-root collision test          — `[ -e "$ref/$kind" ]`. **ACCEPTS on a stale tree**,
#                                               producing no message at all. The dangerous one.
#   3. `git check-ignore`                    — reads `.gitignore`, whose no-trailing-slash rules
#                                               landed later. REFUSES.
#
# WHAT DOES *NOT* MOVE, and why the ticket's option 3 does not dissolve this on its own: the
# include SOURCES (`node_modules`, the vendored trees, `settings.local.json`) are **untracked and
# ignored** — measured, all seven, against a control (`README.md` reads TRACKED). They cannot come
# from `git show` and must exist as real files in a real checkout. So the reference tree stays
# load-bearing. But they are not in git at all, so **staleness cannot break them**: a tree five
# commits behind still has a perfectly good `node_modules`. The tree dependency remains and is
# provably immune to the failure this ticket is about. That settles a disagreement about where the dependency may stay.
# ============================================================================================
echo "$SELF reference tree: $REF"
echo "$SELF fetching $FT_REMOTE..."
git -C "$REF" fetch "$FT_REMOTE" || die "git fetch $FT_REMOTE failed — a worktree must start at the tip the gate measures"
git -C "$REF" rev-parse --verify --quiet "$FT_REMOTE/main" >/dev/null \
    || die "no $FT_REMOTE/main after fetching — every tracked read below comes from it, and guessing at a substitute is how a stale tree got read as a current one"

validate_branch "$BRANCH" "$REF"

DEST="$(dirname "$REF")/CC-$SLUG"
[ -e "$DEST" ] && die "$DEST already exists — pick another slug, or reap it first (scripts/worktree-reap.sh)"

# Site 1: the include list, from `hub/main` rather than from the checkout.
ENTRIES=$(entries_or_die "$REF")

# Site 3's other half stays here: the include SOURCES must exist as real files. Untracked, so this
# is a question about the reference tree's filesystem and not about its commit — see the block above.
printf '%s\n' "$ENTRIES" | while IFS=' ' read -r mode path; do
    [ -n "$mode" ] || continue
    [ -e "$REF/$path" ] || die ".worktreeinclude lists '$path', which does not exist in the reference tree $REF. A missing copy must fail loudly, not produce a worktree that looks fine and behaves subtly wrong. (This is about the FILE, not the commit: these paths are untracked, so a reference tree behind $FT_REMOTE/main is not the cause.)"
done || exit 1

# NEW BRANCH OR EXISTING ONE — `-b` only when the branch does not already exist.
#
# The first version always passed `-b`, so a worktree for an EXISTING branch was impossible, and the
# refusal blamed the wrong thing: git said `a branch named '<b>' already exists` and this script
# added "already checked out somewhere? see: git worktree list", pointing the reader at the worktree
# registry for a condition that has nothing to do with it. Found by using the script — twice in five
# minutes, adopting a branch pushed by another session and re-entering a branch after a merge, which
# is an ordinary thing to want and not an edge case.
#
# An existing branch is checked out AS IS: no `hub/main` argument, because that would be a request to
# RESET it to main and silently discard its commits. That is the whole hazard in this arm, and the
# reason it is a separate call rather than a variable holding `-b` or not.
if git -C "$REF" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    echo "$SELF branch '$BRANCH' already exists — checking it out as is, NOT resetting it to $FT_REMOTE/main"
    git -C "$REF" worktree add "$DEST" "$BRANCH" \
        || die "git worktree add failed for existing branch '$BRANCH' — it is checked out in another worktree (git worktree list), or its ref is unreadable"
else
    git -C "$REF" worktree add -b "$BRANCH" "$DEST" "$FT_REMOTE/main" \
        || die "git worktree add failed creating '$BRANCH' from $FT_REMOTE/main — see: git worktree list"
fi

# without these the branch's upstream points at main, and git's own push advice lands work
# there with no PR and no gate. Set on the worktree, not the reference tree.
git -C "$DEST" config branch.autoSetupMerge simple
git -C "$DEST" config core.hooksPath .githooks

# ============================================================================================
# SITE 3: EVERY ENTRY MUST BE GENUINELY IGNORED — checked IN THE NEW WORKTREE, and this is the one
# place this change moves a check LATER rather than earlier. The trade is stated because an unstated
# one is not defensible.
#
# The rule is unchanged: a TRACKED path in `.worktreeinclude` must never be copied in, because it
# would shadow the checkout with a stale copy that `git status` does not report.
#
# It used to run `cd "$REF" && git check-ignore`, which reads the REFERENCE TREE's ignore rules —
# stale, and wrong in the refusing direction: the no-trailing-slash rules that make these symlinks
# ignorable landed later, so a tree behind that commit refuses every entry with a message blaming
# `.worktreeinclude` for a defect in the tree.
#
# WHY NOT EVALUATE AGAINST `hub/main:.gitignore` instead, which would keep the check before
# creation? Because `check-ignore` needs a work tree, and the ignore rules are NOT one file: the
# vendored `.claude/vendor/playwright-cli/.gitignore` supplies the rule for one of these very
# entries (measured — `check-ignore -v` names that file, not the root one). Reconstructing that
# stack outside a checkout means reimplementing git's precedence, which is a second implementation
# of the thing being checked.
#
# THE COST, PLAINLY: the worktree exists before this validates, so a refusal has to clean up. It is
# bounded — NOTHING has been copied in yet, so the worktree is exactly what `git worktree add`
# produced — and the cleanup is `git worktree remove` with no `--force`, the same refusal-preserving
# call the reaper makes. If that removal itself fails, the path is printed rather than swallowed: a
# stray worktree that nobody was told about is worse than one that was.
# ============================================================================================
bad=$(printf '%s\n' "$ENTRIES" | while IFS=' ' read -r mode path; do
    [ -n "$mode" ] || continue
    (cd "$DEST" && git check-ignore -q "$path") || echo "$path"
done)
if [ -n "$bad" ]; then
    echo "$SELF .worktreeinclude lists path(s) that git does NOT ignore at $FT_REMOTE/main:" >&2
    printf '%s\n' "$bad" | sed "s/^/$SELF   /" >&2
    echo "$SELF A tracked file must never be copied in — it would shadow the checkout with a stale copy" >&2
    echo "$SELF that git status never reports. Remove the line, or fix .gitignore." >&2
    if git -C "$REF" worktree remove "$DEST" 2>/dev/null; then
        echo "$SELF removed the half-built worktree at $DEST" >&2
    else
        echo "$SELF COULD NOT REMOVE $DEST — it is still there and still registered; clean it up by hand" >&2
    fi
    exit 1
fi

# Apply the include list. `ln -s` with an ABSOLUTE target: a relative one is resolved against the
# link's directory, and `.claude/vendor/.../node_modules` sits four levels down, where `../` counting
# is exactly the kind of arithmetic that produces a dangling link nobody notices.
printf '%s\n' "$ENTRIES" | while IFS=' ' read -r mode path; do
    [ -n "$mode" ] || continue
    mkdir -p "$DEST/$(dirname "$path")"
    if [ "$mode" = link ]; then
        ln -s "$REF/$path" "$DEST/$path" || die "failed to link $path"
    else
        cp "$REF/$path" "$DEST/$path" || die "failed to copy $path"
    fi
    echo "$SELF $mode $path"
done || exit 1

echo "$SELF checking..."
if ! check_worktree "$DEST"; then
    die "the worktree was created but does NOT pass its own health check (above). It is at $DEST; fix or remove it."
fi

# THE CODE GRAPH, BUILT HERE SO THE SESSION STARTS WITH ONE. codegraph's MCP server acts only where
# `.codegraph/` exists, and a tree without it answers "not initialized". The index is per tree
# (gitignored), so each worktree builds its own, in seconds. Advisory like the toolchain check: a box
# without codegraph still gets a working tree.
# ONLY WHERE GIT IGNORES `.codegraph/`: an index in a tree that tracks it would make a fresh worktree
# dirty, and the reaper can never remove a dirty tree (test_worktree_lifecycle's clean-tree check).
if ! git -C "$DEST" check-ignore -q .codegraph/ 2>/dev/null; then
    echo "$SELF note  .codegraph/ is not gitignored in this tree, so no code graph was built (not fatal)"
elif command -v codegraph >/dev/null 2>&1; then
    if (cd "$DEST" && CODEGRAPH_TELEMETRY=0 codegraph init --yes >/dev/null 2>&1); then
        echo "$SELF codegraph index built (.codegraph/)"
    else
        echo "$SELF note  codegraph init failed in $DEST (not fatal): run it there to see why"
    fi
else
    echo "$SELF note  codegraph is not on PATH, so this tree has no code graph (not fatal)"
fi

# Registration is not a step: `git worktree list` IS the registry and `worktree add` wrote to it.
# Named here only because the checklist has it as item 7 and its absence would read as an omission.
cat <<EOF

$SELF ready.

  cd $DEST && claude          # launch the session HERE: its project dir is the cwd, and
                              # wiki-auto-commit.sh acts on the tree the session STARTED in.

Registered in \`git worktree list\`, which is what scripts/worktree-reap.sh reads.

This worktree is CLEAN — \`git status --porcelain\` is empty, every \`.worktreeinclude\` symlink
included. That is load-bearing and not incidental: a worktree that starts with untracked symlinks is
one \`git worktree remove\` refuses for ever, so the reaper could never remove it however long its
session had been gone, and "refuse and report dirty" would degrade into "refuse everything". It cost
one character per rule in .gitignore (\`foo/\` ignores the DIRECTORY only, and a symlink is a file to
git); see the A/B/A note at the top of that file.
EOF
