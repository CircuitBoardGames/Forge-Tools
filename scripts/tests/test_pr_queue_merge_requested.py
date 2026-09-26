"""`merge-requested` must never merge on the strength of a listing alone.

THE DEFECT THIS GATES is a measured Forgejo fail-open, not a hypothetical. On hub
2026-08-24, `GET /repos/{o}/{r}/issues?labels=<name>` applies the filter when the name
resolves and **ignores it entirely when it does not**:

    labels=ready-for-agent   ->  8   filter applied
    labels=needs-triage      ->  4   filter applied
    labels=queue:needs-human-review        ->  0   name resolves, on nothing -- correctly empty
    labels=zz-no-such-label  -> 28   the ENTIRE unfiltered set

So a typo, a rename, or a label nobody has created yet turns "merge what is labelled" into
"merge everything open". Note which arm is dangerous: the label that exists but matches
nothing behaves *correctly*, so a suite covering only the happy path and the empty path
would never see this.

`pr-queue.sh` answers it twice, and both lines are tested here:
  1. resolve the label id first -- no id, no listing, no merge;
  2. re-read each returned PR's own labels -- the id proves the NAME resolves, it does not
     prove the server applied the filter to THIS response.

THE POSITIVE CONTROL IS LOad-BEARING. `test_a_labelled_pr_is_merged` is what makes the two
refusal tests mean anything: without it, a stub that merged nothing under any condition
would pass every other assertion in this file.

These run against a stub `hub-api.sh` and a throwaway git repo -- no forge, no network.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PR_QUEUE = REPO / "scripts/pr-queue.sh"

MERGE_LABEL = "queue:merge-requested"

# The stub dispatches on the request the script actually makes. Order matters: the per-issue
# label path is a prefix-sibling of the repo-wide label path.
STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG"
case "$1" in
  pr)
    case "$2" in
      checks) exit 0 ;;                       # green on the first poll
      merge)  printf 'http=200\n'; exit 0 ;;   # `pr merge` prints its code and exits 0
    esac
    exit 0 ;;
  issue)
    # `label_id` resolves through the client's verb now, not a raw /labels fetch.
    # RESOLVE-OR-REFUSE, mirroring the real verb: an id on stdout and 0, or NOTHING and 2.
    # Printing a diagnostic on stdout here would hand the caller a truthy id -- which is the
    # fail-open this whole file is about, and is what a looser stub let through once.
    case "$2" in
      label-id)
        # A MISBEHAVING CLIENT, on demand. `STUB_LABEL_ID_NOISE` makes the verb print a
        # diagnostic to STDOUT and exit 0 for an unknown name -- what a client would do if a
        # future edit sent `die()` to the wrong stream. The caller reads any non-empty stdout as
        # a resolved id, so without pr-queue's digit check this is a merge of every open PR.
        if [ -n "${STUB_LABEL_ID_NOISE:-}" ]; then
          printf 'REFUSING: no label %s\n' "$4"; exit 0
        fi
        printf '%s\n' "$STUB_REPO_LABELS" | python3 -c '
import json, sys
want = sys.argv[1]
for l in json.load(sys.stdin) or []:
    if l.get("name") == want:
        print(l["id"]); raise SystemExit(0)
raise SystemExit(2)' "$4" ;;
      *) printf '{}\n' ;;
    esac
    exit $? ;;
esac
case "$1" in
  */wiki/pages)  printf '[]\n' ;;         # no freeze stands; unanswered would refuse
  */issues/*/labels*)
    # A DELETE retires the labels for later reads, so the cleanup read-back is honest.
    for a in "$@"; do [ "$a" = DELETE ] && : > "$STUB_STATE/deleted"; done
    if [ -f "$STUB_STATE/deleted" ]; then printf '[]\n'; else printf '%s\n' "$STUB_ISSUE_LABELS"; fi ;;
  */labels)      printf '%s\n' "$STUB_REPO_LABELS" ;;
  */issues\?*)
    # PAGE-AWARE ON DEMAND. With STUB_LISTING_PAGES set (a JSON array of pages) this
    # honours `page=`; otherwise it answers STUB_LISTING however it is asked, which is what every
    # other test here wants. A stub that ignored `page` would make a paging assertion pass against
    # a pager that never paged -- the trap the client's own listing test names explicitly.
    if [ -n "${STUB_LISTING_PAGES:-}" ]; then
      _pg=$(printf '%s' "$1" | sed -n 's/.*[?&]page=\([0-9][0-9]*\).*/\1/p')
      [ -n "$_pg" ] || _pg=1
      printf '%s\n' "$STUB_LISTING_PAGES" | python3 -c '
import json, sys
pages = json.load(sys.stdin)
i = int(sys.argv[1])
print(json.dumps(pages[i - 1] if 1 <= i <= len(pages) else []))' "$_pg"
    else
      printf '%s\n' "$STUB_LISTING"
    fi ;;
  */pulls/*)     printf '%s\n' "$STUB_PULL" ;;
  *)             printf '{}\n' ;;
esac
exit 0
"""

PULL = json.dumps(
    {"title": "feat: a title", "head": {"sha": "a" * 40, "ref": "feat"}, "state": "open"}
)
LABEL_EXISTS = json.dumps([{"id": 65, "name": "queue:needs-human-review"}, {"id": 70, "name": MERGE_LABEL}])
LABEL_MISSING = json.dumps([{"id": 65, "name": "queue:needs-human-review"}])
CARRIES = json.dumps([{"id": 70, "name": MERGE_LABEL}])
CARRIES_NOT = json.dumps([{"id": 65, "name": "queue:needs-human-review"}])


@pytest.fixture
def env(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    subprocess.run(["git", "init", "-q", str(hub)], check=True)
    for kv in (["user.email", "t@t"], ["user.name", "t"]):
        subprocess.run(["git", "config", *kv], cwd=hub, check=True)
    (hub / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=hub, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=hub, check=True)
    # No `hub` remote on purpose: `git rev-parse hub/main` then fails, the freshness guard
    # skips, and `pr_head` returns nothing -- so the merge path is reached without a forge.

    api = tmp_path / "stub-api.sh"
    api.write_text(STUB)
    api.chmod(0o755)
    log = tmp_path / "stub.log"
    log.write_text("")
    state = tmp_path / "state"
    state.mkdir()
    return {
        "log": log,
        "env": {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "PR_QUEUE_HUB": str(hub),
            "PR_QUEUE_TOOLS_DIR": str(hub / "scripts"),  # siblings stay the fixture's
            "FORGE_TOOLS_REMOTE": "hub",  # the fixture's forge remote, the name pr-queue.sh defaulted to
            "PR_QUEUE_WT": str(tmp_path / "merge-wt"),
            "PR_QUEUE_REPO": "owner/repo",
            "PR_QUEUE_API": str(api),
            "PR_QUEUE_REWAIT_SECS": "0",
            "PR_QUEUE_REWAIT_POLL_SECS": "0",
            "PR_QUEUE_POLL_SECS": "0",
            "STUB_LOG": str(log),
            "STUB_STATE": str(state),
            "STUB_PULL": PULL,
            "STUB_REPO_LABELS": LABEL_EXISTS,
            "STUB_LISTING": json.dumps([{"number": 5}]),
            "STUB_ISSUE_LABELS": CARRIES,
        },
    }


def _run(env, **extra):
    return subprocess.run(
        ["sh", str(PR_QUEUE), "merge-requested"],
        input="", capture_output=True, text=True,
        env={**env["env"], **extra},
    )


def test_a_labelled_pr_is_merged(env):
    """POSITIVE CONTROL. Without this, every refusal below is satisfied by merging nothing ever."""
    r = _run(env)
    calls = env["log"].read_text()
    assert "pr merge" in calls, f"a correctly labelled PR was not merged:\n{calls}"
    assert r.returncode == 0, r.stdout + r.stderr
    assert "merged 1 of 1" in r.stdout, r.stdout


def test_an_unresolvable_label_refuses_instead_of_merging_everything(env):
    """The first line: no id, no listing. This is the arm that would merge every open PR."""
    r = _run(env, STUB_REPO_LABELS=LABEL_MISSING)
    calls = env["log"].read_text()
    assert "pr merge" not in calls, f"merged despite an unresolvable label:\n{calls}"
    assert "issues?" not in calls, f"listed PRs before proving the label exists:\n{calls}"
    assert r.returncode == 2, r.stdout + r.stderr
    assert "does not resolve" in r.stdout, r.stdout


def test_a_client_that_prints_its_refusal_to_stdout_still_refuses(env):
    """`label_id` resolves through `hub-api.sh issue label-id` now, and the caller reads
    ANY non-empty stdout as a resolved id.

    THIS ARM WAS A LIVE REGRESSION, not a hypothetical. Routing through the verb without a digit
    check re-opened the exact fail-open this file exists to close: the listing ran and every open
    PR was returned. It was caught by the test above, and then the fix made that test stop
    exercising it -- a well-behaved stub never produces the bad output, so the guard became
    unfalsifiable. Removing the digit check left the whole file green.

    So the stub is made to misbehave deliberately: print a diagnostic to STDOUT and exit 0, which
    is what a client would do if a future edit sent `die()` to the wrong stream. Only an
    all-digits id may count as resolved.
    """
    r = _run(env, STUB_LABEL_ID_NOISE="1")
    calls = env["log"].read_text()
    assert "pr merge" not in calls, f"merged on a non-numeric 'id':\n{calls}"
    assert "issues?" not in calls, f"listed PRs on a non-numeric 'id':\n{calls}"
    assert r.returncode == 2, r.stdout + r.stderr
    assert "does not resolve" in r.stdout, r.stdout


def test_a_listing_that_failed_open_is_caught_by_the_read_back(env):
    """The second line: the label resolves, but the returned PR does not carry it.

    This is the live fail-open's exact shape -- the server dropped the filter and answered
    with everything. The id check cannot see it, because the name is fine.
    """
    r = _run(env, STUB_ISSUE_LABELS=CARRIES_NOT)
    calls = env["log"].read_text()
    assert "pr merge" not in calls, f"merged a PR that does not carry the label:\n{calls}"
    assert "does not carry it" in r.stdout, r.stdout
    assert r.returncode == 0, r.stdout + r.stderr
    assert "merged 0 of 1" in r.stdout, r.stdout


def test_the_listing_is_paged_past_the_first_page(env):
    """This was ONE call with `limit=50`: past the cap it drained a silently partial list
    and reported success, in the verb whose whole job is not to miss requested work.

    SMALL PAGES ARE HONEST HERE because the stop condition is "this page added nothing new", not
    "the page was short" -- so 3+2 exercises the same code path 50+10 would, without 60 stub merges.
    A pager keyed on a short page would need the real cap, which for `type=pulls` is UNMEASURED.

    ASSERTED ON THE SEEN COUNT, not on merges, and that is a property of the stub rather than a
    softening. Clearing the label after the first merge writes ONE global `deleted` marker, so every
    later label read returns `[]` and #2-#5 are skipped by the read-back guard. The trailer's "of N"
    is the number the listing yielded, which is exactly the quantity paging changes: unfixed reads
    page 1 only and says "of 3"; paged says "of 5".
    """
    pages = json.dumps([[{"number": 1}, {"number": 2}, {"number": 3}],
                        [{"number": 4}, {"number": 5}],
                        []])
    r = _run(env, STUB_LISTING_PAGES=pages)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "of 5 labelled PR(s)" in r.stdout, f"the second page was never read:\n{r.stdout}"
    assert "of 3 labelled PR(s)" not in r.stdout, "stopped after page 1"


def test_a_forge_that_ignores_page_stops_instead_of_looping(env):
    """The other half of the stop condition. If `page=` is ignored the same rows come back for
    ever; stopping on "no NEW numbers" ends that cleanly, where a short-page test would spin to the
    ceiling and a naive accumulator would merge each PR repeatedly."""
    same = [{"number": 1}, {"number": 2}, {"number": 3}]
    r = _run(env, STUB_LISTING_PAGES=json.dumps([same, same, same, same]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "of 3 labelled PR(s)" in r.stdout, f"duplicates reached the merge loop:\n{r.stdout}"
    # Bounded: it must not have walked to the 40-page ceiling to discover this.
    listings = [l for l in env["log"].read_text().splitlines() if "issues?" in l]
    assert len(listings) <= 3, f"kept paging a forge that was repeating itself: {len(listings)}"


def test_an_empty_listing_says_so_rather_than_reporting_a_drain(env):
    """Nothing requested is a real answer, not the success of skipped work."""
    r = _run(env, STUB_LISTING="[]")
    calls = env["log"].read_text()
    assert "pr merge" not in calls, calls
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing requested" in r.stdout, r.stdout


def test_an_unreadable_listing_is_not_an_empty_one(env):
    """A parse failure must stop, not read as 'no PRs are labelled'."""
    r = _run(env, STUB_LISTING="not json")
    calls = env["log"].read_text()
    assert "pr merge" not in calls, calls
    assert r.returncode == 2, r.stdout + r.stderr
    assert "cannot read the PR listing" in r.stdout, r.stdout


# --- the hold binds a /merge request too (operator decision 2026-09-03) ------------------

CARRIES_AND_HELD = json.dumps(
    [{"id": 70, "name": MERGE_LABEL}, {"id": 65, "name": "queue:needs-human-review"}]
)


def test_a_HELD_pr_is_NOT_merged_by_a_merge_request(env):
    """THE DECISION, AND NOTHING PINNED IT BEFORE. The ticket said it outright -- "no test covers
    `/merge` on a held PR" -- and that was measured true: this change altered the behaviour and the
    28 existing tests in this area passed either way.

    A hold is honoured unless the NEED for it changes, or a HUMAN approves the merge itself (the
    Forgejo web UI, or `approve` on the terminal). A `/merge` comment is none of those three: it is
    written elsewhere, possibly before the hold existed, by someone who may never have seen it, and
    the commenter and the holder can be different people.
    """
    r = _run(env, STUB_ISSUE_LABELS=CARRIES_AND_HELD)
    calls = env["log"].read_text()
    assert "pr merge" not in calls, f"a held PR was merged on a /merge comment:\n{calls}"
    assert "NOT merging" in r.stdout, r.stdout
    assert "A /merge comment does NOT discharge a hold" in r.stdout, "the refusal must say why a /merge comment is not enough"


def test_the_refusal_names_every_way_OUT_of_the_hold(env):
    """A refusal a reader cannot act on becomes a refusal someone routes around -- which is the
    habit measured in practice: merging past a hold, leaving a merged PR carrying a false
    `needs-human-review` artifact. All three exits are named, and `unhold` is the one for "the need
    for review went away", which is the reason that verb exists at all."""
    r = _run(env, STUB_ISSUE_LABELS=CARRIES_AND_HELD)
    assert "pr unhold" in r.stdout, r.stdout
    assert "pr-queue approve" in r.stdout, r.stdout
    assert "web UI" in r.stdout, r.stdout


def test_a_hold_SKIPS_its_pr_rather_than_stopping_the_whole_run(env):
    """Same answer `drain` gives a late hold, and for the same reason: this verb walks every PR
    carrying the request label, so stopping at a held one would let one hold block merges other
    humans asked for. The hold binds the PR it is on and nothing else.

    Both PRs are held here, so "merged 0 of 2" is itself the proof it walked past the first: a stop
    exits with the return code and never reaches the summary line at all."""
    r = _run(env, STUB_ISSUE_LABELS=CARRIES_AND_HELD,
             STUB_LISTING=json.dumps([{"number": 5}, {"number": 6}]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "merged 0 of 2" in r.stdout, r.stdout
    assert "2 SKIPPED, held for review" in r.stdout, (
        "the held count must be reported -- 'merged 0 of 2' alone reads as a broken verb rather "
        "than as the hold working: " + r.stdout)
