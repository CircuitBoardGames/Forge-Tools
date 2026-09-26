"""`prune-merged` must delete a LANDED branch and refuse an unlanded one, offline.

A merged branch's commits can be on `main` under a NEW sha, leaving the branch an ancestor of
nothing. `git branch -d` then refuses work that fully landed, and the obvious next keystroke -- `-D`
-- force-deletes on an unanswered question. The answer comes from the forge:
`pulls?state=closed` filtered on `merged`.

That is universal for a repo that squash-merges. One that merges with merge commits leaves its
branches as ancestors, so `-d` succeeds -- but **these tests still model the rewritten case
deliberately**, because it remains true of every branch squash-merged before such a switch
(permanently: `main` refuses force push) and of any branch the queue rebased. The verb must keep answering for
the era it cannot assume away, so the fixture below still builds a rewritten landing.

The mechanism is `scripts/prune-landed-branches-forgejo.sh`, which already made that call as a
PostToolUse hook; `scripts/pr-queue.sh prune-merged` gives it a name to type. These tests run it
against a STUB `hub-api.sh` and a throwaway repo in tmp_path, so they need no forge and never touch
a real branch.

WHAT WOULD MAKE THIS VACUOUS, and what each test does about it: a pruner that deletes everything and
a pruner that deletes nothing BOTH pass a single happy-path assertion. So every deletion test asserts
the OTHER branch survived, and the two failure modes the forge query has -- an unreachable hub and a
closed-but-unmerged PR -- are asserted to delete nothing while saying so out loud.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
HOOK = REPO / "scripts/prune-landed-branches-forgejo.sh"
PR_QUEUE = REPO / "scripts/pr-queue.sh"

LANDED = "fix/162-landed"
UNLANDED = "fix/162-open"

# Shaped like Forgejo's, including the two measured traps the hook is built around: a merged PR whose
# head branch is already gone reports `head.ref = refs/pull/N/head` and keeps the name in `label`,
# and a closed PR that was DECLINED carries merged=false while still being `state=closed`.
PAYLOAD = json.dumps(
    [
        {"number": 1, "state": "closed", "merged": True,
         "head": {"ref": "refs/pull/1/head", "label": "owner:" + LANDED}},
        {"number": 2, "state": "closed", "merged": False, "merged_at": None,
         "head": {"ref": UNLANDED, "label": UNLANDED}},
    ]
)
NOTHING_MERGED = json.dumps(
    [{"number": 2, "state": "closed", "merged": False, "merged_at": None,
      "head": {"ref": UNLANDED, "label": UNLANDED}}]
)


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A throwaway repo with a `hub` remote and two branches that git CANNOT tell apart.

    The branches are built the way this repo really produces them: each carries its own commits, and
    `LANDED`'s content is then replayed onto `main` as ONE new commit -- a squash merge. That is what
    makes the fixture worth having. Branching both off `main` and leaving them there would make
    `git branch -d` succeed on both, and every assertion below would be about a repo where the bug
    does not exist. `test_the_premise_git_itself_cannot_answer` is the check on the fixture itself.

    The remote URL is never contacted -- the hook reads it only to derive `owner/repo` for the API
    path, and the API is the stub.
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "f").write_text("base\n")
    _git(r, "add", "f")
    _git(r, "commit", "-qm", "base")
    _git(r, "remote", "add", "hub", "https://hub.example/owner/repo.git")

    for b, text in ((LANDED, "landed\n"), (UNLANDED, "open\n")):
        _git(r, "checkout", "-q", "-b", b, "main")
        (r / b.replace("/", "-")).write_text(text)
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", "work on " + b)
        (r / b.replace("/", "-")).write_text(text * 2)
        _git(r, "commit", "-qam", "more work on " + b)

    # The squash merge: LANDED's tree lands on main under a sha that is not in its history.
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "-q", "--squash", LANDED)
    _git(r, "commit", "-qm", "work on " + LANDED + " (#1)")
    return r


def _stub(tmp_path: pathlib.Path, payload: str, rc: int = 0) -> pathlib.Path:
    """A stub standing in for a script must BE a script: real shebang, exec bit."""
    s = tmp_path / "hub-api-stub.sh"
    s.write_text("#!/bin/sh\ncat <<'PAYLOAD_EOF'\n" + payload + "\nPAYLOAD_EOF\nexit %d\n" % rc)
    s.chmod(0o755)
    return s


def _paged_stub(tmp_path: pathlib.Path, pages: list[str]) -> pathlib.Path:
    """A stub that answers `&page=N` with a DIFFERENT list, which the single-call version never needed.

    Forgejo caps a page at MAX_RESPONSE_ITEMS=50, so a full page means "ask again" and a short one
    means "that was the end". The stub reproduces exactly that contract and nothing else; anything
    past the supplied pages is the empty list a real forge returns off the end.
    """
    s = tmp_path / "hub-api-paged-stub.sh"
    body = ["#!/bin/sh", 'case "$1" in']
    for i, payload in enumerate(pages, start=1):
        (tmp_path / ("page%d.json" % i)).write_text(payload)
        body.append('  *page=%d) cat "%s" ;;' % (i, tmp_path / ("page%d.json" % i)))
    body += ["  *) echo '[]' ;;", "esac"]
    s.write_text("\n".join(body) + "\n")
    s.chmod(0o755)
    return s


def _filler(n: int, first: int = 1000) -> list[dict]:
    """`n` closed-but-declined PRs, to make a page FULL without landing anything.

    merged=false, so they are inert to the filter -- their only job is to be 50 items long, which is
    what makes page 1 look like there is more behind it.
    """
    out = [
        {"number": first + i, "state": "closed", "merged": False, "merged_at": None,
         "head": {"ref": "filler/%d" % (first + i), "label": "filler/%d" % (first + i)}}
        for i in range(n)
    ]
    # One of them IS merged, on a branch that does not exist locally. Without it a page of purely
    # declined PRs takes the "no closed PR is merged at all" early exit, which never reaches the
    # per-branch verdict loop -- so a test aimed at the KEEP line would be asserting on the wrong
    # arm. This makes the page look like a real forge page: mostly noise, something landed in it.
    if out:
        out[0]["merged"] = True
        out[0]["merged_at"] = "2026-08-01T00:00:00Z"
    return out


def _prune(
    repo: pathlib.Path, stub: pathlib.Path, *args: str, **env: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(HOOK), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        # FORGE_TOOLS_REMOTE: the fixture's forge remote is `hub`, the name the pruner once defaulted to.
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), "HUB_API_SH": str(stub),
             "FORGE_TOOLS_REMOTE": "hub", **env},
    )


def _branches(repo: pathlib.Path) -> set[str]:
    return set(_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split())


def test_the_premise_git_itself_cannot_answer(repo: pathlib.Path) -> None:
    """`git branch -d` refuses the landed branch — the false negative this verb exists to replace.

    Without this the whole change is unmotivated, and it is asserted rather than asserted-about: the
    branches here are ordinary unmerged-looking refs, exactly like a squash-merged one.
    """
    r = subprocess.run(["git", "branch", "-d", LANDED], cwd=repo, capture_output=True, text=True)
    assert r.returncode != 0, "git -d accepted the branch; the premise under test does not hold here"
    assert LANDED in _branches(repo)


def test_dry_run_names_both_verdicts_and_deletes_nothing(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    out = _prune(repo, _stub(tmp_path, PAYLOAD), "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "WOULD DELETE " + LANDED in out.stdout, out.stdout
    assert "KEEP        " + UNLANDED in out.stdout, out.stdout
    # The dry run is the default, so this arm is the one a mistyped invocation lands on.
    assert {LANDED, UNLANDED} <= _branches(repo)


def test_delete_removes_only_what_the_forge_calls_merged(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    out = _prune(repo, _stub(tmp_path, PAYLOAD), "--delete")
    assert out.returncode == 0, out.stderr
    left = _branches(repo)
    assert LANDED not in left, out.stdout          # merged=true, and its ref was already a pull ref
    assert UNLANDED in left, out.stdout            # closed but NOT merged: the refusing arm
    assert "main" in left


def test_delete_leaves_the_remote_branch_and_says_so(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The report must name the side it deleted, because the convention it automates says both.

    Asserted BEHAVIOURALLY, not by grepping the message for a word: a bare remote is created and the
    landed branch pushed to it, so the surviving remote ref is what proves the claim rather than the
    sentence describing it. Pairing the two is the point -- if a later change starts deleting the
    remote and leaves this wording, this test goes red on the wording; if it changes the wording
    without deleting the remote, it goes red on the ref.

    Measured on 2026-08-21 before this was fixed: `prune-merged --delete fix/162-prune-merged`
    printed `deleted landed:` and `git ls-remote --heads hub` still listed the branch.
    """
    # A SECOND remote, deliberately not `hub`: the fixture's `hub` URL is load-bearing (the hook
    # parses owner/repo out of it), and repointing it would fail the test for the wrong reason.
    # A reachable remote of any name proves the general claim, which is the stronger one -- this arm
    # pushes to NO remote at all, so no remote ref anywhere is deleted.
    bare = tmp_path / "mirror.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(repo, "remote", "add", "mirror", str(bare))
    _git(repo, "push", "-q", "mirror", LANDED)
    assert LANDED in _git(repo, "ls-remote", "--heads", "mirror"), "fixture never pushed the branch"

    out = _prune(repo, _stub(tmp_path, PAYLOAD), "--delete")
    assert out.returncode == 0, out.stderr
    assert LANDED not in _branches(repo), "local branch survived; the delete arm did nothing"
    assert LANDED in _git(repo, "ls-remote", "--heads", "mirror"), (
        "a remote branch was deleted -- either widen the message or restore the refusal"
    )
    # NAME THE SIDE, not the sentence. This asserted the literal string `LOCAL only`, which a later
    # change reworded -- the verdict now leads with what was deleted rather than with the word `landed`,
    # which used to sit against a sha that had never landed. The PROPERTY under test is unchanged
    # and is what is asserted here: the line must say the local ref went and the remote did not, so
    # a reader of the standing "delete remote AND local" convention cannot read it as both.
    assert "LOCAL" in out.stdout and "remote branch remains" in out.stdout, (
        f"the message must name which side was deleted, or it reads as both: {out.stdout}")


def test_a_merged_branch_with_no_local_ref_is_reported_as_an_orphan(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The sweep's universe is `refs/heads/`, so a remote-only merged branch was invisible.

    THIS TOOL CREATES THE STATE ITSELF: `--delete` removes the local ref and says the remote remains,
    so from the next run onward that branch has no local ref and never appears in any verdict again.
    The branch most likely to be orphaned on hub is the one the sweep could not report.

    The fixture's `hub` URL is an unreachable placeholder, so this repoints it at a real bare repo --
    the orphan path is the one code path here that genuinely contacts the remote, and a fake URL
    would make the test pass by failing to look. `owner/repo` still parses out of a path-style URL.

    Report only, never delete: the assertion that the remote ref SURVIVES is half the test.
    """
    bare = tmp_path / "hub.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(repo, "remote", "set-url", "hub", str(bare))
    _git(repo, "push", "-q", "hub", LANDED)
    _git(repo, "branch", "-D", LANDED)
    assert LANDED not in _branches(repo), "fixture still has a local ref; nothing would be orphaned"
    assert LANDED in _git(repo, "ls-remote", "--heads", "hub"), "fixture never pushed the branch"

    out = _prune(repo, _stub(tmp_path, PAYLOAD), "--dry-run")
    assert out.returncode == 0, out.stderr
    orphans = [ln for ln in out.stdout.splitlines() if "ORPHAN" in ln]
    assert len(orphans) == 1, f"expected exactly one orphan line, got {orphans}\nfull stdout:\n{out.stdout}\nstderr:\n{out.stderr}"
    assert LANDED in orphans[0], orphans[0]
    assert LANDED in _git(repo, "ls-remote", "--heads", "hub"), (
        "the orphan path deleted a remote branch -- it must report only"
    )


def test_an_orphan_report_is_silent_when_every_merged_branch_has_a_local_ref(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The negative arm. Without it the test above passes for a hook that prints ORPHAN always.

    Also pins the cost rule: with no candidate there is nothing to look up, so the remote is never
    contacted. The fixture's `hub` URL is left unreachable here on purpose -- if this path ever
    starts calling `ls-remote` unconditionally, this test pays the timeout that change would inflict
    on every other one.
    """
    out = _prune(repo, _stub(tmp_path, PAYLOAD), "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "ORPHAN" not in out.stdout, (
        f"reported an orphan while {LANDED} still has a local ref:\n{out.stdout}"
    )


def test_a_closed_unmerged_pr_is_never_a_licence_to_delete(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """`?state=merged` is not a filter on Forgejo — every closed PR must be re-checked client side.

    The message names the number of closed PRs the run read. It used to say "NO closed PR on
    <repo> is merged", which was the same overclaim as the KEEP line one arm below: a statement about
    the forge produced by a windowed query.
    """
    out = _prune(repo, _stub(tmp_path, NOTHING_MERGED), "--delete")
    assert out.returncode == 0, out.stderr
    assert {LANDED, UNLANDED} <= _branches(repo)
    assert "NO merged PR among the 1 closed PRs" in out.stdout, out.stdout


def test_an_unreachable_hub_exits_nonzero_and_deletes_nothing(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Not-asked must never read as not-merged. As a hook this is advisory; when ASKED it must fail."""
    out = _prune(repo, _stub(tmp_path, "gateway timeout", rc=7), "--delete")
    assert out.returncode != 0, out.stdout
    assert {LANDED, UNLANDED} <= _branches(repo)


def test_an_unreadable_response_deletes_nothing(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """HTTP 200 carrying a message object is what hub answers an unauthenticated call with."""
    out = _prune(repo, _stub(tmp_path, '{"message":"Only signed in user is allowed"}'), "--delete")
    assert {LANDED, UNLANDED} <= _branches(repo)
    assert out.returncode != 0, out.stdout


def test_named_branches_narrow_the_scope(repo: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """A caller who names a branch gets a verdict on that branch and nothing else touched."""
    out = _prune(repo, _stub(tmp_path, PAYLOAD), "--delete", UNLANDED)
    assert out.returncode == 0, out.stderr
    assert {LANDED, UNLANDED} <= _branches(repo), "named an unmerged branch and it was deleted"
    assert LANDED not in out.stdout


def test_a_branch_landed_beyond_the_first_page_is_still_found(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A full page 1 is not the whole forge, and stopping there was a silent false KEEP.

    Measured against a real forge: `?state=closed&limit=50` returned only the newest fifty, so a
    docs branch -- landed by PR #78 -- was reported
    `KEEP ... hub lists NO merged PR with this head branch`, worded identically to a branch that
    never had a PR. Here page 1 is 50 declined PRs and LANDED is merged on page 2.

    This is the deletion assertion, so it carries the other half: UNLANDED, which is on neither
    page, must survive. A pruner that simply deleted everything it could not find would pass the
    first assertion alone.
    """
    stub = _paged_stub(
        tmp_path,
        [
            json.dumps(_filler(50)),
            json.dumps(
                [{"number": 78, "state": "closed", "merged": True,
                  "head": {"ref": "refs/pull/78/head", "label": "owner:" + LANDED}}]
            ),
        ],
    )
    out = _prune(repo, stub, "--delete")
    assert out.returncode == 0, out.stderr
    assert LANDED not in _branches(repo), out.stdout
    assert UNLANDED in _branches(repo), out.stdout


def test_the_keep_verdict_cites_only_the_prs_it_actually_searched(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A verdict may claim what it measured. `hub lists NO merged PR` was a claim about the forge.

    Worth doing regardless of pagination, per the ticket: the old sentence was unsupportable by a
    windowed query even when the window happened to contain the answer. The count is asserted as a
    NUMBER the run printed -- 51 closed PRs across the two pages -- not as a phrase, so a message
    that says "searched" while counting nothing goes red.
    """
    stub = _paged_stub(tmp_path, [json.dumps(_filler(50)), NOTHING_MERGED])
    out = _prune(repo, stub, "--dry-run")
    assert out.returncode == 0, out.stderr
    keep = [ln for ln in out.stdout.splitlines() if "KEEP" in ln and UNLANDED in ln]
    assert keep, out.stdout
    assert "51 closed PRs searched" in keep[0], keep[0]
    assert "hub lists NO merged PR" not in out.stdout, "the unsupportable claim is back"


def test_hitting_the_page_cap_is_said_out_loud(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The cap exists for a forge that IGNORES `page` -- and a capped run must not sound exhaustive.

    Fail-safe is indistinguishable from success: stopping early is the SAFE direction (it keeps), so
    nothing else would ever reveal it. With the cap at 1 the run sees only page 1, LANDED survives,
    and the line must say the older PRs were not searched.
    """
    stub = _paged_stub(
        tmp_path,
        [
            json.dumps(_filler(50)),
            json.dumps(
                [{"number": 78, "state": "closed", "merged": True,
                  "head": {"ref": "refs/pull/78/head", "label": "owner:" + LANDED}}]
            ),
        ],
    )
    out = _prune(repo, stub, "--delete", PRUNE_PAGE_MAX="1")
    assert out.returncode == 0, out.stderr
    assert LANDED in _branches(repo), "the cap did not stop the loop"
    keep = [ln for ln in out.stdout.splitlines() if "KEEP" in ln and LANDED in ln]
    assert keep, out.stdout
    assert "50 closed PRs searched" in keep[0], keep[0]
    assert "NOT searched" in keep[0], keep[0]


def test_pr_queue_verb_dispatches_and_defaults_to_dry_run(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """`pr-queue.sh prune-merged` with no flag must not delete: the wrong default is unrecoverable.

    The verb `cd`s to the real consumer checkout, so it is not run here; what is asserted is the two
    properties that make it safe — the bare form passes --dry-run, and only --delete passes --delete.
    """
    src = PR_QUEUE.read_text()
    assert 'if [ "${1:-}" = "prune-merged" ]; then' in src, "verb gone — test would be vacuous"
    block = src[src.index('= "prune-merged"'):]
    block = block[: block.index("QUEUE=$(cat)")]
    assert '--delete) shift; exec env PRUNE_REPO="$REPO" sh "$PRUNE" --delete' in block
    assert '*)                exec env PRUNE_REPO="$REPO" sh "$PRUNE" --dry-run' in block


def test_both_pruner_call_sites_carry_the_target_repo(tmp_path: pathlib.Path) -> None:
    """Every invocation of the pruner passes `PRUNE_REPO`, or `--repo` silently asks the wrong repo.

    Asserted over ALL call sites rather than the two named above, because the defect the section
    below records was in the one this file's other test never looked at: `delete_merged_branch`, the
    invocation a real drain actually takes. A wiring test that pins the call site it was written
    for leaves every sibling free to drift, which is exactly what happened.
    """
    src = PR_QUEUE.read_text()
    calls = [l.strip() for l in src.splitlines() if 'sh "$PRUNE"' in l or 'sh "$_prune"' in l]
    assert len(calls) == 3, "pruner call sites moved; re-read them: %r" % calls
    unguarded = [c for c in calls if "PRUNE_REPO=" not in c]
    assert not unguarded, "these reach the pruner without naming the repo: %r" % unguarded


def test_a_flag_after_the_branch_names_is_refused_not_treated_as_a_branch(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """THE REGRESSION. `case` reads only `$1`, so `prune-merged <branch> --delete` used to put
    `--delete` into BRANCHES and leave MODE=hook -- the ADVISORY mode, which exits 0 and deletes
    nothing. Measured 2026-08-30 while retiring a landed branch by hand:

        [prune-landed-branches-forgejo] KEEP        --delete — no merged PR with this head branch
                                                    among the 304 closed PRs searched

    A confident per-branch verdict about a branch that does not exist, while the real one was never
    examined and survived. The exit code was 0 and the branch was still there.

    Both arms matter. The exit code alone would pass on a script that rejected everything, and the
    absence of a KEEP line alone would pass on a script that crashed."""
    out = _prune(repo, _stub(tmp_path, PAYLOAD), LANDED, "--delete")

    assert out.returncode == 2, f"a misplaced flag must be an error, not a branch: {out.stdout}"
    assert "must come BEFORE the branch names" in out.stderr, out.stderr

    # THE USAGE HALF MUST NAME A COMMAND SOMEONE CAN TYPE. Shipped wrong the first time: the
    # message interpolated `$SELF` twice, so the example read
    # `[prune-landed-branches-forgejo] [--dry-run|--delete] [branch...]` -- a bracketed LOG TAG
    # where argv[0] belongs. It passed review and CI because the assertion above only checks the
    # sentence, and a usage line is exactly the text nobody re-reads. Asserted structurally rather
    # than as a fixed string, so it catches the class and not just that instance.
    assert "pr-queue prune-merged" in out.stderr, (
        f"the usage half must name the command a session types: {out.stderr!r}")
    assert out.stderr.count("[prune-landed-branches-forgejo]") == 1, (
        f"the log prefix belongs once, at the front -- not inside the usage: {out.stderr!r}")
    assert "--delete" not in out.stdout, "the flag must never get a per-branch verdict"
    assert "KEEP" not in out.stdout, "no verdict at all should be printed"
    # And nothing was touched -- the refusal is before any deletion.
    assert {LANDED, UNLANDED} <= _branches(repo)


def test_the_documented_order_still_works(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """THE CONTROL for the test above. Without it, a guard that rejected every invocation with a
    branch argument would pass -- and rejecting the correct calling convention is a worse bug than
    the one being fixed."""
    out = _prune(repo, _stub(tmp_path, PAYLOAD), "--delete", LANDED)

    assert out.returncode == 0, out.stderr
    assert LANDED not in _branches(repo), out.stdout
    assert UNLANDED in _branches(repo), "an explicit branch list must not judge anything else"


# ---------------------------------------------------------------------------
# WHICH REPO THE VERDICT WAS REACHED FROM
#
# The pruner derived its repo from the forge remote of the checkout it ran in. That is right while
# the branch belongs to that checkout's repo and wrong under `pr-queue.sh --repo <target>`, where
# the queue runs from one tree and merges on the target: a real drain printed
# `KEEP agent/... among the 823 closed PRs searched` -- the running tree's history -- about a
# target repo's branch, for a repo holding 13 PRs in total.
#
# None of the tests above could see it. Every one of them asserts on stdout or on branches, and a
# search of the wrong repo produces a well-formed verdict either way; what no test captured was the
# API PATH the stub was asked for. So these record it.
# ---------------------------------------------------------------------------


def _recording_stub(tmp_path: pathlib.Path, payload: str) -> tuple[pathlib.Path, pathlib.Path]:
    """A stub that writes down every path it was asked for, then answers normally.

    The recording is the whole point: `REPO_PATH` reaches the forge only as a path segment, so the
    repo a verdict was reached from is invisible to an assertion on stdout or on branches -- which
    is why the defect survived every existing test in this file.
    """
    log = tmp_path / "asked.txt"
    s = tmp_path / "hub-api-recording-stub.sh"
    s.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$1\" >> " + str(log) + "\ncat <<'PAYLOAD_EOF'\n"
        + payload + "\nPAYLOAD_EOF\n"
    )
    s.chmod(0o755)
    return s, log


def test_prune_repo_overrides_the_remote_derivation(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """With PRUNE_REPO set, the forge is asked about THAT repo and not the checkout's remote.

    The fixture's `hub` remote is `owner/repo`; a cross-repo drain is asking about somewhere else
    entirely. Both arms are asserted -- the target appears AND `owner/repo` does not -- because a
    pruner that queried both would satisfy the first alone while still reading the wrong history.
    """
    stub, log = _recording_stub(tmp_path, PAYLOAD)
    out = _prune(repo, stub, "--dry-run", PRUNE_REPO="acme/app")
    assert out.returncode == 0, out.stderr
    asked = log.read_text()
    assert "/repos/acme/app/pulls" in asked, asked
    assert "owner/repo" not in asked, asked


def test_without_prune_repo_the_remote_still_decides(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The in-repo case, unchanged by the override.

    PASSES ON BASE: it must. The fix inserts a branch AHEAD of the remote derivation, and the risk
    of inserting it is that the ordinary same-repo run -- every PostToolUse hook firing, and every
    drain without `--repo` -- stops using the remote it has always used. This pins the path the fix
    must not disturb, so it is a test of the insertion, not of the feature.
    """
    stub, log = _recording_stub(tmp_path, PAYLOAD)
    out = _prune(repo, stub, "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "/repos/owner/repo/pulls" in log.read_text(), log.read_text()


def test_a_malformed_prune_repo_refuses_rather_than_falling_back(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A caller that names a repo badly must not be served the remote's answer instead.

    Falling back would be the friendly choice and the wrong one: the caller stated which repo it
    meant, so quietly answering about a different one reproduces the wrong-repo verdict with the
    intent reversed.
    It refuses and deletes nothing.
    """
    stub, log = _recording_stub(tmp_path, PAYLOAD)
    before = _branches(repo)
    out = _prune(repo, stub, "--delete", PRUNE_REPO="not-a-repo")
    assert "PRUNE_REPO" in (out.stdout + out.stderr), out.stdout + out.stderr
    assert not log.exists() or log.read_text() == "", "it asked the forge anyway"
    assert _branches(repo) == before, "it deleted something while refusing"


def test_the_keep_verdict_names_the_repo_it_searched(
    repo: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A KEEP must say WHERE it looked, or it reads identically right and wrong.

    This is the half that made the wrong-repo verdict cost a human two logs side by side: the line carried a
    count and no repo, and `among the 823 closed PRs searched` is only implausible to a reader who
    already knows which repo was meant.
    """
    # PAYLOAD, not NOTHING_MERGED: a page where nothing merged takes the early exit above the
    # verdict loop, so a KEEP-line assertion against it would be asserting on an arm that never ran.
    stub, _ = _recording_stub(tmp_path, PAYLOAD)
    out = _prune(repo, stub, "--dry-run", PRUNE_REPO="acme/app")
    keep = [l for l in out.stdout.splitlines() if "KEEP" in l]
    assert keep, out.stdout
    assert all("acme/app" in l for l in keep), keep
