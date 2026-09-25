"""Which processes are agent sessions, read from /proc -- harness knowledge, so it is configurable.

`pr-queue.sh`, `worktree-reap.sh` and `handoff.sh` import it for `AGENT_COMMS` and `foreign`. The
doctrine: detect agents from `comm` + cwd out of /proc, never by matching command text.

`FORGE_TOOLS_AGENT_COMMS` names the agents' exact `comm` values, space-separated; the default is
`claude omp`. AN EMPTY OR BLANK VALUE MEANS THE DEFAULT, never an empty set: `worktree-reap.sh`
reads "no agent owns this tree" as permission to delete it, so an empty agent set would make every
worktree look orphaned. `comm` is truncated at 15 chars (TASK_COMM_LEN includes the NUL).
"""
from __future__ import annotations

import os

AGENT_COMMS = tuple(os.environ.get("FORGE_TOOLS_AGENT_COMMS", "").split()) or ("claude", "omp")


def _read(pid, name: str, base: str) -> str:
    try:
        with open(os.path.join(base, str(pid), name)) as f:
            return f.read()
    except OSError:
        return ""


def foreign(pid, proc: str) -> bool:
    """True when `pid` PROVABLY runs as another user -- so it is not one of this user's sessions.

    Measured: dsh's Agent SDK ships a binary whose comm is literally `claude`, run as the `dsh`
    service user. procfs makes another uid's cwd unreadable by design, so every consumer that
    refuses on "an agent I cannot place" refused for days on a process that cannot write our trees.

    Proof only: a missing or unparseable `status` is NOT foreign, so an agent we cannot read keeps
    counting. The real uid is compared rather than the owner of `/proc/<pid>`, which reads root for
    a non-dumpable process. Callers that locate agents by cwd skip a foreign one only where that cwd is
    UNREADABLE: run as root, every session here is "foreign" yet every cwd is readable, so a root
    reaper still counts them all.
    """
    for line in _read(pid, "status", proc).splitlines():
        if line.startswith("Uid:"):
            try:
                return int(line.split()[1]) != os.getuid()
            except (IndexError, ValueError):
                return False
    return False
