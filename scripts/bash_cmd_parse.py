"""Shared Bash-command parsing for the PreToolUse guards.

Extracted from `self-match-guard.py` when `lifeline-guard.py` needed the same three things. Not a
general shell parser and must never become one: it exists so a guard can ask "which commands does
this call actually run, and what is the real command name" without each guard re-deriving the
answer -- and getting a different one.

Every function here is deliberately conservative in the same direction: when the text cannot be
resolved, it returns *less*, so a guard built on it stays silent rather than firing on something it
misread. The module name has underscores because hooks import it (`self-match-guard.py` cannot be
imported -- hyphens).
"""
import os
import re
import shlex

ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")

# Prefixes that run another command, so the real command name is further along. Found because a
# mutation FAILED to fail: relaxing self-match-guard's first-token check changed no test, and asking
# why showed the flag walk was masking it -- and that `doas pkill -f x` was therefore silent. On this
# box doas is constant, so that was the gap, not the mutation.
WRAPPERS = {"doas", "sudo", "env", "nohup", "time", "command", "builtin", "exec", "nice", "ionice",
            "timeout", "setsid", "stdbuf"}


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc BODIES -- they are data, not commands.

    Segments are split on newlines, so without this a heredoc line reading `pgrep -f <pattern>`
    parses as an invocation. That is not hypothetical: self-match-guard fired on the very
    `git commit` whose message documented the measurement, which is the likeliest place anyone
    writes these strings.

    IDEMPOTENT, and it has to be: `segments()` strips before parsing, so any caller that also stripped
    (`tree-custody-guard`, since deleted, did) ran it twice, and a second pass over `cat <<EOF` with its body and
    marker already gone swallowed every remaining LINE OF THE COMMAND -- the trailing `rm -rf /` went
    invisible. What makes it idempotent is skipping only to a closing marker that actually EXISTS
    ahead. An unterminated heredoc therefore keeps its body as text, which is the loud direction and
    the one this box requires.
    """
    lines, out, i = cmd.split("\n"), [], 0
    while i < len(lines):
        out.append(lines[i])
        m = HEREDOC.search(lines[i])
        if m and any(ln.strip() == m.group(2) for ln in lines[i + 1:]):
            marker = m.group(2)
            i += 1
            while lines[i].strip() != marker:
                i += 1
        i += 1
    return "\n".join(out)


def unwrap(tokens: list) -> list:
    """Strip `VAR=val` prefixes and command-runners to reach the actual command name."""
    i = 0
    while i < len(tokens) and (ASSIGN.match(tokens[i]) or os.path.basename(tokens[i]) in WRAPPERS):
        i += 1
    return tokens[i:]


# git's global options that take a SEPARATE value, so the subcommand is further along.
GIT_OPTS_WITH_VALUE = {"-c", "-C", "--exec-path", "--git-dir", "--work-tree", "--namespace"}


def git_subcommand(tokens: list) -> tuple:
    """`(subcommand, -C dir or None)` for an UNWRAPPED segment, `("", None)` when it is not git.

    THE ONE WALK. git_trigger took the first non-flag token, so `git -c k=v push` read as
    subcommand `k=v`; gate-watch-rearm walked the options but compared `seg[0]` to "git" literally, so
    `/usr/bin/git push` and `X=1 git push` were no push. Two guards, same question, 4 of 7 disagreed."""
    if not tokens or os.path.basename(tokens[0]) != "git":
        return "", None
    i, cdir = 1, None
    while i < len(tokens) and tokens[i].startswith("-"):
        if tokens[i] == "-C" and i + 1 < len(tokens):
            cdir = tokens[i + 1]
        i += 2 if tokens[i] in GIT_OPTS_WITH_VALUE else 1
    return (tokens[i] if i < len(tokens) else ""), cdir


SEPARATOR_CHARS = "|&;\n"
# `\n` joins shlex's own punctuation set so a newline comes back as its OWN token instead of being
# eaten as whitespace. Adjacent punctuation accumulates into runs, so `&&\n` and `\n\n` arrive as one
# token each -- which is why a separator is "every character is a separator character" rather than
# membership in a fixed set.
PUNCTUATION = "();<>|&\n"


def _tokenise(text: str) -> list:
    lex = shlex.shlex(text, posix=True, punctuation_chars=PUNCTUATION)
    lex.whitespace_split = True
    lex.whitespace = " \t\r"  # NOT "\n" -- see PUNCTUATION
    return list(lex)


def _split(tokens: list) -> list:
    out, cur = [], []
    for tok in tokens:
        if tok and all(c in SEPARATOR_CHARS for c in tok):
            out.append(cur)
            cur = []
        else:
            cur.append(tok)
    out.append(cur)
    return out


def segments(cmd: str) -> list:
    """Tokenised command segments of a Bash call, heredoc bodies removed.

    TOKENISE FIRST, THEN SPLIT -- never the other way round. The first version split the raw text on
    a `||`/`&&`/`|;&` regex and shlex'd the pieces, which cut straight through quoted arguments: an `earlyoom
    --avoid '^(ttyd|tmux)$'` was chopped at the pipe into two fragments with unbalanced quotes, both
    unparseable, so `lifeline-guard` returned SILENT for every realistic avoid regex -- including the
    one the box actually ships. Silence from a parse failure is indistinguishable from silence meaning
    "this command is fine", which is the whole failure mode these guards exist to avoid. Caught by a
    test asserting the guard still complains about `-m 99` when the avoid list IS correct.

    `punctuation_chars=True` makes shlex emit `|`, `&&`, `;` as their own tokens while leaving quoted
    text intact, so `ls|wc` still splits and `'^(a|b)$'` still does not.

    NEWLINES ARE A SEPARATOR, NOT A PRE-SPLIT. The second version split the heredoc-stripped text on
    newlines and tokenised each line, because `whitespace_split` consumes newlines and would otherwise
    merge a heredoc's trailing command into the line above it (self-match-guard's
    `test_a_real_command_after_a_heredoc_is_still_seen`). But splitting first cuts through a quoted
    argument the same way the first version's regex split did: `git commit -m "subject\n\nearlyoom
    -m 99 loop"` lost its quote context, line 1 was unbalanced and parsed to `[]`, and every line of
    what was quoted DATA was re-read as a command someone had typed -- so lifeline-guard denied a
    commit message that merely quoted `earlyoom -m 99`, and git_trigger read prose mentioning
    `git push` as aiming at a push. Fixed by keeping the newline as a token (`PUNCTUATION`) instead of
    removing it before shlex sees it, which preserves the heredoc property without the pre-split.

    If the WHOLE text cannot be tokenised, this falls back to the old per-line parse rather than
    returning nothing. That is deliberate and is the only direction allowed here: every guard on this
    box is built so an unreadable line reaches a regex or a refusal, never silence, so the fallback
    must not be quieter than the behaviour it replaces.

    An unparseable line (unbalanced quotes) yields `[]`: a guard should say nothing about text it could
    not read, and must not crash the tool call it was inspecting.
    """
    text = strip_heredocs(cmd)
    try:
        return _split(_tokenise(text))
    except ValueError:  # unbalanced quoting: fall through to the cruder split below
        pass
    out = []
    for line in text.split("\n"):
        try:
            out += _split(_tokenise(line))
        except ValueError:
            out.append([])
    return out


def segments_and_unreadable(cmd: str) -> tuple:
    """`(segments, unreadable_lines)` — the idiom every guard with a regex fallback needs.

    Three guards (`git_trigger`, `verification-guard`, `box-guard`) each hand-rolled "split on
    newlines, `segments()` each line, regex the lines that yielded nothing". The split was the bug
    above, and doing it in the caller put it out of `segments()`' reach. So the whole shape lives here
    once: segments come from the WHOLE text, and a line that cannot be tokenised on its own is
    returned as text for the caller's regex.

    Empty token lists are filtered out, because `[[]]` -- one empty segment -- is what an unreadable
    line yields, and `if not segs` on the unfiltered list reads that as "parsed, nothing in it" and
    falls through to silence. That shape shipped broken twice in this repo.
    """
    text = strip_heredocs(cmd)
    segs = [s for s in segments(text) if s]
    bad = [ln for ln in text.split("\n") if ln.strip() and not any(segments(ln))]
    return segs, bad


# `sh script.sh …` — the SCRIPT is the command, not the shell. Deliberately NOT folded into
# `unwrap()`: that is shared with `self-match-guard` and `lifeline-guard`, which must still see the
# shell itself, and widening it would change what they match without anyone asking for it.
_SHELL_RUNNERS = {"sh", "bash", "dash", "ash", "zsh", "ksh"}


def invokes(cmd: str, name: str, *subcommand: str) -> bool:
    """Is `name`, followed by `subcommand`, invoked IN COMMAND POSITION?

    THE FAILURE THIS EXISTS FOR: a guard keyed on a substring fires on prose. Measured 2026-09-04 --
    `prune-landed-branches.sh` matched `gh[[:space:]]+pr[[:space:]]+merge` against raw command text,
    so a `python3 -c` whose source contained the literal "gh pr merge" ran the GitHub prune path,
    including its delete loop in any checkout with a GitHub remote. A first fix removed the heredoc half
    of that by stripping heredoc bodies; a mention outside a heredoc still matched, in both pruners.

    Matching is per SEGMENT, after `unwrap()` strips `VAR=val` prefixes and wrappers, so
    `X=1 doas ./scripts/hub-api.sh pr merge` matches and `echo "gh pr merge"` does not -- the name
    has to be the thing being run. Compared on BASENAME, so `scripts/hub-api.sh`,
    `./scripts/hub-api.sh` and a bare `hub-api.sh` are one case.

    CEILING: `sh -c '<text>'` is not descended into, so a command buried in a nested shell reads as
    absent. For a PostToolUse pruner that means not pruning, which is the safe direction; a guard
    that DENIES on a match must not rely on this to be complete.
    """
    for seg in segments(cmd):
        toks = unwrap(seg)
        while len(toks) >= 2 and os.path.basename(toks[0]) in _SHELL_RUNNERS \
                and not toks[1].startswith("-"):
            toks = toks[1:]
        if not toks or os.path.basename(toks[0]) != name:
            continue
        if list(toks[1:1 + len(subcommand)]) == list(subcommand):
            return True
    return False
