"""handoff.sh — the read-back is the whole point, so these tests attack the read-back.

`scripts/handoff.sh` resolves `hub-api.sh` as `$(dirname $0)/hub-api.sh`. That is the seam: copying
the script into a tmpdir beside a STUB hub-api.sh exercises every path without touching the forge.

WHAT IS DELIBERATELY NOT TESTED, stated rather than left to look like coverage: that the real
Forgejo accepts these requests, that a wiki write dispatches no CI, and that the wiki is absent from
the GitHub mirror. None of those are properties of this script and no stub can establish them; they
were measured against a live forge. A green run here means the script's
LOGIC is right, not that the surface behaves.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

# THIS checkout's handoff.sh, by path: it finds hub-api.sh, ft-config.sh and its other files beside
# itself, so the harness copies those siblings too (hub-api.sh excepted: that is the stub).
REAL = Path(__file__).resolve().parents[1] / "handoff.sh"
FT_SIBLINGS = [p for p in REAL.parent.iterdir() if p.is_file() and p.name not in ("handoff.sh", "hub-api.sh")]
# Site values as a consumer's config would set them; the harness runs with HOME=/nonexistent, so no
# config file is read and they are passed here.
FT_SITE = {"FORGE_TOOLS_FORGE_URL": "https://forge.example.org",
           "FORGE_TOOLS_REPO": "o/r",
           "FORGE_TOOLS_INJECTED_PAGES": "Session Cache,Hot Cache",
           "FORGE_TOOLS_TRIGGER_PAGES": "TODO",
           "FORGE_TOOLS_REMOTE": "hub"}
# The cache budget lives in the CONSUMER's session-start injector, which Forge-Tools does not ship:
# publish reads CACHE_CLIFF_CHARS and CACHE_NOTICE_RESERVE_CHARS out of whatever HANDOFF_CACHE_HOOK
# names. A stub with a consumer's real values (10,000 and 700: a ceiling of 9,300) stands in for it.
STUB_HOOK = "CACHE_CLIFF_CHARS=10000\nCACHE_NOTICE_RESERVE_CHARS=700\n"


def _cache_hook(script):
    hook = Path(script).parent / "cache-inject-stub.sh"
    if not hook.exists():
        hook.write_text(STUB_HOOK)
    return str(hook)


def _harness(tmp_path, pages, page_content=None, pages_after=None):
    """Lay out handoff.sh next to a stub hub-api.sh that serves `pages`/`page_content`.

    The stub `cat`s canned response files rather than embedding JSON in shell quoting: the
    escaping needed for the latter is its own source of bugs, and a test harness that is hard to
    read is a test harness nobody checks.
    """
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "handoff.sh").write_text(REAL.read_text())
    for sib in FT_SIBLINGS:
        (d / sib.name).write_bytes(sib.read_bytes())
    (d / "pages.json").write_text(json.dumps(pages))

    if page_content is None:
        # The real client's shape for a missing page: forge 404 JSON on stdout, non-zero exit.
        page_case = """printf '%s' "$(cat "$HERE/404.json")"; exit 22"""
        (d / "404.json").write_text(json.dumps({"message": "The target could not be found."}))
    else:
        page_case = """cat "$HERE/page.json" """
        (d / "page.json").write_text(
            json.dumps({"content_base64": base64.b64encode(page_content).decode()})
        )

    # `pages_after` is the listing the forge serves once a DELETE has landed. Supplying it models a
    # deletion that really took; withholding it models one the forge accepted and did not apply,
    # which is the case the read-back exists to catch and the only way to test that it can fail.
    if pages_after is not None:
        (d / "pages-after.json").write_text(json.dumps(pages_after))

    (d / "hub-api.sh").write_text(
        textwrap.dedent("""\
        #!/bin/sh
        # $1 is the API path; writes are swallowed. A DELETE is RECORDED rather than swallowed --
        # "the script made no call at all" is the assertion for every refusal test, and a stub
        # that silently accepts everything cannot distinguish a refusal from a success.
        HERE=$(dirname "$0")
        case "$1" in
          */wiki/pages|*/wiki/pages\?*)  cat "$HERE/pages.json" ;;
          */wiki/page/*)
            case "$*" in
              *"-X DELETE"*)
                echo "$1" >> "$HERE/deleted.log"
                # `if`, not `[ -f ... ] && cp`: as the LAST command in this branch the `&&` form
                # exits 1 whenever pages-after.json is absent, so the stub reports a FAILED delete
                # and the script never reaches its read-back. The read-back test then passes on
                # the wrong error and the branch it claims to cover is never executed.
                if [ -f "$HERE/pages-after.json" ]; then
                    cp "$HERE/pages-after.json" "$HERE/pages.json"
                fi
                ;;
              *) %s ;;
            esac ;;
          */wiki/new)    : ;;
        esac
        """) % page_case
    )
    return d / "handoff.sh"


def _run(script, *args, **env):
    """HOME is overridden so the script's default file path cannot resolve to a real handoff --
    a test that silently read the operator's live document would be measuring the wrong thing.

    PATH IS INHERITED, NOT INVENTED. An earlier version hardcoded `/usr/bin:/bin:/usr/local/bin`,
    which happened to contain `python3` on this box and did not on the CI runner, so `digest()`
    found no interpreter and every comparison failed. Green here, red there, and the local run was
    not the same measurement as the remote one.
    """
    # HANDOFF_LOCK is pinned under the script's own tmp dir. Without it every publish here took the
    # BOX's lock, /tmp/handoff-publish.<repo>.lock -- shared with live sessions and with whoever ran
    # last: a 644 file owned by another user is "Permission denied" and a REFUSING, which is how ten
    # tests here went red on the runner on 2026-09-12 after another user's publish.
    # HANDOFF_CACHE_HOOK points the copied script at the REAL injector. `publish` derives an
    # injected page's ceiling from `wiki-cache-inject.sh` rather than restating a number, and
    # these tests run a COPY of handoff.sh from a tmp dir where no `.claude/hooks/` exists. Letting
    # the copy find nothing would test a script that cannot see its own budget; pointing it at the
    # real hook keeps the one source of truth these tests exist to protect.
    e = {**FT_SITE, "HOME": "/nonexistent", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
         "HANDOFF_CACHE_HOOK": _cache_hook(script),
         "HANDOFF_LOCK": str(Path(script).parent / "handoff.lock"), **env}
    return subprocess.run(
        ["sh", str(script), *args], capture_output=True, text=True, env=e, timeout=60
    )


PAGE = [{"title": "Coordinator Handoff", "sub_url": "Coordinator-Handoff"}]


def test_verify_identical_when_content_matches(tmp_path):
    content = b"# handoff\n\nbody text\n"
    s = _harness(tmp_path, PAGE, page_content=content)
    f = tmp_path / "draft.md"
    f.write_bytes(content)
    r = _run(s, "verify", str(f))
    assert r.returncode == 0, r.stderr
    assert "IDENTICAL" in r.stdout


def test_trailing_newline_survives_the_read_back(tmp_path):
    """THE REGRESSION THIS PINS. An earlier version carried the remote copy in `REMOTE=$(fetch)`.
    Command substitution strips trailing newlines, so read-back silently lost the final byte of
    every document. It was misdiagnosed as Forgejo normalising the newline -- the forge does no
    such thing -- and nearly 'fixed' by loosening the comparison to ignore EOF newlines, which
    would have weakened a real check to accommodate a bug in the harness around it.

    A page whose stored content ends in a newline must NOT match a local file without one."""
    s = _harness(tmp_path, PAGE, page_content=b"# handoff\n\nbody text\n")  # forge copy: WITH \n
    f = tmp_path / "draft.md"
    f.write_bytes(b"# handoff\n\nbody text")  # local: without
    assert _run(s, "verify", str(f)).returncode != 0

    f.write_bytes(b"# handoff\n\nbody text\n")  # now byte-identical
    r = _run(s, "verify", str(f))
    assert r.returncode == 0, r.stderr
    assert "IDENTICAL" in r.stdout


@pytest.mark.parametrize(
    "local",
    [
        b"# handoff\n\nbody texX\n",  # one character
        b"# handoff\n",  # truncated
        b"# handoff\n\nbody text\nextra\n",  # appended
        b"",  # empty draft
    ],
    ids=["one-char", "truncated", "appended", "empty"],
)
def test_content_differences_are_caught(tmp_path, local):
    """The loosening above must not have swallowed real differences."""
    s = _harness(tmp_path, PAGE, page_content=b"# handoff\n\nbody text")
    f = tmp_path / "draft.md"
    f.write_bytes(local)
    assert _run(s, "verify", str(f)).returncode != 0


def test_absent_page_is_not_reported_as_a_match(tmp_path):
    """The dangerous false positive: no page at all read as 'nothing differs'."""
    s = _harness(tmp_path, [], page_content=None)
    f = tmp_path / "draft.md"
    f.write_bytes(b"anything\n")
    r = _run(s, "verify", str(f))
    assert r.returncode != 0
    assert "IDENTICAL" not in r.stdout


def test_page_address_comes_from_the_forge_not_the_title(tmp_path):
    """The bug that broke the first real publish: Forgejo escapes a literal '-' in a title to '.-',
    so a path built from the title 404s on the page it just created. The sub_url below deliberately
    does not match the title; resolution must still succeed."""
    pages = [{"title": "Coordinator Handoff", "sub_url": "Coordinator-Handoff.-weird"}]
    s = _harness(tmp_path, pages, page_content=b"x")
    f = tmp_path / "draft.md"
    f.write_bytes(b"x")
    assert _run(s, "verify", str(f)).returncode == 0
    assert "Coordinator-Handoff.-weird" in _run(s, "url").stdout


def test_url_refuses_to_guess_when_the_page_is_absent(tmp_path):
    s = _harness(tmp_path, [], page_content=None)
    r = _run(s, "url")
    assert r.returncode != 0
    assert "forge.example.org" not in r.stdout


def _forge_unreachable(script_path, exit_code=7):
    """Replace the API stub with one that answers nothing -- a forge that is down, a wireguard link
    that dropped, a wrong HUB_URL.

    `exit_code=0` is the SECOND shape and is not redundant: a client can exit 0 having produced
    nothing (a proxy returning an empty 200, a truncated read, a `curl` whose output went nowhere).
    Two guards cover the two shapes, and a test that only models the failing exit never reaches the
    one guarding the empty one -- found by injecting the original `[]` coercion and watching the
    suite stay green, which is a finding rather than a pass.
    """
    (Path(script_path).parent / "hub-api.sh").write_text("#!/bin/sh\nexit %d\n" % exit_code)


def test_an_unreachable_forge_says_cannot_tell_not_page_absent(tmp_path):
    """THE NETWORK SIBLING OF THE TEST BELOW, and the same defect.

    Measured before the fix, with a control: `HANDOFF_PAGE='Hot Cache' show` returned 4102 bytes,
    and the same command against an unreachable forge printed `no wiki page "Hot Cache"` -- of a
    page that is injected into every session at launch. One expression did it,
    `json.loads(stdin or "[]")`, which turns an empty response into an empty page LISTING; the API
    call's status was then lost to a pipe, so nothing downstream could tell the two apart.

    This matters beyond tidiness: `session-succeed start` REFUSES to start a successor on this
    signal, and `verify` now distinguishes a deleted page from an unreadable one. Both
    act on the difference.
    """
    s = _harness(tmp_path, PAGE, page_content=b"x")
    _forge_unreachable(s)

    r = _run(s, "show", HANDOFF_PAGE="Coordinator Handoff")
    out = r.stdout + r.stderr
    assert r.returncode == 2, out
    assert "CANNOT TELL" in out
    assert "NOT an absence" in out
    assert "handoff: no wiki page" not in out, "an unreachable forge reported as a missing page"


def test_a_forge_that_answers_NOTHING_but_succeeds_is_also_cannot_tell(tmp_path):
    """THE SECOND SHAPE, and the one the original bug actually lived in.

    `json.loads(stdin or "[]")` turned an EMPTY response into an empty page listing. A client that
    exits non-zero is caught earlier, so a test modelling only that never reaches this guard --
    proven by injecting the `[]` coercion back and watching all 40 tests stay green. An injection
    that fails to fail is a finding: here it said the corpus never drove the mutated line.
    """
    s = _harness(tmp_path, PAGE, page_content=b"x")
    _forge_unreachable(s, exit_code=0)

    r = _run(s, "show", HANDOFF_PAGE="Coordinator Handoff")
    out = r.stdout + r.stderr
    assert r.returncode == 2, out
    assert "CANNOT TELL" in out
    assert "handoff: no wiki page" not in out, (
        "an empty-but-successful response reported as a missing page -- the original defect")


def test_a_genuinely_absent_page_is_still_reported_as_absent(tmp_path):
    """THE CONTROL for the test above. Without it the fix is indistinguishable from 'never say a
    page is missing', which would be a worse defect in the opposite direction -- and it is the one
    a fix written only against the unreachable case would produce."""
    s = _harness(tmp_path, [{"title": "Some Other Page", "sub_url": "Some-Other-Page"}])

    r = _run(s, "show", HANDOFF_PAGE="Coordinator Handoff")
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "no wiki page" in out
    assert "CANNOT TELL" not in out, "a real absence hedged into 'cannot tell'"


def test_missing_python3_says_cannot_check_not_page_absent(tmp_path):
    """The false negative that hid inside the tool built to end false negatives.

    Every JSON/hash step shells out to python3. With none on PATH the page lookup fails, and the
    script used to report `no wiki page ... nothing to compare` -- "I could not tell" delivered in
    the words "it is not there". A seat reading that would conclude the handoff was never published.
    """
    s = _harness(tmp_path, PAGE, page_content=b"x")
    f = tmp_path / "draft.md"
    f.write_bytes(b"x")

    bindir = tmp_path / "nopy"
    bindir.mkdir()
    for tool in ("sh", "cat", "printf", "mktemp", "wc", "rm", "mv", "tr", "dirname"):
        for cand in (Path("/bin") / tool, Path("/usr/bin") / tool):
            if cand.exists():
                (bindir / tool).symlink_to(cand)
                break

    r = _run(s, "verify", str(f), PATH=str(bindir))
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "python3" in out, out
    # Anchored on the REPORT line, not the bare phrase: the guard's own message quotes
    # "no wiki page" while explaining what it refuses to say, so a substring check matches the
    # explanation and fails a correct implementation. (Caught by this test failing green-side-up.)
    assert "handoff: no wiki page" not in out, "absence of an interpreter reported as absence of the page"

    # Control: the identical PATH plus python3 must get past this guard, or the test above
    # would pass for a machine with no `sh` either and prove nothing about python3.
    (bindir / "python3").symlink_to(Path(os.environ.get("_PY3", "/usr/bin/python3")))
    r2 = _run(s, "verify", str(f), PATH=str(bindir))
    assert "no python3" not in (r2.stdout + r2.stderr)


def test_publish_refuses_an_empty_file(tmp_path):
    """Publishing nothing over a good handoff would destroy the only authoritative copy."""
    s = _harness(tmp_path, PAGE, page_content=b"x")
    f = tmp_path / "draft.md"
    f.write_bytes(b"")
    r = _run(s, "publish", str(f))
    assert r.returncode != 0
    assert "refusing" in (r.stdout + r.stderr).lower()


# --- an empty page name is a failed command, not a request for the default -------------------


def test_an_empty_HANDOFF_PAGE_refuses_rather_than_using_the_default(tmp_path):
    """THE INCIDENT THIS EXISTS FOR, 2026-08-29.

    The documented recipe for a per-session handoff is

        HANDOFF_PAGE="$(sh scripts/session-attest.sh page)" sh scripts/handoff.sh publish <file>

    and it ran in a worktree whose checkout predated `session-attest.sh`. The substitution printed
    its error to stderr and produced an EMPTY STRING; `${HANDOFF_PAGE:-Coordinator Handoff}` treats
    unset and empty alike, so the default was supplied silently and a session handoff overwrote the
    SHARED coordinator page — 16,316 bytes replaced by 3,812, recovered only because the wiki is a
    git repository.

    Every guard involved worked and none was positioned to catch it: `session-attest.sh page`
    refuses an unset id, but the script was absent; `publish` verified by read-back and truthfully
    reported byte-identical, to the wrong page. The defect is in the COMPOSITION — a command
    substitution turning a missing dependency into a fallback.
    """
    script = _harness(tmp_path, PAGE, page_content=b"x\n")
    r = _run(script, "show", HANDOFF_PAGE="")
    assert r.returncode == 2, f"an empty page name was accepted:\n{r.stdout}{r.stderr}"
    assert "set but EMPTY" in r.stderr, r.stderr
    assert "Coordinator" not in r.stdout, "it fell back to the shared page anyway"


def test_an_UNSET_HANDOFF_PAGE_still_uses_the_default(tmp_path):
    """The control, and the reason the fix is `-` rather than deleting the default. Unset is the
    ordinary case — the coordinator page — and must be unchanged. Without this arm the test above
    would pass equally against a script that refused every invocation."""
    script = _harness(tmp_path, PAGE, page_content=b"hello\n")
    r = _run(script, "show")
    assert r.returncode == 0, r.stderr
    assert "hello" in r.stdout, r.stdout
# --------------------------------------------------------------------------------------------
# publish says whether the page names the session that wrote it.
#
# THE FAILURE THESE PIN. `session-attest.sh` shipped and nothing called it. Measured
# 2026-08-30, one of six wiki pages carried an attestation block and it was written by the seat that
# built the tool -- so the capability existed, was one line to use, and was not used. The report is
# therefore about DISCOVERABILITY, and these tests assert on both arms because a reporter that
# always fires and one that never fires are equally useless and look identical from one run.

def _publish_ok(tmp_path, body: bytes):
    """Publish `body` through the stub, with the forge echoing it back so publish can succeed."""
    s = _harness(tmp_path, PAGE, page_content=body)
    f = tmp_path / "draft.md"
    f.write_bytes(body)
    # THE STUB ECHOES CANNED BYTES. A real forge returns what was sent; this one returns
    # `page_content`, so a block the script computes at publish can never round-trip
    # through the read-back here. Measure-off for the byte-identity tests; the block has its own.
    r = _run(s, "publish", str(f), HANDOFF_NO_MEASURE="1")
    assert r.returncode == 0, f"publish should succeed: {r.stdout}\n{r.stderr}"
    assert "VERIFIED" in r.stdout
    return r


def test_publish_reports_a_page_that_names_no_session(tmp_path):
    r = _publish_ok(tmp_path, b"# handoff\n\nI merged four PRs tonight.\n")
    assert "carries no attestation block" in r.stderr
    assert "session-attest stamp --session" in r.stderr, "the report must carry the command that fixes it"


def test_publish_reports_a_stamp_that_prose_has_buried(tmp_path):
    """`publish` replaces only the TRAILING stamped block, anchored at EOF, and that is
    deliberate -- an unanchored strip once deleted a QUOTED example out of a page documenting the
    mechanism, and the read-back verified the damage as byte-perfect.

    The cost is that prose written BELOW a stamp does not update it. It buries it, a new stamp is
    appended after the prose, and the page carries a measurement that is no longer true ABOVE the
    live one. Measured on "Handoff CC 1": the buried block still named `agent/918-secrets`, a branch
    that no longer existed.

    REPORTED, NEVER STRIPPED -- stripping is case D again."""
    body = ("# handoff\n\nwork\n\n"
            "<!-- handoff-measured:begin -->\nmeasured-at: OLD\n<!-- handoff-measured:end -->\n\n"
            "<!-- session-attest:begin -->\nattested-session: 45cda3e8-f820-44cf-b5d8-f4544d374c76\n<!-- session-attest:end -->\n\n"
            "## Corrections appended after the stamp\n\nprose\n\n"
            "<!-- handoff-measured:begin -->\nmeasured-at: NEW\n<!-- handoff-measured:end -->\n\n"
            "<!-- session-attest:begin -->\nattested-session: 45cda3e8-f820-44cf-b5d8-f4544d374c76\n<!-- session-attest:end -->\n").encode()
    r = _publish_ok(tmp_path, body)
    assert "no longer trailing" in r.stderr, r.stderr
    assert "buried above the live one" in r.stderr
    # The bytes must NOT be touched: stripping a mid-document block is exactly that damage.
    assert (tmp_path / "draft.md").read_bytes().count(b"measured-at: OLD") == 1, (
        "the buried block was removed -- that is the defect this warning exists INSTEAD of")


def test_publish_is_silent_about_an_archived_predecessor_stamp__control(tmp_path):
    """THE CONTROL, and the one that decides whether this warning is usable at all.

    `slot claim` copies the predecessor page in verbatim between `predecessor-handoff` fences,
    stamps and all -- so a page carrying an archived handoff has stamped blocks above its live one
    BY DESIGN, on every succession. Warning there would fire on the common case, which is how a
    warning teaches its reader to ignore it.

    Measured on the live "Handoff CC 1": two archived pairs inside the fence, one live pair after
    it, and nothing to report."""
    body = ("# handoff\n\nwork\n\n"
            "<!-- predecessor-handoff:begin -->\n"
            "<!-- handoff-measured:begin -->\nmeasured-at: PREDECESSOR\n<!-- handoff-measured:end -->\n\n"
            "<!-- session-attest:begin -->\nattested-session: 45cda3e8-f820-44cf-b5d8-f4544d374c76\n<!-- session-attest:end -->\n"
            "<!-- predecessor-handoff:end -->\n\n"
            "<!-- handoff-measured:begin -->\nmeasured-at: NEW\n<!-- handoff-measured:end -->\n\n"
            "<!-- session-attest:begin -->\nattested-session: 45cda3e8-f820-44cf-b5d8-f4544d374c76\n<!-- session-attest:end -->\n").encode()
    r = _publish_ok(tmp_path, body)
    assert "no longer trailing" not in r.stderr, (
        "an archived predecessor stamp was reported as buried; this fires on every succession: %s"
        % r.stderr)


def test_publish_is_silent_when_the_page_is_attested(tmp_path):
    """THE CONTROL. Without this, a reporter hardcoded to `true` passes the test above."""
    body = (b"# handoff\n\nI merged four PRs tonight.\n\n"
            b"<!-- session-attest:begin -->\n"
            b"attested-session: 45cda3e8-f820-44cf-b5d8-f4544d374c76\n"
            b"attested-pid: 17564\n"
            b"<!-- session-attest:end -->\n")
    r = _publish_ok(tmp_path, body)
    assert "attestation" not in r.stderr, f"attested page must not be reported: {r.stderr!r}"


def test_the_report_does_not_block_the_publish(tmp_path):
    """Repo state -- Session Cache, Hot Cache, TODO -- goes through this same transport and no
    session authors it. The report must never become a refusal for those, so this pins exit 0 and
    a completed read-back on the unattested path specifically."""
    r = _publish_ok(tmp_path, b"# Session Cache\n\nbox state, authored by nobody\n")
    assert "carries no attestation block" in r.stderr
    assert "Publishing anyway" in r.stderr


# --------------------------------------------------------------------------------------------
# publish SIGNS a handoff page, rather than only telling the caller to.
#
# WHY SIGNING AND NOT ONLY REPORTING. The report above is the discoverability half, and what followed is the
# evidence that discoverability is not enough on its own: `session-attest.sh`'s `page` verb NAMES
# `handoff.sh publish` as its call site, in a comment, and nothing ever edited this file. A consumer
# written in prose and never gated is the shape of that miss.
#
# WHY THE DRAFT FILE IS REWRITTEN. `publish` proves it worked by digesting the local file and the
# read-back and demanding they match; `verify` compares draft to page. Stamping only the uploaded
# bytes would make a good publish report MISMATCH and `verify` report DIFFER for ever. These tests
# therefore assert on the FILE and on stderr. They deliberately do NOT assert publish's exit code:
# the stub serves back fixed bytes, so a run that stamps necessarily fails its own read-back inside
# the harness. That is an artefact of the stub, not of the script, and asserting on it would pin the
# harness rather than the behaviour.

def _harness_with_attest(tmp_path, pages, page_content=None):
    """`_harness`, with the REAL `session-attest` reached by command name.

    It comes from the Session-Attest repo: on the forge a pinned-tag checkout puts it on PATH, and
    this asserts rather than skips when it is absent -- a skip would pass every test below having
    signed nothing."""
    assert shutil.which("session-attest"), (
        "the real `session-attest` (Session-Attest repo) must be on PATH for these tests")
    return _harness(tmp_path, pages, page_content=page_content)


SID = "45cda3e8-f820-44cf-b5d8-f4544d374c76"
BEGIN = "<!-- session-attest:begin -->"


def test_publish_stamps_a_handoff_page(tmp_path):
    s = _harness_with_attest(tmp_path, PAGE, page_content=b"x")
    f = tmp_path / "draft.md"
    f.write_bytes(b"# handoff\n\nI merged four PRs tonight.\n")
    r = _run(s, "publish", str(f), FORGE_TOOLS_SESSION_ID=SID)
    body = f.read_text()
    assert body.count(BEGIN) == 1, f"draft must be signed exactly once:\n{body}"
    assert f"attested-session: {SID}" in body
    assert "stamped" in r.stderr
    assert "I merged four PRs tonight." in body, "stamping must not eat the prose"


def test_stamping_replaces_rather_than_accumulates(tmp_path):
    """Republishing four times must leave ONE signature, and the last one must be the true one."""
    s = _harness_with_attest(tmp_path, PAGE, page_content=b"x")
    f = tmp_path / "draft.md"
    f.write_bytes(b"# handoff\n\nbody\n")
    for _ in range(3):
        _run(s, "publish", str(f), FORGE_TOOLS_SESSION_ID=SID)
    assert f.read_text().count(BEGIN) == 1, "a re-publish appended a second block"


def test_repo_state_pages_are_never_stamped(tmp_path):
    """THE SCOPE CONTROL. Without it, a predicate hardcoded to `true` passes every test above.

    `Session Cache`, `Hot Cache` and `TODO` ride this same transport and are REPO state — no session
    authors what the box currently is, so a signature there would claim authorship of a measurement.
    """
    pages = [{"title": "Session Cache", "sub_url": "Session-Cache"}]
    s = _harness_with_attest(tmp_path, pages, page_content=b"x")
    f = tmp_path / "cache.md"
    f.write_bytes(b"# Session Cache\n\nbox state, authored by nobody\n")
    r = _run(s, "publish", str(f), FORGE_TOOLS_SESSION_ID=SID, HANDOFF_PAGE="Session Cache")
    assert BEGIN not in f.read_text(), "repo state must not be signed"
    assert "stamped" not in r.stderr


OTHER_SID = "9570d0d1-1111-4222-8333-444455556666"

# A SESSION ACCOUNT page, not the seat`s rolling `Coordinator Handoff`. The guard splits on the page
# KIND: `Coordinator Handoff` is handed between seats and its signature legitimately changes hands
# (pinned by test_a_republish_by_another_session_replaces_the_signature); `Handoff <id>` is one
# session`s account of its own work and must not silently change author.
ACCOUNT_PAGE = [{"title": "Handoff CC 9", "sub_url": "Handoff-CC-9"}]


def _signed_draft(tmp_path, sid, name="draft.md"):
    """A draft already carrying somebody`s signature -- what a RESTORE looks like on disk."""
    f = tmp_path / name
    f.write_text(
        "# Handoff CC 9\n\ntheir account of their work\n\n"
        "<!-- session-attest:begin -->\n"
        "attested-session: %s\n"
        "attested-pid: 4242\n"
        "<!-- session-attest:end -->\n" % sid)
    return f


def _publish_account(s, f, **env):
    return _run(s, "publish", str(f), HANDOFF_PAGE="Handoff CC 9", **env)


def test_publishing_a_session_account_attested_to_ANOTHER_session_is_refused(tmp_path):
    """`publish` stamped unconditionally, so restoring someone else`s handoff re-attached
    the RESTORING session`s attestation to a document it did not write. Measured on a real page:
    13,427 bytes became 14,222 and it then asserted that the restoring session wrote it -- truthful
    about its author, false about the authorship, produced by the act of repair.

    NOTE the exit code is not asserted: this harness`s transport does not store what it is given,
    so `publish` returns 1 on read-back mismatch in EVERY test here. A returncode assertion would
    pass for the wrong reason, which is why the sibling tests assert on the draft and on stderr.
    """
    s = _harness_with_attest(tmp_path, ACCOUNT_PAGE, page_content=b"x")
    f = _signed_draft(tmp_path, OTHER_SID)
    before = f.read_text()
    r = _publish_account(s, f, FORGE_TOOLS_SESSION_ID=SID)
    assert "REFUSING to stamp" in r.stderr, r.stderr
    assert OTHER_SID in r.stderr and SID in r.stderr, (
        "the refusal must print BOTH ids -- a reader cannot act on `they differ`")
    assert "HANDOFF_NO_STAMP=1" in r.stderr, (
        "the flag has been in the usage text since this was filed and was still not found by the "
        "person who needed it; the refusal is where a caller is guaranteed to be looking")
    assert f.read_text() == before, "a refused publish must not have rewritten the draft"
    assert "updated wiki page" not in r.stderr, "it must refuse BEFORE uploading anything"


def test_HANDOFF_NO_STAMP_restores_another_sessions_page_with_its_signature_intact(tmp_path):
    """THE PERMITTING ARM. Testing only the refusal would be satisfied by a publish that refuses
    everything, and the restore path is the reason the capability exists."""
    s = _harness_with_attest(tmp_path, ACCOUNT_PAGE, page_content=b"x")
    f = _signed_draft(tmp_path, OTHER_SID)
    r = _publish_account(s, f, FORGE_TOOLS_SESSION_ID=SID, HANDOFF_NO_STAMP="1")
    body = f.read_text()
    assert "attested-session: %s" % OTHER_SID in body, "the original signature must survive"
    assert SID not in body, "the restoring session must not appear on a page it did not write"
    assert "stamped" not in r.stderr
    assert "REFUSING to stamp" not in r.stderr


def test_republishing_YOUR_OWN_session_account_still_stamps(tmp_path):
    """THE REGRESSION CONTROL. The common case is a session republishing its own handoff, which
    already carries its own block -- if that were refused the verb would be unusable for the thing
    it exists to do, and the two tests above would still pass."""
    s = _harness_with_attest(tmp_path, ACCOUNT_PAGE, page_content=b"x")
    f = _signed_draft(tmp_path, SID)
    r = _publish_account(s, f, FORGE_TOOLS_SESSION_ID=SID)
    body = f.read_text()
    assert "REFUSING to stamp" not in r.stderr, r.stderr
    assert body.count(BEGIN) == 1, "still exactly one signature"
    assert "attested-session: %s" % SID in body


def test_the_COORDINATOR_page_still_changes_hands_freely(tmp_path):
    """THE SCOPE CONTROL for that refusal, and without it the refusal above could be a blanket one.

    `Coordinator Handoff` is the seat`s rolling page: it is handed between seats by design and the
    signature must follow the current author. A refusal there would break the case
    test_a_republish_by_another_session_replaces_the_signature exists to protect -- which is how
    the first version of this fix was caught.
    """
    s = _harness_with_attest(tmp_path, PAGE, page_content=b"x")
    f = _signed_draft(tmp_path, OTHER_SID)
    r = _run(s, "publish", str(f), FORGE_TOOLS_SESSION_ID=SID)
    assert "REFUSING to stamp" not in r.stderr, r.stderr
    assert "attested-session: %s" % SID in f.read_text(), (
        "the coordinator page must name its CURRENT author")


def test_cannot_stamp_without_a_session_id_and_says_so(tmp_path):
    """THE OTHER ARM. FORGE_TOOLS_SESSION_ID lives only in hook and Bash-tool child environments,
    so cron, CI and a bare shell cannot sign. That must be a note and never a refusal."""
    s = _harness_with_attest(tmp_path, PAGE, page_content=b"x")
    f = tmp_path / "draft.md"
    f.write_bytes(b"# handoff\n\nbody\n")
    r = _run(s, "publish", str(f))  # no FORGE_TOOLS_SESSION_ID
    assert BEGIN not in f.read_text()
    assert "cannot stamp" in r.stderr
    assert "UNSTAMPED" in r.stderr


def test_signing_keeps_file_and_page_identical(tmp_path):
    """THE INVARIANT SIGNING MUST NOT BREAK — carried over from the route-successor seat's commit,
    which is where this test was written.

    `publish` proves success by digesting the local file against the read-back, and `verify`
    compares draft to page. If signing changed the uploaded bytes without changing the file, a good
    publish would report MISMATCH and every later `verify` would report DIFFER on a correct page.
    So after signing, the file must be exactly what a byte-comparison against the page will see."""
    s = _harness_with_attest(tmp_path, PAGE, page_content=b"x")
    f = tmp_path / "draft.md"
    f.write_bytes(b"# handoff\n\nbody\n")
    _run(s, "publish", str(f), FORGE_TOOLS_SESSION_ID=SID)
    signed = f.read_bytes()
    # The stub serves fixed bytes, so re-serve the signed file as the page and assert verify agrees.
    second = tmp_path / "second"
    second.mkdir()
    s2 = _harness_with_attest(second, PAGE, page_content=signed)
    f2 = second / "draft.md"
    f2.write_bytes(signed)
    r = _run(s2, "verify", str(f2))
    assert r.returncode == 0, f"a signed draft must still verify against its page: {r.stderr}"
    assert "IDENTICAL" in r.stdout


def test_publish_survives_a_missing_session_attest(tmp_path):
    """Also from the route-successor seat. An absent tool must publish unsigned, never explode --
    and, since the tool moved to its own repo, must say WHICH command is missing."""
    s = _harness(tmp_path, PAGE, page_content=b"x")
    f = tmp_path / "draft.md"
    f.write_bytes(b"# handoff\n\nbody\n")
    # PATH with every directory that holds `session-attest` removed: the command is absent.
    path = ":".join(d for d in os.environ.get("PATH", "/usr/bin:/bin").split(":")
                    if d and not os.path.exists(os.path.join(d, "session-attest")))
    assert not shutil.which("session-attest", path=path), "fixture: the command must be off PATH"
    r = _run(s, "publish", str(f), FORGE_TOOLS_SESSION_ID=SID, PATH=path)
    assert BEGIN not in f.read_text()
    assert "cannot stamp" in r.stderr
    assert "MISSING COMMAND `session-attest`" in r.stderr and "Session-Attest" in r.stderr, r.stderr


def test_a_republish_by_another_session_replaces_the_signature(tmp_path):
    """THE DEFECT THIS RULES OUT, and it is why signing replaces rather than skips.

    An implementation that skips when a block is already present keeps the FIRST author's signature
    for ever, so a page edited and republished by a second session still names the first. For a
    feature whose entire purpose is saying who wrote the page, that is the wrong answer, and it is
    not hypothetical: this page was republished by two different seats tonight."""
    s = _harness_with_attest(tmp_path, PAGE, page_content=b"x")
    f = tmp_path / "draft.md"
    f.write_bytes(b"# handoff\n\nbody\n")
    _run(s, "publish", str(f), FORGE_TOOLS_SESSION_ID="1111aaaa-0000-0000-0000-000000000000")
    assert "attested-session: 1111aaaa-0000-0000-0000-000000000000" in f.read_text()
    _run(s, "publish", str(f), FORGE_TOOLS_SESSION_ID="2222bbbb-0000-0000-0000-000000000000")
    body = f.read_text()
    assert body.count(BEGIN) == 1, "two signatures on one page"
    assert "attested-session: 2222bbbb-0000-0000-0000-000000000000" in body
    assert "1111aaaa" not in body, "the page still names the previous session as its author"


def test_a_quoted_example_block_is_not_eaten(tmp_path):
    """SILENT DATA LOSS, found by the route-successor seat against the live forge, and the read-back
    is structurally unable to catch it: the strip runs BEFORE the digest, so `publish` verifies the
    damaged document as byte-perfect and reports VERIFIED.

    The page most likely to quote these markers is a page ABOUT attestation — a note on page signing, or
    a handoff explaining the mechanism to its successor.

    THE SECOND CASE IS WHY ANCHORING AT EOF IS NOT ENOUGH. Once such a page has been signed once it
    holds a quoted example AND a real trailing block, and a lazy pattern anchored with `\\Z` starts
    at the QUOTED marker and expands to the REAL one, deleting the example and everything between.
    Both cases are asserted here; the `\\Z` form passes the first and fails the second."""
    s = _harness_with_attest(tmp_path, PAGE, page_content=b"x")
    f = tmp_path / "draft.md"
    quoted = (
        "# a note on page signing\n\nSign a handoff like this:\n\n```\n"
        f"{BEGIN}\nattested-session: EXAMPLE-DO-NOT-DELETE\n"
        "<!-- session-attest:end -->\n```\n\nprose after the example.\n"
    )
    f.write_text(quoted)
    _run(s, "publish", str(f), FORGE_TOOLS_SESSION_ID=SID)
    body = f.read_text()
    assert "EXAMPLE-DO-NOT-DELETE" in body, "a quoted example was eaten out of the document"
    assert "prose after the example." in body
    assert body.count(BEGIN) == 2, "one quoted example plus one real signature"

    # Now re-sign the already-signed page: the example must STILL survive.
    _run(s, "publish", str(f), FORGE_TOOLS_SESSION_ID="3333cccc-0000-0000-0000-000000000000")
    body = f.read_text()
    assert "EXAMPLE-DO-NOT-DELETE" in body, "re-signing ate the quoted example"
    assert "prose after the example." in body
    assert body.count(BEGIN) == 2, "re-signing must replace the real block only"
    assert "attested-session: 3333cccc-0000-0000-0000-000000000000" in body


# ==============================================================================================
# THE CACHE SIZE CEILING AND `--dry-run`.
#
# WHY THESE TESTS ARE SAFE WHERE MY ad-hoc VERIFICATION WAS NOT, because the difference is the
# whole lesson of the dry-run incident and is easy to read backwards. `_harness` copies `handoff.sh` next to a STUB
# `hub-api.sh` whose write paths are `:` -- so a publish here cannot reach the forge no matter which
# page title it names. What destroyed two live wiki pages on 2026-08-30 was the REAL script against
# the REAL client, typed at a shell, with a real page title. The harness is the isolation; the page
# title never was.
#
# `test_the_harness_is_a_stub_and_not_the_real_client` is the control for exactly that, because an
# isolation you assert but never prove is the case `.claude/rules/verification.md` exists for.
# ==============================================================================================

CACHE_PAGES = [{"title": "Session Cache", "sub_url": "Session-Cache"}]


def _sized(tmp_path, name, chars):
    """A file of `chars` CHARACTERS whose byte count is deliberately larger.

    Em-dashed prose, because the cliff counts characters and these pages are full of them -- a
    fixture of plain ASCII would let a byte-based implementation pass every test here.
    """
    f = tmp_path / name
    unit = "word — pad\n"          # 11 characters, 13 bytes
    f.write_text(unit * (chars // len(unit)), encoding="utf-8")
    return f


def test_the_harness_is_a_stub_and_not_the_real_client(tmp_path):
    """THE CONTROL FOR EVERY TEST BELOW. Without it they could all be talking to the forge.

    Asserts on where the traffic went, not on the absence of an error: the stub serves a page list
    containing exactly one title of our choosing, which the real client could not produce.
    """
    s = _harness(tmp_path, [{"title": "ZZ-Stub-Only", "sub_url": "ZZ-Stub-Only"}], b"stub body\n")
    r = _run(s, "show", HANDOFF_PAGE="ZZ-Stub-Only")
    assert "stub body" in r.stdout, (
        f"the harness did not serve the canned page, so these tests may be reaching the real "
        f"forge:\n{r.stdout}\n{r.stderr}")


def test_publish_REFUSES_an_over_budget_injected_page(tmp_path):
    """The defect this guards: past 10,000 characters of one hook's stdout the cache is replaced by
    a ~2KB preview and stops arriving SILENTLY (docs/agents/session-cache-cliff.md)."""
    s = _harness(tmp_path, CACHE_PAGES, b"old\n")
    big = _sized(tmp_path, "big.md", 12000)
    r = _run(s, "publish", str(big), HANDOFF_PAGE="Session Cache")
    assert r.returncode != 0, f"an over-budget cache page was accepted:\n{r.stdout}{r.stderr}"
    assert "REFUSING to publish" in r.stderr, r.stderr
    assert "VERIFIED" not in r.stdout, "it refused and published anyway"


def test_publish_ALLOWS_the_same_oversized_file_on_a_page_that_is_not_injected(tmp_path):
    """The ceiling is about INJECTION, not about wiki pages. A 12KB handoff is legitimate: it is
    read on demand, never injected at SessionStart, so it cannot hit the cliff.

    Without this arm the guard could be `refuse anything large` and every other test would pass.
    """
    s = _harness(tmp_path, PAGE, b"old\n")
    big = _sized(tmp_path, "big.md", 12000)
    r = _run(s, "publish", str(big), HANDOFF_PAGE="Coordinator Handoff")
    assert "REFUSING to publish" not in r.stderr, (
        f"a non-injected page was refused on size:\n{r.stderr}")


def test_an_acceptable_cache_page_is_not_refused(tmp_path):
    """THE NEGATIVE CONTROL THAT COST TWO LIVE PAGES BEFORE `--dry-run` EXISTED.

    "Not refused" can only be observed by letting the write proceed -- a refusal is inert, its
    absence is not. In this harness the write is swallowed by the stub, which is what makes the
    assertion expressible here and is precisely what an ad-hoc shell run does not have.
    """
    s = _harness(tmp_path, CACHE_PAGES, b"old\n")
    ok = _sized(tmp_path, "ok.md", 500)
    r = _run(s, "publish", str(ok), HANDOFF_PAGE="Session Cache")
    assert "REFUSING to publish" not in r.stderr, (
        f"a small cache page was refused:\n{r.stderr}")


def test_a_near_ceiling_em_dashed_cache_page_is_counted_in_characters_not_bytes(tmp_path):
    """`wc -m` counts BYTES when no locale is exported -- the Bash tool's env, and this one
    (`_run` builds the env from scratch) -- so an 8,998-character page of 10,634 bytes was refused
    against the 9,300 ceiling that the :634 comment says it must pass. One counter now feeds both
    the refusal and the report. The 12,000-character test above is the control that still refuses."""
    s = _harness(tmp_path, CACHE_PAGES, b"old\n")
    near = _sized(tmp_path, "near.md", 9000)
    r = _run(s, "publish", "--dry-run", str(near), HANDOFF_PAGE="Session Cache")
    assert "REFUSING to publish" not in r.stderr, r.stderr
    assert r.returncode == 0, r.stdout + r.stderr


def test_publish_REFUSES_when_the_page_listing_cannot_be_read(tmp_path):
    """rc 2 from the listing took the POST branch, and the lock, the block guard and the
    stale-base check all live under PATCH -- so a transient listing failure turned a guarded update
    into an unguarded create diagnosis."""
    s = _harness(tmp_path, CACHE_PAGES, b"old\n")
    _forge_unreachable(s)
    ok = _sized(tmp_path, "ok.md", 500)
    r = _run(s, "publish", "--dry-run", str(ok), HANDOFF_PAGE="Session Cache")
    out = r.stdout + r.stderr
    assert r.returncode == 2, out
    assert "CANNOT TELL" in out, out
    assert "would have created" not in out, "an unreadable listing was read as an absent page"


def test_delete_REFUSES_when_the_page_listing_cannot_be_read(tmp_path):
    """the delete arm of the same flatten."""
    s = _harness(tmp_path, [{"title": "Handoff CC 9", "sub_url": "Handoff-CC-9"}], b"x\n")
    _forge_unreachable(s)
    r = _run(s, "delete", "--dry-run", HANDOFF_PAGE="Handoff CC 9")
    out = r.stdout + r.stderr
    assert r.returncode == 2, out
    assert "CANNOT TELL" in out, out
    assert "nothing to delete" not in out, out


def test_expect_sha_on_a_page_resolved_as_absent_is_refused(tmp_path):
    """A stated base with no page to compare it against is a stale-base check that silently
    did not run; the dry run said "would have created" and exited 0."""
    s = _harness(tmp_path, [], None)
    ok = _sized(tmp_path, "ok.md", 500)
    r = _run(s, "publish", "--dry-run", str(ok), HANDOFF_PAGE="Session Cache",
             HANDOFF_EXPECT_SHA="0" * 64)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "would have created" not in r.stdout, r.stdout


def test_a_title_past_the_first_listing_page_is_still_found(tmp_path):
    """`sub_url_for_title` read ONE unpaged listing (the forge's default 30) while
    `_all_titles` paged, so the 31st page was "absent" to `show`/`url`/`publish`. The stub serves
    50 titles on page 1 and P51 on page 2; the control is a title on neither page."""
    pages = [{"title": f"P{i}", "sub_url": f"P{i}"} for i in range(1, 51)]
    s = _harness(tmp_path, pages, b"hello from P51\n")
    (s.parent / "pages-2.json").write_text(json.dumps([{"title": "P51", "sub_url": "P51"}]))
    stub = s.parent / "hub-api.sh"
    anchor = '*/wiki/pages|*/wiki/pages\\?*)'
    src = stub.read_text()
    assert anchor in src, f"fault-injection anchor {anchor!r} is gone -- this test would inject nothing"
    stub.write_text(src.replace(
        anchor, '*"page=2"*) cat "$HERE/pages-2.json" ;;\n          ' + anchor, 1))
    r = _run(s, "url", HANDOFF_PAGE="P51")
    assert r.returncode == 0 and "wiki/P51" in r.stdout, r.stdout + r.stderr
    r = _run(s, "url", HANDOFF_PAGE="P52")
    assert r.returncode == 1, "a title on no page must still be absent:\n" + r.stdout + r.stderr


def test_dry_run_decides_without_sending_anything(tmp_path):
    s = _harness(tmp_path, CACHE_PAGES, b"old\n")
    ok = _sized(tmp_path, "ok.md", 500)
    r = _run(s, "publish", "--dry-run", str(ok), HANDOFF_PAGE="Session Cache")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY RUN" in r.stdout, r.stdout
    # It must say WHICH it would be. `updated` vs `created` is resolved against the forge's own
    # page list, and it is the distinction the dry-run fix needed and did not have.
    assert "would have updated" in r.stdout, r.stdout
    assert "VERIFIED" not in r.stdout, "a dry run performed the publish and verified it"


def test_dry_run_on_a_MISSING_page_says_created_not_updated(tmp_path):
    """The other arm of that distinction. Reported wrong, a dry run would tell someone they were
    creating a new page while they were about to overwrite one."""
    s = _harness(tmp_path, [], None)      # no pages at all -> the 404 shape
    ok = _sized(tmp_path, "ok.md", 500)
    r = _run(s, "publish", "--dry-run", str(ok), HANDOFF_PAGE="Session Cache")
    assert "would have created" in r.stdout, r.stdout


def test_dry_runs_exit_status_IS_the_verdict(tmp_path):
    """So a caller can assert on it rather than grepping prose -- which is what makes the negative
    control above writable at all."""
    s = _harness(tmp_path, CACHE_PAGES, b"old\n")
    big = _sized(tmp_path, "big.md", 12000)
    r = _run(s, "publish", "--dry-run", str(big), HANDOFF_PAGE="Session Cache")
    assert r.returncode != 0, f"a dry run of a REFUSED publish reported success:\n{r.stdout}"
    assert "REFUSING to publish" in r.stderr, r.stderr


# ---------------------------------------------------------------------------
# delete
#
# Deletion is the one verb with no undo on the forge, so the tests that matter are the REFUSALS --
# and a refusal is only meaningful beside a control showing the verb deletes something. A suite
# where every case refuses passes just as green as one that discriminates.
# ---------------------------------------------------------------------------

HANDOFF_PAGES = [
    {"title": "Coordinator Handoff", "sub_url": "Coordinator-Handoff"},
    {"title": "Session Cache", "sub_url": "Session-Cache"},
    {"title": "Handoff CC 3", "sub_url": "Handoff-CC-3"},
]


def _deleted(script):
    log = script.parent / "deleted.log"
    return log.read_text().splitlines() if log.exists() else []


def test_delete_removes_a_handoff_page_and_verifies_its_absence(tmp_path):
    """The control. Without it, every refusal below is satisfied by a verb that does nothing."""
    s = _harness(tmp_path, HANDOFF_PAGES, pages_after=HANDOFF_PAGES[:2])
    r = _run(s, "delete", HANDOFF_PAGE="Handoff CC 3")
    assert r.returncode == 0, r.stderr
    assert "VERIFIED" in r.stdout
    assert len(_deleted(s)) == 1
    assert "Handoff-CC-3" in _deleted(s)[0]


def test_delete_refuses_a_repo_state_page(tmp_path):
    s = _harness(tmp_path, HANDOFF_PAGES)
    r = _run(s, "delete", HANDOFF_PAGE="Session Cache")
    assert r.returncode != 0
    assert "REFUSING" in r.stderr
    assert _deleted(s) == [], "a refusal that still issued the DELETE is not a refusal"


def test_delete_refuses_the_shared_coordinator_page(tmp_path):
    """The discriminating case: `_page_is_a_handoff` MATCHES "Coordinator Handoff" -- it is the
    predicate for what gets SIGNED. Reusing it here would delete the shared seat page, which is
    the 16,316-byte loss recorded at the top of handoff.sh."""
    s = _harness(tmp_path, HANDOFF_PAGES)
    r = _run(s, "delete", HANDOFF_PAGE="Coordinator Handoff")
    assert r.returncode != 0
    assert "REFUSING" in r.stderr
    assert _deleted(s) == []


def test_a_page_still_listed_after_a_successful_delete_is_reported(tmp_path):
    """Fault injection: the forge accepts the DELETE and the listing does not change. Without the
    read-back this exits 0 and the caller believes a claimed number was freed."""
    s = _harness(tmp_path, HANDOFF_PAGES)  # no pages_after -> the listing never changes
    r = _run(s, "delete", HANDOFF_PAGE="Handoff CC 3")
    assert r.returncode != 0
    assert "STILL LISTED" in r.stderr
    assert len(_deleted(s)) == 1, "the DELETE should have been attempted before the read-back"


def test_delete_of_an_absent_page_says_nothing_to_delete(tmp_path):
    s = _harness(tmp_path, HANDOFF_PAGES)
    r = _run(s, "delete", HANDOFF_PAGE="Handoff CC 9")
    assert r.returncode != 0
    assert "nothing to delete" in r.stderr
    assert _deleted(s) == []


def test_delete_dry_run_sends_nothing(tmp_path):
    s = _harness(tmp_path, HANDOFF_PAGES)
    r = _run(s, "delete", "--dry-run", HANDOFF_PAGE="Handoff CC 3")
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout
    assert _deleted(s) == []


# --- a handoff makes claims about what its reader will have loaded ----------------------
#
# THE MEASURED INSTANCE: a predecessor wrote "guard-bypass-guard.py is live on main ... you are very
# likely the first session it can refuse" -- true of hub/main, false for the successor it had placed
# in a tree six commits behind that contained neither the hook nor its rule.
#
# reference-tree-guard RAN and was CORRECT: `0 behind, 0 ahead` at SessionStart. The tree drifted six
# behind DURING the session, from merges that session performed. Not a missing instrument -- a
# measurement that was true when taken and carried forward as current by the party who invalidated it.


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _tree_at(tmp_path, behind: bool, with_remote: bool = True):
    """A work tree with a real `hub` remote, sitting either level with main or one commit behind.

    A REAL REMOTE, not a hand-written ref: the check fetches before counting, because
    `HEAD..hub/main` against a ref nobody updated measures the distance to a stale idea of main --
    a check reporting 0 because it never looked. A fixture that skipped the fetch would leave that
    exact path unexercised.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.org")
    _git(seed, "config", "user.name", "claude")
    (seed / "a.txt").write_text("one\n")
    _git(seed, "add", "-A"); _git(seed, "commit", "-qm", "first")
    first = _git(seed, "rev-parse", "HEAD").stdout.strip()
    (seed / "a.txt").write_text("two\n")
    _git(seed, "add", "-A"); _git(seed, "commit", "-qm", "second")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "remote", "rename", "origin", "hub")
    if behind:
        _git(work, "checkout", "-q", first)
    if not with_remote:
        _git(work, "remote", "remove", "hub")
    return work


def _publish_from(script, cwd, page, body=b"a handoff\n", tmp_path=None):
    f = Path(cwd) / "draft.md"
    f.write_bytes(body)
    return subprocess.run(
        ["sh", str(script), "publish", "--dry-run", str(f)],
        capture_output=True, text=True, cwd=str(cwd), timeout=60,
        env={**FT_SITE, "HOME": "/nonexistent", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "HANDOFF_PAGE": page, "HANDOFF_LOCK": str(Path(script).parent / "handoff.lock"),
             # See _run: an injected page's ceiling is derived from the injector, and this
             # harness runs a COPY of handoff.sh from a tree that has no `.claude/hooks/`.
             "HANDOFF_CACHE_HOOK": _cache_hook(script)},
    )


def test_publish_warns_when_the_handoff_is_written_from_a_stale_tree(tmp_path):
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=True)
    r = _publish_from(s, work, "Handoff abc")
    assert "1 commit(s) behind hub/main" in r.stderr, r.stderr


def test_the_warning_says_which_half_of_the_class_it_covers(tmp_path):
    """Its silence must not read as 'the environmental claims are sound'. PreToolUse hooks follow the
    TREE live -- proved by probe, two sessions launched before a guard existed had it DENYING once
    the tree carried it. SessionStart-injected surfaces are pinned at launch and no tree measurement
    reaches them."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=True)
    r = _publish_from(s, work, "Handoff abc")
    assert "COVERED by this warning: PreToolUse hooks and guards" in r.stderr
    assert "NOT COVERED" in r.stderr
    assert "pinned at the successor's launch" in r.stderr


def test_publish_is_silent_when_the_tree_is_current(tmp_path):
    """THE CONTROL. A warning that always fires is noise, and this one sits on the path of every
    handoff -- so the quiet case is the one that decides whether it survives."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False)
    r = _publish_from(s, work, "Handoff abc")
    assert "behind hub/main" not in r.stderr, r.stderr
    assert "UNMEASURED" not in r.stderr


def test_the_check_does_not_leak_onto_repo_state_pages(tmp_path):
    """THE NEGATIVE. `Session Cache` and `Hot Cache` are repo state; they carry no claims about a
    reader's environment, so warning on them is noise on the path of every cache write."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=True)
    r = _publish_from(s, work, "Session Cache", body=b"small cache\n")
    assert "behind hub/main" not in r.stderr, r.stderr


def test_an_unfetchable_remote_is_UNMEASURED_rather_than_silent(tmp_path):
    """Zero-because-unmeasured must not read as zero-because-current -- the failure this repo keeps
    meeting. A stale-tree handoff makes false claims whether or not the check could run."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=True, with_remote=False)
    r = _publish_from(s, work, "Handoff abc")
    assert "currency is UNMEASURED" in r.stderr
    assert "not the same as current" in r.stderr


def test_publish_still_refuses_an_over_cliff_cache_from_a_stale_tree(tmp_path):
    """The new note must not displace the refusal that was already there: a warning printed on the
    way past a guard is the shape where a guard quietly stops mattering."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=True)
    r = _publish_from(s, work, "Session Cache", body=b"x" * 9600)
    assert r.returncode == 1
    assert "REFUSING to publish" in r.stderr


# --- the wiki IS the registry, and a slot is claimed by creating its page -----
#
# A short registry read hands out a number a live terminal already holds, and the next publish
# overwrites its handoff. Measured on the live forge 2026-08-31: `X-Total-Count` is present on every
# response (so truncation is DETECTABLE without knowing the cap), `limit=2..6` is honoured exactly,
# and `limit=0`/`limit=1` are IGNORED -- a caller probing with limit=1 gets the default page size.


def _registry(tmp_path, titles, total=None, races=0, refuse="", listings_ok=None):
    """A stub forge that serves a paged listing WITH headers and refuses to create an existing page.

    The header file matters: the real client passes `-D`, and the allocator refuses when
    X-Total-Count is missing. A stub that ignored `-D` would exercise only the refusal path.
    `total` overrides the header so a SHORT READ can be modelled -- without that the truncation
    guard has no failing case and is untestable.

    THE CREATE'S FAILURES ARE MODELLED SEPARATELY, because telling them apart is the point:
    `races` -- that many creates fail AND leave the page behind, as a lost race does (the winner's
    page is what `POST /wiki/new` refused on); `refuse` -- every create fails with this text on
    stderr and leaves nothing, as a refused write does; `listings_ok` -- how many registry reads
    succeed before the listing itself fails. The old single `create_fails` made every failure a
    race by construction, which is the misreport the ticket is about, pinned as a test.
    """
    d = tmp_path / "scripts"
    if not d.exists():
        d.mkdir()
    (d / "handoff.sh").write_text(REAL.read_text())
    for sib in FT_SIBLINGS:
        (d / sib.name).write_bytes(sib.read_bytes())
    (d / "titles.json").write_text(json.dumps([{"title": t, "sub_url": t.replace(" ", "-")}
                                               for t in titles]))
    (d / "total.txt").write_text(str(len(titles) if total is None else total))
    (d / "races").write_text(str(races))
    (d / "refuse").write_text(refuse)
    (d / "listings_ok").write_text("" if listings_ok is None else str(listings_ok))
    (d / "hub-api.sh").write_text(textwrap.dedent("""\
        #!/bin/sh
        HERE=$(dirname "$0")
        REQ="$1"; shift
        HDR=""
        while [ $# -gt 0 ]; do
          case "$1" in
            -D) HDR="$2"; shift 2 ;;
            *) shift ;;
          esac
        done
        case "$REQ" in
          */wiki/pages*)
            [ -n "$HDR" ] && printf 'HTTP/1.1 200 OK\\r\\nX-Total-Count: %s\\r\\n\\r\\n' \\
                "$(cat "$HERE/total.txt")" > "$HDR"
            # page=2+ returns an empty array: the whole listing fits in one page here.
            case "$REQ" in
              *page=1*|*page=1)
                if [ -s "$HERE/listings_ok" ]; then
                  left=$(cat "$HERE/listings_ok")
                  [ "$left" -gt 0 ] || exit 1
                  echo $((left - 1)) > "$HERE/listings_ok"
                fi
                cat "$HERE/titles.json" ;;
              *) echo '[]' ;;
            esac ;;
          */wiki/new)
            cat > "$HERE/last-create.json"
            echo x >> "$HERE/creates"
            if [ -s "$HERE/refuse" ]; then cat "$HERE/refuse" >&2; exit 1; fi
            left=$(cat "$HERE/races")
            if [ "$left" -gt 0 ]; then
              echo $((left - 1)) > "$HERE/races"
              python3 -c 'import json, sys; h = sys.argv[1]; t = json.load(open(h + "/last-create.json"))["title"]; L = json.load(open(h + "/titles.json")); L.append({"title": t, "sub_url": t.replace(" ", "-")}); json.dump(L, open(h + "/titles.json", "w")); open(h + "/total.txt", "w").write(str(len(L)))' "$HERE"
              exit 22
            fi
            exit 0 ;;
          *) exit 0 ;;
        esac
        """))
    (d / "hub-api.sh").chmod(0o755)
    return d / "handoff.sh"


def _slot(script, *args, **env):
    e = {**FT_SITE, "HOME": "/nonexistent", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
         "HANDOFF_PAGE": "Coordinator Handoff",
         "HANDOFF_LOCK": str(Path(script).parent / "handoff.lock"), **env}
    return subprocess.run(["sh", str(script), "slot", *args],
                          capture_output=True, text=True, env=e, timeout=60)


def _created_title(script):
    return json.loads((Path(script).parent / "last-create.json").read_text())["title"]


def test_slot_claim_takes_the_lowest_free_number(tmp_path):
    s = _registry(tmp_path, ["Handoff CC 1", "Handoff CC 3", "Hot Cache"])
    r = _slot(s, "claim", "CC", FORGE_TOOLS_SESSION_ID="abc")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "CC 2"
    assert _created_title(s) == "Handoff CC 2"


def _created_body(script):
    blob = json.loads((Path(script).parent / "last-create.json").read_text())["content_base64"]
    return base64.b64decode(blob).decode()


def test_claiming_your_own_slot_still_names_you(tmp_path):
    """THE CONTROL for that fix, and the regression the fix could have caused.

    The delegated path is the one that was broken, but the ordinary path -- a session claiming its
    own number -- must keep taking the holder from the environment. A fix that made the argument
    mandatory would have broken every self-claim while making the reported bug go away.
    """
    s = _registry(tmp_path, ["Handoff CC 1"])
    assert _slot(s, "claim", "CC", FORGE_TOOLS_SESSION_ID="self-1").returncode == 0
    assert "- holder session: `self-1`" in _created_body(s)


def test_a_delegated_claim_names_the_holder_it_was_GIVEN_not_the_claimer(tmp_path):
    """`start` claims a slot FOR the successor while running as the PREDECESSOR, so a
    holder read from the environment stamped the wrong session into the successor's own registry
    entry -- silently, since `SUCCESSION_SLOT` was correct and only the page disagreed.

    The environment is set to the CLAIMER here and must not reach the page: that is the whole bug.
    """
    s = _registry(tmp_path, ["Handoff CC 1"])
    r = _slot(s, "claim", "CC", "held by nobody yet -- claimed by pred-1",
              FORGE_TOOLS_SESSION_ID="pred-1")
    assert r.returncode == 0, r.stdout + r.stderr
    body = _created_body(s)
    assert "- holder session: `held by nobody yet -- claimed by pred-1`" in body
    assert "`pred-1`" not in body, f"the claimer's session leaked into the page:\n{body}"


def test_the_stub_never_writes_the_hyphenated_key_that_lineage_owns(tmp_path):
    """A SHADOWING GUARD, not style. `session-succeed lineage` writes `- holder-session:` in the
    lineage block and its reader takes the FIRST match in the page (`sed ... | head -n 1`). This
    line is PROVISIONAL, that one is AUTHORITATIVE, and a lookup must never have to choose.

    THE ORDER CURRENTLY FAVOURS THE AUTHORITY, so this guards a trap rather than a live bug --
    stated precisely because the first version of this comment had it backwards. `lineage` splices
    its block just after the first heading, ABOVE this line (measured on `Handoff CC 1`: lineage at
    line 7, stub at 24), so a unified key would resolve correctly today. Nothing contracts that
    placement -- it is one regex for `^#[^\\n]*\\n` in another script. Keeping the keys distinct
    makes the wrong answer unreachable instead of merely unlucky.
    """
    s = _registry(tmp_path, ["Handoff CC 1"])
    assert _slot(s, "claim", "CC", FORGE_TOOLS_SESSION_ID="self-1").returncode == 0
    body = _created_body(s)
    assert "- holder session:" in body, "the stub must still attribute the claim"
    # MATCHED THE WAY THE READER MATCHES: `sed -n 's/^- holder-session: *//p'` is anchored at line
    # start, so a substring test is the wrong predicate -- it fires on prose that merely NAMES the
    # key, which this stub deliberately does when pointing at the authority. Asserting on line
    # starts is what the shadowing bug would actually look like.
    shadowing = [l for l in body.splitlines() if l.startswith("- holder-session:")]
    assert not shadowing, (
        "the stub wrote the key lineage owns, at line start where the reader will find it first; "
        f"a predecessor lookup would read this instead of the lineage block: {shadowing}")


def test_a_freed_slot_is_reused(tmp_path):
    """Deleting a page frees its number -- that is what makes the registry bounded rather than a
    log. The control is that freeing 2 does not disturb 1 or 3."""
    s = _registry(tmp_path, ["Handoff CC 1", "Handoff CC 2", "Handoff CC 3"])
    assert _slot(s, "claim", "CC").stdout.strip() == "CC 4"
    s = _registry(tmp_path, ["Handoff CC 1", "Handoff CC 3"])       # CC 2 closed, page deleted
    assert _slot(s, "claim", "CC").stdout.strip() == "CC 2"


def test_prefixes_do_not_share_a_number_space(tmp_path):
    """`CC 1` and `OMP 1` are different terminals. A shared counter would make one harness's
    claims silently block the other's."""
    s = _registry(tmp_path, ["Handoff CC 1", "Handoff CC 2"])
    assert _slot(s, "claim", "OMP").stdout.strip() == "OMP 1"


def test_a_short_registry_read_refuses_rather_than_allocating(tmp_path):
    """THE INJECTION the registry's ceiling asks for. The forge says there are 9 pages and the listing
    returns 2 -- allocating from that hands out a number a live terminal holds."""
    s = _registry(tmp_path, ["Handoff CC 1", "Handoff CC 2"], total=9)
    r = _slot(s, "claim", "CC")
    assert r.returncode == 1
    assert "registry read is SHORT" in r.stderr
    assert "hands out a slot number" in r.stderr
    assert not (Path(s).parent / "last-create.json").exists(), "nothing may be created on a short read"


def test_a_missing_total_header_is_also_a_refusal(tmp_path):
    """No header is not 'nothing to compare against' -- it is an unverifiable read."""
    s = _registry(tmp_path, ["Handoff CC 1"], total="")
    r = _slot(s, "claim", "CC")
    assert r.returncode == 1
    assert "SHORT" in r.stderr


def _creates(script):
    p = Path(script).parent / "creates"
    return len(p.read_text().splitlines()) if p.exists() else 0


def test_losing_the_creation_race_is_retried_and_takes_the_next_number__control(tmp_path):
    """Two sessions reading 'lowest unclaimed' concurrently pick the same number; nothing on this
    side can prevent that. `POST /wiki/new` 4xxs on an existing page, so the FORGE settles it and
    the loser re-scans. A REAL race leaves the winner's page behind -- modelled that way now,
    where it used to be a create that always failed and added nothing.

    THE CONTROL for the refusal tests below: a genuine race must still retry."""
    s = _registry(tmp_path, ["Handoff CC 1"], races=1)
    r = _slot(s, "claim", "CC")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "was taken between the read and the write" in r.stderr
    assert r.stdout.strip() == "CC 3", "the lost number is now held; the retry takes the next one"
    assert _creates(s) == 2


def test_a_registry_that_keeps_moving_gives_up_after_5_attempts__control(tmp_path):
    """A CONTROL, like the test above: a registry that moves under every attempt is still a race,
    retried to exhaustion -- the behaviour the fix keeps. It passes on the base by design."""
    s = _registry(tmp_path, ["Handoff CC 1"], races=99)
    r = _slot(s, "claim", "CC")
    assert r.returncode == 1
    assert r.stderr.count("was taken between the read and the write") == 5, r.stderr
    assert "could not claim a slot in 5 attempts" in r.stderr


REFUSAL = "hub-api: REFUSING: this client is a former version of scripts/hub-api.sh on hub/main"


def test_a_create_refused_for_another_reason_is_named_and_not_retried(tmp_path):
    """measured 2026-09-15: from the reference tree two commits behind `hub/main`, every
    claim attempt printed 'taken between the read and the write' and the claim failed 10 of 10,
    while the same command from a current worktree succeeded on attempt 1. The create was being
    REFUSED as a stale client, and that refusal went to /dev/null. A failure that leaves no page
    is not a race: say what the client said, and do not retry what will only repeat."""
    s = _registry(tmp_path, ["Handoff CC 1"], refuse=REFUSAL)
    r = _slot(s, "claim", "CC")
    assert r.returncode == 1
    assert REFUSAL in r.stderr, "the client's own refusal was discarded:\n" + r.stderr
    assert "was taken between the read and the write" not in r.stderr, "a refusal was reported as a race"
    assert _creates(s) == 1, "a refusal was retried"


def test_a_failed_create_with_an_unreadable_registry_stops_and_names_the_error(tmp_path):
    """If the registry cannot be re-read, a race cannot be told from a refusal -- so it stops, with
    the client's words, rather than retrying blind."""
    s = _registry(tmp_path, ["Handoff CC 1"], refuse=REFUSAL, listings_ok=1)
    r = _slot(s, "claim", "CC")
    assert r.returncode == 1
    assert REFUSAL in r.stderr, r.stderr
    assert "was taken between the read and the write" not in r.stderr, r.stderr


def test_a_hyphenated_prefix_is_refused_because_it_breaks_the_page_url(tmp_path):
    """Measured: a hyphen anywhere in a title flips the forge's space escaping to `+`, so
    `Handoff CC-3` is addressable only as `Handoff+CC.-3`."""
    s = _registry(tmp_path, [])
    r = _slot(s, "claim", "CC-X")
    assert r.returncode == 2
    assert "alphanumeric" in r.stderr


def test_slot_list_distinguishes_an_empty_registry_from_an_unread_one(tmp_path):
    s = _registry(tmp_path, ["Hot Cache", "TODO"])
    r = _slot(s, "list")
    assert "0 claimed slot(s); 2 other page(s)" in r.stdout
    assert "NO SLOTS CLAIMED" in r.stdout
    assert "absence rather than a failed read" in r.stdout


def test_slot_list_reads_the_real_titles_not_an_empty_stream(tmp_path):
    """THE CONTROL FOR A BUG THIS ACTUALLY HAD: the first version fed titles through a pipe while
    `python3 -` was already taking the script from stdin, so every title read as absent and the
    output said '0 other page(s)' against a wiki holding six."""
    s = _registry(tmp_path, ["Handoff CC 1", "Handoff OMP 2", "Hot Cache"])
    r = _slot(s, "list")
    assert "CC 1" in r.stdout and "OMP 2" in r.stdout
    assert "2 claimed slot(s); 1 other page(s)" in r.stdout


# --- publish sends the DRAFT, so a block only on the PAGE is destroyed ------------------
#
# `session-succeed lineage` splices `<!-- lineage:begin -->` and `<!-- predecessor-handoff:begin
# -->` onto a handoff page. Measured 2026-09-03: a session ran `lineage`, published its handoff over
# the top hours later, and the block existed and then silently did not. Its successor measured ZERO
# lineage blocks on the fetched page during the audit that followed.
#
# THE READ-BACK CANNOT COVER THIS. It proves the forge's copy matches the draft -- *what I sent is
# what is there*, never *what was there is still there*. A good publish and a destructive one
# produce byte-identical output, which is why this needed a check of its own.

LINEAGE_PAGE = b"""# Handoff CC 9

<!-- lineage:begin -->
- holder-session: 11111111-2222-3333-4444-555555555555
<!-- lineage:end -->

prose the author owns
"""


def test_publish_REFUSES_to_drop_a_block_the_page_carries(tmp_path):
    """The defect, stated as a test: a draft built from a stale copy silently deletes the block."""
    s = _harness(tmp_path, PAGE, page_content=LINEAGE_PAGE)
    f = tmp_path / "draft.md"
    f.write_bytes(b"# Handoff CC 9\n\nrewritten from scratch, no lineage block\n")
    r = _run(s, "publish", str(f))

    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSING" in r.stderr, r.stderr
    # NAME THE BLOCK. "something would be lost" is not actionable; the marker is.
    assert "<!-- lineage:begin -->" in r.stderr, r.stderr
    assert "Nothing has been sent" in r.stderr, r.stderr
    # AND IT MUST REFUSE BEFORE WRITING, not report after. A refusal that still published would
    # satisfy every assertion above while doing the exact damage.
    assert "VERIFIED" not in r.stdout, r.stdout
    assert "updated wiki page" not in r.stdout, r.stdout


def test_a_draft_that_KEEPS_the_block_still_publishes(tmp_path):
    """THE POSITIVE CONTROL. Without it, a check that refused every publish would pass the test
    above -- and a transport that never writes is a worse defect than the one being fixed."""
    r = _publish_ok(tmp_path, LINEAGE_PAGE)
    assert "REFUSING" not in r.stderr, r.stderr


def test_the_attest_block_is_EXCLUDED_because_replacing_it_is_the_job(tmp_path):
    """`publish` stamps and REPLACES `session-attest` by design. Counting it would refuse
    every ordinary publish of an already-signed page -- the check would fire on its own correct
    case, which is how a guard gets disabled rather than fixed."""
    s = _harness(tmp_path, PAGE, page_content=(
        b"# Coordinator Handoff\n\n<!-- session-attest:begin -->\nattested-session: old\n"
        b"<!-- session-attest:end -->\n"))
    f = tmp_path / "draft.md"
    f.write_bytes(b"# Coordinator Handoff\n\nno attest block in the draft\n")
    r = _run(s, "publish", str(f))
    assert "REFUSING" not in r.stderr, r.stderr
    # Scoped to the MARKER, not the string "session-attest": the unrelated "carries no attestation
    # block" advice names `session-attest.sh`, so a bare substring check fails on a correct run --
    # a false FAIL, which costs the same as a false pass.
    assert "<!-- session-attest:begin -->" not in r.stderr, r.stderr


def _lineage_draft(tmp_path, holder):
    f = tmp_path / "draft.md"
    f.write_text("# Handoff CC 9\n\n<!-- lineage:begin -->\n- holder-session: %s\n"
                 "<!-- lineage:end -->\n\nprose\n" % holder)
    return f


def test_publish_REFUSES_a_lineage_holder_that_is_not_the_signing_session(tmp_path):
    """`Handoff CC 1` went out with the PREVIOUS generation's lineage block pasted back
    (`holder-session: d9285a57`) under `attested-session: 20eaea3f`, and the successor inherited a
    chain that skipped a generation. Both lines were on the page; nothing compared them."""
    s = _harness_with_attest(tmp_path, ACCOUNT_PAGE, page_content=b"x")
    r = _publish_account(s, _lineage_draft(tmp_path, OTHER_SID), FORGE_TOOLS_SESSION_ID=SID)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSING" in r.stderr and "names another session as holder" in r.stderr, r.stderr
    assert OTHER_SID in r.stderr and SID in r.stderr, r.stderr
    assert "VERIFIED" not in r.stdout and "updated wiki page" not in r.stdout, r.stdout


def test_a_lineage_holder_that_IS_the_signing_session_publishes__control(tmp_path):
    """THE CONTROL: the ordinary case, `lineage` run by the session that then publishes."""
    s = _harness_with_attest(tmp_path, ACCOUNT_PAGE, page_content=b"x")
    r = _publish_account(s, _lineage_draft(tmp_path, SID), FORGE_TOOLS_SESSION_ID=SID)
    assert "names another session as holder" not in r.stderr, r.stderr


def test_a_marker_QUOTED_IN_A_FENCE_is_documentation_not_a_block(tmp_path):
    """A page ABOUT this mechanism quotes these markers. The same trap made an earlier attestation
    check refuse to publish that note, so scanning raw text would refuse to publish the
    documentation of the thing it protects."""
    s = _harness(tmp_path, PAGE, page_content=(
        b"# Coordinator Handoff\n\nHow it works:\n\n```\n<!-- lineage:begin -->\n"
        b"<!-- lineage:end -->\n```\n"))
    f = tmp_path / "draft.md"
    f.write_bytes(b"# Coordinator Handoff\n\nrewritten, no fence\n")
    r = _run(s, "publish", str(f))
    assert "REFUSING" not in r.stderr, r.stderr


def test_HANDOFF_DROP_BLOCKS_is_the_stated_way_to_mean_it(tmp_path):
    """Restoring a page or retiring a stale archive is legitimate. The escape hatch is explicit and
    its failure is VISIBLE -- you lose a block you named a variable to drop -- which is the property
    the retired `from-mode` override lacked."""
    s = _harness(tmp_path, PAGE, page_content=LINEAGE_PAGE)
    f = tmp_path / "draft.md"
    f.write_bytes(b"# Handoff CC 9\n\ndeliberately without the block\n")
    r = _run(s, "publish", str(f), HANDOFF_DROP_BLOCKS="1")
    assert "REFUSING" not in r.stderr, r.stderr
    # It got as far as the write and the read-back, which is where a real mismatch is reported.
    assert "MISMATCH" in r.stderr or "VERIFIED" in r.stdout, r.stdout + r.stderr


# --- publish had no precondition, so a concurrent write was a silent lost update --------
#
# THE FORGE OFFERS NO COMPARE-AND-SWAP, MEASURED not assumed: this hub's `swagger.v1.json` gives
# PATCH /wiki/page/{pageName} the body `CreateWikiPageOptions` (content_base64, message, title) and
# NO header parameters -- no `head_commit_id` as on the merge endpoint, no `If-Match`. So an atomic
# refusal is unavailable at any price and this is the best a client can do.
#
# WHAT IT THEREFORE DOES AND DOES NOT COVER. It closes the WIDE window -- a draft built from a fetch
# seconds, minutes or hours old, which is the failure that actually happened (9,461 bytes to 43 on
# the live `Session Cache`, 2026-08-30). It CANNOT close the gap between publish's own pre-read and
# its PATCH. These tests pin the first and must not be read as covering the second.

def _sha(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()


def test_publish_REFUSES_when_the_page_moved_under_the_draft(tmp_path):
    """The lost update, as a test: the caller based its draft on one page and the forge has another."""
    on_forge = b"# Coordinator Handoff\n\nsomebody else republished this\n"
    s = _harness(tmp_path, PAGE, page_content=on_forge)
    f = tmp_path / "draft.md"
    f.write_bytes(b"# Coordinator Handoff\n\nmy edit, built from an older copy\n")

    r = _run(s, "publish", str(f), HANDOFF_EXPECT_SHA=_sha(b"the page as it was when I fetched it"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSING" in r.stderr and "moved since your draft" in r.stderr, r.stderr
    # BOTH SHAS PRINTED. "they differ" is not auditable; the two values are.
    assert _sha(on_forge) in r.stderr, r.stderr
    assert "Nothing has been sent" in r.stderr, r.stderr
    assert "VERIFIED" not in r.stdout, r.stdout


def test_a_MATCHING_base_publishes_normally(tmp_path):
    """THE POSITIVE CONTROL. Without it, a check that refused every publish carrying the variable
    would pass the test above -- and the variable is meant to be set on the common path."""
    body = b"# Coordinator Handoff\n\nunchanged\n"
    s = _harness(tmp_path, PAGE, page_content=body)
    f = tmp_path / "draft.md"
    f.write_bytes(body)
    # HANDOFF_NO_MEASURE: the stub echoes canned bytes, so a publish-time block cannot round-trip.
    r = _run(s, "publish", str(f), HANDOFF_EXPECT_SHA=_sha(body), HANDOFF_NO_MEASURE="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "VERIFIED" in r.stdout, r.stdout
    assert "REFUSING" not in r.stderr, r.stderr


def test_an_UNSET_expected_sha_does_not_silently_pass_as_a_match(tmp_path):
    """Absent means NOT CHECKED, and the two must not look alike. An unset variable skips the
    comparison entirely -- it must never render as a base that was compared and agreed."""
    on_forge = b"# Coordinator Handoff\n\nsomebody else republished this\n"
    s = _harness(tmp_path, PAGE, page_content=on_forge)
    f = tmp_path / "draft.md"
    f.write_bytes(on_forge)
    r = _run(s, "publish", str(f), HANDOFF_NO_MEASURE="1")
    assert r.returncode == 0, r.stdout + r.stderr
    # It published, and it claimed nothing about a base it never had.
    assert "moved since your draft" not in r.stderr, r.stderr
    assert "stale-base" not in r.stdout, r.stdout


def test_an_unreadable_page_reports_the_stale_base_check_did_NOT_run(tmp_path):
    """`could not look` must never read as `base unchanged` -- the vacuous-check failure this repo
    keeps filing. The publish still proceeds; what must not happen is silence."""
    s = _harness(tmp_path, PAGE, page_content=None)   # forge 404s the page read
    f = tmp_path / "draft.md"
    f.write_bytes(b"# Coordinator Handoff\n\nanything\n")
    r = _run(s, "publish", str(f), HANDOFF_EXPECT_SHA=_sha(b"whatever"))
    assert "did NOT run" in r.stderr, r.stderr
    assert "never 'base unchanged'" in r.stderr, r.stderr


# --- publish now serialises its read-modify-write, as edit_body already did ------------------
#
# An earlier fix gave publish an expected-base comparison, which closes the WIDE window and leaves the gap
# between that comparison and the PATCH. Its resolution said only the server could close that gap;
# that was wrong. Re-reading cannot -- LOCKING can, for every writer through this client, which on
# this box is the realistic population: several sessions on one machine each republishing at its end.
#
# UNTESTED HERE, AND SAID RATHER THAN IMPLIED: the no-flock degradation branch. Removing flock(1)
# from PATH without also removing python3 is not reliably expressible in this harness, so that arm
# is covered by reading only. It fails LOUD by construction (it prints before proceeding), which is
# the property that matters, but nothing here proves it.

def test_publish_REFUSES_when_another_publisher_holds_the_lock(tmp_path):
    """THE NEGATIVE CONTROL, and it can only be observed by actually holding the lock.

    Waiting is right for ordinary contention -- a publish is seconds, and refusing instantly would
    turn a busy moment into a failed handoff exactly when a shift ends. What must not happen is
    proceeding as though the lock had been taken, so the timeout refuses and says so.
    """
    import fcntl
    body = b"# Coordinator Handoff\n\nunchanged\n"
    s = _harness(tmp_path, PAGE, page_content=body)
    f = tmp_path / "draft.md"
    f.write_bytes(body)
    lock = tmp_path / "publish.lock"

    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        r = _run(s, "publish", str(f), HANDOFF_LOCK=str(lock), HANDOFF_LOCK_WAIT="1", HANDOFF_NO_MEASURE="1")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "could not take the publish lock" in r.stderr, r.stderr
    assert "Nothing has been sent" in r.stderr, r.stderr
    assert "VERIFIED" not in r.stdout, r.stdout


def test_publish_succeeds_when_the_lock_is_FREE(tmp_path):
    """THE POSITIVE CONTROL. Without it, a lock that could never be acquired would satisfy the test
    above -- a transport that never writes is worse than the race it was added to prevent."""
    body = b"# Coordinator Handoff\n\nunchanged\n"
    s = _harness(tmp_path, PAGE, page_content=body)
    f = tmp_path / "draft.md"
    f.write_bytes(body)
    lock = tmp_path / "publish.lock"

    r = _run(s, "publish", str(f), HANDOFF_LOCK=str(lock), HANDOFF_LOCK_WAIT="1", HANDOFF_NO_MEASURE="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "VERIFIED" in r.stdout, r.stdout
    assert "could not take" not in r.stderr, r.stderr
    # The lock was really used, not merely configured: publish creates it if absent.
    assert lock.exists(), "no lock file was created -- the critical section is unguarded"


def test_publish_takes_a_lock_file_it_can_READ_but_not_write(tmp_path):
    """The shared-lock rule at this site: the lock is opened READ-ONLY. On the box the shared lock in
    sticky /tmp is owned by the forge runner, and `protected_regular` refuses any O_CREAT open of
    another user's file whatever its mode -- so an append open died with "cannot create" and no
    session could publish. A 0444 file this user owns fails an append open the same way and needs
    no second user, so it stands in for that file here."""
    body = b"# Coordinator Handoff\n\nunchanged\n"
    s = _harness(tmp_path, PAGE, page_content=body)
    f = tmp_path / "draft.md"
    f.write_bytes(body)
    lock = tmp_path / "publish.lock"
    lock.touch()
    lock.chmod(0o444)

    r = _run(s, "publish", str(f), HANDOFF_LOCK=str(lock), HANDOFF_LOCK_WAIT="1", HANDOFF_NO_MEASURE="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "VERIFIED" in r.stdout, r.stdout
    assert "cannot create" not in r.stderr and "REFUSING" not in r.stderr, r.stderr


def test_the_lock_is_RELEASED_so_a_later_publish_is_not_blocked(tmp_path):
    """A lock held past the process is a wedge that looks like contention. The kernel drops it on
    exit; this pins that a second publish through the same path still goes through."""
    body = b"# Coordinator Handoff\n\nunchanged\n"
    s = _harness(tmp_path, PAGE, page_content=body)
    f = tmp_path / "draft.md"
    f.write_bytes(body)
    lock = tmp_path / "publish.lock"

    first = _run(s, "publish", str(f), HANDOFF_LOCK=str(lock), HANDOFF_LOCK_WAIT="1", HANDOFF_NO_MEASURE="1")
    second = _run(s, "publish", str(f), HANDOFF_LOCK=str(lock), HANDOFF_LOCK_WAIT="1", HANDOFF_NO_MEASURE="1")
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr


# --- publish re-measures the tree into a block the author cannot write by hand ----------
#
# A handoff's state claims decay, and the reader they are written for is the one who cannot
# re-derive them. `publish` now appends the cheap readings -- branch, ahead/behind, dirty count,
# open PR, `git worktree list` -- beside the prose, so the two can be seen to disagree.

MBEGIN, MEND = "<!-- handoff-measured:begin -->", "<!-- handoff-measured:end -->"


def _publish_in(script, cwd, page, body, env=None):
    # The draft lives OUTSIDE the tree being measured, or it is itself one of the dirty files.
    f = Path(cwd).parent / "draft.md"
    f.write_text(body)
    r = subprocess.run(
        ["sh", str(script), "publish", "--dry-run", str(f)],
        capture_output=True, text=True, cwd=str(cwd), timeout=60,
        env={**FT_SITE, "HOME": "/nonexistent", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "HANDOFF_PAGE": page, **(env or {})},
    )
    return r, f


def test_publish_appends_a_measured_block_to_a_handoff_page(tmp_path):
    """THE INJECTION: prose claiming the tree is clean, on a tree that is one behind and dirty."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=True)
    (work / "scratch.txt").write_text("uncommitted\n")
    r, f = _publish_in(s, work, "Handoff abc", "# handoff\n\nall worktrees cleaned, tree is clean.\n")
    body = f.read_text()
    assert body.count(MBEGIN) == 1 and body.count(MEND) == 1, body
    block = body[body.index(MBEGIN):body.index(MEND)]
    assert f"measured-in: {work.resolve()}" in block, block
    assert "ahead/behind hub/main: 0/1" in block, block
    assert "dirty files: 1" in block, block
    assert "worktrees:\n  " + str(work.resolve()) in block, block
    assert "PUBLISHING box only" in block, "the ceiling must travel with the readings"
    assert "all worktrees cleaned, tree is clean." in body, "the prose is kept, not corrected"
    assert "re-measured" in r.stderr


def test_the_measured_block_reports_every_listed_worktrees_branch(tmp_path):
    """`Handoff CC 1` handed over `agent/274-probe-doc` as "pushed at handoff if the push
    below succeeded", and the block measured only the publishing tree's own branch, so the successor
    ran `git ls-remote` itself. A pushed branch one ahead, and one that never left the box."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False)
    _git(work, "config", "user.email", "t@example.org")
    _git(work, "config", "user.name", "claude")
    pushed, local = tmp_path / "wt-pushed", tmp_path / "wt-local"
    _git(work, "worktree", "add", "-q", "-b", "agent/pushed", str(pushed))
    (pushed / "b.txt").write_text("ahead\n")
    _git(pushed, "add", "-A"); _git(pushed, "commit", "-qm", "ahead")
    _git(pushed, "push", "-q", "hub", "agent/pushed")
    _git(work, "worktree", "add", "-q", "-b", "agent/local", str(local))
    sha = _git(pushed, "rev-parse", "HEAD").stdout.strip()[:10]

    _, f = _publish_in(s, work, "Handoff abc", "# handoff\n\nagent/pushed: pushed if the push below succeeded\n")
    body = f.read_text()
    block = body[body.index(MBEGIN):body.index(MEND)]
    assert f"  agent/pushed  on hub: {sha}  ahead/behind hub/main: 1/0" in block, block
    assert "  agent/local  on hub: not on hub  ahead/behind hub/main: 0/0" in block, block


def _fake_proc(tmp_path, agents):
    """A /proc with only the pids we put in it. `agents` is {pid: (comm, cwd)}.

    `cwd=None` leaves the symlink off, which raises OSError out of `readlink` -- the same branch a
    real EACCES takes. A fixture cannot make a cwd unreadable without a second uid, and the branch
    under test is "readlink raised", not which errno it raised.
    """
    proc = tmp_path / "fakeproc"
    for pid, (comm, cwd) in agents.items():
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        if cwd is not None:
            (d / "cwd").symlink_to(cwd)
    return proc


def test_the_measured_block_names_the_session_that_owns_each_worktree(tmp_path):
    """The audit of `Handoff CC 5`: the block listed seventeen trees and one stale on-hub branch with
    nothing saying which were abandoned, which is the single fact the next action needs. Reaping a
    peer's tree on inference has already cost this box a session's scratchpad.

    Ownership is an agent cwd AT or UNDER the tree -- wider than `cosession.others()` on purpose: a
    session sitting in a subdirectory of its own tree must not read as absent, because absent is the
    reading that gets a tree deleted. A non-agent process in a tree confers nothing.
    """
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False)
    _git(work, "config", "user.email", "t@example.org")
    _git(work, "config", "user.name", "claude")
    owned, sub, busy = tmp_path / "wt-owned", tmp_path / "wt-sub", tmp_path / "wt-busy"
    for i, wt in enumerate((owned, sub, busy)):
        _git(work, "worktree", "add", "-q", "-b", "agent/w%d" % i, str(wt))
    (sub / "deep").mkdir()
    proc = _fake_proc(tmp_path, {
        4001: ("claude", owned),          # cwd IS the tree
        4002: ("omp", sub / "deep"),      # cwd is UNDER the tree -- still its owner
        4003: ("pytest", busy),           # occupancy, never ownership
    })

    _, f = _publish_in(s, work, "Handoff abc", "# handoff\n\nall worktrees cleaned.\n",
                       env={"COSESSION_PROC": str(proc)})
    block = f.read_text()
    block = block[block.index(MBEGIN):block.index("branches (")]
    # BY LINE, because `git worktree list` pads the path column: an assertion that assumed one
    # spacing would pass or fail on the width of the longest path in the fixture.
    def owner_of(path):
        hits = [l for l in block.splitlines() if l.strip().startswith(str(path.resolve()) + " ")]
        assert len(hits) == 1, "expected exactly one line for %s, got %r" % (path, hits)
        return hits[0].split("  owner: ", 1)[1]

    assert owner_of(owned) == "claude pid 4001", block
    assert owner_of(sub) == "omp pid 4002", "a cwd UNDER the tree still owns it"
    assert owner_of(busy) == "NONE FOUND", "a pytest process in a tree is occupancy, not an owner"
    assert owner_of(work) == "NONE FOUND", block
    assert "NONE FOUND IS NOT PROOF OF ABANDONMENT" in block, \
        "the ceiling must travel with the reading, or absence gets read as a fact"


def test_an_owner_it_cannot_measure_never_reads_as_unowned(tmp_path):
    """FAILS SOFT TO UNMEASURED, NEVER TO NONE FOUND. This is the whole block's doctrine
    and the dangerous direction is specific here: a tree that reads unowned gets deleted. Not a
    `__control` -- it fails on the unfixed tree, where there is no owner column at all.
    """
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False)
    _, f = _publish_in(s, work, "Handoff abc", "# handoff\n\nbody\n",
                       env={"COSESSION_PROC": str(tmp_path / "no-such-proc")})
    block = f.read_text()
    block = block[block.index(MBEGIN):block.index("branches (")]
    assert "owner: UNMEASURED" in block, block
    assert "NONE FOUND" not in block, "an unreadable /proc must not render as 'nobody owns this'"
    assert "is unreadable" in block, "say WHY it could not be measured, once"


def test_an_agent_whose_cwd_cannot_be_read_is_reported_beside_the_findings(tmp_path):
    """The unreadable-cwd condition, and the reason this does not just call `worktree-reap.sh`: the reaper
    refuses EVERY verdict while one agent is unaccounted for. Measured on this box 2026-09-22 -- one
    foreign `claude` and its whole report collapses to a single refusal line. Positive attribution
    does not need that pid resolved, so this reports what it sees and names what it cannot.
    """
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False)
    proc = _fake_proc(tmp_path, {4004: ("claude", None)})
    _, f = _publish_in(s, work, "Handoff abc", "# handoff\n\nbody\n",
                       env={"COSESSION_PROC": str(proc)})
    block = f.read_text()
    block = block[block.index(MBEGIN):block.index("branches (")]
    assert "UNACCOUNTED: 1 live agent(s)" in block and "4004(claude)" in block, block
    assert "refuses every" in block, "name the consequence, not just the count"


def test_an_unplaceable_agent_of_another_uid_is_not_UNACCOUNTED(tmp_path):
    """every handoff on this box reported dsh's SDK `claude` (uid 992) as unaccounted.
    Another uid's process is not our session; the same fixture at OUR uid is the test above."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False)
    proc = _fake_proc(tmp_path, {4004: ("claude", None)})
    (proc / "4004" / "status").write_text("Uid:\t%d\t%d\t%d\t%d\n" % ((os.getuid() + 1,) * 4))
    _, f = _publish_in(s, work, "Handoff abc", "# handoff\n\nbody\n",
                       env={"COSESSION_PROC": str(proc)})
    block = f.read_text()
    block = block[block.index(MBEGIN):block.index("branches (")]
    assert "UNACCOUNTED" not in block, block


def test_branch_state_with_no_remote_reads_UNMEASURED__control(tmp_path):
    """No `hub` to ask must never render as "not on hub" -- that would be an absence read as a fact."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False, with_remote=False)
    _, f = _publish_in(s, work, "Handoff abc", "# handoff\n\nbody\n")
    block = f.read_text()
    assert "on hub: UNMEASURED" in block and "not on hub" not in block, block


def test_a_republish_replaces_the_measured_block_rather_than_accumulating(tmp_path):
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False)
    _, f = _publish_in(s, work, "Handoff abc", "# handoff\n\nbody\n")
    for _ in range(2):
        subprocess.run(["sh", str(s), "publish", "--dry-run", str(f)], cwd=str(work),
                       capture_output=True, text=True, timeout=60,
                       env={**FT_SITE, "HOME": "/nonexistent", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "HANDOFF_PAGE": "Handoff abc"})
    body = f.read_text()
    assert body.count(MBEGIN) == 1, body
    assert body.count("body\n") == 1, "the prose was duplicated"


def test_the_measured_block_sits_BEFORE_the_signature_so_both_stay_single(tmp_path):
    """The stamp replaces an attestation only when it sits at EOF. A block appended after it would
    make every republish accumulate signatures -- the shape the attestation tests pinned against."""
    s = _harness_with_attest(tmp_path, PAGE, page_content=b"x")
    work = _tree_at(tmp_path, behind=False)
    f = tmp_path / "draft.md"
    f.write_text("# handoff\n\nbody\n")
    env = {**FT_SITE, "HOME": "/nonexistent", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HANDOFF_PAGE": "Handoff abc", "FORGE_TOOLS_SESSION_ID": SID}
    for _ in range(3):
        subprocess.run(["sh", str(s), "publish", "--dry-run", str(f)], cwd=str(work),
                       capture_output=True, text=True, timeout=60, env=env)
    body = f.read_text()
    assert body.count(MBEGIN) == 1 and body.count(BEGIN) == 1, body
    assert body.rfind(MBEGIN) < body.rfind(BEGIN), "the measured block must precede the signature"


def test_a_quoted_measured_block_in_the_prose_survives(tmp_path):
    """Only the script's own LAST reading is replaced; a page explaining the mechanism may quote one."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False)
    quoted = f"here is what one looks like:\n\n{MBEGIN}\nmeasured-at: EXAMPLE-KEEP-ME\n{MEND}\n\nmore prose\n"
    _, f = _publish_in(s, work, "Handoff abc", "# handoff\n\n" + quoted)
    body = f.read_text()
    assert "EXAMPLE-KEEP-ME" in body, body
    assert body.count(MBEGIN) == 2, body


def test_repo_state_pages_get_no_measured_block__control(tmp_path):
    """Session Cache and Hot Cache are repo state with no author whose claims a reading could
    contradict; a block there would also eat into a page with a hard size ceiling."""
    pages = [{"title": "Session Cache", "sub_url": "Session-Cache"}]
    s = _harness(tmp_path, pages, page_content=b"x")
    work = _tree_at(tmp_path, behind=False)
    _, f = _publish_in(s, work, "Session Cache", "# Session Cache\n\nbox state\n")
    assert MBEGIN not in f.read_text()


def test_a_restore_under_HANDOFF_NO_STAMP_keeps_the_other_sessions_reading(tmp_path):
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False)
    theirs = f"# theirs\n\n{MBEGIN}\nmeasured-in: /their/box THEIR-READING\n{MEND}\n"
    _, f = _publish_in(s, work, "Handoff CC 9", theirs, env={"HANDOFF_NO_STAMP": "1"})
    assert "THEIR-READING" in f.read_text()
    assert f.read_text().count(MBEGIN) == 1


def test_the_lost_block_refusal_exempts_the_measured_block(tmp_path):
    """The page carries the script's own block from the last publish; a fresh draft never does.
    Without the exemption every second publish of a handoff would be refused as destructive."""
    live = f"# handoff\n\nold\n\n{MBEGIN}\nmeasured-at: earlier\n{MEND}\n".encode()
    s = _harness(tmp_path, PAGE, page_content=live)
    work = _tree_at(tmp_path, behind=False)
    f = tmp_path / "draft.md"
    f.write_text("# handoff\n\nnew\n")
    r = subprocess.run(["sh", str(s), "publish", str(f)], cwd=str(work),
                       capture_output=True, text=True, timeout=60,
                       env={**FT_SITE, "HOME": "/nonexistent", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "HANDOFF_PAGE": "Coordinator Handoff"})
    assert "DESTROY" not in r.stderr and "carries a block your draft does not" not in r.stderr, r.stderr


def test_a_tree_with_no_remote_reports_UNMEASURED_not_a_value(tmp_path):
    """An unmeasured line that read as a number would be this ticket's defect re-created."""
    s = _harness(tmp_path, PAGE, page_content=b"old\n")
    work = _tree_at(tmp_path, behind=False, with_remote=False)
    _, f = _publish_in(s, work, "Handoff abc", "# handoff\n\nbody\n")
    block = f.read_text().split(MBEGIN)[1]
    assert "ahead/behind hub/main: UNMEASURED" in block, block
    assert "open-pr: UNMEASURED" in block, block


# --- publish must not overwrite a page a DIFFERENT LIVE session holds -----------------
#
# MEASURED 2026-09-21 from the forge's own wiki revisions of `Handoff CC 1`:
#     18:06:15  slot: claim CC 1     edad0f6c claims; page created
#     22:12:49  handoff: publish     4826be48 publishes ONTO it
#     22:12:56  its `predecessor` record, 7 seconds later
# No `slot: claim` between them: the second session never claimed the slot, it named the page and
# published. The holder found out four hours later from `slot list`.
#
# Publish's existing refusals could not have caught it. They are shaped for a WRITTEN page -- a
# from-scratch draft, a dropped `<!-- x:begin -->` block, and the attestation guard which
# refuses to replace another session's signature. A fresh claim page is a near-empty stub carrying
# NONE of those. The object needing protection was the CLAIM, so the thing to check is the HOLDER.

HOLDER_OTHER = "4826be48-0000-4000-8000-000000000001"
HOLDER_ME = "edad0f6c-0000-4000-8000-000000000002"
CLAIM_STUB = (
    "# Handoff CC 1\n\n**Slot claimed, no handoff written yet.**\n\n"
    "- holder session: `%s`\n"
    "- PROVISIONAL: the line above is what the CLAIMER said at claim time.\n"
)


def _harness_holder(tmp_path, page_content, live):
    """`_harness` plus a session-attest.sh stub whose `resolve` liveness we control.

    Stubbed rather than real because the whole question is what happens when a session IS or IS
    NOT live, and the real script answers from this box's registry -- which the test cannot stage.
    """
    s = _harness(tmp_path, PAGE, page_content=page_content)
    # On PATH under the command's name, which is how handoff.sh reaches it.
    stub = s.parent / "attest-bin" / "session-attest"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  resolve) [ "%s" = 1 ] && exit 0 || exit 1 ;;\n'
        '  host) echo testbox ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n" % (1 if live else 0)
    )
    stub.chmod(0o755)
    return s


def _pub(s, tmp_path, **env):
    f = tmp_path / "draft.md"
    f.write_bytes(b"# Handoff CC 1\n\nreal work\n")
    env.setdefault("PATH", "%s:%s" % (s.parent / "attest-bin", os.environ.get("PATH", "/usr/bin:/bin")))
    return _run(s, "publish", "--dry-run", str(f), HANDOFF_PAGE="Coordinator Handoff", **env)


def test_publish_REFUSES_a_page_held_by_a_different_LIVE_session(tmp_path):
    s = _harness_holder(tmp_path, (CLAIM_STUB % HOLDER_OTHER).encode(), live=True)
    r = _pub(s, tmp_path, FORGE_TOOLS_SESSION_ID=HOLDER_ME)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSING to publish over" in r.stderr and HOLDER_OTHER in r.stderr, r.stderr
    assert "HANDOFF_ALLOW_FOREIGN_HOLDER=1" in r.stderr, "the refusal must name its override"


def test_publish_reads_the_PROVISIONAL_holder_key_too(tmp_path):
    """The claim page carries only `- holder session:` (SPACE). `- holder-session:` (HYPHEN) is
    written later by `lineage`. A guard reading only the authoritative key would protect every page
    EXCEPT the freshly-claimed stub -- which is the only kind that was actually taken."""
    s = _harness_holder(tmp_path, (CLAIM_STUB % HOLDER_OTHER).encode(), live=True)
    assert "- holder-session:" not in CLAIM_STUB % HOLDER_OTHER, "fixture must carry ONLY the space key"
    r = _pub(s, tmp_path, FORGE_TOOLS_SESSION_ID=HOLDER_ME)
    assert r.returncode == 1 and "REFUSING to publish over" in r.stderr, r.stderr


def test_publishing_to_YOUR_OWN_page_never_consults_the_registry__control(tmp_path):
    """The own-page path must return before any resolve, so a registry that cannot answer can never
    lock a session out of its own page. Proven by making resolve ALWAYS say not-live: if the guard
    consulted it for an own page, the notice would appear."""
    s = _harness_holder(tmp_path, (CLAIM_STUB % HOLDER_ME).encode(), live=False)
    r = _pub(s, tmp_path, FORGE_TOOLS_SESSION_ID=HOLDER_ME)
    assert "REFUSING to publish over" not in r.stderr, r.stderr
    assert "does not resolve to a live one" not in r.stderr, (
        "an own-page publish consulted the registry:\n" + r.stderr)


def test_a_foreign_holder_that_is_NOT_live_proceeds_and_says_so(tmp_path):
    """`session-attest.sh resolve` says of itself that the registry holds live sessions only, so a
    miss does not distinguish ENDED from never-existed. Refusing here would block every legitimate
    publish to an inherited page whose claimer is gone -- the `adopt` case by construction."""
    s = _harness_holder(tmp_path, (CLAIM_STUB % HOLDER_OTHER).encode(), live=False)
    r = _pub(s, tmp_path, FORGE_TOOLS_SESSION_ID=HOLDER_ME)
    assert "REFUSING to publish over" not in r.stderr, r.stderr
    assert "does not resolve to a live one" in r.stderr, "it proceeded SILENTLY:\n" + r.stderr


def test_a_foreign_holder_whose_liveness_CANNOT_be_checked_is_REFUSED(tmp_path):
    """with `session-attest` off PATH, "does not resolve" would read as "not live" and a
    live holder's page would be overwritten. The same fixture as the test above, minus the command:
    there it proceeds, here it must refuse, and say which command is missing."""
    s = _harness_holder(tmp_path, (CLAIM_STUB % HOLDER_OTHER).encode(), live=False)
    path = ":".join(d for d in os.environ.get("PATH", "/usr/bin:/bin").split(":")
                    if d and not os.path.exists(os.path.join(d, "session-attest")))
    r = _pub(s, tmp_path, FORGE_TOOLS_SESSION_ID=HOLDER_ME, PATH=path)
    assert r.returncode == 1, r.stderr
    assert "REFUSING to publish over" in r.stderr and "cannot be checked" in r.stderr, r.stderr
    assert "MISSING COMMAND `session-attest`" in r.stderr, r.stderr


def test_the_override_lets_a_deliberate_takeover_through(tmp_path):
    """BOTH HALVES IN ONE TEST, because the override half alone proves nothing.

    Asserting only that the override does NOT refuse is trivially true on a tree with no guard --
    there is no refusal to suppress. The differential gate caught exactly that and was
    right to: the first version of this test passed on the merge base. So it now pins the PAIR --
    the same fixture must refuse WITHOUT the variable and proceed WITH it, which cannot both hold
    unless the guard exists and the variable is what disarms it.
    """
    s = _harness_holder(tmp_path, (CLAIM_STUB % HOLDER_OTHER).encode(), live=True)
    off = _pub(s, tmp_path, FORGE_TOOLS_SESSION_ID=HOLDER_ME)
    assert "REFUSING to publish over" in off.stderr, (
        "the fixture must refuse without the override, or the next assertion is vacuous:\n" + off.stderr)
    on = _pub(s, tmp_path, FORGE_TOOLS_SESSION_ID=HOLDER_ME, HANDOFF_ALLOW_FOREIGN_HOLDER="1")
    assert "REFUSING to publish over" not in on.stderr, on.stderr


def test_a_page_with_no_holder_line_is_not_guarded__control(tmp_path):
    """Most pages are not slot pages. The guard must be silent on them, or it becomes a tax on
    every publish rather than a protection for claims."""
    s = _harness_holder(tmp_path, b"# Some page\n\nno holder line here\n", live=True)
    r = _pub(s, tmp_path, FORGE_TOOLS_SESSION_ID=HOLDER_ME)
    assert "REFUSING to publish over" not in r.stderr and "does not resolve" not in r.stderr, r.stderr
