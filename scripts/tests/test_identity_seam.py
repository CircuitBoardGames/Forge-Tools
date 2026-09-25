"""The identity seam: the core reads no harness variable.

The core takes the caller's session id and wake pid as FORGE_TOOLS_SESSION_ID and
FORGE_TOOLS_WAKE_PID; `adapters/claude-code` maps Claude Code's own names onto them; gate-watch's
state defaults to a harness-neutral XDG state dir instead of `~/.claude/`.
"""
import os
import re
import subprocess
from pathlib import Path

from test_gate_watch import SHA, _env, run

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "adapters/claude-code/hooks/map-identity.sh"
HARNESS = re.compile(r"CLAUDE_[A-Z_]*|CLAUDECODE|OMPCODE|DSH_SESSION_ID")


def test_core_scripts_read_no_harness_variable():
    """Comment lines may explain the adapter mapping; no other line may name a harness variable."""
    files = [p for p in (REPO / "scripts").rglob("*")
             if p.is_file() and "tests" not in p.relative_to(REPO / "scripts").parts
             and p.suffix != ".pyc"]
    assert REPO / "scripts/gate-watch.py" in files and REPO / "scripts/pr-queue.sh" in files  # scanned something
    hits = ["%s:%d: %s" % (p.relative_to(REPO), n, line.strip())
            for p in files
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1)
            if not line.lstrip().startswith("#") and HARNESS.search(line)]
    assert hits == [], "harness variable read in the core -- map it in adapters/, not here:\n" + "\n".join(hits)


def _hook_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "FORGE_TOOLS_"))}
    env.update(extra)
    return env


def test_adapter_hook_exports_the_two_quoted_names(tmp_path):
    envfile = tmp_path / "env"
    r = subprocess.run(["sh", str(HOOK)], capture_output=True, text=True,
                       env=_hook_env(CLAUDE_ENV_FILE=str(envfile), CLAUDE_CODE_SESSION_ID="s'1 $x", CLAUDE_PID="4242"))
    assert r.returncode == 0, r.stderr
    assert envfile.read_text() == ("export FORGE_TOOLS_SESSION_ID='s'\\''1 $x'\n"
                                   "export FORGE_TOOLS_WAKE_PID='4242'\n")
    # Round-trips through the shell that will source it.
    out = subprocess.run(["sh", "-c", '. "$1"; printf "%s|%s" "$FORGE_TOOLS_SESSION_ID" "$FORGE_TOOLS_WAKE_PID"',
                          "sh", str(envfile)], capture_output=True, text=True, env=_hook_env()).stdout
    assert out == "s'1 $x|4242"


def test_adapter_hook_without_env_file_writes_nothing(tmp_path):
    r = subprocess.run(["sh", str(HOOK)], capture_output=True, text=True, cwd=tmp_path,
                       env=_hook_env(CLAUDE_CODE_SESSION_ID="s1", CLAUDE_PID="4242"))
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_gate_watch_state_defaults_to_the_xdg_state_dir(tmp_path):
    env, _, _ = _env(tmp_path)
    override = Path(env.pop("GATE_WATCH_REGISTRY"))
    xdg = tmp_path / "xdg"                                 # does not exist yet: register creates it
    env.update(XDG_STATE_HOME=str(xdg), HOME=str(tmp_path / "home"))
    assert run(env, "register", "o/r", SHA).returncode == 0
    assert (xdg / "forge-tools/gate-watch.jsonl").is_file()
    assert not (tmp_path / "home/.claude").exists()

    del env["XDG_STATE_HOME"]                              # unset: ~/.local/state
    assert run(env, "register", "o/r", "b" * 40).returncode == 0
    assert (tmp_path / "home/.local/state/forge-tools/gate-watch.jsonl").is_file()

    env["GATE_WATCH_REGISTRY"] = str(override)             # the override still wins
    assert run(env, "register", "o/r", "c" * 40).returncode == 0
    assert "c" * 40 in override.read_text()
    assert "c" * 40 not in (tmp_path / "home/.local/state/forge-tools/gate-watch.jsonl").read_text()
