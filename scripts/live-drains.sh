#!/bin/sh
# live-drains.sh -- print every `pr-queue.sh` run live on this box, one per line; exit 0 if any,
# 1 if none. The ONE definition of "is a drain already running": a drain-duty hook skips
# its turn on it, and a hand-typed `pr-queue.sh drain|approve|merge-requested` prints it. That verb
# had no check at all, so on 2026-09-22 two hand-typed drains collided and the second stopped at
# rc=8 on "main moved under us" -- true, and attributed to nothing its reader could see.
#
# It reports and never refuses. It is NOT the concurrency guard: a second `drain` defers to the
# live one through `pr-queue.sh`'s `_claim_queue`, because two drains are two fronts.
#
# Two signals, because neither covers every run:
#   1. a `<FORGE_TOOLS_MERGE_SCRATCH_PREFIX>-<pid>` scratch tree whose pid is live and OLDER than the tree. A run makes the
#      tree only for a batch or an admission, and leaves it standing on exits 4/5/6, so a live pid
#      NEWER than the tree only inherited the number. Age is `ps -o etimes`.
#   2. a recent run log some process holds open -- every detached run holds its own as stdout for
#      exactly its lifetime. No `fuser` means this signal finds nothing, and 1 is all.
#      An open log is not proof of a drain (a `tail -f` holds it too); that false reading costs a
#      skipped duty or one extra line, and matching argv instead would buy the self-match trap.
#
# LIVE_DRAINS_WT_BASE / LIVE_DRAINS_LOG_DIR override where it looks; `pr-queue.sh` passes its own.
set -u
_ft_cfg="$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")/ft-config.sh"
[ -r "$_ft_cfg" ] || { echo "live-drains: cannot read $_ft_cfg -- Forge-Tools' config reader must sit beside this script" >&2; exit 2; }
. "$_ft_cfg"
WT_BASE="${LIVE_DRAINS_WT_BASE:-$FORGE_TOOLS_MERGE_SCRATCH_PREFIX}"
LOG_DIR="${LIVE_DRAINS_LOG_DIR:-$HOME/.cache/pr-queue}"
found=1

for _d in "$WT_BASE"-*; do
    [ -d "$_d" ] || continue
    _p=${_d##*-}
    case "$_p" in ''|*[!0-9]*) continue ;; esac
    [ -d "/proc/$_p" ] || continue
    # The pid's REAL age, not `/proc/<pid>`'s mtime: procfs stamps that when the inode is first
    # looked up, so a drain nobody had looked at read as newer than its own tree.
    _age=$(ps -o etimes= -p "$_p" 2>/dev/null | tr -d ' ')
    [ -n "$_age" ] || continue
    [ "$_age" -lt $(( $(date +%s) - $(stat -c %Y "$_d") )) ] && continue   # started after the tree: recycled
    echo "pid $_p  tree $_d"
    found=0
done

# shellcheck disable=SC2044 # generated names (`<ts>-<pid>-<verb>.log`), never whitespace; a
# `find | while` loop would set `found` in a subshell and lose it.
for _l in $(find "$LOG_DIR" -maxdepth 1 -name '*.log' -mmin -1440 2>/dev/null); do
    _pids=$(fuser "$_l" 2>/dev/null) || continue
    echo "pid$(printf '%s' "$_pids" | tr -s ' ' ' ')  log $_l"
    found=0
done
exit $found
