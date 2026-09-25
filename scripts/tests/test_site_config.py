"""Site values are configuration.

Every site value is FORGE_TOOLS_<KEY>, from the environment or the config file (env wins), read by
scripts/ft-config.sh and scripts/ft_config.py. The forge URL and the owner have NO default: a
command that needs one refuses by name rather than guessing somebody else's forge.
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def test_core_code_names_one_public_site_only():
    """The whole-repo scan (test_public_standard.py) forbids every private mark; this pins the one
    site it allows, the fallow action's public source, to exactly one place: the template default."""
    from test_public_standard import ALLOW
    files = subprocess.run(["git", "-C", str(REPO), "ls-files", "scripts"], capture_output=True,
                           text=True, check=True).stdout.split()
    files = [f for f in files if not f.startswith("scripts/tests/")]
    assert len(files) > 10, "the scan found almost nothing to scan: %r" % files
    public = [(f, n) for f in files for n, line in enumerate((REPO / f).read_text(errors="replace").splitlines(), 1)
              if not line.lstrip().startswith("#") and ALLOW[0].search(line)]
    assert [f for f, _ in public] == ["scripts/templates/fallow.yml"], public


def _clean_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FORGE_TOOLS_", "HUB_", "HANDOFF_", "PR_QUEUE_", "DOCTOR_"))}
    env.update(HOME=str(tmp_path), FORGE_TOOLS_CONFIG=str(tmp_path / "no-config"))
    env.update(extra)
    return env


def test_a_missing_forge_url_refuses_naming_the_key(tmp_path):
    cfg = tmp_path / "hub-api.conf"
    cfg.write_text('header = "Authorization: token %s"\n' % ("0" * 40))
    cfg.chmod(0o600)
    r = subprocess.run(["sh", str(SCRIPTS / "hub-api.sh"), "/api/v1/version"], capture_output=True, text=True,
                       env=_clean_env(tmp_path, HUB_API_CONFIG=str(cfg)), cwd=str(tmp_path), timeout=60)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FORGE_TOOLS_FORGE_URL is not set" in r.stderr and str(tmp_path / "no-config") in r.stderr, r.stderr


def test_a_missing_owner_refuses_naming_the_key(tmp_path):
    repo = tmp_path / "consumer"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://forge.invalid/someone/thing.git"],
                   check=True)
    r = subprocess.run(["sh", str(SCRIPTS / "handoff.sh"), "show"], capture_output=True, text=True,
                       env=_clean_env(tmp_path), cwd=str(repo), timeout=60)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FORGE_TOOLS_OWNER is not set" in r.stderr, r.stderr
    # CONTROL: the owner set, the repo name comes off the remote and the same call goes on to the forge.
    r = subprocess.run(["sh", str(SCRIPTS / "handoff.sh"), "show"], capture_output=True, text=True,
                       env=_clean_env(tmp_path, FORGE_TOOLS_OWNER="acme", HUB_URL="http://127.0.0.1:9",
                                      HUB_API_CONFIG=str(tmp_path / "absent.conf")),
                       cwd=str(repo), timeout=60)
    assert "FORGE_TOOLS_OWNER" not in r.stderr and "acme/thing" in r.stderr, r.stderr


def _read(tmp_path, key, lang, **env):
    if lang == "sh":
        cmd = ["sh", "-c", '. "$1"; eval "printf %%s \\"\\$FORGE_TOOLS_%s\\""' % key, "sh",
               str(SCRIPTS / "ft-config.sh")]
    else:
        cmd = ["python3", "-c", "import sys; sys.path.insert(0, %r); import ft_config; print(ft_config.get(%r), end='')"
               % (str(SCRIPTS), key)]
    return subprocess.run(cmd, capture_output=True, text=True, env=_clean_env(tmp_path, **env), timeout=60)


def test_the_environment_beats_the_config_file_in_both_readers(tmp_path):
    conf = tmp_path / "config"
    conf.write_text("# a comment\nFORGE_TOOLS_REMOTE=fromfile\nFORGE_TOOLS_OWNER='quoted org'\nPATH=/nope\n")
    for lang in ("sh", "py"):
        # CONTROL first: the file IS read, so the next assertion is about precedence, not absence.
        r = _read(tmp_path, "REMOTE", lang, FORGE_TOOLS_CONFIG=str(conf))
        assert r.stdout == "fromfile", (lang, r.stdout, r.stderr)
        assert _read(tmp_path, "OWNER", lang, FORGE_TOOLS_CONFIG=str(conf)).stdout == "quoted org", lang
        assert "skipping 'PATH=/nope'" in r.stderr, (lang, r.stderr)
        r = _read(tmp_path, "REMOTE", lang, FORGE_TOOLS_CONFIG=str(conf), FORGE_TOOLS_REMOTE="fromenv")
        assert r.stdout == "fromenv", (lang, r.stdout, r.stderr)
        # And the neutral default when neither says.
        assert _read(tmp_path, "REMOTE", lang).stdout == "origin", lang
