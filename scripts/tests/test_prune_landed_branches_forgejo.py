"""Unit tests for the Forgejo prune hook (`scripts/prune-landed-branches-forgejo.sh`).

Same doctrine as `test_prune_landed_branches.py`, against a different forge: every failure mode of
this hook is silent, because "deleted nothing" is also what a hook that never fired, never parsed its
input, or never reached hub produces. So the tests are PAIRS -- for each guard, one case where the
branch MUST die and one where it MUST survive, differing only in the thing that guard inspects. A
hook that deleted nothing would fail every `must die` case; one that deleted everything would fail
every `must survive` case.

Three of the pairs exist for Forgejo specifically, and each was measured against the real hub on
2026-08-12 before being fixtured here:

  * `?state=merged` is NOT a filter -- Forgejo falls back to `all` on an unrecognised state, so it
    returns closed-unmerged and OPEN pull requests too. `test_a_closed_but_unmerged_pr_...` and
    `test_the_request_asks_for_closed_not_merged` are the two halves of that.
  * `head.ref` becomes `refs/pull/<N>/head` once the head branch is deleted, and the branch name
    survives only in `head.label` -- so a hook keyed on `ref` prunes nothing for exactly the PRs
    that landed. `test_a_merged_pr_whose_head_ref_is_a_pull_ref_...` pins the fallback.
  * a response missing the `merged` field is a schema change, not "nothing landed".

Isolation: each test builds its own git repo under `tmp_path` with a `hub` remote, and points the
hook at a stub `hub-api.sh` that is a real executable script owning its own output and recording the
path it was asked for.
"""
import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "scripts/prune-landed-branches-forgejo.sh"
MERGE_CMD = "scripts/hub-api.sh pr merge acme/app 42 'title (#42)'"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


# The remote name the pruner defaulted to before it became configuration.
_WAS_DEFAULT = {"FORGE_TOOLS_REMOTE": "hub"}


def make_repo(tmp_path: Path, branches: list[str], *, hub_remote: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("base\n")
    git(repo, "add", "f.txt")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    if hub_remote:
        git(repo, "remote", "add", "hub",
            "http://192.0.2.10:3000/acme/app.git")
    for b in branches:
        git(repo, "branch", b)
    return repo


def pr(number: int, *, merged: bool, ref: str | None = None, label: str = "") -> dict:
    """One pull request in the shape hub actually returns.

    `ref` defaults to `refs/pull/<N>/head`, which is what Forgejo reports once the head branch is
    gone -- i.e. the shape every just-merged PR has, and the one a naive port reads as no match."""
    return {
        "number": number,
        "state": "closed",
        "merged": merged,
        "merged_at": "2026-08-12T21:50:26Z" if merged else None,
        "head": {"ref": f"refs/pull/{number}/head" if ref is None else ref, "label": label},
    }


def make_hub_api(tmp_path: Path, payload, *, fail: str | None = None) -> tuple[Path, Path]:
    """A stub hub-api.sh that IS a script -- an exit-0 wrapper around an unknown file proves nothing.

    Returns (script, request_log). The log records the argv it was called with, so a test can assert
    on the request rather than only on the outcome."""
    d = tmp_path / "stub"
    d.mkdir(exist_ok=True)
    script, log = d / "hub-api.sh", d / "requested"
    if fail is not None:
        body = f'printf %s "{fail}" >&2\nexit 1\n'
    else:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        body = "cat <<'JSONEOF'\n" + text + "\nJSONEOF\n"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n' + body)
    script.chmod(0o755)
    return script, log


def run(repo: Path, hub_api: Path, command: str = MERGE_CMD) -> str:
    env = {**os.environ, **_WAS_DEFAULT, "HUB_API_SH": str(hub_api)}
    r = subprocess.run(["sh", str(HOOK)], cwd=repo, text=True, capture_output=True, env=env,
                       input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))
    assert r.returncode == 0, f"the hook must never block a merge: rc={r.returncode} {r.stderr}"
    return r.stdout


def branches(repo: Path) -> set[str]:
    return set(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())


# --- the pair that proves the hook does its job at all ------------------------------------------

def test_a_merged_branch_is_deleted(tmp_path):
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    out = run(repo, api)
    assert "landed" not in branches(repo), "a branch whose hub PR is MERGED must be deleted"
    assert "landed" in out, "the deletion must be reported, not done silently"


def test_the_installed_command_name_fires_too(tmp_path):
    """`hub-api` is what a caller types once the command is installed; matched on basename, so the
    hook must fire for it as it does for `hub-api.sh`, or installing the command silently stops
    every prune."""
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api, command="hub-api pr merge acme/app 42 'title (#42)'")
    assert "landed" not in branches(repo), "`hub-api pr merge` must trigger the prune"


def test_a_branch_with_no_merged_pr_survives(tmp_path):
    """Same repo, same command -- only hub's answer differs. This is the differential."""
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(tmp_path, [pr(42, merged=True, ref="something-else",
                                        label="something-else")])
    run(repo, api)
    assert "landed" in branches(repo), "a branch absent from the MERGED list must never be deleted"


# --- Forgejo fail-open 1: `state=merged` is not a filter -----------------------------------------

def test_a_closed_but_unmerged_pr_does_not_get_its_branch_deleted(tmp_path):
    """MEASURED on hub: `?state=merged` falls back to `all`, so closed-unmerged and OPEN PRs come
    back too. Only the `merged` field distinguishes them, and this is the arm that proves the hook
    reads it rather than trusting the query."""
    repo = make_repo(tmp_path, ["abandoned", "landed"])
    api, _ = make_hub_api(tmp_path, [
        pr(41, merged=False, ref="abandoned", label="abandoned"),
        pr(42, merged=True, ref="landed", label="landed"),
    ])
    run(repo, api)
    assert "abandoned" in branches(repo), "a CLOSED-BUT-UNMERGED PR's branch must survive"
    assert "landed" not in branches(repo), "...and the same run must still delete the merged one"


def test_the_request_asks_for_closed_not_merged(tmp_path):
    """The other half: asking hub for `state=merged` would hand the filter every open PR in the
    repo, and a bug in the filter would then delete a branch under active review."""
    repo = make_repo(tmp_path, ["landed"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api)
    requested = log.read_text()
    assert "state=closed" in requested, f"must query state=closed; asked for: {requested!r}"
    assert "state=merged" not in requested, \
        f"state=merged is not a Forgejo filter — it returns everything: {requested!r}"


# --- Forgejo fail-open 2: head.ref is a pull ref once the branch is gone --------------------------

def test_a_merged_pr_whose_head_ref_is_a_pull_ref_is_still_matched(tmp_path):
    """The shape EVERY just-merged PR has on hub. Keyed on `ref` alone this deletes nothing, from a
    successful API call -- the exact silence that reads as a clean prune."""
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(tmp_path, [pr(42, merged=True, label="landed")])  # ref=refs/pull/42/head
    run(repo, api)
    assert "landed" not in branches(repo), \
        "the branch name must be recovered from head.label when head.ref is refs/pull/N/head"


def test_a_cross_repo_label_resolves_to_the_branch_name(tmp_path):
    """Forgejo labels a fork's head `owner:branch`. `:` is illegal in a branch name, so the part
    after it is unambiguous -- and without stripping it, nothing matches."""
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(tmp_path, [pr(42, merged=True, label="someone:landed")])
    run(repo, api)
    assert "landed" not in branches(repo), "an `owner:branch` label must resolve to `branch`"


# --- one pair per guard --------------------------------------------------------------------------

def test_main_survives_even_if_hub_lists_it(tmp_path):
    """HEAD is DETACHED here, and that detail is the whole test: with `main` checked out git refuses
    to delete it anyway and the hook's own guard is masked."""
    repo = make_repo(tmp_path, ["landed"])
    git(repo, "checkout", "-q", "--detach")
    api, _ = make_hub_api(tmp_path, [pr(41, merged=True, ref="main", label="main"),
                                     pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api)
    assert "main" in branches(repo), "main must never be deleted, whatever hub says"
    assert "landed" not in branches(repo), "...but the run must still have been live"


def test_a_branch_checked_out_in_a_worktree_survives(tmp_path):
    """The protection is GIT'S: `git branch -D` exits 1 on a branch checked out in any worktree.
    Pinned as an OUTCOME, so it notices if git ever stops refusing."""
    repo = make_repo(tmp_path, ["landed", "in-use"])
    git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "in-use")
    api, _ = make_hub_api(tmp_path, [pr(41, merged=True, ref="in-use", label="in-use"),
                                     pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api)
    assert "in-use" in branches(repo), "a branch checked out in a worktree must survive"
    assert "landed" not in branches(repo), "...and the same run must still delete the free one"


def test_it_does_not_fire_on_a_command_that_is_not_a_merge(tmp_path):
    repo = make_repo(tmp_path, ["landed"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    out = run(repo, api, command="scripts/hub-api.sh pr checks acme/app abc123")
    assert "landed" in branches(repo), "only `hub-api.sh pr merge` may trigger a prune"
    assert not log.exists(), "a non-merge command must not even reach hub"
    assert out.strip() == ""


def test_it_does_not_fire_on_gh_pr_merge(tmp_path):
    """The forges do not know about each other. A `gh pr merge` is the GitHub hook's moment, and
    this one must stay out of it -- otherwise one artefact is answering for both."""
    repo = make_repo(tmp_path, ["landed"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api, command="gh pr merge 452 --squash")
    assert "landed" in branches(repo), "a GitHub merge must not drive the Forgejo prune"
    assert not log.exists()


def test_it_does_not_fire_on_a_heredoc_that_merely_mentions_the_command(tmp_path):
    """A commit message documenting the command is not an invocation of it."""
    repo = make_repo(tmp_path, ["landed"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api, command="git commit -F - <<'EOF'\nuse scripts/hub-api.sh pr merge to land\nEOF")
    assert "landed" in branches(repo), "prose in a heredoc must not trigger a prune"
    assert not log.exists()


# --- loud degradation: the contract the GitHub corpus already pins, extended ----------------------

def test_an_unreachable_hub_is_loud_and_deletes_nothing(tmp_path):
    """Fail-safe is indistinguishable from success unless the degradation says so out loud."""
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(tmp_path, None, fail="curl: (7) Failed to connect to hub port 3000")
    out = run(repo, api)
    assert "landed" in branches(repo), "an unreachable hub must never be read as 'nothing landed'"
    assert "cannot read" in out and "not pruning" in out, "a prune that could not run must say so"


def test_an_absent_token_is_loud_and_deletes_nothing(tmp_path):
    """hub-api.sh dies on a missing/!=600 config. That is a non-zero exit with a message on stderr,
    and the message is what a reader needs -- a bare 'nothing to prune' would be a lie."""
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(tmp_path, None,
                          fail="hub-api: no valid token at ~/.config/forge-tools/hub-api.conf")
    out = run(repo, api)
    assert "landed" in branches(repo)
    assert "no valid token" in out, f"the reason must survive to the reader: {out!r}"


def test_an_unauthenticated_object_response_is_loud(tmp_path):
    """hub answers an unauthenticated API call with a JSON OBJECT at HTTP 200 -- a successful-looking
    response carrying no data. Read as a list it is empty; read honestly it is a failure."""
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(
        tmp_path, '{"message":"Only signed in user is allowed to call APIs.","url":"..."}')
    out = run(repo, api)
    assert "landed" in branches(repo)
    assert "did not return a pull list" in out, f"must not read an object as an empty list: {out!r}"


def test_a_response_missing_the_merged_field_is_loud(tmp_path):
    """A schema change produces an empty filter result from a 200 response, which is the same
    silence as a healthy steady state. The two must not be the same output."""
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(tmp_path, [
        {"number": 42, "state": "closed", "head": {"ref": "landed", "label": "landed"}}])
    out = run(repo, api)
    assert "landed" in branches(repo), "an unrecognisable response must never delete a branch"
    assert "no merged/merged_at field" in out, f"a schema change must be named: {out!r}"


def test_an_empty_closed_list_is_silent(tmp_path):
    """The control for the three tests above: a genuinely empty steady state says NOTHING, which is
    what makes the noise in those tests a signal rather than a constant."""
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(tmp_path, [])
    out = run(repo, api)
    assert "landed" in branches(repo)
    assert out.strip() == "", f"a healthy no-op must be silent: {out!r}"


def test_a_missing_hub_remote_is_loud(tmp_path):
    """`gh` infers the repo from origin; nothing infers it here. A repo with no `hub` remote is a
    real condition, not a reason to guess."""
    repo = make_repo(tmp_path, ["landed"], hub_remote=False)
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    out = run(repo, api)
    assert "landed" in branches(repo)
    assert "no 'hub' remote" in out, f"must name the missing remote: {out!r}"
    assert not log.exists()


def test_a_missing_hub_api_is_loud(tmp_path):
    repo = make_repo(tmp_path, ["landed"])
    out = run(repo, tmp_path / "does-not-exist")
    assert "landed" in branches(repo)
    assert "not executable" in out, f"must say hub-api.sh is unusable: {out!r}"


# --- the repo path is derived, so pin the derivation ---------------------------------------------

def test_the_owner_repo_comes_from_the_hub_remote_url(tmp_path):
    repo = make_repo(tmp_path, ["landed"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api)
    assert "/api/v1/repos/acme/app/pulls" in log.read_text()


def test_an_ssh_hub_remote_url_also_resolves(tmp_path):
    """hub serves git over ssh on :2222 as well as http, and both forms appear in real checkouts."""
    repo = make_repo(tmp_path, ["landed"])
    git(repo, "remote", "set-url", "hub",
        "ssh://git@192.0.2.10:2222/acme/app.git")
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api)
    assert "/api/v1/repos/acme/app/pulls" in log.read_text()
    assert "landed" not in branches(repo)


# --- the one branch the forge oracle can never clear ----------------------------------------
#
# A branch that never had a PR falls in the "no PR at all" bucket and `prune-merged` prints KEEP for
# it for ever -- including when it is an ANCESTOR of main, i.e. has no commits of its own at all.
# Ancestry is a false negative about SQUASHED work; it is not wrong here, because a contained branch
# has no content that could have been rewritten.
#
# WHAT WOULD MAKE THESE VACUOUS: the fixture above builds branches with `git branch b` off main, so
# EVERY branch in it is contained in main. A test that only asserted "the contained branch was
# deleted" would pass on a hook that deleted everything. So each test below carries a branch with a
# commit of its own and asserts it SURVIVED the same run.

def commit_on(repo: Path, branch: str, text: str = "own\n") -> None:
    """Give `branch` a commit of its own, so it is NOT an ancestor of main."""
    git(repo, "checkout", "-q", branch)
    (repo / f"{branch.replace('/', '_')}.txt").write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"work on {branch}")
    git(repo, "checkout", "-q", "main")


def set_hub_main(repo: Path) -> None:
    """The remote-tracking ref the new arm reads. `make_repo` adds the remote but never fetches, so
    it does not exist by default -- which is itself the fail-closed case, asserted below."""
    git(repo, "update-ref", "refs/remotes/hub/main", git(repo, "rev-parse", "main"))


def run_mode(repo: Path, hub_api: Path, mode: str, *branches_: str) -> tuple[str, int]:
    env = {**os.environ, **_WAS_DEFAULT, "HUB_API_SH": str(hub_api)}
    r = subprocess.run(["sh", str(HOOK), mode, *branches_], cwd=repo, text=True,
                       capture_output=True, env=env)
    return r.stdout + r.stderr, r.returncode


def test_a_branch_contained_in_main_is_cleared_though_no_pr_ever_existed(tmp_path):
    """The case: `agent/74-findings`, measured 2026-08-26 -- 0 commits ahead of hub/main, an
    ancestor of it, no PR of its own, and KEEP for ever."""
    repo = make_repo(tmp_path, ["empty/no-pr", "has/own-work"])
    commit_on(repo, "has/own-work")
    set_hub_main(repo)
    api, _ = make_hub_api(tmp_path, [pr(42, merged=False, ref="other", label="other")])
    out, rc = run_mode(repo, api, "--delete", "empty/no-pr", "has/own-work")
    assert rc == 0, out
    assert "empty/no-pr" not in branches(repo), f"the contained branch must be cleared: {out}"
    assert "has/own-work" in branches(repo), \
        f"a branch with a commit of its own must NOT be cleared -- this arm is not 'delete all': {out}"


def merge_into_main(repo: Path, branch: str) -> None:
    """Land `branch` on main the way the merge queue does: a real merge commit, so its sha is PRESERVED and the
    branch becomes a genuine ancestor. Under the old `Do=squash` this shape was unreachable."""
    git(repo, "checkout", "-q", "main")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
        "merge", "--no-ff", "-q", "-m", f"land {branch} (#1)", branch)


def test_a_contained_branch_with_REAL_COMMITS_is_cleared_since_431(tmp_path):
    """The arm above used to be safe for a reason that EXPIRED, and this pins what replaced it.

    The old argument was structural: "it only fires on a branch with NO commits of its own, because
    a branch with content is not an ancestor of `main` under squash-merge". True of squash, never
    true of this code -- the predicate is `--is-ancestor` and nothing else. Since merges
    preserve shas, so a landed branch WITH content is an ancestor and reaches this arm.

    Deleting it is still correct, on a premise that cannot expire: `--is-ancestor b main` holds IFF
    every commit reachable from `b` is reachable from `main`, so `main` carries the content and only
    the NAME is lost (`git branch <name> <sha>` restores it).

    Both directions asserted, because the dangerous failure here is deleting an UNLANDED branch --
    `main` refuses force push, and the next keystroke after a wrong refusal is `-D`."""
    repo = make_repo(tmp_path, ["landed/work", "unlanded/work"])
    commit_on(repo, "landed/work")
    commit_on(repo, "unlanded/work")
    merge_into_main(repo, "landed/work")
    set_hub_main(repo)
    # hub knows nothing about either branch, so only the ancestry arm can clear anything.
    api, _ = make_hub_api(tmp_path, [pr(42, merged=False, ref="other", label="other")])
    out, rc = run_mode(repo, api, "--delete", "landed/work", "unlanded/work")
    assert rc == 0, out
    assert "landed/work" not in branches(repo), (
        f"a branch whose real commits are all on main must be cleared -- this is the merge-commit shape "
        f"the old squash-era reasoning declared impossible: {out}")
    assert "unlanded/work" in branches(repo), (
        f"a branch with commits NOT on main must survive; ancestry may only withhold the "
        f"verdict, never grant it wrongly: {out}")


def test_the_verdict_does_not_claim_hub_said_so(tmp_path):
    """hub was never consulted about this branch, so the line may not read as a forge verdict."""
    repo = make_repo(tmp_path, ["empty/no-pr"])
    set_hub_main(repo)
    api, _ = make_hub_api(tmp_path, [pr(42, merged=False, ref="other", label="other")])
    out, _ = run_mode(repo, api, "--dry-run", "empty/no-pr")
    assert "empty/no-pr" in out and "already on refs/remotes/hub/main" in out, out
    line = [ln for ln in out.splitlines() if "empty/no-pr" in ln][0]
    assert "merged per hub" not in line, f"must not claim an authority it did not consult: {line}"


def test_the_dry_run_line_is_what_the_reaper_parses(tmp_path):
    """`scripts/worktree-reap.sh` extracts landed branches with
    `sed -n 's/^.*WOULD DELETE \\([^(]*\\)(.*$/\\1/p'`. If this arm's line does not fit that shape,
    the reaper silently never sees it -- the exact blindness this is about."""
    repo = make_repo(tmp_path, ["empty/no-pr"])
    set_hub_main(repo)
    api, _ = make_hub_api(tmp_path, [pr(42, merged=False, ref="other", label="other")])
    out, _ = run_mode(repo, api, "--dry-run", "empty/no-pr")
    extracted = subprocess.run(["sed", "-n", r"s/^.*WOULD DELETE \([^(]*\)(.*$/\1/p"],
                               input=out, text=True, capture_output=True).stdout.split()
    assert extracted == ["empty/no-pr"], f"the reaper would parse {extracted!r} out of: {out}"


def test_without_the_remote_tracking_ref_it_refuses_to_judge(tmp_path):
    """No `refs/remotes/hub/main` means no verdict -- absence of the ref is not permission."""
    repo = make_repo(tmp_path, ["empty/no-pr"])   # deliberately no set_hub_main()
    api, _ = make_hub_api(tmp_path, [pr(42, merged=False, ref="other", label="other")])
    out, rc = run_mode(repo, api, "--delete", "empty/no-pr")
    assert rc == 0, out
    assert "empty/no-pr" in branches(repo), f"with no hub/main it must fall through to KEEP: {out}"


def test_a_stale_hub_main_can_only_withhold_the_verdict(tmp_path):
    """Ancestry is monotonic as main advances, so a hub/main pinned BEHIND main withholds the
    verdict and can never grant it wrongly. This is why the hook does not fetch."""
    repo = make_repo(tmp_path, ["empty/no-pr"])
    set_hub_main(repo)                    # hub/main == the base commit
    commit_on(repo, "empty/no-pr")        # branch now AHEAD of that pinned ref
    api, _ = make_hub_api(tmp_path, [pr(42, merged=False, ref="other", label="other")])
    out, _ = run_mode(repo, api, "--delete", "empty/no-pr")
    assert "empty/no-pr" in branches(repo), f"ahead of the pinned hub/main: must survive: {out}"


def test_it_still_clears_when_hub_lists_no_merged_pr_at_all(tmp_path):
    """The early exit used to return before this arm, making it conditional on some UNRELATED
    branch having landed."""
    repo = make_repo(tmp_path, ["empty/no-pr", "has/own-work"])
    commit_on(repo, "has/own-work")
    set_hub_main(repo)
    api, _ = make_hub_api(tmp_path, [])   # hub answers: no closed PRs whatsoever
    out, rc = run_mode(repo, api, "--delete", "empty/no-pr", "has/own-work")
    assert rc == 0, out
    assert "empty/no-pr" not in branches(repo), out
    assert "has/own-work" in branches(repo), out


def test_a_repo_wide_sweep_does_not_clear_contained_branches(tmp_path):
    """THE GUARD THAT KEEPS THIS A FIX AND NOT A SWEEP, and it was added because the first version
    of this change did not have it.

    Measured on the real repo before the guard existed: the arm fired on TEN local branches --
    `wayfinder/161`, `wayfinder/206`, `wayfinder/210`, `work`, `stage-296`, three `worktree-agent-*`
    refs and the branch the change was being written on. All contained in main, all deliberate
    bookmarks. `prune-merged --delete` is repo-wide in its common no-argument form, so without this
    it would have swept all ten to clear one findings branch. No commits would have been lost; the
    NAMES would have been."""
    repo = make_repo(tmp_path, ["wayfinder/161", "empty/no-pr"])
    set_hub_main(repo)
    api, _ = make_hub_api(tmp_path, [pr(42, merged=False, ref="other", label="other")])
    out, rc = run_mode(repo, api, "--delete")          # no branch named: a sweep
    assert rc == 0, out
    assert {"wayfinder/161", "empty/no-pr"} <= branches(repo), \
        f"a sweep must not clear contained branches, only a NAMED query may: {out}"
    # ...and naming one still reaches it, so the guard narrowed the arm without removing it.
    out, _ = run_mode(repo, api, "--delete", "empty/no-pr")
    assert "empty/no-pr" not in branches(repo), f"a named query must still clear it: {out}"
    assert "wayfinder/161" in branches(repo), f"and must touch nothing it was not asked about: {out}"


def test_main_is_never_cleared_by_the_contained_arm(tmp_path):
    """`main` is trivially an ancestor of itself."""
    repo = make_repo(tmp_path, [])
    set_hub_main(repo)
    api, _ = make_hub_api(tmp_path, [])
    out, _ = run_mode(repo, api, "--delete", "main")
    assert "main" in branches(repo), f"main must survive its own ancestry: {out}"


# --- the real hub-api.sh, not only the stub -------------------------------------------------------

def test_every_inline_python_block_survives_shell_quoting():
    """A real bug this hook carried for one test run, and the reason it is worth a test.

    The response filter is `python3 -c '<script>'`. Shell single quotes admit no escapes, so an
    apostrophe anywhere in that script -- in a docstring, in a message like a quoted phrase -- ENDS
    the argument early. Python then gets a truncated program, `|| true` swallows the error, and the
    hook prunes NOTHING from a perfectly healthy API call: silent, and identical to the steady state.

    The first version of this test looked for a bare apostrophe and PASSED on the injected fault,
    because it stopped at the first quote and so never saw the one that mattered -- it reproduced the
    bug it was checking for. What is asserted instead is the property that actually matters: extract
    each block exactly as the shell would, and require it to COMPILE. A stolen quote truncates a
    statement, which is a SyntaxError; nothing else about the block has to be guessed at.
    """
    text = HOOK.read_text()
    blocks, rest = [], text
    while "python3 -c '" in rest:
        rest = rest.split("python3 -c '", 1)[1]
        # No escaping inside shell single quotes: the argument is everything up to the next quote.
        body, _, rest = rest.partition("'")
        blocks.append(body)
    assert len(blocks) == 2, \
        f"expected 2 inline python blocks, found {len(blocks)} — this test cannot pass vacuously"
    for b in blocks:
        compile(b, str(HOOK), "exec")  # SyntaxError here IS the finding


def test_the_real_hub_api_accepts_the_path_this_hook_builds(tmp_path):
    """A stub proves the hook's logic and nothing about the interface it is coded against. This
    exercises the REAL script's argument handling: an unknown path is a passthrough call, so the
    failure must come from hub or the token — never from hub-api.sh rejecting the arguments."""
    real = REPO / "scripts/hub-api.sh"
    r = subprocess.run(["sh", str(real), "/api/v1/repos/x/y/pulls?state=closed&limit=50"],
                       text=True, capture_output=True,
                       env={**os.environ, "HUB_API_CONFIG": str(tmp_path / "absent.conf")})
    assert "usage:" not in (r.stdout + r.stderr).lower(), \
        f"the path form must be a passthrough call, not a usage error: {r.stdout}{r.stderr}"
    assert "no valid token" in r.stderr, \
        f"with no config the real script must refuse loudly: {r.stdout!r} {r.stderr!r}"


def test_the_deleted_line_does_not_present_the_local_sha_as_the_landed_commit(tmp_path):
    """The deletion was right; the evidence printed beside it was false.

    Measured 2026-08-22 on a `wayfinder/` branch after its PR merged: the line printed `ab5d92437e`, and
    `git merge-base --is-ancestor ab5d92437e hub/main` answered 1 -- that commit was never on
    `main` in any form. `7c707552ee` was what landed. The sha comes from `git rev-parse` on the
    LOCAL ref, and `pr-queue.sh` rebases every branch it admits, so for an admitted branch the
    local sha is the PRE-rebase commit and is an ancestor of nothing.

    This builds that exact shape rather than asserting a string: hub says the PR merged, while the
    local ref points at a commit that is provably NOT on main. The deletion must still happen --
    hub is the authority on landing -- and the printed sha must not be labelled as the thing that
    landed.
    """
    repo = make_repo(tmp_path, ["admitted/work"])
    commit_on(repo, "admitted/work")
    set_hub_main(repo)
    local_sha = git(repo, "rev-parse", "--short", "admitted/work")

    # The premise of the ticket, asserted rather than assumed: this sha never landed.
    contained = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "admitted/work", "refs/remotes/hub/main"],
        capture_output=True)
    assert contained.returncode != 0, (
        "fixture is wrong: the local head must NOT be on main, or this is not the measured shape")

    api, _ = make_hub_api(tmp_path, [pr(217, merged=True, ref="admitted/work")])
    out, rc = run_mode(repo, api, "--delete", "admitted/work")
    assert rc == 0, out

    assert "admitted/work" not in branches(repo), (
        f"hub said the PR merged, so the deletion is correct and must not regress: {out}")
    assert local_sha in out, (
        f"the sha is still worth printing -- it is what `git branch <name> {local_sha}` needs to "
        f"undo the delete. It must be present, just not mislabelled: {out}")
    assert "deleted landed" not in out, (
        f"the word `landed` sat directly against the sha, which is what made a commit that was "
        f"never on main read as evidence of landing: {out}")
    assert "NOT the commit that landed" in out, (
        f"the line must say what the sha IS -- the local ref removed -- rather than leaving a "
        f"reader to assume it is the landed commit: {out}")


# --- the merged-set cache (hub#, 2026-09-04) ----------------------------------------------------
#
# WHAT WOULD MAKE THESE VACUOUS: asserting only that a branch was deleted. Deletion is what BOTH the
# cached and uncached paths do, so it cannot tell them apart -- and a cache that silently never
# engaged would pass every such test while still paying the 14s walk it exists to remove. Every test
# below therefore counts REQUESTS to the hub stub, which is the observable that actually changes.

CACHE_NAME = "prune-landed-merged.cache"


def cache_of(repo: Path) -> Path:
    """Where the hook keeps it: the git COMMON dir, so worktrees share one refresh."""
    return repo / ".git" / CACHE_NAME


def requests_to(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def age(path: Path, minutes: int) -> None:
    old = time.time() - minutes * 60
    os.utime(path, (old, old))


def test_a_second_run_does_not_ask_hub_again(tmp_path):
    """The whole point: 46 of 49 measured runs walked ~10 pages to delete nothing."""
    repo = make_repo(tmp_path, ["a/one", "a/two"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, label="a/one")])
    run(repo, api)
    first = requests_to(log)
    assert first > 0, "the cold run must actually ask hub, or the control below proves nothing"
    assert cache_of(repo).exists(), "a complete walk must leave a cache behind"
    run(repo, api)
    assert requests_to(log) == first, (
        f"the warm run must ask hub NOTHING -- it asked {requests_to(log) - first} more time(s)")


def test_a_cached_run_still_deletes_the_merged_branch(tmp_path):
    """The cache must not turn the hook into a no-op: behaviour identical, cost different."""
    repo = make_repo(tmp_path, ["a/one"])
    api, _ = make_hub_api(tmp_path, [pr(42, merged=True, label="a/one")])
    run(repo, api)                      # populates the cache and deletes a/one
    git(repo, "branch", "a/one")        # same name lands again; only the cache answers this time
    run(repo, api)
    assert "a/one" not in branches(repo), "a warm run must still delete off the cached set"


def test_a_stale_cache_is_refetched(tmp_path):
    repo = make_repo(tmp_path, ["a/one"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, label="a/one")])
    run(repo, api)
    before = requests_to(log)
    age(cache_of(repo), 60 * 24)        # a day old, well past the 180-minute default
    run(repo, api)
    assert requests_to(log) > before, (
        "a cache older than the TTL must be re-walked, or it would pin a stale answer for ever")


def test_an_empty_cache_file_is_a_miss_not_an_answer(tmp_path):
    """A truncated write and 'the forge lists no merged PR' are different facts."""
    repo = make_repo(tmp_path, ["a/one"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, label="a/one")])
    cache_of(repo).parent.mkdir(parents=True, exist_ok=True)
    cache_of(repo).write_text("")
    run(repo, api)
    assert requests_to(log) > 0, "an empty cache must fall through to the walk, not read as empty set"
    assert "a/one" not in branches(repo), "and the walk must still do its job"


def test_the_cache_is_never_read_outside_hook_mode(tmp_path):
    """`$seen`/`$truncated` are what let a KEEP verdict say how much it searched. They are
    only correct for a walk this run actually did, so the audit modes may never take the fast path."""
    repo = make_repo(tmp_path, ["a/one"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, label="a/one")])
    run(repo, api)                      # warm the cache in hook mode
    git(repo, "branch", "a/one")
    before = requests_to(log)
    out, rc = run_mode(repo, api, "--dry-run")
    assert rc == 0, out
    assert requests_to(log) > before, (
        f"--dry-run must do its own full walk however warm the cache is: {out}")


def test_a_truncated_walk_is_not_cached(tmp_path):
    """Caching a capped walk would turn a loud one-run truncation into a quiet three-hour one."""
    repo = make_repo(tmp_path, ["a/one"])
    full_page = [pr(n, merged=False, label=f"x/{n}") for n in range(50)]
    api, log = make_hub_api(tmp_path, full_page)   # never a short page -> always hits the cap
    env = {**os.environ, **_WAS_DEFAULT, "HUB_API_SH": str(api), "PRUNE_PAGE_MAX": "2"}
    r = subprocess.run(["sh", str(HOOK)], cwd=repo, text=True, capture_output=True, env=env,
                       input=json.dumps({"tool_name": "Bash", "tool_input": {"command": MERGE_CMD}}))
    assert r.returncode == 0, r.stderr
    assert requests_to(log) > 0, "the capped run must still have asked, or this proves nothing"
    assert not cache_of(repo).exists(), (
        "a run that hit the page cap has NOT seen older history and must not cache that answer")


def test_the_cache_lives_outside_the_working_tree(tmp_path):
    """It is derived state. Inside the tree it would show up in `git status` and could be committed."""
    repo = make_repo(tmp_path, ["a/one"])
    api, _ = make_hub_api(tmp_path, [pr(42, merged=True, label="a/one")])
    run(repo, api)
    assert cache_of(repo).exists(), "precondition: the cache was written"
    assert git(repo, "status", "--porcelain") == "", (
        "the cache must not appear as an untracked file in the working tree")


# --- command position: the sibling's ceiling, closed here too -------------------------------------

def _run_hook(hook: Path, repo: Path, hub_api: Path, command: str):
    env = {**os.environ, **_WAS_DEFAULT, "HUB_API_SH": str(hub_api)}
    return subprocess.run(["sh", str(hook)], cwd=repo, text=True, capture_output=True, env=env,
                          input=json.dumps({"tool_name": "Bash",
                                            "tool_input": {"command": command}}))


def test_a_mention_outside_a_heredoc_does_not_fire(tmp_path):
    """`test_it_does_not_fire_on_a_heredoc_that_merely_mentions_the_command` covers the heredoc.
    This is the same phrase in ordinary command text, which that one cannot see."""
    repo = make_repo(tmp_path, ["landed"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api, command='echo "scripts/hub-api.sh pr merge o/r 1 x"')
    assert not log.exists(), "a quoted mention must never reach hub"
    assert "landed" in branches(repo)


def test_the_sh_script_form_STILL_fires(tmp_path):
    """THE REGRESSION THIS FIX COULD EASILY HAVE SHIPPED. `sh scripts/hub-api.sh pr merge …` is a
    real invocation in this repo's transcripts, and `sh` is NOT in `unwrap()`'s WRAPPERS -- so
    anchoring on the first token alone would have made this hook silently stop firing on it, which
    looks exactly like a merge with nothing to prune. `invokes()` strips shell runners itself."""
    repo = make_repo(tmp_path, ["landed"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api, command="cd /somewhere; sh scripts/hub-api.sh pr merge o/r 42 'subject (#42)'")
    assert log.exists(), "the `sh <script>` form is a real merge and must still be seen"
    assert "landed" not in branches(repo)


def test_a_wrapped_invocation_still_fires(tmp_path):
    repo = make_repo(tmp_path, ["landed"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    run(repo, api, command="FOO=1 ./scripts/hub-api.sh pr merge o/r 42 'subject (#42)'")
    assert log.exists(), "VAR=val and a relative path must not hide a real merge"
    assert "landed" not in branches(repo)


def test_an_unimportable_parser_refuses_LOUDLY(tmp_path):
    """Refusing is the safe direction for a deleter; silence is not. The old fallback was the raw
    substring, i.e. a fallback to the defect being fixed."""
    d = tmp_path / "lonely"
    d.mkdir()
    copy = d / HOOK.name
    copy.write_text(HOOK.read_text())
    repo = make_repo(tmp_path, ["landed"])
    api, log = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    r = _run_hook(copy, repo, api, MERGE_CMD)
    assert r.returncode == 0, "a PostToolUse hook must never fail the tool call"
    assert not log.exists(), "it must not prune on an unparsed command"
    assert "landed" in branches(repo)
    assert "bash_cmd_parse" in r.stdout, f"the refusal must say why: {r.stdout!r}"


def test_a_hand_run_resolves_its_sibling_client_without_hub_api_sh(tmp_path):
    """`pr-queue prune-merged` runs this script by hand, with no HUB_API_SH: the client is then its
    sibling hub-api.sh, found through SELF_DIR. SELF_DIR used to be set only in hook mode, so under
    `set -u` every hand run died at that line before pruning anything -- and every other test here
    sets HUB_API_SH, which short-circuits the expansion and hid it."""
    repo = make_repo(tmp_path, ["landed"])
    api, _ = make_hub_api(tmp_path, [pr(42, merged=True, ref="landed", label="landed")])
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("prune-landed-branches-forgejo.sh", "ft-config.sh", "bash_cmd_parse.py"):
        (tools / name).write_bytes((HOOK.parent / name).read_bytes())
    (tools / "hub-api.sh").write_bytes(api.read_bytes())
    (tools / "hub-api.sh").chmod(0o755)
    env = {k: v for k, v in {**os.environ, **_WAS_DEFAULT}.items() if k != "HUB_API_SH"}
    r = subprocess.run(["sh", str(tools / "prune-landed-branches-forgejo.sh"), "--dry-run"], cwd=repo,
                       text=True, capture_output=True, env=env)
    assert "parameter not set" not in r.stderr, r.stderr
    assert r.returncode == 0, r.stdout + r.stderr
    assert "landed" in r.stdout + r.stderr, "the sibling client was never asked"
