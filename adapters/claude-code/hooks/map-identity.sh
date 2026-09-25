#!/bin/sh
# Claude Code SessionStart hook: hand this session's identity to Forge-Tools.
#
# The Forge-Tools core reads NO harness variable. It takes the caller's session id from
# FORGE_TOOLS_SESSION_ID (claim ownership, handoff holder and signature, gate-watch's `session`
# field) and the pid to wake from FORGE_TOOLS_WAKE_PID (gate-watch register/subscribe/adopt,
# pr-queue's detach, `pr create`'s watch). This hook is the only place those are mapped from Claude
# Code's own names:
#
#     CLAUDE_CODE_SESSION_ID -> FORGE_TOOLS_SESSION_ID
#     CLAUDE_PID             -> FORGE_TOOLS_WAKE_PID
#
# It appends shell-quoted `export` lines to $CLAUDE_ENV_FILE. MEASURED:
# a SessionStart hook's exports written to $CLAUDE_ENV_FILE DO reach later Bash tool calls in
# `claude -p`. UNMEASURED: whether CLAUDE_PID is present in the SessionStart hook's own
# environment; a variable that is unset or empty here is simply not exported, so the core then
# behaves as it does from a bare shell (no wake, "no FORGE_TOOLS_WAKE_PID").
#
# Not under Claude Code (no CLAUDE_ENV_FILE): writes nothing, exits 0. Never fails a session start.
[ -n "${CLAUDE_ENV_FILE:-}" ] || exit 0

# 'it'"'"'s' quoting: safe for any value `eval`/`.` will read back.
q() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

{
    [ -z "${CLAUDE_CODE_SESSION_ID:-}" ] || echo "export FORGE_TOOLS_SESSION_ID=$(q "$CLAUDE_CODE_SESSION_ID")"
    [ -z "${CLAUDE_PID:-}" ] || echo "export FORGE_TOOLS_WAKE_PID=$(q "$CLAUDE_PID")"
} >> "$CLAUDE_ENV_FILE" 2>/dev/null || true
exit 0
