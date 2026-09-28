"""Tests for `scripts/worktree-create.sh` and `scripts/worktree-reap.sh`.

Built as PAIRS, for the reason `test_prune_landed_branches.py` gives: every failure mode here is
silent in the dangerous direction. A reaper that reaped nothing and a reaper that reaped everything
both look calm on a happy path, and a health check that never fails is indistinguishable from a
healthy worktree. So each guard gets one case where the subject MUST be reported and one where it
MUST NOT, differing only in the thing that guard inspects.

The dangerous direction is specific: the reaper's job is deletion, so its degraded modes are tested
too — a /proc scan that yields nothing must REFUSE, not conclude that every worktree is an orphan.

Isolation: each test builds its own git repo and its own fake `/proc` under `tmp_path`. No test
reads the real /proc, spawns an agent, or touches the live worktree registry.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CREATE = REPO / "scripts/worktree-create.sh"
REAP = REPO / "scripts/worktree-reap.sh"

# The include list the fixtures stand up. Kept small and explicit rather than parsed from the real
# `.worktreeinclude`: a test that reads the file under test's own data cannot fail when that data is
# wrong, which is the whole class of defect `.worktreeinclude` exists to make reviewable.
FIXTURE_INCLUDE = """\
link shared_deps
copy local_config.json
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        text=True, capture_output=True, check=True).stdout.strip()


def make_ref_tree(tmp_path: Path) -> Path:
    """A reference tree that looks enough like this repo for both scripts to act on it."""
    ref = tmp_path / "RefTree"
    ref.mkdir()
    git(ref, "init", "-q", "-b", "main")
    (ref / ".gitignore").write_text("shared_deps\nlocal_config.json\n")
    (ref / ".worktreeinclude").write_text(FIXTURE_INCLUDE)
    (ref / "f.txt").write_text("base\n")
    # A repo-root DIRECTORY whose name is a legal branch kind. `docs` is the real case: `docs/` is a
    # directory here and `docs/<slug>` branches exist in this repo's history, which is exactly
    # the collision.
    (ref / "docs").mkdir()
    (ref / "docs" / "d.md").write_text("d\n")
    git(ref, "add", ".gitignore", ".worktreeinclude", "f.txt", "docs/d.md")
    git(ref, "commit", "-qm", "base")
    # The machine-local state a worktree cannot inherit.
    (ref / "shared_deps").mkdir()
    (ref / "shared_deps" / "pkg").write_text("x\n")
    (ref / "local_config.json").write_text("{}\n")
    # `worktree-create.sh` cuts from `hub/main`, so the fixture needs that ref and a `hub` remote it
    # can fetch from without a network. A remote pointing at itself satisfies both.
    git(ref, "remote", "add", "hub", str(ref))
    git(ref, "fetch", "-q", "hub")
    return ref


def set_include(ref: Path, text: str, gitignore: str | None = None) -> None:
    """Change `.worktreeinclude` (and optionally `.gitignore`) the way the script now reads them.

    The creator reads BOTH from `hub/main`, not from the reference tree's checkout —
    that is the whole point of the change, so a fixture that only writes the working tree is testing
    a path the script no longer takes. Committing is what makes the edit visible; the script's own
    `git fetch hub` then refreshes `hub/main`, because the fixture's `hub` remote is the repo itself.
    """
    (ref / ".worktreeinclude").write_text(text)
    paths = [".worktreeinclude"]
    if gitignore is not None:
        (ref / ".gitignore").write_text(gitignore)
        paths.append(".gitignore")
    git(ref, "add", *paths)
    git(ref, "commit", "-qm", "fixture: change the include list")


def make_worktree(ref: Path, name: str, branch: str) -> Path:
    """A worktree set up the way `worktree-create.sh` sets one up, without invoking it.

    Used by the reaper tests, which are about ownership and dirtiness and must not fail for a
    reason belonging to the creator.
    """
    wt = ref.parent / name
    git(ref, "worktree", "add", "-q", str(wt), "-b", branch, "hub/main")
    (wt / "shared_deps").symlink_to(ref / "shared_deps")
    (wt / "local_config.json").write_text("{}\n")
    return wt


def make_proc(tmp_path: Path, agents: dict, name: str = "proc") -> Path:
    """A fake /proc. `agents` maps pid -> (comm, cwd); every entry gets `comm` and a `cwd` symlink.

    Filler non-agent pids are always present, because the reaper refuses a scan that yielded no
    `comm` at all — a fixture with only agents in it would exercise a different arm than production.
    """
    proc = tmp_path / name
    proc.mkdir(exist_ok=True)
    for pid, comm in (("1", "init"), ("2", "sshd"), ("3", "bash")):
        d = proc / pid
        d.mkdir(exist_ok=True)
        (d / "comm").write_text(comm + "\n")
        (d / "cwd").symlink_to(tmp_path)
    for pid, (comm, cwd) in agents.items():
        d = proc / str(pid)
        d.mkdir(exist_ok=True)
        (d / "comm").write_text(comm + "\n")
        (d / "cwd").symlink_to(cwd)
    return proc


def make_oracle(tmp_path: Path, merged, *, fail: bool = False) -> Path:
    """A stub for `prune-landed-branches-forgejo.sh --dry-run`, owning its own output.

    A real executable rather than a monkeypatch: the reaper shells out to it, and an exit-0 wrapper
    around an unknown file is not a stub of anything. The contract is the real hook's, verified by
    running that hook against live branches — `WOULD DELETE <branch>(<sha>)` for merged, anything
    else for not-merged, and a NON-ZERO exit when hub could not be asked.

    `merged="ALL"` is the permissive default used by the ownership and dirty-refusal tests, which
    predate the forge oracle and are not about the forge. It keeps them testing the thing they name.
    """
    o = tmp_path / f"oracle-{'fail' if fail else 'ok'}.sh"
    if fail:
        o.write_text("#!/bin/sh\necho '[stub] cannot reach hub' >&2\nexit 3\n")
    elif merged == "ALL":
        o.write_text("#!/bin/sh\nshift\nfor b in \"$@\"; do\n"
                     "  echo \"[stub] WOULD DELETE $b(deadbeef) — merged per hub\"\ndone\n")
    else:
        o.write_text(
            "#!/bin/sh\nshift\nfor b in \"$@\"; do\n"
            "  case \"$b\" in\n"
            + "".join(f'    "{m}") echo "[stub] WOULD DELETE $b(deadbeef) — merged per hub" ;;\n'
                      for m in merged)
            + "    *) echo \"[stub] KEEP        $b — no merged PR with this head branch\" ;;\n"
              "  esac\ndone\n")
    o.chmod(0o755)
    return o


def reap(ref: Path, proc: Path, *args: str, oracle: Path | None = None,
         env_extra: dict | None = None):
    """Run the reaper. `oracle` defaults to one that says every branch landed.

    That default is deliberate and is why the forge arms have their own tests: it keeps the
    ownership and dirty tests measuring ownership and dirtiness. The real oracle is a network call
    to hub, so no test drives it — the arms that matter (merged, not-merged, unreachable) are driven
    through the stub, and the real hook's own suite covers the query it wraps.
    """
    if oracle is None:
        oracle = make_oracle(proc.parent, "ALL")
    env = dict(os.environ, WORKTREE_REAP_PROC=str(proc), WORKTREE_REAP_ORACLE=str(oracle),
               FORGE_TOOLS_REMOTE="hub")  # the fixture's remote: reap judged against hub/main before it became configuration
    env.update(env_extra or {})
    return subprocess.run(["sh", str(REAP), *args], cwd=str(ref), text=True,
                          capture_output=True, env=env)


def create(ref: Path, *args: str, **extra: str):
    # The toolchain probe measures the BOX, not the worktree, and is advisory in the script. Off
    # here so a test never depends on whether this machine has run `npm ci`.
    env = dict(os.environ, WORKTREE_CHECK_TOOLCHAIN="0",
               FORGE_TOOLS_REMOTE="hub")  # the fixture's remote: create cut from hub/main before it became configuration
    env.update(extra)
    return subprocess.run(["sh", str(CREATE), *args], cwd=str(ref), text=True,
                          capture_output=True, env=env)


# ---------------------------------------------------------------------------------------------
# The reaper: ownership is cwd.

def test_orphan_is_reaped_and_occupied_worktree_is_not(tmp_path):
    """The pair. Same two worktrees, same command; only who is sitting in them differs.

    A reaper that reaped everything fails the `live` assertion; one that reaped nothing fails the
    `orphan` assertion. Neither can pass by accident.
    """
    ref = make_ref_tree(tmp_path)
    live = make_worktree(ref, "CC-live", "agent/live")
    orphan = make_worktree(ref, "CC-orphan", "agent/orphan")
    proc = make_proc(tmp_path, {4242: ("claude", live)})

    r = reap(ref, proc, "--delete")
    assert r.returncode == 0, r.stderr
    assert live.exists(), f"a worktree with a live agent in it was reaped:\n{r.stdout}"
    assert not orphan.exists(), f"an orphan was not reaped:\n{r.stdout}"
    assert "LIVE" in r.stdout and "REAPED" in r.stdout


def test_reaping_does_not_delete_the_branch(tmp_path):
    """`git worktree remove` leaves the branch ref, which is why the reaper never has to ask the
    forge whether the work landed. If this ever changes, the whole no-second-answer argument in the
    script header goes with it."""
    ref = make_ref_tree(tmp_path)
    make_worktree(ref, "CC-orphan", "agent/orphan")
    proc = make_proc(tmp_path, {})

    reap(ref, proc, "--delete")
    assert "agent/orphan" in git(ref, "branch", "--list", "agent/orphan")


def test_a_session_in_a_subdirectory_still_owns_its_worktree(tmp_path):
    """A custody guard's stated ceiling is that cwd must EQUAL the toplevel, and it invites widening.

    Here the widening is mandatory rather than nice: a session that `cd`s into `scripts/` would
    otherwise read as an orphan, with deletion on the other side of the answer.
    """
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-live", "agent/live")
    sub = wt / "deep" / "deeper"
    sub.mkdir(parents=True)
    proc = make_proc(tmp_path, {4242: ("claude", sub)})

    r = reap(ref, proc, "--delete")
    assert wt.exists(), f"a session in a subdirectory was treated as absent:\n{r.stdout}"


def test_a_sibling_sharing_a_name_prefix_does_not_confer_ownership(tmp_path):
    """`CC-live2` must not satisfy `CC-live`. A plain string prefix test passes this by accident in
    the wrong direction — it would mark the UNOCCUPIED `CC-live` as live and never reap it."""
    ref = make_ref_tree(tmp_path)
    bare = make_worktree(ref, "CC-live", "agent/live")
    sibling = make_worktree(ref, "CC-live2", "agent/live2")
    proc = make_proc(tmp_path, {4242: ("claude", sibling)})

    r = reap(ref, proc, "--delete")
    assert sibling.exists(), f"the occupied sibling was reaped:\n{r.stdout}"
    assert not bare.exists(), (
        "CC-live was spared because CC-live2 is occupied — the path comparison is not "
        f"boundary-aware:\n{r.stdout}")


def test_omp_counts_as_an_owner_not_only_claude(tmp_path):
    """The agent set comes from `agent_comms.AGENT_COMMS`, and omp is in it. A reaper that saw only
    `claude` would delete an omp session's worktree — the exact one-directional blindness a
    custody guard records being fixed in the custody guard."""
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-omp", "agent/omp-owned")
    proc = make_proc(tmp_path, {4242: ("omp", wt)})

    r = reap(ref, proc, "--delete")
    assert wt.exists(), f"an omp session's worktree was reaped:\n{r.stdout}"


def test_a_non_agent_process_does_not_confer_ownership(tmp_path):
    """The negative half of the pair above: `comm` must be an AGENT, not merely a process. A shell
    left sitting in a worktree is not an OWNER.

    RE-SCOPED. This used to assert the tree was REMOVED, on the argument that treating a
    shell as an owner makes the reaper inert. The measured case that overturned it was a full suite
    -- nine processes, no agent -- reading as an empty tree with deletion on the other side. So the
    shell is still not an owner (no LIVE line), and the tree is still not removed, because occupied
    is not empty. The two questions are answered separately and both are printed.
    """
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-shellonly", "agent/shellonly")
    proc = make_proc(tmp_path, {4242: ("bash", wt)})

    r = reap(ref, proc, "--delete")
    assert "LIVE" not in r.stdout, f"a plain `bash` was accepted as the owning agent:\n{r.stdout}"
    assert "OCCUPIED" in r.stdout and "bash:4242" in r.stdout, r.stdout
    assert wt.exists(), f"a tree with a live process in it was removed:\n{r.stdout}"


# ---------------------------------------------------------------------------------------------
# The reaper: occupancy is not ownership, and it is not emptiness either.

def test_a_suite_running_in_a_SUBDIRECTORY_of_a_landed_orphan_holds_it(tmp_path):
    """THE INJECTION from the ticket: the exact ORPHAN predicate (no agent, forge says landed,
    tree clean) plus one non-agent process cwd'd in a subdirectory, which is how a suite runs."""
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-suite", "agent/suite")
    sub = wt / "scripts" / "tests"
    sub.mkdir(parents=True)
    proc = make_proc(tmp_path, {4242: ("python3", sub)})

    r = reap(ref, proc, "--delete")
    assert r.returncode == 0, r.stderr
    assert wt.exists(), f"a tree with a suite running in it was removed:\n{r.stdout}"
    assert "OCCUPIED" in r.stdout and "python3:4242" in r.stdout, r.stdout
    assert "REAPED" not in r.stdout and "ORPHAN" not in r.stdout


def test_the_hold_clears_when_the_process_is_gone__control(tmp_path):
    """Kill it and the same tree is reaped: a refusal that never clears is 'never reap'."""
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-suite", "agent/suite")
    held = reap(ref, make_proc(tmp_path, {4242: ("python3", wt)}, name="proc-busy"), "--delete")
    assert wt.exists() and "OCCUPIED" in held.stdout, held.stdout

    freed = reap(ref, make_proc(tmp_path, {}, name="proc-quiet"), "--delete")
    assert not wt.exists(), f"an emptied tree was not reaped:\n{freed.stdout}"
    assert "REAPED" in freed.stdout


def test_a_process_in_a_SIBLING_tree_does_not_occupy_this_one__control(tmp_path):
    """Path-boundary prefix match, the same one ownership uses: `CC-x2` is not under `CC-x`."""
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-x", "agent/x")
    other = make_worktree(ref, "CC-x2", "agent/x2")
    proc = make_proc(tmp_path, {4242: ("python3", other)})

    r = reap(ref, proc, "--delete")
    assert not wt.exists(), f"a process in a sibling tree held this one:\n{r.stdout}"
    assert other.exists()


def test_the_reference_tree_is_never_a_candidate(tmp_path):
    """Under premise 10 nobody works in the reference tree, so "no agent has it as cwd" is its
    NORMAL state — which without this exclusion makes it the first thing reaped."""
    ref = make_ref_tree(tmp_path)
    proc = make_proc(tmp_path, {})

    r = reap(ref, proc, "--delete")
    assert ref.exists() and (ref / "f.txt").exists()
    assert str(ref) not in r.stdout.replace(f"reference tree {ref}", "")


# ---------------------------------------------------------------------------------------------
# The reaper: refusing dirty, and refusing when unsure.

def test_a_dirty_orphan_is_refused_and_reported(tmp_path):
    """The refusal is git's own, and `--force` is never passed."""
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-dirty", "agent/dirty")
    (wt / "unsaved.txt").write_text("work nobody has committed\n")
    proc = make_proc(tmp_path, {})

    r = reap(ref, proc, "--delete")
    assert wt.exists(), "a dirty orphan was removed"
    assert (wt / "unsaved.txt").read_text() == "work nobody has committed\n"
    assert "REFUSED" in r.stdout, r.stdout
    # It must SAY why, not merely decline: the reader has to know work is sitting there.
    assert "contains modified or untracked files" in r.stdout


def test_no_force_anywhere_in_the_reaper(tmp_path):
    """The one rule that is absolute. Asserted on the CODE, not on behaviour: a `--force` added on a
    path no test exercises would pass every test above while being the single change that turns this
    script into one that destroys uncommitted work.

    Asserted at the CALL SITE, not by searching the source for the string. Comment-stripping alone
    is not enough and the first version of this test proved it: the script's own report says
    "WOULD REFUSE (never --force)", so a substring search over non-comment lines fails on prose that
    exists precisely to promise the opposite. That is the vacuous-check shape
    to beware of — a check satisfied by the text describing the check. So: find every line that
    invokes `git worktree remove`, and assert none of them passes a force flag.
    """
    lines = [ln for ln in REAP.read_text().splitlines() if not ln.lstrip().startswith("#")]
    removals = [ln for ln in lines if "worktree remove" in ln]
    assert removals, "no `git worktree remove` call site found — this test is not looking at anything"
    for ln in removals:
        assert "--force" not in ln and " -f" not in ln, f"forced removal at: {ln.strip()}"


def test_a_broken_proc_scan_refuses_rather_than_reaping_everything(tmp_path):
    """The degraded mode that matters. An unreadable scan yields an empty agent list, and an empty
    agent list means "every worktree is an orphan" — with deletion on the other side.

    The control below is what makes this a real assertion: the SAME worktree, same command, healthy
    scan, IS reaped. So the refusal is about the broken scan and not about this worktree.
    """
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-orphan", "agent/orphan")
    empty = tmp_path / "emptyproc"
    empty.mkdir()

    r = reap(ref, empty, "--delete")
    assert r.returncode == 3, f"a broken scan did not fail closed (rc={r.returncode})"
    assert wt.exists(), "a broken /proc scan reaped a worktree"
    assert "broken scan" in r.stderr, r.stderr

    control = reap(ref, make_proc(tmp_path, {}), "--delete")
    assert control.returncode == 0
    assert not wt.exists(), (
        "the control did not reap either, so the refusal above proves nothing about the scan")


def test_prunable_metadata_is_reported_and_pruned(tmp_path):
    """A worktree whose DIRECTORY is gone leaves registry metadata behind. `git worktree list
    --porcelain` calls it `prunable` with a machine-readable reason, and `gc.worktreePruneExpire`
    defaults to three months — far too slow here, so it is pruned explicitly rather than waited for.

    Report mode must mention it and not act; `--delete` must clear it.
    """
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-gone", "agent/gone")
    for child in sorted(wt.rglob("*"), reverse=True):
        child.unlink() if not child.is_dir() or child.is_symlink() else child.rmdir()
    wt.rmdir()
    proc = make_proc(tmp_path, {})

    r = reap(ref, proc)
    assert "prunable" in r.stdout, r.stdout
    assert "CC-gone" in git(ref, "worktree", "list", "--porcelain"), "report mode pruned"

    reap(ref, proc, "--delete")
    assert "CC-gone" not in git(ref, "worktree", "list", "--porcelain")


def test_report_mode_removes_nothing(tmp_path):
    """The default. A tool whose default is deletion is one nobody runs to look."""
    ref = make_ref_tree(tmp_path)
    clean = make_worktree(ref, "CC-clean", "agent/clean")
    dirty = make_worktree(ref, "CC-dirty", "agent/dirty")
    (dirty / "unsaved.txt").write_text("x\n")
    proc = make_proc(tmp_path, {})

    r = reap(ref, proc)
    assert clean.exists() and dirty.exists()
    # Clean and dirty must read DIFFERENTLY in the report: they need different action from the
    # reader, and one "would reap" line for both hides the refusal until --delete is typed.
    assert "ORPHAN" in r.stdout and "DIRTY" in r.stdout


# ---------------------------------------------------------------------------------------------
# Creation, and the health check that must be able to fail.

def test_the_worktree_prefix_is_configuration_not_a_deployment_name(tmp_path):
    """`CC-` is one deployment's name for its hub checkout, and every consumer's worktrees wore it.
    FORGE_TOOLS_WORKTREE_PREFIX names it; the default stays `CC-` so existing trees keep matching."""
    ref = make_ref_tree(tmp_path)
    r = create(ref, "demo", FORGE_TOOLS_WORKTREE_PREFIX="wt-")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "wt-demo").is_dir() and not (tmp_path / "CC-demo").exists(), os.listdir(tmp_path)


def test_create_produces_a_worktree_that_passes_its_own_check(tmp_path):
    ref = make_ref_tree(tmp_path)
    r = create(ref, "demo")
    assert r.returncode == 0, r.stdout + r.stderr
    wt = tmp_path / "CC-demo"
    assert (wt / "shared_deps").is_symlink()
    assert (wt / "local_config.json").exists()
    assert create(ref, "--check", str(wt)).returncode == 0


def test_a_created_worktree_is_clean_so_the_reaper_can_ever_remove_it(tmp_path):
    """Not cosmetic. A worktree that starts with untracked symlinks is one `git worktree remove`
    refuses for ever, so "refuse and report dirty" would degrade into "refuse everything" and the
    reaper would never remove anything at all. It is a property of `.gitignore`, so it is asserted
    here rather than assumed."""
    ref = make_ref_tree(tmp_path)
    assert create(ref, "demo").returncode == 0
    wt = tmp_path / "CC-demo"
    assert git(wt, "status", "--porcelain") == "", (
        "a freshly created worktree is dirty; the reaper can never remove it")


@pytest.mark.parametrize("fault,expect", [
    ("missing-copy", "MISSING"),
    ("dangling-link", "DANGLING"),
    ("real-dir-where-link", "PRESENT-NOT-LINK"),
])
def test_the_health_check_reports_each_fault_and_clears_when_repaired(tmp_path, fault, expect):
    """Inject, see it reported, repair, see the report clear — one fault at a time, so a green is
    attributable to THIS arm and not to another one masking it.

    The dangling case is the one worth having: `ls -l` renders it normally and `git status` is
    empty, so nothing else on the box reports it.
    """
    ref = make_ref_tree(tmp_path)
    assert create(ref, "demo").returncode == 0
    wt = tmp_path / "CC-demo"
    assert create(ref, "--check", str(wt)).returncode == 0, "baseline is not clean"

    if fault == "missing-copy":
        (wt / "local_config.json").unlink()
        repair = lambda: (wt / "local_config.json").write_text("{}\n")
    elif fault == "dangling-link":
        (ref / "shared_deps").rename(ref / "shared_deps.moved")
        repair = lambda: (ref / "shared_deps.moved").rename(ref / "shared_deps")
    else:
        (wt / "shared_deps").unlink()
        (wt / "shared_deps").mkdir()
        def repair():
            (wt / "shared_deps").rmdir()
            (wt / "shared_deps").symlink_to(ref / "shared_deps")

    bad = create(ref, "--check", str(wt))
    assert bad.returncode == 1, f"{fault} was not reported:\n{bad.stdout}"
    assert expect in bad.stdout, f"{fault} reported, but not as {expect}:\n{bad.stdout}"

    repair()
    good = create(ref, "--check", str(wt))
    assert good.returncode == 0, f"{fault} repaired but still reported:\n{good.stdout}"


def test_an_existing_branch_is_adopted_rather_than_refused(tmp_path):
    """Making a worktree for a branch that already exists is ordinary — adopting one another session
    pushed, or re-entering a branch after a merge. The first version always passed `-b`, so this was
    impossible, and the refusal blamed the worktree registry for a condition unrelated to it."""
    ref = make_ref_tree(tmp_path)
    git(ref, "branch", "agent/already-exists")

    r = create(ref, "reuse", "agent/already-exists")
    assert r.returncode == 0, f"an existing branch was refused:\n{r.stdout}{r.stderr}"
    wt = tmp_path / "CC-reuse"
    assert git(wt, "branch", "--show-current") == "agent/already-exists"


def test_adopting_an_existing_branch_does_not_reset_it_to_main(tmp_path):
    """The hazard in that arm, and the reason it is a separate call rather than a conditional `-b`.

    Passing `hub/main` alongside an existing branch is a request to RESET it — the worktree would
    come up looking fine with the branch's commits silently discarded. So: the branch's own commit
    must still be HEAD, and its file must be present.
    """
    ref = make_ref_tree(tmp_path)
    git(ref, "branch", "agent/haswork")
    # Put a commit on the branch that main does not have.
    wt0 = ref.parent / "tmpwt"
    git(ref, "worktree", "add", "-q", str(wt0), "agent/haswork")
    (wt0 / "only-on-branch.txt").write_text("work\n")
    git(wt0, "add", "only-on-branch.txt")
    git(wt0, "commit", "-qm", "work that must survive")
    tip = git(wt0, "rev-parse", "HEAD")
    git(ref, "worktree", "remove", str(wt0))

    assert create(ref, "adopt", "agent/haswork").returncode == 0
    wt = tmp_path / "CC-adopt"
    assert git(wt, "rev-parse", "HEAD") == tip, "adopting the branch reset it"
    assert (wt / "only-on-branch.txt").exists(), "the branch's own commit was discarded"


def test_a_tracked_path_in_the_include_list_is_refused(tmp_path):
    """A tracked file copied in would shadow the checkout with a stale copy that `git status` does
    not report. Caught before the worktree exists, not after."""
    ref = make_ref_tree(tmp_path)
    set_include(ref, "copy f.txt\n")   # f.txt IS tracked

    r = create(ref, "demo")
    assert r.returncode != 0
    assert "does NOT ignore" in r.stderr + r.stdout
    assert not (tmp_path / "CC-demo").exists(), "the worktree was created despite the refusal"


def test_a_missing_include_source_fails_loudly(tmp_path):
    """"A missing copy must fail loudly, not produce a worktree that looks fine and behaves subtly
    wrong"."""
    ref = make_ref_tree(tmp_path)
    set_include(ref, "copy nonexistent.json\n", gitignore="nonexistent.json\n")

    r = create(ref, "demo")
    assert r.returncode != 0
    assert "does not exist in the reference tree" in r.stderr + r.stdout


def test_an_unspecified_setup_is_refused_rather_than_treated_as_empty(tmp_path):
    """An absent `.worktreeinclude` and an empty one produce the same silence otherwise, and the
    first is a broken checkout while the second is a deliberate choice.

    This is also the regression test for a real fail-open found on the first end-to-end run: the
    parser's `die` ran inside a command substitution, so it printed its refusal, exited the SUBSHELL,
    and the run carried on to create the worktree anyway.
    """
    ref = make_ref_tree(tmp_path)
    git(ref, "rm", "-q", ".worktreeinclude")
    git(ref, "commit", "-qm", "fixture: drop the include list")

    r = create(ref, "demo")
    assert r.returncode != 0, r.stdout
    out = r.stderr + r.stdout
    # The wording moved when the read became `git show hub/main:.worktreeinclude`, so an
    # absent list refuses with git's own "does not exist in 'hub/main'". Asserting the INTENT rather
    # than the old sentence: it must name the file, and it must not silently proceed on an empty list.
    assert ".worktreeinclude" in out, out
    assert "does not exist" in out or "empty" in out, out
    assert not (tmp_path / "CC-demo").exists(), "the refusal printed but the worktree was made anyway"


# ---------------------------------------------------------------------------------------------
# Branch naming.

@pytest.mark.parametrize("branch,ok", [
    ("agent/272-worktree-lifecycle", True),
    ("feat/a-b.c", True),
    ("wiki/notes", True),
    # A bare prefix may never be a branch name: git refs are paths, so `feat` and `feat/x` cannot
    # coexist in either creation order. Creating `feat` makes every `feat/<x>` uncreatable for ever.
    ("feat", False),
    ("agent/", False),
    ("Agent/X", False),
    # The known trap: a branch name that resolves as a path, which a checker reads as one.
    # `docs` is a directory at the fixture's repo root, and `docs/x` is otherwise perfectly legal —
    # so this case can only be rejected by the root-collision rule, not by the name regex.
    ("docs/x", False),
])
def test_branch_names_are_validated(tmp_path, branch, ok):
    ref = make_ref_tree(tmp_path)
    r = create(ref, "slug", branch)
    if ok:
        assert r.returncode == 0, f"{branch!r} was rejected:\n{r.stdout}{r.stderr}"
    else:
        assert r.returncode != 0, f"{branch!r} was accepted:\n{r.stdout}"
        assert not (tmp_path / "CC-slug").exists()


def test_the_repo_root_collision_rule_is_about_the_real_root(tmp_path):
    """The negative control for the case above: `docs/x` is rejected ONLY because `docs` exists at
    the repo root. Remove that directory and the identical branch name becomes legal — so the check
    is reading the real root, not matching a hardcoded list of forbidden words, and the rejection
    above is not the name regex passing under a different flag."""
    ref = make_ref_tree(tmp_path)
    git(ref, "rm", "-q", "-r", "docs")
    git(ref, "commit", "-qm", "drop docs")
    assert not (ref / "docs").exists(), "fixture precondition: `docs` must be gone from the root"
    r = create(ref, "slug", "docs/x")
    assert r.returncode == 0, f"docs/x rejected with no `docs` at the root:\n{r.stdout}{r.stderr}"


# ---------------------------------------------------------------------------------------------
# The shared-dependency seam.

def test_touching_a_manifest_while_node_modules_is_shared_is_reported(tmp_path):
    """Symlinked deps are correct until the branch changes what should be installed; then the shared
    tree answers for a dependency set this branch does not have, and the wrongness is silent — the
    install resolves, to the wrong versions."""
    ref = make_ref_tree(tmp_path)
    assert create(ref, "demo").returncode == 0
    wt = tmp_path / "CC-demo"
    (wt / "node_modules").symlink_to(ref / "shared_deps")

    before = create(ref, "--check", str(wt))
    assert before.returncode == 0, f"baseline already failing:\n{before.stdout}"

    (wt / "package.json").write_text("{}\n")
    git(wt, "add", "package.json")
    git(wt, "commit", "-qm", "touch manifest")

    r = create(ref, "--check", str(wt))
    assert r.returncode == 1, f"a manifest change on shared deps was not reported:\n{r.stdout}"
    assert "SHARED-DEPS-DIVERGED" in r.stdout


def test_a_vendored_manifest_does_not_trip_the_seam(tmp_path):
    """The negative half. Vendored trees under `.claude/vendor/` carry their own manifests and
    lockfiles and are not ours to install, so a change there says nothing about our node_modules.
    Without this, every vendor re-sync would demand a pointless local install."""
    ref = make_ref_tree(tmp_path)
    assert create(ref, "demo").returncode == 0
    wt = tmp_path / "CC-demo"
    (wt / "node_modules").symlink_to(ref / "shared_deps")

    vendored = wt / ".claude/vendor/thing"
    vendored.mkdir(parents=True)
    (vendored / "package.json").write_text("{}\n")
    git(wt, "add", "-f", ".claude/vendor/thing/package.json")
    git(wt, "commit", "-qm", "vendored manifest")

    r = create(ref, "--check", str(wt))
    assert r.returncode == 0, f"a vendored manifest tripped the seam:\n{r.stdout}"


# ---------------------------------------------------------------------------------------------
# the creator must not depend on the reference tree's CHECKOUT being current.
#
# The reference tree was made read-only and deliberately gave it no updater, so nothing advances
# it. Measured 2026-08-25: it sat 5 commits behind `hub/main`, predating `.worktreeinclude`, and the
# creator refused at the moment a session was starting work.
#
# Each test below rewinds the reference tree's WORKING TREE behind `hub/main` and asserts the
# creator reads the tip anyway. Each fails against the old code, which is what makes them
# proof rather than decoration — the site each one covers is named in its docstring.

def rewind_ref(ref: Path, make_old, make_new) -> None:
    """Leave `hub/main` at a NEW commit while the reference tree's checkout sits at an OLD one.

    `make_old` and `make_new` each mutate the tree; each is committed. The working tree is then
    detached onto the old commit, so the checkout and the tip genuinely disagree — which is the
    condition the fix is about and which no other test in this file creates.
    """
    def stage():
        # NEVER `git add -A`. The include SOURCES (`shared_deps`, `local_config.json`) are untracked
        # by design, and a moment when the fixture's own .gitignore does not cover them is exactly
        # what these tests construct — so `-A` commits them, and a TRACKED path is one
        # `git check-ignore` never reports as ignored, however well the rules match. That produced a
        # false failure on the ignore-rules test and it looked like a defect in the script.
        git(ref, "add", "-u")                                   # tracked edits + deletions
        for extra in (".gitignore", ".worktreeinclude", "docs", "marker.txt"):
            if (ref / extra).exists():
                git(ref, "add", extra)

    make_old()
    stage()
    git(ref, "commit", "-qm", "old state")
    old = git(ref, "rev-parse", "HEAD")
    make_new()
    stage()
    git(ref, "commit", "-qm", "new state")
    # The guard for the trap above: if a source ever becomes tracked, say so here rather than
    # letting it surface as an unexplained refusal from the script under test.
    for src in ("shared_deps", "local_config.json"):
        tracked = subprocess.run(["git", "-C", str(ref), "ls-files", "--error-unmatch", src],
                                 capture_output=True).returncode == 0
        assert not tracked, f"fixture leaked: {src} became tracked, so check-ignore will never match it"
    git(ref, "checkout", "-q", old)          # detach the CHECKOUT behind the tip
    git(ref, "fetch", "-q", "hub")
    assert git(ref, "rev-parse", "HEAD") != git(ref, "rev-parse", "hub/main"), (
        "fixture precondition: the checkout must actually be behind hub/main")


def test_stale_reference_tree_still_yields_a_correct_worktree(tmp_path):
    """Site 1 — the include list. This is the failure actually hit.

    `hub/main`'s list has an entry the stale checkout's does not. If the creator read the checkout,
    that entry would simply never be applied.
    """
    ref = make_ref_tree(tmp_path)
    rewind_ref(
        ref,
        lambda: (ref / ".worktreeinclude").write_text("link shared_deps\n"),
        lambda: (ref / ".worktreeinclude").write_text("link shared_deps\ncopy local_config.json\n"),
    )

    r = create(ref, "demo")
    assert r.returncode == 0, f"a stale reference tree broke creation:\n{r.stdout}{r.stderr}"
    wt = tmp_path / "CC-demo"
    assert (wt / "local_config.json").exists(), (
        "the entry that exists only on hub/main was not applied — the list came from the checkout")


def test_stale_reference_tree_does_not_silently_accept_a_colliding_branch(tmp_path):
    """Site 2 — the repo-root collision test, and THE ONE THAT FAILS IN THE ACCEPTING DIRECTION.

    `hub/main` has a `docs/` root entry; the stale checkout does not. Reading the checkout lets
    `docs/x` straight through with NO message at all, producing exactly the shape the check
    exists to prevent. The other two stale-tree sites refuse and at least say something; this one is
    silent, which is why it is the one worth leading with.
    """
    ref = make_ref_tree(tmp_path)
    rewind_ref(
        ref,
        lambda: git(ref, "rm", "-q", "-r", "docs"),
        lambda: ((ref / "docs").mkdir(), (ref / "docs" / "d.md").write_text("d\n")),
    )
    assert not (ref / "docs").exists(), "fixture: the stale checkout must not have docs/"

    r = create(ref, "slug", "docs/x")
    assert r.returncode != 0, (
        "a colliding branch name was ACCEPTED because the stale checkout lacks the directory "
        f"hub/main has — silently, which is the whole hazard:\n{r.stdout}")
    assert "repo-root entry" in r.stdout + r.stderr
    assert not (tmp_path / "CC-slug").exists()


def test_stale_reference_tree_ignore_rules_do_not_cause_a_false_refusal(tmp_path):
    """Site 3 — the ignore check. Wrong in the REFUSING direction on a stale tree.

    The no-trailing-slash rules that make these symlinks ignorable landed later, so a checkout
    behind that commit refuses every entry, blaming `.worktreeinclude` for a defect in the tree.
    Here `hub/main` ignores the path and the stale checkout does not.
    """
    ref = make_ref_tree(tmp_path)
    rewind_ref(
        ref,
        lambda: (ref / ".gitignore").write_text("local_config.json\n"),          # shared_deps NOT ignored
        lambda: (ref / ".gitignore").write_text("shared_deps\nlocal_config.json\n"),
    )

    r = create(ref, "demo")
    assert r.returncode == 0, (
        f"a stale .gitignore caused a false refusal:\n{r.stdout}{r.stderr}")
    assert (tmp_path / "CC-demo" / "shared_deps").is_symlink()


def test_creating_never_moves_the_reference_tree(tmp_path):
    """The ticket's other requirement: the creator must not silently advance or reset the tree that
    owns the object store. It does not touch `$REF`'s HEAD at all — asserted rather than assumed,
    because "fast-forward it for the user" is the obvious next change and this is what forbids it
    landing unnoticed."""
    ref = make_ref_tree(tmp_path)
    rewind_ref(
        ref,
        lambda: (ref / "marker.txt").write_text("old\n"),
        lambda: (ref / "marker.txt").write_text("new\n"),
    )
    before = git(ref, "rev-parse", "HEAD")

    assert create(ref, "demo").returncode == 0
    assert git(ref, "rev-parse", "HEAD") == before, "the creator moved the reference tree"
    assert (ref / "marker.txt").read_text() == "old\n", "the reference tree's checkout changed"


# ---------------------------------------------------------------------------------------------
# "no live process" is not "work finished".
#
# The reaper is per-box: run from any worktree it sees every other agent's tree on the machine.
# cwd-ownership answers *is a live process sitting here*, and reading that as *is this work done*
# makes a session that ENDED with unfinished work indistinguishable from one that is DONE.
#
# Measured 2026-08-25 from one session's tree: three peer worktrees came back ORPHAN / WOULD REMOVE,
# one of them (`agent/295-client-currency`) with an OPEN PR and 2 commits ahead of main.

def test_an_unowned_worktree_whose_work_did_not_land_is_held_not_reaped(tmp_path):
    """The hazard itself. Unowned, perfectly clean, and NOT finished.

    Clean is the dangerous case, not the safe one: the dirty-refusal covers uncommitted work, so the
    session that committed before it ended is the least protected. Worktrees were made clean at birth
    on purpose, which removed the accidental shield an untracked symlink used to provide.
    """
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-inflight", "agent/inflight")
    git(wt, "rm", "-q", "f.txt")
    git(wt, "commit", "-qm", "work that has not landed")
    assert git(wt, "status", "--porcelain") == "", "fixture: the tree must be CLEAN"
    proc = make_proc(tmp_path, {})                      # nobody home

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, []))   # hub: nothing merged
    assert wt.exists(), f"an in-flight peer worktree was reaped:\n{r.stdout}"
    assert "HELD" in r.stdout, r.stdout
    assert "not finished" in r.stdout


def test_a_landed_orphan_is_still_reaped(tmp_path):
    """The other half of the pair, and the one that stops the fix from being "disable the feature".

    Identical setup to the test above; only hub's answer differs.
    """
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-done", "agent/done")
    proc = make_proc(tmp_path, {})

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, ["agent/done"]))
    assert not wt.exists(), f"a genuinely finished orphan was not reaped:\n{r.stdout}"
    assert "REAPED" in r.stdout


def test_an_unreachable_forge_holds_everything(tmp_path):
    """Fail closed. "Could not ask" must never become "nothing is in flight".

    The control is the same worktree under a working oracle, so the hold is attributable to the
    forge being unreachable and not to anything about this worktree.
    """
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-done", "agent/done")
    proc = make_proc(tmp_path, {})

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, [], fail=True))
    assert wt.exists(), "a worktree was reaped while hub was unreachable"
    assert "HELD" in r.stdout and "could not be asked" in r.stdout

    # The control is this CALL, not its return value: the assertion below reads the
    # filesystem effect. Bound to a name until 2026-09-20, which read as a result
    # nobody checked.
    reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, ["agent/done"]))
    assert not wt.exists(), (
        "the control did not reap either, so the hold above says nothing about the forge arm")


def test_a_detached_worktree_already_in_main_is_reaped(tmp_path):
    """No branch means no question hub can answer -- but none is needed when the commit is
    already in `hub/main`: everything the checkout contains is on main by construction.

    This test previously asserted the opposite. The old reason ("refusing is the only honest arm")
    was too broad: it held EVERY detached tree for ever, and the merge flow detaches them routinely
    -- freeing a branch so it can be retired is what produces one. Measured 2026-08-27 on the live
    box: five of twelve worktrees were held, all five for detachment alone, and four of those five
    had HEADs already in main's history.
    """
    ref = make_ref_tree(tmp_path)
    wt = ref.parent / "CC-detached"
    git(ref, "worktree", "add", "-q", "--detach", str(wt), "hub/main")
    proc = make_proc(tmp_path, {})

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, "ALL"))
    assert not wt.exists(), f"a redundant detached worktree was held:\n{r.stdout}"


def test_a_detached_worktree_NOT_in_main_is_still_held(tmp_path):
    """The other arm of the pair, and the one that makes the first safe.

    A detached HEAD carrying a commit that exists nowhere else is the case the old blanket refusal
    was really protecting. Identical to the test above except for where HEAD sits, so the two
    together show the check discriminates rather than that it always answers one way.

    The message assertion moved and got STRONGER rather than looser. It used to accept
    `NOT in`, which was part of a sentence claiming the tree "holds commits that exist nowhere
    else" — a content claim this arm never measures. It now pins BOTH halves of the corrected
    verdict: that ancestry is what was evaluated, and that the discredited content claim is gone.
    In this fixture the commit genuinely IS unique, so the old wording was accidentally true here
    and the test could not have caught the defect; the measured case was a pre-merge copy.
    """
    ref = make_ref_tree(tmp_path)
    wt = ref.parent / "CC-detached-ahead"
    git(ref, "worktree", "add", "-q", "--detach", str(wt), "hub/main")
    (wt / "only-here.txt").write_text("a commit that is on no other ref\n")
    git(wt, "add", "-A")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "unlanded")
    proc = make_proc(tmp_path, {})

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, "ALL"))
    assert wt.exists(), f"a detached worktree holding unique commits was reaped:\n{r.stdout}"
    assert "not an ancestor of" in r.stdout, r.stdout
    assert "ANCESTRY, not content" in r.stdout, r.stdout
    assert "exist nowhere else" not in r.stdout, (
        "the verdict is claiming content-uniqueness again; it measures ancestry\n"
        + r.stdout)


@pytest.mark.parametrize("marker,word", [("BISECT_LOG", "bisect"), ("MERGE_HEAD", "merge"),
                                         ("CHERRY_PICK_HEAD", "cherry-pick")])
def test_an_in_progress_operation_holds_a_detached_worktree(tmp_path, marker, word):
    """The old test's stated reason survives the change, and this is where it now lives.

    "A detached worktree is what a session mid-rebase or mid-bisect leaves behind" is true, and a
    BISECT sits on a HISTORICAL commit -- which is in `hub/main` by definition and would otherwise
    read as maximally safe to delete. `git status --porcelain` does not show any of these states, so
    the dirty refusal that protects every other arm does not protect this one.
    """
    ref = make_ref_tree(tmp_path)
    wt = ref.parent / f"CC-midop-{word}"
    git(ref, "worktree", "add", "-q", "--detach", str(wt), "hub/main")
    gd = git(wt, "rev-parse", "--absolute-git-dir")
    (Path(gd) / marker).write_text("")
    assert not git(wt, "status", "--porcelain"), (
        "fixture invalid: the marker made the tree dirty, so this would pass for the wrong reason")
    proc = make_proc(tmp_path, {})

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, "ALL"))
    assert wt.exists(), f"a worktree mid-{word} was reaped:\n{r.stdout}"
    assert word in r.stdout and "IN PROGRESS" in r.stdout, r.stdout


def test_the_forge_is_asked_once_for_the_whole_set(tmp_path):
    """Not per worktree. A network call per tree turns a routine report into N calls and makes the
    reaper's cost scale with how many peers happen to be on the box."""
    ref = make_ref_tree(tmp_path)
    for n in ("a", "b", "c"):
        make_worktree(ref, f"CC-{n}", f"agent/{n}")
    counter = tmp_path / "calls"
    oracle = tmp_path / "counting-oracle.sh"
    oracle.write_text(
        "#!/bin/sh\necho x >> %s\nshift\nfor b in \"$@\"; do "
        "echo \"[stub] KEEP $b\"; done\n" % counter)
    oracle.chmod(0o755)

    reap(ref, make_proc(tmp_path, {}), oracle=oracle)
    assert counter.exists(), "the oracle was never called"
    assert counter.read_text().count("x") == 1, (
        f"the forge was asked {counter.read_text().count('x')} times for 3 worktrees")


# ---------------------------------------------------------------------------------------------
# A verdict may state only what its signal measured.
#
# Two measured defects are one defect on opposite arms. One HELD for ever what it should have reaped,
# claiming content-uniqueness from an ancestry test. The other REAPED what it should have held, claiming
# "no live agent" from a cwd test that is inert for any session launched in the reference tree.
# Both repairs are the same: narrow the verdict to the predicate that was actually evaluated, and
# do not let a destructive arm run on a signal that established nothing.

def test_a_landed_orphan_is_refused_while_an_agent_sits_in_the_reference_tree(tmp_path):
    """The measured case: three live sessions, all cwd the reference tree, live set empty.

    The forge arm passes — hub says the branch landed — so before this fix the tree was REAPED. But
    landing is not leaving: a session that lands its PR and keeps working is held by ownership
    alone, and ownership here matched nothing AND established nothing. The destructive arm must
    refuse. This is the exact shape that removed a live peer's worktree on 2026-08-28.
    """
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-orphan", "agent/orphan")
    # The session is real and running; its cwd is the reference tree, which is never a candidate.
    proc = make_proc(tmp_path, {4242: ("claude", ref)})

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, ["agent/orphan"]))
    assert r.returncode == 0, r.stderr
    assert wt.exists(), f"a tree was removed while ownership had established nothing:\n{r.stdout}"
    assert "REFUSED" in r.stdout, r.stdout
    assert "ESTABLISHED NOTHING" in r.stdout, r.stdout


def test_the_same_landed_orphan_is_still_reaped_on_a_quiet_box(tmp_path):
    """The control, and the reason the test above is not just "the reaper stopped working".

    Identical fixture except that no agent process exists at all. There an empty live set is a real
    answer rather than an inert one, so the refusal must NOT fire and the tree must go. Without
    this, a reaper that refused unconditionally would pass the test above.
    """
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-orphan", "agent/orphan")
    proc = make_proc(tmp_path, {})

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, ["agent/orphan"]))
    assert r.returncode == 0, r.stderr
    assert not wt.exists(), f"a genuine orphan on a quiet box was not reaped:\n{r.stdout}"
    assert "REAPED" in r.stdout, r.stdout
    assert "ESTABLISHED NOTHING" not in r.stdout, (
        "the unlocatable-agent hedge fired with no agent running\n" + r.stdout)


def test_an_agent_in_its_own_worktree_does_not_disarm_deletion_elsewhere(tmp_path):
    """The third discriminator: agents exist, and they are LOCATABLE.

    A session launched in its worktree (the pattern `worktree-create.sh` advises) is exactly what
    cwd-ownership was built for. Its presence says nothing is unlocatable, so a genuine orphan
    beside it must still be reaped — otherwise the fix would disable the reaper whenever any agent
    is running anywhere, which is almost always.
    """
    ref = make_ref_tree(tmp_path)
    live = make_worktree(ref, "CC-live", "agent/live")
    orphan = make_worktree(ref, "CC-orphan", "agent/orphan")
    proc = make_proc(tmp_path, {4242: ("claude", live)})

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, ["agent/orphan"]))
    assert live.exists(), r.stdout
    assert not orphan.exists(), f"a locatable agent disarmed deletion of an orphan:\n{r.stdout}"


def test_no_verdict_claims_a_tree_is_unowned(tmp_path):
    """The shared repair, asserted as one property rather than per-message.

    Every HELD/ORPHAN line used to open with the word `unowned`, which is the conclusion rather than
    the measurement — the test located no owner, which is a different statement whenever a session
    is unlocatable. Asserted across all four refusing arms at once so a new arm cannot reintroduce
    it quietly.
    """
    ref = make_ref_tree(tmp_path)
    make_worktree(ref, "CC-held", "agent/held")          # forge says not landed -> HELD
    det = ref.parent / "CC-detached-ahead"               # not an ancestor        -> HELD
    git(ref, "worktree", "add", "-q", "--detach", str(det), "hub/main")
    (det / "only-here.txt").write_text("unique\n")
    git(det, "add", "-A")
    git(det, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "unlanded")
    proc = make_proc(tmp_path, {4242: ("claude", ref)})

    r = reap(ref, proc, oracle=make_oracle(tmp_path, []))
    assert "HELD" in r.stdout, r.stdout          # the arms under test actually ran
    assert "— unowned," not in r.stdout, (
        "a verdict is asserting unownedness rather than reporting what it located\n" + r.stdout)
    assert "no owner was located" in r.stdout, r.stdout


def _agent_with_unreadable_cwd(proc: Path, pid: int, uid: int) -> None:
    """An agent whose cwd symlink is absent -- the branch a real EACCES takes -- running as `uid`."""
    d = proc / str(pid)
    d.mkdir()
    (d / "comm").write_text("claude\n")
    (d / "status").write_text(f"Name:\tclaude\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")


def test_an_unplaceable_agent_of_ANOTHER_uid_does_not_disarm_the_reaper(tmp_path):
    """dsh's SDK binary is comm `claude` at uid 992, and its cwd is unreadable to us by
    design, so the reaper refused every verdict for days. Another uid's process is not our session.
    """
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-orphan", "agent/orphan")
    proc = make_proc(tmp_path, {})
    _agent_with_unreadable_cwd(proc, 4242, os.getuid() + 1)

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, ["agent/orphan"]))
    assert not wt.exists(), f"a foreign-uid agent still disarmed the reaper:\n{r.stdout}{r.stderr}"


def test_an_unplaceable_agent_of_OUR_uid_still_refuses__control(tmp_path):
    """The same fixture at our own uid: the ownership ceiling, which the foreign-uid skip must not weaken."""
    ref = make_ref_tree(tmp_path)
    wt = make_worktree(ref, "CC-orphan", "agent/orphan")
    proc = make_proc(tmp_path, {})
    _agent_with_unreadable_cwd(proc, 4242, os.getuid())

    r = reap(ref, proc, "--delete", oracle=make_oracle(tmp_path, ["agent/orphan"]))
    assert wt.exists(), f"a same-uid agent we cannot place was ignored:\n{r.stdout}"
    assert "cwd is unreadable" in r.stdout + r.stderr, r.stdout + r.stderr
