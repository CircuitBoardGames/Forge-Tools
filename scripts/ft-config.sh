# shellcheck shell=sh
# ft-config.sh -- SOURCED by every Forge-Tools shell script. The one reader of site configuration.
#
# Every site value is an environment variable named FORGE_TOOLS_<KEY>. It may also be set in the
# config file, ${FORGE_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/forge-tools/config}, as plain
# `FORGE_TOOLS_<KEY>=value` lines (`#` starts a comment line; one pair of surrounding quotes and a
# leading `~/` are understood). THE ENVIRONMENT WINS: a key already in the environment, even empty,
# is never overwritten from the file. `config.example` at the repo root documents every key.
#
# The file is PARSED, NEVER SOURCED OR EVAL'D: a value is taken literally, and a line whose name is
# not FORGE_TOOLS_<A-Z0-9_> is reported and skipped, so the file cannot set PATH or run a command.
# scripts/ft_config.py is the same reader for Python; the two must agree.

FT_CONFIG_FILE="${FORGE_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/forge-tools/config}"
if [ -r "$FT_CONFIG_FILE" ]; then
    FT_CR=$(printf '\r')
    while IFS= read -r _ft_line || [ -n "$_ft_line" ]; do
        _ft_line=${_ft_line%"$FT_CR"}                        # a CRLF file
        _ft_line=${_ft_line%"${_ft_line##*[! 	]}"}          # trailing blanks
        _ft_line=${_ft_line#"${_ft_line%%[! 	]*}"}         # leading blanks
        case "$_ft_line" in ''|'#'*) continue ;; esac
        _ft_k=${_ft_line%%=*}; _ft_v=${_ft_line#*=}
        case "$_ft_k" in
            "$_ft_line"|FORGE_TOOLS_CONFIG|*[!A-Z0-9_]*|FORGE_TOOLS_) _ft_k="" ;;
            FORGE_TOOLS_*) ;;
            *) _ft_k="" ;;
        esac
        if [ -z "$_ft_k" ]; then
            printf '%s\n' "forge-tools: $FT_CONFIG_FILE: skipping '$_ft_line' -- not a FORGE_TOOLS_<KEY>=value line" >&2
            continue
        fi
        case "$_ft_v" in \"*\"|\'*\') [ ${#_ft_v} -ge 2 ] && { _ft_v=${_ft_v#?}; _ft_v=${_ft_v%?}; } ;; esac
        # shellcheck disable=SC2088 # a literal `~/` in the FILE, expanded here on purpose
        case "$_ft_v" in '~/'*) _ft_v="$HOME/${_ft_v#'~/'}" ;; esac
        printenv "$_ft_k" >/dev/null 2>&1 || export "$_ft_k=$_ft_v"
    done < "$FT_CONFIG_FILE"
fi
unset _ft_line _ft_k _ft_v

# NEUTRAL DEFAULTS, for the keys that have one. Not exported: a child script re-sources this file
# and computes its own, so a default never masquerades as a setting.
: "${FORGE_TOOLS_REMOTE:=origin}"
: "${FORGE_TOOLS_MERGE_SCRATCH_PREFIX:=${XDG_CACHE_HOME:-$HOME/.cache}/forge-tools/merge}"
: "${FORGE_TOOLS_CREDENTIALS_DIR:=${XDG_CONFIG_HOME:-$HOME/.config}/forge-tools}"
: "${FORGE_TOOLS_WORKTREE_PREFIX:=CC-}"

# ft_need KEY WHAT -- exit 1, naming FORGE_TOOLS_KEY and the config file, when it is unset or empty.
# For the keys with NO neutral default (the forge URL, the owner): guessing either acts on somebody
# else's forge or repo, so an unconfigured site is told what to set instead.
ft_need() {
    case "$1" in ''|*[!A-Z0-9_]*) printf '%s\n' "forge-tools: ft_need: bad key '$1'" >&2; exit 2 ;; esac
    eval "_ft_have=\${FORGE_TOOLS_$1:-}"
    [ -n "$_ft_have" ] && return 0
    printf '%s\n' "forge-tools: FORGE_TOOLS_$1 is not set -- $2." \
        "  It has no default. Set it in the environment or in $FT_CONFIG_FILE (see config.example)." >&2
    exit 1
}

# ft_checkout -- the consumer checkout: FORGE_TOOLS_CHECKOUT, else the cwd's git top level.
ft_checkout() {
    printf '%s' "${FORGE_TOOLS_CHECKOUT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd -P)}"
}

# ft_repo -- sets FORGE_TOOLS_REPO (owner/name of the consumer repo) when it is not set:
# FORGE_TOOLS_OWNER (required) plus the name the forge remote's URL ends in.
ft_repo() {
    [ -n "${FORGE_TOOLS_REPO:-}" ] && return 0
    ft_need OWNER "the forge owner (user or org) of the repo this acts on; or set FORGE_TOOLS_REPO=owner/name"
    _ft_url=$(git -C "$(ft_checkout)" remote get-url "$FORGE_TOOLS_REMOTE" 2>/dev/null) || _ft_url=""
    _ft_name=${_ft_url%.git}; _ft_name=${_ft_name%/}; _ft_name=${_ft_name##*[/:]}
    [ -n "$_ft_name" ] || ft_need REPO "no '$FORGE_TOOLS_REMOTE' remote (FORGE_TOOLS_REMOTE) in $(ft_checkout) to read the repo name from"
    FORGE_TOOLS_REPO="$FORGE_TOOLS_OWNER/$_ft_name"
}

# ft_forge_hosts -- the hosts that name the forge: the FORGE_TOOLS_FORGE_URL host (or HUB_URL's,
# which overrides it) plus FORGE_TOOLS_FORGE_HOSTS (space-separated: ssh aliases, other addresses).
ft_forge_hosts() {
    _ft_u=${HUB_URL:-${FORGE_TOOLS_FORGE_URL:-}}
    _ft_u=${_ft_u#*://}; _ft_u=${_ft_u%%/*}; _ft_u=${_ft_u##*@}; _ft_u=${_ft_u%%:*}
    printf '%s\n' $_ft_u ${FORGE_TOOLS_FORGE_HOSTS:-}
}
