"""bootstrap-target-repo.sh places the gate where the hub forge reads it, and provisions.

The forge reads `.forgejo/workflows` INSTEAD of `.github/workflows` when it exists, so a template
placed under `.github` on a fork carrying upstream workflows was skipped or ignored.

SAFETY: every run points HUB_API_CONFIG at nothing and HUB_URL at a closed port, so a script that
ignores the HUB_API stub (the unfixed one does) reaches the REAL client and dies there rather than
writing to the forge.
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/bootstrap-target-repo.sh"


def _target(tmp_path):
    t = tmp_path / "target"
    t.mkdir()
    subprocess.run(["git", "init", "-q", str(t)], check=True)
    return t


def _run(tmp_path, *args, stub=None):
    env = dict(os.environ, HUB_API_CONFIG=str(tmp_path / "no-such.conf"), HUB_URL="http://127.0.0.1:9")
    if stub:
        env["HUB_API"] = str(stub)
    return subprocess.run(["bash", str(SCRIPT), *map(str, args)], capture_output=True, text=True, env=env, timeout=60)


def test_the_gate_lands_in_forgejo_workflows(tmp_path):
    t = _target(tmp_path)
    r = _run(tmp_path, t)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (t / ".forgejo/workflows/test.yml").read_text() == (REPO / "scripts/templates/test.yml").read_text()
    assert (t / ".forgejo/workflows/fallow.yml").exists()
    assert not (t / ".github").exists(), "nothing belongs under .github on this forge"


def test_forge_repo_provisions_rather_than_only_protecting(tmp_path):
    t = _target(tmp_path)
    log = tmp_path / "calls"
    stub = tmp_path / "hub-api.sh"
    stub.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n')
    stub.chmod(0o755)
    r = _run(tmp_path, t, "--forge-repo", "o/name", stub=stub)
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.read_text().splitlines() == ["repo provision o/name --kind node"]


def test_an_existing_gate_file_is_never_overwritten__control(tmp_path):
    t = _target(tmp_path)
    (t / ".forgejo/workflows").mkdir(parents=True)
    (t / ".forgejo/workflows/test.yml").write_text("mine\n")
    r = _run(tmp_path, t)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (t / ".forgejo/workflows/test.yml").read_text() == "mine\n"


FALLOW_DEFAULT = "https://github.com/CircuitBoardGames/Fallow-Forgejo@416775ca7c67af339a01860bb32e55184fd58007"
MIRROR = "http://127.0.0.1:3300/example/fallow@416775ca7c67af339a01860bb32e55184fd58007"


def _uses(t):
    return [l.split("uses:", 1)[1].split("#")[0].strip()
            for l in (t / ".forgejo/workflows/fallow.yml").read_text().splitlines()
            if l.lstrip().startswith("- uses:") and "allow" in l]


def test_FORGE_TOOLS_FALLOW_ACTION_replaces_the_public_fallow_ref(tmp_path, monkeypatch):
    """the template defaults to the action's PUBLIC source; a deployment with a local
    mirror names it in FORGE_TOOLS_FALLOW_ACTION and the placed file uses that instead."""
    monkeypatch.setenv("FORGE_TOOLS_CONFIG", str(tmp_path / "no-config"))
    t = _target(tmp_path)
    monkeypatch.delenv("FORGE_TOOLS_FALLOW_ACTION", raising=False)
    r = _run(tmp_path, t)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _uses(t) == [FALLOW_DEFAULT], "CONTROL: unset, the template's public default is placed"
    t2 = tmp_path / "second"
    t2.mkdir()
    subprocess.run(["git", "init", "-q", str(t2)], check=True)
    monkeypatch.setenv("FORGE_TOOLS_FALLOW_ACTION", MIRROR)
    r = _run(tmp_path, t2)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _uses(t2) == [MIRROR], (t2 / ".forgejo/workflows/fallow.yml").read_text()
    # The template itself is never edited: the substitution is into the placed copy only.
    assert FALLOW_DEFAULT in (REPO / "scripts/templates/fallow.yml").read_text()
