"""Which path a merge took is written down at merge time, because it cannot be recovered.

WHY A RECORD AT ALL. `merge_queued` carries guarantees a merge outside it does not: the review
hold, branch auto-retire, hold-labelling, and the `head_commit_id` CAS. Detecting
merges that skipped them needs a discriminator, and both candidates fail:

  * THE SUBJECT FORM is byte-identical for a queue merge and a hand-called `hub-api.sh pr merge`.
    It has to be -- the merge convention requires `(#N)` in the subject for any call of that verb,
    because the forge appends nothing. Measured on a real merge.
  * BRANCH SURVIVAL conflates "the queue retired it" with "a human retired it an hour later", and
    decays toward "retired" for every path as cleanup happens. Measured on a PR merged in the web
    UI and tidied by hand.

So this file gates the first option: write it at the moment it is knowable.

WHAT IT DOES NOT ESTABLISH, stated because a discriminator must be shown to
separate all three paths and not two. The web-UI path cannot be exercised here at all -- it is
Forgejo's own template, produced by a server this suite does not run -- so the classification arm
below asserts on the recorded SHAPE of that subject, not on a merge that produced it. And nothing
here proves Forgejo stores `MergeMessageField` verbatim; that is measured on the forge, once, by
the PR that ships this. A green run of this file means the client SENDS the right payload.

THE FORGERY CEILING IS DELIBERATE. `HUB_API_MERGE_PATH` is an env var, not a credential: a caller
who copies `merge_queued`'s command line stamps `pr-queue` on a merge the queue never made, and
nothing detects it. That is recorded in the source and is not a defect this file can close -- only
the forge writing the trailer itself would, and it will not.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_API = REPO_ROOT / "scripts" / "hub-api.sh"
QUEUE = REPO_ROOT / "scripts" / "pr-queue.sh"


# Any forge URL: the curl stub answers every host.
_OLD_URL = {"FORGE_TOOLS_FORGE_URL": "https://forge.example.org"}


def _client(tmp_path):
    """hub-api.sh pointed at a stub forge, so the merge POST is captured rather than sent.

    The stub records the payload of the merge call and answers every read with the minimum the
    verb needs. It is a curl stand-in, so it sees exactly the argv `api()` builds.
    """
    cfg = tmp_path / "hub-api.conf"
    cfg.write_text('header = "Authorization: token %s"\n' % ("a" * 40))
    cfg.chmod(0o600)
    return cfg


def _merge(tmp_path, *, env_extra=None, expect_payload=True):
    """Run `pr merge` against a stubbed curl and return the JSON payload it would have POSTed.

    `expect_payload=False` for the refusal arm, where NO payload is the assertion -- calling
    `pytest.fail` on the absence would turn the passing case into a failure.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    captured = tmp_path / "payload.json"
    curl = tmp_path / "curl"
    # Records the -d body of the merge POST and answers the commits read with one commit.
    curl.write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do\n"
        "  case \"$prev\" in -d) printf '%s' \"$a\" > '{cap}' ;; esac\n"
        "  prev=$a\n"
        "done\n"
        "case \"$*\" in\n"
        "  *'/merge'*) printf 'merged: http=200\\n' ;;\n"
        # `\\\\n` here so the FILE contains `\\n` and printf emits a literal backslash-n -- the JSON
        # escape. Written `\\n` the file gets `\n`, printf emits a real newline, and the payload is
        # invalid JSON inside a string: the client then falls back to an empty body and this fixture
        # silently stops testing body preservation. Caught by the assertion below, which is the only
        # reason it is not still wrong.
        "  *'/commits'*) printf '[{\"commit\":{\"message\":\"subj\\\\n\\\\nbody line\"}}]' ;;\n"
        "  *'/wiki/pages'*) printf '[]' ;;\n"          # no queue freeze
        "  *) printf '{}' ;;\n"
        "esac\n"
        "exit 0\n".replace("{cap}", str(captured))
    )
    curl.chmod(0o755)
    env = dict(
        os.environ,
        HUB_API_CONFIG=str(_client(tmp_path)), **_OLD_URL,
        PATH="%s:%s" % (tmp_path, os.environ.get("PATH", "")),
        # The trailer is the two-parent form's record; the default is fast-forward,
        # whose record is a label -- tested below with a stub that keeps EVERY payload.
        HUB_API_MERGE_STYLE="merge",
    )
    env.update(env_extra or {})
    r = subprocess.run(
        ["sh", str(HUB_API), "pr", "merge", "o/r", "42", "a subject (#42)",
         "b" * 40],
        capture_output=True, text=True, env=env, timeout=60,
    )
    if not captured.exists():
        if not expect_payload:
            return None, r
        pytest.fail("no merge payload captured; client said:\n%s\n%s" % (r.stdout, r.stderr))
    return json.loads(captured.read_text()), r


# ------------------------------------------------------------------ the record itself


def test_a_hand_called_merge_records_itself_as_direct(tmp_path):
    """THE DEFAULT MUST BE HONEST. A merge that is not the queue's must not be silent about it --
    silence is what made this unrecoverable in the first place."""
    payload, r = _merge(tmp_path)
    body = payload.get("MergeMessageField", "")
    assert "Merge-Path: hub-api-direct" in body, (body, r.stdout, r.stderr)


def test_the_queue_records_itself_as_the_queue(tmp_path):
    """What `merge_queued` sets. Asserted on the client, since that is where the trailer is written."""
    payload, _ = _merge(tmp_path, env_extra={"HUB_API_MERGE_PATH": "pr-queue"})
    assert "Merge-Path: pr-queue" in payload.get("MergeMessageField", "")


def test_the_two_paths_are_distinguishable(tmp_path):
    """THE WHOLE POINT, and the thing neither existing discriminator could do.

    Same subject, same sha, same PR number -- the inputs that made a queue merge and a hand merge
    byte-identical before this change. Only the trailer differs.
    """
    direct, _ = _merge(tmp_path / "a")
    queued, _ = _merge(tmp_path / "b", env_extra={"HUB_API_MERGE_PATH": "pr-queue"})
    assert direct["MergeTitleField"] == queued["MergeTitleField"], (
        "the fixture changed the subject, so this proves nothing about the trailer")
    assert direct["MergeMessageField"] != queued["MergeMessageField"]


def test_the_trailer_does_not_eat_the_body(tmp_path):
    """CONTROL. The earlier lesson: the merge message is where reasoning lives, and a change here
    that dropped it would be a data-loss bug wearing a provenance fix."""
    payload, _ = _merge(tmp_path)
    body = payload["MergeMessageField"]
    assert "body line" in body, body
    # A trailer must be its own paragraph or `git interpret-trailers` will not see it.
    assert body.endswith("\n\nMerge-Path: hub-api-direct"), repr(body)


def test_a_bodyless_pr_still_records_the_path(tmp_path):
    """The `else` branch. A PR whose commits carry no body used to send no MergeMessageField at
    all; if the trailer rode only on the populated branch, exactly the terse PRs -- the ones most
    likely to be hand-merged in a hurry -- would record nothing."""
    captured = tmp_path / "payload.json"
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do\n"
        "  case \"$prev\" in -d) printf '%s' \"$a\" > '{cap}' ;; esac\n"
        "  prev=$a\n"
        "done\n"
        "case \"$*\" in\n"
        "  *'/merge'*) printf 'merged: http=200\\n' ;;\n"
        "  *'/commits'*) printf '[{\"commit\":{\"message\":\"subject only\"}}]' ;;\n"
        "  *'/wiki/pages'*) printf '[]' ;;\n"          # no queue freeze
        "  *) printf '{}' ;;\n"
        "esac\n"
        "exit 0\n".replace("{cap}", str(captured))
    )
    curl.chmod(0o755)
    env = dict(os.environ, HUB_API_CONFIG=str(_client(tmp_path)), **_OLD_URL,
               PATH="%s:%s" % (tmp_path, os.environ.get("PATH", "")),
               HUB_API_MERGE_STYLE="merge")            # the trailer is the merge form's record
    subprocess.run(["sh", str(HUB_API), "pr", "merge", "o/r", "42", "a subject (#42)", "b" * 40],
                   capture_output=True, text=True, env=env, timeout=60)
    payload = json.loads(captured.read_text())
    assert payload.get("MergeMessageField") == "Merge-Path: hub-api-direct", payload


def test_a_junk_path_value_is_refused(tmp_path):
    """The value lands in a commit message that cannot be rewritten -- `main` refuses force push
    for admins too. A malformed one is permanent, so it is refused before the POST, not after."""
    payload, r = _merge(tmp_path, env_extra={"HUB_API_MERGE_PATH": "not a; path"},
                        expect_payload=False)
    out = r.stdout + r.stderr
    assert payload is None, "a junk path value still reached the merge POST: %r" % (payload,)
    assert "HUB_API_MERGE_PATH" in out and r.returncode != 0, out


# ------------------------------------------------------------------ the queue passes it


def test_pr_queue_sets_the_variable_on_its_own_merge_only(tmp_path):
    """Scope matters as much as presence. Exported at the top of the file, the var would stamp
    `pr-queue` on any `pr merge` a human happened to run in the same shell -- a provenance record
    that lies is worse than none."""
    src = QUEUE.read_text()
    assert "HUB_API_MERGE_PATH=pr-queue \"$API\" pr merge" in src, (
        "the queue no longer sets the merge path on its merge call")
    assert "export HUB_API_MERGE_PATH" not in src, (
        "the merge path is exported, so it covers more than the queue's own merge")


def test_the_fourth_path_is_unknown_rather_than_bucketed(tmp_path):
    """A merge by a fourth route must report as UNKNOWN rather than being silently
    bucketed. There is no classifier here to test -- that is the reader's -- so this asserts the property
    the record gives it: absence of a trailer is distinguishable from either recorded value, and the
    web-UI template is distinguishable from both by its own subject shape.
    """
    payload, _ = _merge(tmp_path)
    recorded = payload["MergeMessageField"]
    web_ui_subject = "Merge pull request 'a title' (#42) from branch into main"
    assert "Merge-Path:" in recorded
    assert "Merge-Path:" not in web_ui_subject
    # A pushed merge commit carries neither, which is the unknown case and must stay distinct.
    pushed = "some merge commit body with no trailer at all"
    assert "Merge-Path:" not in pushed and not pushed.startswith("Merge pull request ")


# ------------------------------------------------------------------ reading the record back

def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout


def _history_with_four_paths(tmp_path):
    """A `hub`-remoted clone whose main carries one merge of each shape. The web-ui one is the
    DELIBERATE BYPASS the ticket asks for: Forgejo's own template subject with no trailer."""
    forge = tmp_path / "forge.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(forge)], check=True)
    hub = tmp_path / "hub"
    subprocess.run(["git", "clone", "-q", "-o", "hub", str(forge), str(hub)], check=True)
    _git(hub, "config", "user.email", "t@t"); _git(hub, "config", "user.name", "t")
    (hub / "f").write_text("0\n"); _git(hub, "add", "-A"); _git(hub, "commit", "-qm", "base")
    for i, msg in enumerate([
        "queued one (#1)\n\nMerge-Path: pr-queue",
        "direct two (#2)\n\nMerge-Path: hub-api-direct",
        "Merge pull request 'web three' (#3) from agent/three into main",
        "old four (#4)",
        "Merge pull request #70 from acme/claude/pre-migration",
    ], start=1):
        _git(hub, "checkout", "-q", "-b", f"b{i}")
        (hub / f"f{i}").write_text("x\n"); _git(hub, "add", "-A"); _git(hub, "commit", "-qm", f"work {i}")
        _git(hub, "checkout", "-q", "main")
        _git(hub, "merge", "-q", "--no-ff", "-m", msg, f"b{i}")
    _git(hub, "push", "-q", "hub", "main")
    _git(hub, "fetch", "-q", "hub")
    return hub


# The consumer repo and remote, which pr-queue.sh reads from configuration.
_WAS_DEFAULT = {"PR_QUEUE_REPO": "acme/app", "FORGE_TOOLS_REMOTE": "hub"}


def _merge_paths(hub, *args):
    return subprocess.run(["sh", str(QUEUE), "merge-paths", *args], capture_output=True, text=True,
                          timeout=60, env=dict(os.environ, PR_QUEUE_HUB=str(hub), PR_QUEUE_TOOLS_DIR=str(hub / "scripts"), **_WAS_DEFAULT))


def test_merge_paths_names_each_path_and_the_deliberate_bypass(tmp_path):
    hub = _history_with_four_paths(tmp_path)
    r = _merge_paths(hub, "10")
    assert r.returncode == 0, r.stdout + r.stderr
    import re
    lines = {m.group(3): m.group(2) for m in
             (re.match(r"^([0-9a-f]{10})\s+(\S+)\s+(.*)$", l) for l in r.stdout.splitlines()) if m}
    assert lines["queued one (#1)"] == "pr-queue"
    assert lines["direct two (#2)"] == "hub-api-direct"
    assert lines["Merge pull request 'web three' (#3) from agent/three into main"] == "web-ui", "the bypass was not named"
    assert lines["old four (#4)"] == "unknown"
    assert lines["Merge pull request #70 from acme/claude/pre-migration"] == "unknown", (
        "a pre-migration GitHub merge must not be reported as a web-ui bypass")
    assert "pr-queue 1, hub-api-direct 1, web-ui 1, unknown 2." in r.stdout, r.stdout


def test_merge_paths_is_a_report_not_a_gate(tmp_path):
    """Exit 0 with a bypass present: a gate here would refuse a legitimate hand-merge in an incident."""
    hub = _history_with_four_paths(tmp_path)
    assert _merge_paths(hub, "10").returncode == 0


def test_merge_paths_refuses_a_non_numeric_window(tmp_path):
    hub = _history_with_four_paths(tmp_path)
    r = _merge_paths(hub, "ten")
    assert r.returncode == 1 and "must be a number" in r.stdout + r.stderr


# --- under fast-forward the record is a label, not a trailer -------------------------

def _merge_ff(tmp_path, *, path_env=None, readback_labels='[{"name":"merge-path:hub-api-direct"}]'):
    """`pr merge` under the default style against a curl stub that logs EVERY request line
    (url + payload) in order, and answers the label readback with `readback_labels`."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "requests.log"
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "url=''; data=''\n"
        "for a in \"$@\"; do\n"
        "  case \"$prev\" in -d) data=$a ;; esac\n"
        "  case \"$a\" in http*) url=$a ;; esac\n"
        "  prev=$a\n"
        "done\n"
        "printf '%s\\t%s\\n' \"$url\" \"$data\" >> '{log}'\n"
        "case \"$url\" in\n"
        "  */merge) printf 'merged: http=200\\n' ;;\n"
        "  */issues/42/labels) if [ -n \"$data\" ]; then printf '{}'; else printf '{rb}'; fi ;;\n"
        "  */labels) printf '{}' ;;\n"
        "  *) printf '[]' ;;\n"
        "esac\n"
        "exit 0\n".replace("{log}", str(log)).replace("{rb}", readback_labels)
    )
    curl.chmod(0o755)
    env = dict(os.environ, HUB_API_CONFIG=str(_client(tmp_path)), **_OLD_URL,
               PATH="%s:%s" % (tmp_path, os.environ.get("PATH", "")), HUB_API_MERGE_STYLE="")
    env.update(path_env or {})
    r = subprocess.run(["sh", str(HUB_API), "pr", "merge", "o/r", "42", "a subject"],
                       capture_output=True, text=True, env=env, timeout=60)
    lines = [l.split("\t", 1) for l in log.read_text().splitlines()] if log.exists() else []
    return lines, r


def test_a_fast_forward_writes_no_trailer_and_labels_the_pr_after_the_merge(tmp_path):
    lines, r = _merge_ff(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    merge = [d for u, d in lines if u.endswith("/merge")]
    assert merge and json.loads(merge[0]) == {"Do": "fast-forward-only"}, merge
    assert "Merge-Path" not in merge[0]
    urls = [u for u, _ in lines]
    label = [d for u, d in lines if u.endswith("/issues/42/labels") and d]
    assert label and json.loads(label[-1]) == {"labels": ["merge-path:hub-api-direct"]}, lines
    assert [u for u in urls if u.endswith("/merge")][0] and urls.index(next(u for u in urls if u.endswith("/merge"))) < urls.index(next(u for u in urls if u.endswith("/issues/42/labels") and True))
    assert "merge-path: hub-api-direct (label on #42)" in r.stdout, r.stdout


def test_the_queue_path_lands_on_the_label_too(tmp_path):
    lines, r = _merge_ff(tmp_path, path_env={"HUB_API_MERGE_PATH": "pr-queue"},
                         readback_labels='[{"name":"merge-path:pr-queue"}]')
    assert r.returncode == 0, r.stdout + r.stderr
    label = [d for u, d in lines if u.endswith("/issues/42/labels") and d]
    assert json.loads(label[-1]) == {"labels": ["merge-path:pr-queue"]}


def test_a_label_that_does_not_stick_is_said_and_the_merge_stands(tmp_path):
    lines, r = _merge_ff(tmp_path, readback_labels="[]")
    assert r.returncode == 0 and "merged: http=200" in r.stdout
    assert "did NOT stick" in r.stdout, r.stdout


def _merge_paths_with_api(hub, api_stub, *args):
    return subprocess.run(["sh", str(QUEUE), "merge-paths", *args], capture_output=True, text=True,
                          timeout=60, env=dict(os.environ, PR_QUEUE_HUB=str(hub), PR_QUEUE_API=str(api_stub),
                                   PR_QUEUE_TOOLS_DIR=str(hub / "scripts"), **_WAS_DEFAULT))


def test_merge_paths_reads_the_fast_forward_era_from_the_forge_beside_the_trailer_era(tmp_path):
    hub = _history_with_four_paths(tmp_path)
    api = tmp_path / "api-stub.sh"
    api.write_text("#!/bin/sh\ncase \"$1\" in */pulls?state=closed*) cat '%s' ;; *) echo '[]' ;; esac\n" % (tmp_path / "pulls.json"))
    api.chmod(0o755)
    (tmp_path / "pulls.json").write_text(json.dumps([
        {"number": 7, "merged": True, "title": "ff by the queue", "labels": [{"name": "merge-path:pr-queue"}]},
        {"number": 8, "merged": True, "title": "ff by hand", "labels": [{"name": "merge-path:hub-api-direct"}]},
        {"number": 9, "merged": True, "title": "web ui", "labels": []},
        {"number": 10, "merged": False, "title": "closed unmerged", "labels": []},
    ]))
    r = _merge_paths_with_api(hub, api, "10")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr-queue 1, hub-api-direct 1, web-ui 1, unknown 2." in r.stdout, "the trailer era must still be reported"
    assert "#7     pr-queue" in r.stdout and "#8     hub-api-direct" in r.stdout and "#9     unlabelled" in r.stdout, r.stdout
    assert "#10" not in r.stdout, "a closed-unmerged PR is not a landing"
    assert "(fast-forward era): 3 merged PR(s) among the last 4 closed: pr-queue 1, hub-api-direct 1, unlabelled 1." in r.stdout, r.stdout


def test_merge_paths_says_NOT_CHECKED_when_the_forge_cannot_be_read(tmp_path):
    hub = _history_with_four_paths(tmp_path)
    api = tmp_path / "api-stub.sh"
    api.write_text("#!/bin/sh\necho 'not json'\n"); api.chmod(0o755)
    r = _merge_paths_with_api(hub, api, "10")
    assert r.returncode == 0 and "(fast-forward era): NOT CHECKED" in r.stdout, r.stdout
