"""The forge's close-directive shape: a close keyword and an issue ref on one line.

`pr-queue.sh` imports it, and so can a commit-subject hook that denies close directives, so
there is one definition and the two cannot drift. The negation pattern (a close directive stated
as "does not close #N") stays in such a hook.
"""
import re

_CLOSE_KW = r"(?:close[sd]?|closing|fix(?:e[sd]|es)?|resolve[sd]?|resolving)"

# THE GENERAL FORM, of which the guard's negation pattern is one instance. What the forge
# actually parses is a close keyword followed by an issue ref -- nothing else. It has no notion of negation, and none
# of TENSE or NARRATION either, so a sentence merely REPORTING that something closed a ticket is
# read as an instruction to close it.
#
# The gap is not laziness, it is the parser being modelled: `closes the loop on #21` is not a
# close directive on any forge and must not be denied, while `closed #49` is one.
#
# `[ \t]*` AND NOT `\s*`, WHICH COST A RED CI RUN. `\s` matches newlines, so a subject ending in
# the word `fix` followed by a body line starting `#71` matched across the blank line between
# them -- and denied `#71 remains open.`, one of the safe wordings this guard's own refusal
# message recommends. Advice that is itself denied is worse than no advice. A close keyword and its
# number are on ONE line or they are not a directive.
CLOSE_REF = re.compile(r"\b" + _CLOSE_KW + r"\b[ \t]*(#\d+)", re.IGNORECASE)
