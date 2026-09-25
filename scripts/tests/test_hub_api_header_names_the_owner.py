"""`hub-api.sh`'s header must document the owner the client actually talks to.

WHY THIS EXISTS. Two coordinator sessions in one shift independently guessed `claude` as the owner
segment of an API path, because `HUB_USER` defaults to `claude`, it authors every commit and every
issue on the forge, and nothing beside it said it was not the owner. The wrong guess returns a
well-formed 404 -- {"message":"The target couldn't be found."} -- which names no owner and reads as a
missing endpoint rather than a bad argument. The header block this test defends says so.

DERIVED, NOT RECALLED. The expected owner comes from the FORGE_TOOLS_REPO example line of
config.example, the one place this repository documents what a repo setting looks like. If that
example changes, this test fails until the header's examples follow. Hardcoding an owner here
would re-encode the recall the block is about, and would pass just as happily against a header that
had gone stale with it.

SCOPED TO THE HEADER COMMENT, deliberately. The body carries `/api/v1/repos/issues/search`, a real
CROSS-REPO endpoint whose path has no owner segment at all -- a naive scan reads `issues` as the owner
and fails for a reason that has nothing to do with the defect. The header is also exactly where a
reader looks, so widening the scan would test a different claim.

DELIBERATELY NOT ASSERTED: that the owner is correct *against a forge*. That would need a live
remote, and would measure the runner's checkout URL rather than anything this repository says.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_API = REPO_ROOT / "scripts" / "hub-api.sh"
CONFIG_EXAMPLE = REPO_ROOT / "config.example"

# Everything above `set -eu` is the header comment: the verb list, the token-containment rule and
# the owner block. Splitting on it rather than a line number so an edit above cannot silently
# shrink the corpus this test scans.
def header_of(path):
    text = path.read_text()
    head, sep, _ = text.partition("\nset -eu")
    assert sep, "%s has no `set -eu`, so the header could not be delimited" % path.name
    return head


def expected_owner_repo():
    """`#FORGE_TOOLS_REPO=<owner>/<repo>`, the example line in config.example."""
    m = re.search(r'^#?FORGE_TOOLS_REPO=([^/\s]+)/([^/\s]+)$', CONFIG_EXAMPLE.read_text(), re.M)
    assert m, "config.example no longer shows FORGE_TOOLS_REPO=<owner>/<repo>"
    return m.group(1), m.group(2)


def hub_user_default():
    m = re.search(r'HUB_USER="\$\{HUB_USER:-([^}"]+)\}"', HUB_API.read_text())
    assert m, "hub-api.sh no longer declares a HUB_USER default in the expected shape"
    return m.group(1)


# PRESCRIPTIVE examples only -- those written as an INVOCATION, `hub-api.sh "<path>"`. The header
# also shows the WRONG path in prose, on purpose, because naming the failing guess is most of what
# the block teaches. The first version of this test scanned every `/api/v1/repos/...` in the header
# and went red on that counter-example: a check firing on exactly the text it exists to protect.
# Distinguishing them by SYNTAX beats an allowlist, which would have had to hardcode `claude` --
# re-encoding the recall the block is about, inside the test written to prevent it.
INVOCATION = re.compile(r'hub-api\.sh\s+"?/api/v1/repos/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)')


def test_every_prescribed_path_in_the_header_names_the_real_owner():
    owner, repo = expected_owner_repo()
    found = INVOCATION.findall(header_of(HUB_API))
    # An empty corpus reports clean, so the absence of examples is itself a failure: the header
    # documents the owner by SHOWING it, and a header with no runnable example teaches nothing.
    assert found, "the hub-api.sh header prescribes no `hub-api.sh /api/v1/repos/<owner>/<repo>` call"
    wrong = sorted({"%s/%s" % (o, r) for o, r in found if (o, r) != (owner, repo)})
    assert not wrong, "header prescribes %s; config.example shows %s/%s" % (wrong, owner, repo)


def test_the_header_says_the_auth_account_is_not_the_owner():
    """The whole defect: `HUB_USER` is the natural guess and it is wrong.

    Asserting the two are DIFFERENT, not that the header contains a sentence -- a prose match would
    pass on a paraphrase that had lost the point, and fail on a better one that kept it.
    """
    owner, _ = expected_owner_repo()
    assert hub_user_default() != owner, (
        "HUB_USER now equals the owner, so the header's warning is false and must be removed")
