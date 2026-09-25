"""`hub-api.sh` refuses to WRITE when the running client is a former version of itself.

THE INCIDENT. On 2026-08-24 five PRs were merged with a copy of `hub-api.sh` that predated the
body fix. `Do=squash` with only `MergeTitleField` squashes with an empty body, so every
merge silently discarded the PR's reasoning, its `Closes #N` and its `Co-Authored-By:` trailers.
Nothing failed: HTTP 200, branches pruned, tickets auto-closed. `main` refuses force push, so those
commits are wrong permanently. A sixth PR, merged from a fresh worktree, kept its 828-byte body — same
client name, same call shape, only the blob differed.

WHY THESE TESTS BUILD A REAL REPOSITORY. The guard's whole subject is git object identity: which
blob is running, and whether that blob is a former version of its own path on a ref. A stubbed git
would test the stub. So each test constructs a repository, commits two versions of the script, and
puts the older one back on disk — which is the incident, reproduced in miniature.

`HUB_API_CURRENCY_REF` names the ref to compare against and suppresses the fetch. It is a redirect,
not a skip: these tests exercise the real comparison against a ref they built, rather than the
network. There is deliberately no "skip the check" variable to test around.

THE REGRESSION THESE PIN. The first implementation ran its history walk with `git -C <script dir>`,
and a git pathspec resolves against the process CWD rather than the repo root — so
`git -C scripts/ rev-list -- scripts/hub-api.sh` hunted `scripts/scripts/hub-api.sh`, matched
nothing, and reported a genuinely STALE client as `unknown`, proceeding. It failed in the accepting
direction, which produces no message at all. Reading the code did not catch it; running a real
former version did. Every test here runs the script from `scripts/`, so that path stays covered.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

HUB_API = Path(__file__).resolve().parents[1] / "hub-api.sh"


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr}"
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> dict:
    """A repo whose `scripts/hub-api.sh` has two committed versions.

    Returns the blobs so a test can choose which one is on disk: `old` is a former version of the
    path on `fakemain`, `new` is its tip.
    """
    root = tmp_path / "clone"
    (root / "scripts").mkdir(parents=True)
    git(tmp_path, "init", "-q", str(root))
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")

    dest = root / "scripts" / "hub-api.sh"
    dest.write_bytes(HUB_API.read_bytes())
    dest.chmod(0o755)
    # The client sources its config reader from beside itself; an install carries both.
    (root / "scripts" / "ft-config.sh").write_bytes((HUB_API.parent / "ft-config.sh").read_bytes())
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "v1")
    old = git(root, "hash-object", "scripts/hub-api.sh")

    with dest.open("a") as fh:
        fh.write("\n# a later change\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "v2")
    new = git(root, "hash-object", "scripts/hub-api.sh")
    git(root, "branch", "-f", "fakemain", "HEAD")

    return {"root": root, "script": dest, "old": old, "new": new}


def put(repo: dict, blob: str) -> None:
    """Place a specific committed version on disk as the runnable script."""
    content = subprocess.run(["git", "-C", str(repo["root"]), "cat-file", "-p", blob],
                             capture_output=True, timeout=60)
    repo["script"].write_bytes(content.stdout)
    repo["script"].chmod(0o755)
    assert git(repo["root"], "hash-object", "scripts/hub-api.sh") == blob


# WHERE A WRITE THAT PASSES THE GUARD ACTUALLY GOES. This is the load-bearing part of the
# harness, not boilerplate.
#
# `hub-api.sh` takes `HUB_URL` from FORGE_TOOLS_FORGE_URL -- which a deployment's config file sets to
# its REAL forge -- and `CFG` from `$FORGE_TOOLS_CREDENTIALS_DIR/hub-api.conf`, which holds a live token. So a test that
# merely omits those variables does not isolate anything — it falls back to the production forge
# with a working credential. Measured the hard way while building this guard: an early run of
# `issue tag` reached the real hub and added a label to a real issue, which had to be reversed.
#
# Both are therefore OVERRIDDEN, never unset: a config the script will read but that carries a
# fake token, and a URL pointing at a closed port. A write that gets past the guard fails at the
# connection, and the live token is never even opened.
SINK_URL = "http://127.0.0.1:1"          # RFC-reserved port, refuses instantly


def isolated_env(tmp_path: Path, ref: str) -> dict:
    cfg = tmp_path / "fake-hub-api.conf"
    cfg.write_text('header = "Authorization: token 0000000000000000000000000000000000000000"\n')
    cfg.chmod(0o600)                      # the client refuses a config that is not 600
    return dict(os.environ,
                FORGE_TOOLS_CONFIG=str(tmp_path / "no-forge-tools-config"),  # no site config file leaks in
                HUB_API_CURRENCY_REF=ref,
                HUB_API_CONFIG=str(cfg),
                HUB_URL=SINK_URL)


def run(repo: dict, *args: str, ref: str = "fakemain") -> subprocess.CompletedProcess:
    """Invoke the on-disk client against the sink. Assertions are about the GUARD; anything that
    gets past it dies at the closed port, which is itself asserted below."""
    env = isolated_env(repo["root"].parent, ref)
    return subprocess.run([str(repo["script"]), *args], capture_output=True, text=True,
                          timeout=120, env=env, cwd=str(repo["root"]))


# --- the incident: a former version, writing -----------------------------------------------

def test_a_stale_client_refuses_to_write(repo):
    put(repo, repo["old"])
    r = run(repo, "issue", "tag", "owner/repo", "1", "somelabel")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "STALE" in r.stderr
    # Both blobs, because a refusal naming only one leaves the reader to go and look up the other.
    assert repo["old"] in r.stderr and repo["new"] in r.stderr


def test_the_stale_refusal_happens_before_any_forge_call(repo):
    """It must refuse rather than write-and-report. No hub config exists here, so if the guard let
    it through the failure would name the config or the network instead of the client."""
    put(repo, repo["old"])
    r = run(repo, "issue", "tag", "owner/repo", "1", "somelabel")
    assert "STALE" in r.stderr
    for wrong in ("hub-api.conf", "Could not resolve host", "curl"):
        assert wrong not in r.stderr, f"reached the forge path: {r.stderr}"


@pytest.mark.parametrize("verb", [
    ("issue", "note", "owner/repo", "1", "x"),
    ("issue", "close", "owner/repo", "1"),
    ("issue", "label", "owner/repo", "1", "task"),
    ("pr", "merge", "owner/repo", "1", "subject"),
    ("repo", "provision", "owner/repo", "--kind", "python"),
    ("repo", "create", "owner/repo", "--kind", "python"),
    ("repo", "fork", "https://example.invalid/x.git", "owner/repo", "--kind", "rust"),
])
def test_every_write_verb_is_guarded(repo, verb):
    put(repo, repo["old"])
    r = run(repo, *verb)
    assert "STALE" in r.stderr, f"{verb} was not guarded: {r.stdout + r.stderr}"


# --- reads are deliberately NOT guarded ----------------------------------------------------

@pytest.mark.parametrize("verb", [
    ("issue", "zoom", "owner/repo", "1"),
    ("issue", "list", "owner/repo"),
    ("pr", "checks", "owner/repo", "a" * 40),
])
def test_reads_are_not_refused_even_when_stale(repo, verb):
    """A stale reader returns stale-shaped data the caller can see; a stale writer changes the
    forge permanently and nothing shows. Guarding reads would add refusals with no incident
    behind them, and a guard that fires when it need not is the one that gets worked around."""
    put(repo, repo["old"])
    r = run(repo, *verb)
    assert "STALE" not in r.stderr, r.stderr


# --- the other two states ------------------------------------------------------------------

def test_a_current_client_writes_without_comment(repo):
    put(repo, repo["new"])
    r = run(repo, "issue", "tag", "owner/repo", "1", "somelabel")
    assert "STALE" not in r.stderr
    assert "UNRELEASED" not in r.stderr, "a current client must be silent, not merely allowed"


def test_an_unreleased_client_proceeds_but_says_so(repo):
    """Local edits or a feature branch. Refusing here would make the client unable to ship its own
    fix — a guard that must be disabled to be useful is the guard that gets disabled."""
    repo["script"].write_bytes(HUB_API.read_bytes() + b"\n# uncommitted local edit\n")
    repo["script"].chmod(0o755)
    r = run(repo, "issue", "tag", "owner/repo", "1", "somelabel")
    assert "STALE" not in r.stderr
    assert "UNRELEASED" in r.stderr


# --- cannot-tell refuses, rather than reporting an answer it never established --------------

def test_an_unresolvable_ref_proceeds_with_a_notice(repo):
    """CORRECTED BY CI, and the correction is the interesting part.

    This originally refused, by analogy with `pr-queue.sh`. The analogy was wrong: that script runs
    interactively on one box, while this client runs everywhere — including CI, whose checkout lives
    under `/var/lib/forgejo-runner/.cache/act/...` and has NO `hub/main` ref. Refusing there turned
    35 existing tests red, each reporting `cannot tell whether this client is current`, in an
    environment that structurally cannot answer. A guard that fails closed everywhere it cannot ask
    gets deleted, and protects nobody. Only a POSITIVE staleness match refuses now."""
    put(repo, repo["new"])
    r = run(repo, "issue", "tag", "owner/repo", "1", "somelabel", ref="no/such/ref")
    assert "STALE" not in r.stderr
    assert "cannot tell" in r.stderr, "it must say it could not ask, not go silent"


def test_an_untracked_client_proceeds_with_a_notice(repo, tmp_path):
    """A copy outside its checkout's index has no version to compare against. Same reasoning as
    above: it cannot be evaluated, so it is reported rather than refused."""
    loose = repo["root"] / "scripts" / "hub-api-copy.sh"
    loose.write_bytes(HUB_API.read_bytes())
    loose.chmod(0o755)
    env = dict(os.environ, HUB_API_CURRENCY_REF="fakemain")
    r = subprocess.run([str(loose), "issue", "tag", "owner/repo", "1", "x"],
                       capture_output=True, text=True, timeout=120, env=env,
                       cwd=str(repo["root"]))
    assert "STALE" not in r.stderr
    assert "not tracked" in r.stderr


def test_a_client_outside_any_checkout_proceeds_with_a_notice(repo, tmp_path):
    outside = tmp_path / "loose"
    outside.mkdir()
    copy = outside / "hub-api.sh"
    copy.write_bytes(HUB_API.read_bytes())
    copy.chmod(0o755)
    (outside / "ft-config.sh").write_bytes((HUB_API.parent / "ft-config.sh").read_bytes())
    env = dict(os.environ, HUB_API_CURRENCY_REF="fakemain")
    r = subprocess.run([str(copy), "issue", "tag", "owner/repo", "1", "x"],
                       capture_output=True, text=True, timeout=120, env=env, cwd=str(outside))
    assert "STALE" not in r.stderr
    assert "not inside a git checkout" in r.stderr


# --- the generic passthrough is classified by METHOD, since it has no verb name -------------

def test_passthrough_is_guarded_by_its_http_method(repo):
    put(repo, repo["old"])
    write = run(repo, "/api/v1/repos/o/r/issues/1/comments", "-X", "POST")
    read = run(repo, "/api/v1/repos/o/r/issues/1")
    assert "STALE" in write.stderr, write.stderr
    assert "STALE" not in read.stderr, read.stderr


# --- prove the isolation isolates ----------------------------------------------------------

def test_the_harness_cannot_reach_the_real_forge(repo):
    """PROOF, not assertion. A CURRENT client is allowed through the guard by design, so this is
    the arm that would hit production if the harness leaked. Its failure must name the sink."""
    put(repo, repo["new"])
    r = run(repo, "issue", "tag", "owner/repo", "1", "somelabel")
    blob = r.stdout + r.stderr
    assert "127.0.0.1" in blob, f"a write that passed the guard did not go to the sink: {blob}"
    hosts = set(re.findall(r"connect to (\S+) port", blob))   # curl names every host it tried
    assert hosts == {"127.0.0.1"}, f"the harness tried a host other than the sink: {blob}"
    real = _site_forge_host()
    assert not real or real == "127.0.0.1" or real not in blob, f"the harness reached the real forge {real}: {blob}"


def _site_forge_host():
    """The host a leaking harness would reach: the forge the site's own config names, or None where
    none is configured (a CI runner). Read in a child, because importing ft_config here would copy
    the site's keys into this process's environment -- and from there into every test's."""
    r = subprocess.run([sys.executable, "-c", "import ft_config; print(ft_config.get('FORGE_URL') or '')"],
                       cwd=str(HUB_API.parent), capture_output=True, text=True, timeout=60)
    return urllib.parse.urlsplit(r.stdout.strip()).hostname


def test_the_client_defaults_to_the_real_hub_which_is_why_the_override_matters():
    """The positive control for the test above: without an override the default IS production.
    If this ever stops being true the isolation above is protecting against nothing, and a
    harness that isolates nothing looks exactly like one that works.

    Production is not a literal in the client: it is the site's config, which names the
    real forge (FORGE_TOOLS_FORGE_URL) and the live credential's directory. So what is pinned is
    that the URL and the credential FALL THROUGH to that config when the overrides are absent."""
    src = HUB_API.read_text()
    assert 'HUB_URL="${HUB_URL:-${FORGE_TOOLS_FORGE_URL:-}}"' in src
    assert 'CFG="${HUB_API_CONFIG:-$FORGE_TOOLS_CREDENTIALS_DIR/hub-api.conf}"' in src


def test_the_real_credential_is_never_read(repo):
    """The fake config is what the client opens. Asserted on the token it would send: the sink
    refuses the connection, but the config path is chosen before that."""
    put(repo, repo["new"])
    r = run(repo, "issue", "tag", "owner/repo", "1", "somelabel")
    for real in (Path.home() / ".config" / "forge-tools" / "hub-api.conf",
                 Path(os.environ.get("FORGE_TOOLS_CREDENTIALS_DIR", "/nonexistent")) / "hub-api.conf"):
        assert str(real) not in (r.stdout + r.stderr)


def test_a_checkout_without_the_ref_does_not_break_ordinary_use(repo):
    """The CI regression, pinned. A repository with no `hub/main` — which is every CI checkout and
    every fresh clone — must not have its write verbs refused. 35 tests went red on one change for
    exactly this; without this test the next person re-introduces it."""
    put(repo, repo["new"])
    git(repo["root"], "branch", "-D", "fakemain")
    r = run(repo, "issue", "tag", "owner/repo", "1", "somelabel")
    assert "STALE" not in r.stderr
    assert "cannot tell" in r.stderr
