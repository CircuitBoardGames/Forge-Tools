#!/usr/bin/env bash
# bootstrap-target-repo.sh — the mechanical half of bootstrapping a target repo.
#
# Drops the CI test gate and fallow config into a target repo, then prints the
# interactive/agent-driven steps that a human or agent still has to run. This
# handles ONLY the deterministic parts; the knowledge graphs, intake interview,
# and delivery pipeline are agent/human work.
#
# Usage:
#   scripts/bootstrap-target-repo.sh /path/to/target-repo
#   scripts/bootstrap-target-repo.sh /path/to/target-repo --no-fallow   # non-TS/JS target
#   scripts/bootstrap-target-repo.sh /path/to/target-repo --forge-repo owner/name
#
# Idempotent: never overwrites a file the target already has; reports skips.
# Run from a feature branch in the target and review the diff before pushing.

set -euo pipefail

HUB_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"   # through a PATH symlink
TEMPLATES="$HUB_DIR/scripts/templates"
HUB_API="${HUB_API:-$HUB_DIR/scripts/hub-api.sh}"   # overridable so a test never reaches the forge
# Site configuration. FORGE_TOOLS_FALLOW_ACTION, when set, replaces the fallow
# template's public `uses:` ref -- a deployment's local mirror of the same action.
# shellcheck source=ft-config.sh
. "$HUB_DIR/scripts/ft-config.sh"
FALLOW_ACTION="${FORGE_TOOLS_FALLOW_ACTION:-}"
case "$FALLOW_ACTION" in
  *[[:space:]\|\&\\]*) echo "ERR: FORGE_TOOLS_FALLOW_ACTION='$FALLOW_ACTION' is not a plain action ref (owner/repo@sha or a URL)" >&2; exit 2 ;;
esac

TARGET="${1:-}"
NO_FALLOW=false
FORGE_REPO=""
prev=""
for arg in "$@"; do
  [ "$arg" = "--no-fallow" ] && NO_FALLOW=true
  [ "$prev" = "--forge-repo" ] && FORGE_REPO="$arg"
  prev="$arg"
done

if [ -z "$TARGET" ] || [ ! -d "$TARGET" ]; then
  echo "ERR: pass the path to an existing target repo" >&2
  echo "usage: $0 /path/to/target-repo [--no-fallow] [--forge-repo owner/name]" >&2
  exit 2
fi
TARGET="$(cd "$TARGET" && pwd)"
if [ ! -d "$TARGET/.git" ]; then
  echo "ERR: $TARGET is not a git repo" >&2
  exit 2
fi

say()  { printf '%s\n' "$@"; }
copied=0; skipped=0

place() {  # place <template-src> <target-relpath>
  local src="$1" rel="$2" dst="$TARGET/$2"
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ]; then
    say "  skip   $rel (already exists)"; skipped=$((skipped+1))
  else
    cp "$src" "$dst"; say "  create $rel"; copied=$((copied+1))
  fi
}

# `.forgejo/workflows`, NOT `.github/workflows`. The hub forge reads `.forgejo/workflows`
# INSTEAD of `.github` when it exists, so on a fork carrying upstream's `.github` workflows a template
# placed beside them was either skipped (never-overwrite) or ignored, and upstream's CI ran.
say "── Bootstrapping target: $TARGET"
say ""
say "1. CI test gate"
place "$TEMPLATES/test.yml" ".forgejo/workflows/test.yml"
say ""

if [ "$NO_FALLOW" = false ]; then
  say "2. fallow (TS/JS codebase intelligence)"
  fallow_src="$TEMPLATES/fallow.yml"
  if [ -n "$FALLOW_ACTION" ]; then   # substituted into a copy, so an existing target file is still never touched
    fallow_src="$(mktemp)"; trap 'rm -f "$fallow_src"' EXIT
    sed "s|uses: [^ ]*\( *# FORGE_TOOLS_FALLOW_ACTION\)|uses: $FALLOW_ACTION\1|" "$TEMPLATES/fallow.yml" > "$fallow_src"
    grep -qF "uses: $FALLOW_ACTION " "$fallow_src" \
      || { echo "ERR: could not substitute FORGE_TOOLS_FALLOW_ACTION into $TEMPLATES/fallow.yml (marker missing?)" >&2; exit 2; }
    say "  fallow action: $FALLOW_ACTION (FORGE_TOOLS_FALLOW_ACTION)"
  fi
  place "$fallow_src"    ".forgejo/workflows/fallow.yml"
  place "$TEMPLATES/fallowrc.json" ".fallowrc.json"
  say ""
else
  say "2. fallow — skipped (--no-fallow)"
  say ""
fi

say "── Placed $copied file(s), skipped $skipped."
say ""
# THE FORGE ENFORCES THE GATE. Without protection a target repo lands PRs on red
# checks and lets two PRs sit open against a moving base -- measured 2026-09-08 on a target repo.
# `repo provision --kind node` commits the same test.yml to the default branch when it has no gate,
# enables Actions, adds the whitelist collaborators and protects the branch with the contexts the
# gate on that branch registers, reading all of it back. Skipped when no --forge-repo is given, and
# SAID rather than silent, because an unprotected target looks exactly like a protected one from the tree.
if [ -n "$FORGE_REPO" ]; then
  say "3. provision $FORGE_REPO (gate on the default branch, Actions, protection)"
  "$HUB_API" repo provision "$FORGE_REPO" --kind node | sed 's/^/   /'
  if [ "$NO_FALLOW" = false ]; then
    say "   Fallow /* becomes a required context once fallow.yml is on the default branch:"
    say "   re-run hub-api repo provision $FORGE_REPO --kind node after this branch lands."
  fi
  say ""
else
  say "3. forge provisioning — NOT applied (no --forge-repo owner/name). The forge will accept a red"
  say "   or stale merge until you run: hub-api repo provision <owner/name> --kind node"
  say ""
fi
say ""
say "── STILL TODO (agent/human-driven):"
say "   • Verify the test gate is green locally before pushing (lint + tests)."
say "   • /understand + set autoUpdate:true in .understand-anything/config.json"
say "   • /understand-domain  → domain-graph.json"
say "   • /understand-onboard → onboarding guide, link from README"
say "   • /grill-with-docs    → CONTEXT.md + docs/adr/  (interactive interview)"
say "   • /improve quick --issues  → seed the issue tracker"
say "   • /triage → /implement one ticket end-to-end (pipeline acceptance test)"
say "   • Add a page for <target> to your knowledge vault, if you keep one (not in the target)."
