"""Code Forge-Tools once borrowed from a consumer's hooks now lives here.

`close_ref.py` (the CLOSE_REF matcher), `agent_comms.py` (AGENT_COMMS, configurable), and no core
script reaching into a `.claude/hooks/` directory.
"""

import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from close_ref import CLOSE_REF  # noqa: E402


def test_close_ref_matches_what_the_forge_reads_as_a_close():
    """The forge reads keyword + number and nothing else, so a NEGATED or NARRATED close is one."""
    for text in ("Closes #68", "fixed #7", "Does not close #74.", "this resolves #1\nmore"):
        assert CLOSE_REF.search(text), f"missed a close directive: {text!r}"


def test_close_ref_does_not_match_what_the_forge_ignores():
    """No number after the keyword, a newline between them (the red CI run the comment records),
    or a bare `(#N)` cross-reference."""
    for text in ("closes the loop on #11", "a fix\n#13 remains open.", "subject (#42)"):
        assert not CLOSE_REF.search(text), f"matched a non-directive: {text!r}"


def _agent_comms(value):
    env = {k: v for k, v in os.environ.items() if k != "FORGE_TOOLS_AGENT_COMMS"}
    if value is not None:
        env["FORGE_TOOLS_AGENT_COMMS"] = value
    r = subprocess.run([sys.executable, "-c", "import agent_comms; print(' '.join(agent_comms.AGENT_COMMS))"],
                       cwd=SCRIPTS, env=env, capture_output=True, text=True, check=True)
    return r.stdout.split()


def test_agent_comms_default_is_claude_and_omp():
    assert _agent_comms(None) == ["claude", "omp"]


def test_FORGE_TOOLS_AGENT_COMMS_overrides_the_set():
    assert _agent_comms("dsh  claude") == ["dsh", "claude"]


def test_a_BLANK_agent_set_falls_back_to_the_default_never_empty():
    """An empty set makes every worktree look orphaned to `worktree-reap.sh`, which then deletes."""
    assert _agent_comms("") == ["claude", "omp"]
    assert _agent_comms("   ") == ["claude", "omp"]


def test_the_override_reaches_the_reaper(tmp_path):
    """End to end: a `dsh` session owns its worktree only once `dsh` is named an agent."""
    from test_worktree_lifecycle import make_proc, make_ref_tree, make_worktree, reap
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-dsh", "agent/dsh-owned")
    proc = make_proc(tmp_path, {4242: ("dsh", wt)})
    r = reap(ref, proc, env_extra={"FORGE_TOOLS_AGENT_COMMS": "claude omp dsh"})
    assert r.returncode == 0, r.stderr
    assert "LIVE" in r.stdout and "4242" in r.stdout, f"the dsh session was not an owner:\n{r.stdout}"
    # CONTROL: without the override the same process is not an agent, so no LIVE line.
    r = reap(ref, proc, env_extra={"FORGE_TOOLS_AGENT_COMMS": ""})
    assert "LIVE" not in r.stdout, f"dsh counted as an agent without being named:\n{r.stdout}"


def test_no_core_script_reaches_into_a_claude_hooks_directory():
    """Code lines only: a comment may name a hub hook, a path in code may not. No exception: the one
    this used to allow, handoff.sh's default for HANDOFF_CACHE_HOOK, is gone."""
    files = subprocess.run(["git", "ls-files", "scripts"], cwd=SCRIPTS.parent,
                           capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for f in files:
        if f.startswith("scripts/tests/") or f.startswith("scripts/templates/"):
            continue
        for n, line in enumerate((SCRIPTS.parent / f).read_text(errors="replace").splitlines(), 1):
            code = line.lstrip()
            if code.startswith("#") or (".claude/hooks" not in line and '".claude", "hooks"' not in line):
                continue
            hits.append(f"{f}:{n}: {line.strip()}")
    assert files, "git ls-files found nothing, so this scanned nothing"
    assert not hits, "core scripts reach into .claude/hooks:\n" + "\n".join(hits)


def test_the_override_reaches_gate_watch_delivery(tmp_path):
    """gate-watch delivers only to a live AGENT; a `dsh` requester is one once `dsh` is named."""
    from test_gate_watch import SHA, _env, _events, _proc, run
    env, _, sent = _env(tmp_path, rc="0", FORGE_TOOLS_AGENT_COMMS="dsh")
    _proc(tmp_path, comm="dsh")
    run(env, "register", "o/r", SHA)
    run(env, "tick")
    assert sent.exists() and _events(env)[-1]["event"] == "verdict", _events(env)[-1]
    # CONTROL: the default set does not hold `dsh`, so the same requester reads as gone.
    env["FORGE_TOOLS_AGENT_COMMS"] = ""
    sent.unlink()
    run(env, "register", "o/r", "b" * 40)
    run(env, "tick")
    assert not sent.exists() and "is gone" in _events(env)[-1]["why"], _events(env)[-1]
