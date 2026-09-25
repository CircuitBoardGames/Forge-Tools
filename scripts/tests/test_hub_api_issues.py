"""`hub-api.sh issue` — wayfinder operations plus generic file/list/tag/note/close, against Forgejo.

Three of its behaviours fail silently, and they are the reason this file exists:

  * **`?type=issues`**. Forgejo shares one number space between issues and pull requests and
    the parameter is a false friend — it enums to `issues|pulls`, NOT to issue types. Omit it
    and a list call answers with PRs, which on hub's own repo means 3 items, none of them an
    issue. A frontier computed over PRs is wrong and looks completely normal.
  * **The frontier must FAIL CLOSED.** Its output is "you may take this ticket". An
    unreachable dependency call is not "no blockers", and a child that fell out of the
    listing is not "closed" — either read as a green light hands out work that is blocked.
  * **The blocking edge is doubly inverted against GitHub**: Forgejo identifies the other
    issue by NUMBER where GitHub wants a DB id, and `POST .../{index}/dependencies` means
    "{index} depends on the body", so {index} is the blocked ticket. Both inversions write a
    perfectly valid graph pointing the wrong way, which no status code reports.

Like `test_hub_api.py`, these drive the real script over a local HTTP server rather than
mocking its internals: the bugs worth catching live in the plumbing between curl, the JSON
and the exit code. The stub deliberately gives every issue a DB id that differs from its
number (`id == 1000 + number`), so an implementation that sent the id would be caught rather
than accidentally passing.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "hub-api.sh"
FAKE_TOKEN = "0123456789abcdef0123456789abcdef01234567"
MAP_LABEL = "wayfinder:map"
# Long enough that a contended box still lets the second reader arrive, short enough
# that the serialised case (where it never can) does not dominate the suite. It is paid
# ONCE per race: a timed-out wait leaves the barrier broken, so later waits return at once.
BARRIER_TIMEOUT = 8.0


# --------------------------------------------------------------------------- the stub forge


class Forge:
    """Enough Forgejo to run the verbs for real: issues, labels, dependencies, comments."""

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.labels: list[dict] = []
        self.deps: dict[int, list[int]] = {}
        self.comments: dict[int, list[dict]] = {}
        self.seen: list[tuple[str, str]] = []  # (method, path+query) of every request
        self.headers_seen: list[tuple[str, dict]] = []  # (method, request headers) of every request
        self.fail: dict[str, int] = {}  # path-substring -> status, to break one endpoint
        self.ignore_dep_delete = False  # answer a dependency DELETE with 200 and keep the edge
        # path-substring -> answer HTTP 200 with a signed-out message instead of data
        self.signed_out: str | None = None
        self.drop_assignees = False  # Forgejo silently ignores an assignee it will not take
        # A rendezvous on the single-issue GET, so the lost-update interleave below is
        # DETERMINISTIC rather than a bet on machine speed. See `_race`.
        self.get_barrier: threading.Barrier | None = None
        self._next = 1
        self.tokens: list[dict] = []  # GET /api/v1/users/<u>/tokens, for `token-scopes`
        self.protections: list[dict] = []  # /branch_protections, for `repo protect`
        self.protect_drop = False  # the forge accepts the write and silently keeps the OLD rule
        # `repo provision|create|fork`
        self.repos: dict[str, dict] = {}     # full_name -> repo JSON
        self.files: dict[str, dict] = {}     # full_name -> {path: text}, one tree (refs not modelled)
        self.collabs: dict[str, set] = {}    # full_name -> collaborator logins
        self.orgs: set[str] = {"o"}
        self.commits: list[tuple] = []       # (repo, path, branch) of every contents POST
        self.created: list[dict] = []        # bodies of repo-creation POSTs
        self.migrations: list[dict] = []     # bodies of /repos/migrate POSTs
        self.collab_put_ignored = False      # PUT answered 204 and not applied

    def make_repo(self, full: str, **kw) -> dict:
        meta = {"full_name": full, "mirror": False, "empty": False, "default_branch": "main", "has_actions": False}
        meta.update(kw)
        self.repos[full] = meta
        return meta

    def whitelisted(self, full: str, rule: dict) -> dict:
        """THE REAL FORGE'S SILENT DROP: a whitelisted user who is not a collaborator vanishes, HTTP 200
        (measured 2026-09-15 on all 17 non-mirror repos)."""
        if "push_whitelist_usernames" in rule:
            keep = self.collabs.get(full, set())
            rule["push_whitelist_usernames"] = [u for u in rule["push_whitelist_usernames"] if u in keep]
        return rule

    def label(self, name: str) -> dict:
        for lab in self.labels:
            if lab["name"] == name:
                return lab
        lab = {"id": 500 + len(self.labels), "name": name, "color": "#ededed"}
        self.labels.append(lab)
        return lab

    def add(self, title="t", body="", state="open", labels=(), assignees=(), pull=False):
        n = self._next
        self._next += 1
        self.issues[n] = {
            "id": 1000 + n,  # deliberately NOT the number
            "number": n,
            "title": title,
            "body": body,
            "state": state,
            "labels": [self.label(x) for x in labels],
            "assignees": [{"login": a} for a in assignees],
            "html_url": f"http://forge/o/r/issues/{n}",
            "_pull": pull,
        }
        return self.issues[n]

    def view(self, n: int) -> dict:
        return {k: v for k, v in self.issues[n].items() if not k.startswith("_")}

    def listing(self, query: dict) -> list[dict]:
        # No `type` means no filter — which is exactly the trap the caller must avoid.
        want = (query.get("type") or [None])[0]
        state = (query.get("state") or ["open"])[0]
        out = []
        for n in sorted(self.issues):
            it = self.issues[n]
            if want == "issues" and it["_pull"]:
                continue
            if want == "pulls" and not it["_pull"]:
                continue
            if state != "all" and it["state"] != state:
                continue
            if "labels" in query:
                needed = {x for chunk in query["labels"] for x in chunk.split(",") if x}
                have = {lab["name"] for lab in it["labels"]}
                if not needed.issubset(have):
                    continue
            out.append(self.view(n))
        # `limit`/`page` ARE HONOURED, and once they were not — the stub returned the
        # whole set however it was asked. That made every paging assertion vacuous: `paged` and
        # the older `list_issues` both terminate on "this page came back short", and a stub that
        # always answers in full either ends the loop on page 1 (hiding a broken pager) or never
        # ends it at all. The existing tests never noticed because none of them builds 50 issues.
        #
        # A stub that is more forgiving than the real server does not make a test pass safely, it
        # makes it measure something else.
        limit = int((query.get("limit") or [str(len(out) or 1)])[0])
        page = int((query.get("page") or ["1"])[0])
        if limit <= 0 or page <= 0:
            return []
        return out[(page - 1) * limit: page * limit]


FORGE = Forge()


class _Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method: str):
        u = urlparse(self.path)
        FORGE.seen.append((method, self.path))
        FORGE.headers_seen.append((method, dict(self.headers)))
        for frag, code in FORGE.fail.items():
            if frag in u.path:
                return self._send({"message": "injected failure"}, code)
        if FORGE.signed_out and FORGE.signed_out in u.path:
            # hub runs REQUIRE_SIGNIN_VIEW=true, which answers HTTP 200 with this body.
            return self._send({"message": "Only signed in user is allowed to call APIs."})
        q = parse_qs(u.query)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        data = json.loads(raw) if raw else {}
        # The CROSS-REPO search endpoint, routed before the repo-scoped rewrite below because
        # that rewrite would eat `/repos/issues/search` as if `issues` were an owner and
        # `search` a repo -- which is the same mis-parse the guard under test has to avoid.
        if u.path.startswith("/api/v1/users/") and u.path.endswith("/tokens"):
            return self._send(FORGE.tokens)
        if u.path == "/api/v1/repos/issues/search":
            return self._send(FORGE.listing(q))

        if u.path == "/api/v1/repos/migrate" and method == "POST":
            FORGE.migrations.append(data)
            full = "%s/%s" % (data["repo_owner"], data["repo_name"])
            return self._send(FORGE.make_repo(full, mirror=bool(data.get("mirror"))), 201)
        om = re.fullmatch(r"/api/v1/orgs/([^/]+)(/repos)?", u.path)
        if om:
            if om.group(1) not in FORGE.orgs:
                return self._send({"message": "GetOrgByName"}, 404)
            if om.group(2) and method == "POST":
                FORGE.created.append(data)
                meta = FORGE.make_repo("%s/%s" % (om.group(1), data["name"]),
                                       default_branch=data.get("default_branch", "main"), empty=not data.get("auto_init"))
                return self._send(meta, 201)
            return self._send({"name": om.group(1)})
        if u.path == "/api/v1/user":
            return self._send({"login": "claude"})

        rm = re.match(r"^/api/v1/repos/([^/]+/[^/]+)", u.path)
        full = rm.group(1) if rm else ""
        tail = re.sub(r"^/api/v1/repos/[^/]+/[^/]+", "", u.path)

        if full in FORGE.repos:
            meta = FORGE.repos[full]
            if tail == "":
                if method == "PATCH" and "has_actions" in data:
                    meta["has_actions"] = data["has_actions"]
                return self._send(meta)
            if tail.startswith("/contents/"):
                path = tail[len("/contents/"):]
                files = FORGE.files.setdefault(full, {})
                if method == "POST":
                    if path in files:
                        return self._send({"message": "file already exists"}, 422)
                    files[path] = base64.b64decode(data["content"]).decode()
                    FORGE.commits.append((full, path, data.get("branch")))
                    return self._send({"content": {"path": path}}, 201)
                if path in files:
                    return self._send({"path": path, "content": base64.b64encode(files[path].encode()).decode()})
                listing = [{"name": p.rsplit("/", 1)[1], "path": p, "type": "file"}
                           for p in files if p.rsplit("/", 1)[0] == path]
                if listing:
                    return self._send(listing)
                return self._send({"message": "GetContentsOrList", "errors": ["object does not exist"]}, 404)
            cm = re.fullmatch(r"/collaborators/([^/]+)", tail)
            if cm:
                users = FORGE.collabs.setdefault(full, set())
                if method == "PUT":
                    if not FORGE.collab_put_ignored:
                        users.add(cm.group(1))
                    return self._send(None, 204)
                return self._send(None, 204) if cm.group(1) in users else self._send({"message": "not found"}, 404)

        if tail == "/branch_protections":
            if method == "GET":
                return self._send(FORGE.protections)
            rule = FORGE.whitelisted(full, dict(data))
            rule.setdefault("branch_name", rule.get("rule_name"))
            if FORGE.protect_drop:
                rule["status_check_contexts"] = []
            FORGE.protections.append(rule)
            return self._send(rule, 201)
        # ONE PATH SEGMENT, DECODED, as the real forge routes it: `fix%2Fx` names the rule `fix/x`,
        # and a raw `fix/x` is two segments that match nothing here (404, as live).
        pm = re.fullmatch(r"/branch_protections/([^/]+)", tail)
        if pm:
            name = urllib.parse.unquote(pm.group(1))
            for rule in FORGE.protections:
                if (rule.get("branch_name") or rule.get("rule_name")) == name:
                    if method == "PATCH" and not FORGE.protect_drop:
                        rule.update(FORGE.whitelisted(full, dict(data)))
                    return self._send(rule)
            return self._send({"message": "not found"}, 404)

        if tail == "/labels":
            if method == "GET":
                return self._send(FORGE.labels)
            return self._send(FORGE.label(data["name"]), 201)

        if tail == "/issues":
            if method == "GET":
                return self._send(FORGE.listing(q))
            names = [lab["name"] for lab in FORGE.labels if lab["id"] in (data.get("labels") or [])]
            it = FORGE.add(title=data.get("title", ""), body=data.get("body", ""), labels=names)
            return self._send(FORGE.view(it["number"]), 201)

        m = re.fullmatch(r"/issues/(\d+)(/\w+)?(/.+)?", tail)
        if not m:
            return self._send({"message": f"no route {tail}"}, 404)
        num, sub = int(m.group(1)), m.group(2) or ""
        ident = (m.group(3) or "").lstrip("/")   # /labels/<name-or-id> — see below
        if num not in FORGE.issues:
            return self._send({"message": "not found"}, 404)
        it = FORGE.issues[num]

        if sub == "":
            if method == "GET":
                # SNAPSHOT FIRST, THEN WAIT. Reading after the wait delivers FRESH data
                # slowly, which is not what a lost update is made of -- the first draft did
                # that and the fault injection below failed to fail, because the second
                # reader's response was built after the first writer's PATCH had landed.
                # A stale read is a snapshot taken early and delivered late.
                snap = FORGE.view(num)
                if FORGE.get_barrier is not None:
                    try:
                        FORGE.get_barrier.wait(timeout=BARRIER_TIMEOUT)
                    except threading.BrokenBarrierError:
                        # The other reader never came, because it is blocked on the flock.
                        # That IS the serialised case, so proceeding is correct -- and the
                        # barrier stays broken, so the second reader's wait returns at once
                        # instead of paying the timeout again.
                        pass
                return self._send(snap)
            for k in ("body", "state", "title"):
                if k in data:
                    it[k] = data[k]
            if "assignees" in data and not FORGE.drop_assignees:
                it["assignees"] = [{"login": a} for a in data["assignees"]]
            return self._send(FORGE.view(num), 201)

        if sub == "/labels":
            # GET the ATTACHED labels. `pr unhold` reads this before and after removing, so a
            # no-op ("was never held") is distinguishable from a lift, and so a removal the forge
            # accepts but does not apply cannot report success.
            if method == "GET":
                return self._send(it["labels"])
            # DELETE by NAME OR ID, which is what this hub's swagger documents for
            # `/issues/{index}/labels/{identifier}`: "name or id of the label to remove". Both are
            # accepted here so a test cannot pass against a client that only ever sends one.
            if method == "DELETE":
                keep = [lab for lab in it["labels"]
                        if lab["name"] != ident and str(lab["id"]) != ident]
                it["labels"] = keep
                return self._send(None, 204)
            for name in data.get("labels") or []:
                lab = FORGE.label(name) if isinstance(name, str) else \
                    next(x for x in FORGE.labels if x["id"] == name)
                if lab not in it["labels"]:
                    it["labels"].append(lab)
            return self._send(it["labels"], 201)

        if sub == "/dependencies":
            if method == "GET":
                return self._send([FORGE.view(b) for b in FORGE.deps.get(num, [])])
            # Forgejo's body is an IssueMeta: {owner, repo, index}. `index` is the NUMBER.
            idx = data.get("index")
            if idx not in FORGE.issues:
                return self._send({"message": f"no issue with index {idx}"}, 404)
            if method == "DELETE":
                if idx in FORGE.deps.get(num, []) and not FORGE.ignore_dep_delete:
                    FORGE.deps[num].remove(idx)
                return self._send(FORGE.view(num), 200)
            FORGE.deps.setdefault(num, []).append(idx)
            return self._send(FORGE.view(num), 201)

        if sub == "/comments":
            if method == "GET":
                return self._send(FORGE.comments.get(num, []))
            c = {"body": data.get("body", ""), "user": {"login": "alice"}}
            FORGE.comments.setdefault(num, []).append(c)
            return self._send(c, 201)

        return self._send({"message": f"no route {tail}"}, 404)

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def do_PATCH(self):  # noqa: N802
        self._route("PATCH")

    def do_DELETE(self):  # noqa: N802
        self._route("DELETE")

    def do_PUT(self):  # noqa: N802
        self._route("PUT")

    def log_message(self, *a):
        pass


@pytest.fixture
def forge(tmp_path):
    """A fresh forge and a fresh config per test; yields a `run` bound to both."""
    global FORGE
    FORGE = Forge()
    # THREADING, not HTTPServer: the lost-update test drives two clients at once, and a
    # single-threaded server would serialise them at the socket and "prove" a lock that is not
    # there. Sequential tests are unaffected -- they only ever have one client in flight.
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    # poll_interval: shutdown() waits for the loop's next poll, 0.5 s by default -- half a second
    # of idle teardown per test, 131 s across a full CI-like run.
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    cfg = tmp_path / "hub-api.conf"
    cfg.write_text(f'header = "Authorization: token {FAKE_TOKEN}"\n')
    cfg.chmod(0o600)
    env = dict(os.environ)
    env["HUB_API_CONFIG"] = str(cfg)
    env["HUB_URL"] = FORGE.url = f"http://127.0.0.1:{httpd.server_port}"
    env["HUB_USER"] = "alice"
    env["FORGE_TOOLS_PUSH_WHITELIST"] = "carol dave"
    # HEAD, not hub/main: the currency guard otherwise runs a real `git fetch hub main` over the
    # network on EVERY write verb -- 1.4 s per test, 42% of the whole suite (measured 2026-09-16).
    env["HUB_API_CURRENCY_REF"] = "HEAD"

    def run(*args):
        return subprocess.run(
            ["sh", str(SCRIPT), "issue", *args, ],
            capture_output=True, text=True, env=env, timeout=60,
        )

    FORGE.run = run  # type: ignore[attr-defined]
    FORGE.env = env  # type: ignore[attr-defined]  # so a test can add HUB_API_LOCK
    yield FORGE
    httpd.shutdown()


def raw(forge, path: str):
    """Invoke the PASSTHROUGH — `hub-api.sh <path>` — rather than an `issue` verb.

    The fixture's own `run()` hardcodes `issue` as argv[1], and the passthrough is a different
    entry point with a different guard on it. Testing it through `run()` would be
    testing the verb.
    """
    return subprocess.run(["sh", str(SCRIPT), path],
                          capture_output=True, text=True, env=forge.env, timeout=60)


def client(forge, *args):
    """Invoke ANY verb — `hub-api.sh <args...>`. The fixture's `run()` hardcodes `issue` as argv[1],
    so a `pr` verb reached through it becomes `issue pr ...` and is answered by the issue usage
    string. That produced four green-looking failures the first time these tests were written."""
    return subprocess.run(["sh", str(SCRIPT), *args],
                          capture_output=True, text=True, env=forge.env, timeout=60)


def mk_map(forge, body="", children=()):
    block = "<!-- wayfinder:children -->\n" + "".join(f"- #{c}\n" for c in children) + \
        "<!-- /wayfinder:children -->"
    return forge.add(title="map", body=(body + block), labels=[MAP_LABEL])


def list_paths(forge) -> list[str]:
    """Every request that LISTED issues, as opposed to fetching one by number."""
    return [p for m, p in forge.seen if m == "GET" and re.search(r"/issues(\?|$)", p)]


# ------------------------------------------------------------------------- ?type=issues


def test_every_list_call_is_scoped_to_issues(forge):
    """Without `type=issues` a Forgejo listing answers with pull requests.

    The stub serves a PR whose number is the map's first child, so an unscoped listing would
    resolve that child to the PR and the frontier would hand it out. This asserts on the
    bytes the SERVER received rather than on the script's source, so it stays true of any
    implementation.
    """
    forge.add(title="a PR that is not a ticket", pull=True)  # #1
    forge.add(title="real ticket")  # #2
    m = mk_map(forge, children=[2])
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert r.returncode == 0, r.stdout + r.stderr
    seen = list_paths(forge)
    assert seen, "no list call happened at all — this test measured nothing"
    for p in seen:
        assert "type=issues" in p, f"list call not scoped to issues: {p}"


def test_an_unscoped_listing_would_serve_pull_requests__control(forge):
    """Control for the test above.

    It asserts every list call carried `type=issues`, which is also what a run that made no
    list call at all produces. This pins that the stub's PR is invisible only BECAUSE of the
    parameter: fetched without it, the same listing hands back the pull request.
    """
    forge.add(title="a PR", pull=True)
    forge.add(title="real ticket")
    with urllib.request.urlopen(f"{forge.url}/api/v1/repos/o/r/issues?state=all") as resp:
        unscoped = json.load(resp)
    with urllib.request.urlopen(
        f"{forge.url}/api/v1/repos/o/r/issues?state=all&type=issues"
    ) as resp:
        scoped = json.load(resp)
    assert any(i["title"] == "a PR" for i in unscoped), \
        "the stub serves no PRs unfiltered, so the test above proves nothing"
    assert not any(i["title"] == "a PR" for i in scoped)


# ------------------------------------------------------------------ the frontier, fail closed


def test_frontier_refuses_when_a_blocker_cannot_be_read(forge):
    """An unreachable dependency call is NOT 'no blockers'.

    This is the failure that would hand a caller a blocked ticket with a clean exit code.
    """
    forge.add(title="candidate")  # #1
    m = mk_map(forge, children=[1])
    forge.fail["/issues/1/dependencies"] = 500
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSING" in r.stderr
    assert "FRONTIER:" not in r.stdout, "it printed a verdict it could not determine"


def test_frontier_refuses_a_child_missing_from_the_listing(forge):
    """A child on the map that the listing does not contain is unknown, not closed."""
    m = mk_map(forge, children=[404])
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "incomplete list" in r.stderr
    assert "FRONTIER:" not in r.stdout


def test_frontier_takes_the_first_open_unclaimed_unblocked_child_in_map_order(forge):
    closed = forge.add(title="done", state="closed")  # #1
    claimed = forge.add(title="taken", assignees=["someone"])  # #2
    blocked = forge.add(title="blocked")  # #3
    winner = forge.add(title="takeable")  # #4
    later = forge.add(title="also takeable")  # #5
    blocker = forge.add(title="blocker still open")  # #6
    forge.deps[blocked["number"]] = [blocker["number"]]
    m = mk_map(forge, children=[c["number"] for c in (closed, claimed, blocked, winner, later)])

    r = forge.run("frontier", "o/r", str(m["number"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FRONTIER: #4" in r.stdout
    # #5 sits below the break. Without this line the per-child listing reads as the
    # whole child set, and a session reported no parallel work while five takeable children sat
    # below the winner.
    assert "1 further child(ren) NOT EXAMINED" in r.stdout, r.stdout
    assert "#1     skip   closed" in r.stdout
    assert "claimed by someone" in r.stdout
    assert "blocked by #6" in r.stdout


def test_frontier_skips_a_child_that_is_latent_by_decision(forge):
    """open, unassigned, unblocked -- and deliberately not work.

    The three existing skips describe something the forge already knows: finished, held, blocked.
    A ticket left open ON PURPOSE is none of them, so the frontier called it TAKE. Measured twice
    on one map: it returned the same child until that closed, then another, which has been "latent BY DECISION"
    since 2026-08-29 -- so a successor told to take the frontier opens the one ticket carrying a
    written decision not to proceed.
    """
    latent = forge.add(title="open on purpose", labels=["wayfinder:latent"])  # #1
    real = forge.add(title="actual work")  # #2
    m = mk_map(forge, children=[latent["number"], real["number"]])

    r = forge.run("frontier", "o/r", str(m["number"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FRONTIER: #2" in r.stdout, r.stdout
    assert "latent by decision" in r.stdout, r.stdout
    assert "the body says why" in r.stdout, r.stdout


def test_the_same_child_without_the_label_is_taken__control(forge):
    """Control for the skip above: 'not chosen' is also what a frontier that answers nothing
    produces. One field changed, and the same child becomes the winner."""
    latent = forge.add(title="open on purpose")  # #1, no label this time
    real = forge.add(title="actual work")  # #2
    m = mk_map(forge, children=[latent["number"], real["number"]])

    r = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: #1" in r.stdout, r.stdout
    assert "latent by decision" not in r.stdout, r.stdout


# --- a claim whose session has ENDED is takeable, unless the child waits on the operator ----

ENDED_SID = "deadbeef-7330-4de7-a241-3a36fba8b868"


def _resolver(tmp_path, rc, out=""):
    """Stands in for `session-attest.sh resolve`: exit 0 one live match, 1 none, 3 ambiguous."""
    p = tmp_path / "session-attest.sh"
    p.write_text('#!/bin/sh\n[ "$1" = resolve ] || exit 2\necho "%s"\nexit %d\n' % (out, rc))
    p.chmod(0o755)
    return str(p)


def _claimed_by(forge, sid, **kw):
    it = forge.add(assignees=["claude"], **kw)
    forge.comments.setdefault(it["number"], []).append(
        {"body": "Claimed by session %s.\n\nclaim-session: %s" % (sid, sid), "user": {"login": "claude"}})
    return it


def test_claim_RECORDS_the_claiming_session(forge):
    forge.add(title="ticket")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    r = forge.run("claim", "o/r", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert any("claim-session: %s" % ENDED_SID in c["body"] for c in forge.comments.get(1, [])), forge.comments


OTHER_SID = "849dd2a3-0000-4000-8000-000000001457"


def _mine(forge):
    return [c for c in forge.comments.get(1, []) if "claim-session: %s" % ENDED_SID in c["body"]]


def test_claim_REFUSES_a_ticket_whose_claiming_session_is_LIVE(forge, tmp_path):
    """A ticket was claimed over a live claimant, exit 0, and both sessions edited the same
    file for twenty minutes before a push announce surfaced it."""
    _claimed_by(forge, OTHER_SID, title="held")
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    r = forge.run("claim", "o/r", "1")
    assert r.returncode != 0, "a LIVE claim was taken over silently:\n" + r.stdout + r.stderr
    assert "REFUSING" in r.stdout + r.stderr and "849dd2a3" in r.stdout + r.stderr, r.stdout + r.stderr
    assert not _mine(forge), "a refused claim still recorded a claim-session"


def test_claim_TAKES_OVER_a_live_claim_only_when_asked(forge, tmp_path):
    """The handover -- a successor adopting its predecessor's ticket -- names itself.

    PASSES ON BASE: the stub forge accepts any assignee login, so the old `claim` reads `--take-over`
    as a user name and still records a claim-session. The refusal test above is the one that fails
    on base; this one pins that the flag gets through the new refusal."""
    _claimed_by(forge, OTHER_SID, title="held")
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    r = forge.run("claim", "o/r", "1", "--take-over")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _mine(forge), "--take-over did not record the new claim-session"


def test_claim_of_an_ENDED_claimants_ticket_needs_no_flag__control(forge, tmp_path):
    """PASSES ON BASE: an ended claim is takeable, so the refusal must not reach it."""
    _claimed_by(forge, OTHER_SID, title="abandoned")
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 1, "NOT FOUND")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    assert forge.run("claim", "o/r", "1").returncode == 0


def test_reclaiming_your_OWN_live_claim_is_not_refused__control(forge, tmp_path):
    """PASSES ON BASE: the caller IS the live claimant, so there is nobody to take it from."""
    _claimed_by(forge, ENDED_SID, title="mine")
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    assert forge.run("claim", "o/r", "1").returncode == 0


def test_claim_of_a_RELEASED_ticket_is_not_refused_while_its_releaser_lives(forge, tmp_path):
    """`unclaim` clears the assignee and writes no comment, so the newest claim-session
    still names the releaser. Once that session sat idle and `claim` refused everyone, while
    `frontier` offered the ticket as TAKE."""
    it = _claimed_by(forge, OTHER_SID, title="released")
    it["assignees"] = []
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    r = forge.run("claim", "o/r", str(it["number"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _mine(forge), "the claim was not recorded"


def test_unclaim_REFUSES_another_sessions_LIVE_claim_without_take_over(forge, tmp_path):
    """`unclaim; claim` was a two-line bypass of the live-claim refusal:
    `unclaim` read assignees only and never asked whose claim it dropped."""
    it = _claimed_by(forge, OTHER_SID, title="held")
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    r = forge.run("unclaim", "o/r", str(it["number"]), "claude")
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "REFUSING" in out and "849dd2a3" in out, out
    assert forge.issues[it["number"]]["assignees"], "a refused release still cleared the assignee"
    r = forge.run("unclaim", "o/r", str(it["number"]), "claude", "--take-over")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not forge.issues[it["number"]]["assignees"], "--take-over did not release"


def test_unclaim_of_your_OWN_live_claim_is_not_refused__control(forge, tmp_path):
    it = _claimed_by(forge, ENDED_SID, title="mine")
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    r = forge.run("unclaim", "o/r", str(it["number"]), "claude")
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_claim_with_no_session_id_does_not_leave_the_releaser_as_the_LIVE_holder(forge, tmp_path):
    """The no-sid `claim` wrote no record, so the newest claim-session stayed the
    RELEASER's, and a third party was refused with the releaser named as LIVE -- an earlier
    symptom back, now also blocking the real holder's own `resolve`."""
    it = _claimed_by(forge, OTHER_SID, title="released")
    it["assignees"] = []
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ""
    r = forge.run("claim", "o/r", str(it["number"]))
    assert r.returncode == 0, r.stdout + r.stderr
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    r = forge.run("claim", "o/r", str(it["number"]))
    out = r.stdout + r.stderr
    assert "849dd2a3" not in out, "the releaser was still named as the holder:\n" + out
    assert r.returncode == 0, out


def test_resolve_of_a_RELEASED_ticket_is_not_refused_while_its_releaser_lives(forge, tmp_path):
    """the same gap on `resolve`."""
    m = mk_map(forge, body="## Decisions so far\n\n")
    it = _claimed_by(forge, OTHER_SID, title="released")
    it["assignees"] = []
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    r = forge.run("resolve", "o/r", str(it["number"]), str(m["number"]), "done")
    assert r.returncode == 0, r.stdout + r.stderr
    assert forge.issues[it["number"]]["state"] == "closed"


def _claimed_ticket_and_map(forge):
    m = mk_map(forge, body="## Decisions so far\n\n")
    it = _claimed_by(forge, OTHER_SID, title="held")
    return m["number"], it["number"]


def test_resolve_REFUSES_a_ticket_whose_claiming_session_is_LIVE(forge, tmp_path):
    """A ticket was resolved over a live claim on 2026-09-23: `claim` refused, `resolve` did
    not, and it closes the ticket, which is the irreversible half."""
    mapno, n = _claimed_ticket_and_map(forge)
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    r = forge.run("resolve", "o/r", str(n), str(mapno), "the answer")
    assert r.returncode != 0, "a LIVE claim was resolved over:\n" + r.stdout + r.stderr
    assert "REFUSING" in r.stdout + r.stderr and "849dd2a3" in r.stdout + r.stderr, r.stdout + r.stderr
    assert forge.issues[n]["state"] == "open", "a refused resolve still closed the ticket"


def test_resolve_TAKES_OVER_a_live_claim_only_when_asked(forge, tmp_path):
    """PASSES ON BASE: the old `resolve` ignores claims entirely, so it closes the ticket with or
    without the flag. The refusal test above is the one that fails on base; this pins that the flag
    gets through the new refusal."""
    mapno, n = _claimed_ticket_and_map(forge)
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    r = forge.run("resolve", "o/r", str(n), str(mapno), "the answer", "--take-over")
    assert r.returncode == 0, r.stdout + r.stderr
    assert forge.issues[n]["state"] == "closed"


def test_resolving_your_OWN_live_claim_is_not_refused__control(forge, tmp_path):
    """PASSES ON BASE: the caller IS the live claimant."""
    m = mk_map(forge, body="## Decisions so far\n\n")
    it = _claimed_by(forge, ENDED_SID, title="mine")
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    assert forge.run("resolve", "o/r", str(it["number"]), str(m["number"]), "done").returncode == 0


def test_frontier_TAKES_a_child_whose_claiming_session_has_ENDED(forge, tmp_path):
    """Operator, 2026-09-10. A claim held by a session that no longer exists hid its ticket from the
    frontier for ever -- measured on a map where a child's claimant was provably gone."""
    held = _claimed_by(forge, ENDED_SID, title="abandoned claim")
    later = forge.add(title="free")
    m = mk_map(forge, children=[held["number"], later["number"]])
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 1, "NOT FOUND")
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: #%d" % held["number"] in r.stdout, r.stdout + r.stderr
    assert "ENDED" in r.stdout, r.stdout


def test_frontier_SKIPS_a_child_whose_claiming_session_is_LIVE__control(forge, tmp_path):
    held = _claimed_by(forge, ENDED_SID, title="actively held")
    later = forge.add(title="free")
    m = mk_map(forge, children=[held["number"], later["number"]])
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 0, "  pid       4242")
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: #%d" % later["number"] in r.stdout, r.stdout + r.stderr
    assert "(session deadbeef, LIVE)" in r.stdout, r.stdout


def test_frontier_SKIPS_a_claim_that_recorded_no_session(forge, tmp_path):
    """Every claim made before claims were recorded has no record. Unknown liveness is not an ended session."""
    held = forge.add(title="old-style claim", assignees=["claude"])
    later = forge.add(title="free")
    m = mk_map(forge, children=[held["number"], later["number"]])
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 1, "NOT FOUND")   # would say ENDED if asked
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: #%d" % later["number"] in r.stdout, r.stdout + r.stderr
    assert "no claim-session recorded" in r.stdout, r.stdout


def test_claims_lists_only_open_issues_whose_newest_claim_session_is_the_one_asked(forge):
    """A successor inherits its predecessor's claims; this is the listing `verify` prints
    at RATIFIED. Another session's claim, an assigned issue with no record, and an unclaimed issue
    must all stay out, and the trailer must say how many assigned issues were read."""
    me = forge.env["HUB_USER"]      # the verb filters on the configured login, not on a literal

    def claimed(sid, title):
        it = forge.add(assignees=[me], title=title)
        forge.comments.setdefault(it["number"], []).append(
            {"body": "Claimed by session %s.\n\nclaim-session: %s" % (sid, sid), "user": {"login": me}})
        return it

    mine = claimed(ENDED_SID, "predecessor ticket")
    other = claimed("11111111-2222-3333-4444-555555555555", "another session")
    unrecorded = forge.add(title="old-style claim", assignees=[me])
    free = forge.add(title="unclaimed")
    r = forge.run("claims", "o/r", ENDED_SID)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#%d\tpredecessor ticket" % mine["number"] in r.stdout, r.stdout
    for it in (other, unrecorded, free):
        assert "#%d\t" % it["number"] not in r.stdout, r.stdout
    assert "1 claim(s) held by session deadbeef, of 3 open issue(s) assigned to %s read." % me in r.stdout, r.stdout


def test_an_AMBIGUOUS_resolver_answer_is_not_an_ended_session(forge, tmp_path):
    held = _claimed_by(forge, ENDED_SID, title="ambiguous")
    later = forge.add(title="free")
    m = mk_map(forge, children=[held["number"], later["number"]])
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 3, "AMBIGUOUS")
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: #%d" % later["number"] in r.stdout, r.stdout + r.stderr
    assert "UNKNOWN" in r.stdout, r.stdout


def test_frontier_SKIPS_a_waiting_on_operator_child_even_when_its_claimant_ENDED(forge, tmp_path):
    """The constraint the operator's ruling rests on: some tickets wait on a human, and must not
    become takeable merely because the session that claimed them ended."""
    held = _claimed_by(forge, ENDED_SID, title="operator step left", labels=["wayfinder:waiting-operator"])
    later = forge.add(title="free")
    m = mk_map(forge, children=[held["number"], later["number"]])
    forge.env["HUB_API_ATTEST"] = _resolver(tmp_path, 1, "NOT FOUND")
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: #%d" % later["number"] in r.stdout, r.stdout + r.stderr
    assert "waiting on the operator" in r.stdout, r.stdout


def test_frontier_moves_when_the_blocker_closes__control(forge):
    """Control for the skips above.

    Each of them asserts that a ticket was NOT chosen, and 'not chosen' is what a frontier
    that always answers `none` also produces. The same map with one field changed must
    produce a different winner, or the assertions above are about a constant.
    """
    blocked = forge.add(title="blocked")  # #1
    blocker = forge.add(title="blocker")  # #2
    later = forge.add(title="later")  # #3
    forge.deps[1] = [2]
    m = mk_map(forge, children=[1, 3])

    first = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: #3" in first.stdout, first.stdout
    assert "NOT EXAMINED" not in first.stdout, "the winner was the last child; nothing was skipped"

    forge.issues[blocker["number"]]["state"] = "closed"
    second = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: #1" in second.stdout, second.stdout
    assert "1 further child(ren) NOT EXAMINED" in second.stdout, second.stdout
    assert blocked["number"] == 1 and later["number"] == 3


def test_an_unknown_blocker_state_counts_as_blocking(forge):
    """Fail-closed on a value this build does not recognise, rather than `!= open`."""
    forge.add(title="candidate")  # #1
    forge.add(title="weird blocker", state="draft")  # #2
    forge.deps[1] = [2]
    m = mk_map(forge, children=[1])
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: none" in r.stdout, r.stdout + r.stderr


# ------------------------------------------------------------------------- the body scheme


def test_a_human_edited_map_body_survives_a_child_being_added(forge):
    """The map body is prose a human owns; only the marked block may be rewritten.

    The `#99` in the prose must not be mistaken for a child, and the Fog section below the
    block must still be there afterwards.
    """
    m = mk_map(
        forge,
        body="## Notes\n\nSee #99 for background.\n\n## Children\n\n",
        children=[],
    )
    m["body"] += "\n\n## Fog\n\nunknown unknowns\n"
    r = forge.run("child-create", "o/r", str(m["number"]), "first ticket", "why", "research", "--because", "serves this map")
    assert r.returncode == 0, r.stdout + r.stderr
    body = forge.issues[m["number"]]["body"]
    assert "See #99 for background." in body
    assert "## Fog\n\nunknown unknowns" in body
    child = forge.issues[max(forge.issues)]
    assert f"- #{child['number']}\n" in body
    assert "#99" not in body.split("<!-- wayfinder:children -->")[1]


@pytest.mark.parametrize(
    "block, why",
    [
        ("<!-- wayfinder:children -->\n- #1\n\nsomeone deleted the closer\n#2\n",
         "opening marker, no closing one"),
        ("- #1\n<!-- /wayfinder:children -->\n", "closing marker, no opening one"),
        ("<!-- /wayfinder:children -->\n- #1\n<!-- wayfinder:children -->\n", "closer first"),
        ("<!-- wayfinder:children -->\n- #1\n<!-- /wayfinder:children -->\n"
         "<!-- wayfinder:children -->\n- #2\n<!-- /wayfinder:children -->\n", "two blocks"),
        ("<!-- wayfinder:children -->\n- [ ] #1\n- #2\n<!-- /wayfinder:children -->\n",
         "a checkbox — not the encoding, and it would drop #1"),
        ("<!-- wayfinder:children -->\n- #beta\n- #2\n<!-- /wayfinder:children -->\n",
         "a fat-fingered number, which would drop that child"),
        ("<!-- wayfinder:children -->\n- #1\n- #1\n<!-- /wayfinder:children -->\n",
         "the same child twice — map order is then ambiguous"),
        ("<!-- wayfinder:children -->\n- #1\nsome prose someone typed here\n"
         "<!-- /wayfinder:children -->\n", "prose inside the block"),
    ],
)
def test_a_damaged_child_block_is_refused_not_silently_shortened(forge, block, why):
    """The wayfinder map format is the specification; this is the conformance.

    Every one of these would otherwise return a SHORTER list — or an empty one — and a short
    frontier is indistinguishable from a map that is complete. Refusing is the only reading
    that cannot report success over a damaged map.
    """
    forge.add(title="c1")
    forge.add(title="c2")
    m = forge.add(title="map", labels=[MAP_LABEL], body="## Notes\n\nn\n\n" + block)
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert r.returncode == 2, why + "\n" + r.stdout + r.stderr
    assert "REFUSING" in r.stderr
    assert "FRONTIER:" not in r.stdout, "it answered from a list it knew was damaged"


@pytest.mark.parametrize(
    "block, expect",
    [
        ("", []),  # no markers at all: a fresh map, not an error
        ("<!-- wayfinder:children -->\n<!-- /wayfinder:children -->\n", []),
        ("<!-- wayfinder:children -->\n- #2\n- #1\n<!-- /wayfinder:children -->\n", [2, 1]),
        ("<!-- wayfinder:children -->\n- #1 a title that is never validated\n"
         "\n  * #2\n<!-- /wayfinder:children -->\n", [1, 2]),
        ("<!-- wayfinder:children -->\n#1\n#2\n<!-- /wayfinder:children -->\n", [1, 2]),
    ],
)
def test_the_reader_still_accepts_every_benign_variant__control(forge, block, expect):
    """Control for the refusals above: a reader that refused everything would pass them all.

    Order follows the LINES, which is the whole point of the block — `sub_issues` on GitHub
    returns insertion order and Forgejo has no position field at all, so map order lives
    nowhere else. The second case pins that a fresh map is not an error.
    """
    forge.add(title="c1")
    forge.add(title="c2")
    m = forge.add(title="map", labels=[MAP_LABEL], body="## Notes\n\nn\n\n" + block)
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert r.returncode == 0, r.stdout + r.stderr
    order = [int(x) for x in re.findall(r"#(\d+)\s+(?:TAKE|skip)", r.stdout)]
    assert order == expect[:1], (r.stdout, expect)  # it stops at the first takeable
    assert ("FRONTIER: #%d" % expect[0] if expect else "FRONTIER: none") in r.stdout


def type_labels(issue) -> list:
    """The TYPE labels only — `wayfinder:<type>`, excluding the two filter labels.

    The assertions below used to compare the whole label list against a single expected name, which
    was exact and correct while `child-create` attached exactly one label. A later change made it attach
    three, so a whole-list comparison would now fail for a reason that has nothing to do with what
    these tests are about (the contract: the type is never absent, and an explicit type wins).

    Narrowed rather than loosened. `x in labels` would also have made them pass — and would have
    stopped catching the case they exist for, a DEFAULT leaking in alongside an explicit type, since
    both would be present. Excluding exactly the two known filter labels keeps the comparison exact
    over the set that matters.
    """
    names = [lab["name"] for lab in issue["labels"]]
    return [n for n in names
            if n.startswith("wayfinder:") and not n.startswith("wayfinder:map-")]


def test_a_child_declares_its_parent_in_the_first_line(forge):
    """`Part of #<map>` is the half of the link a human reads on the child itself."""
    m = mk_map(forge)
    r = forge.run("child-create", "o/r", str(m["number"]), "t", "the body", "task", "--because", "serves this map")
    assert r.returncode == 0, r.stdout + r.stderr
    child = forge.issues[max(forge.issues)]
    assert child["body"].startswith(f"Part of #{m['number']}\n")
    assert "the body" in child["body"]
    assert type_labels(child) == ["wayfinder:task"]


@pytest.mark.parametrize("extra, expect", [
    ([],           "wayfinder:task"),      # the argument omitted -- the reported case
    (["grilling"], "wayfinder:grilling"),  # an explicit type must NOT be overwritten by the default
    ([""],         "wayfinder:task"),      # explicitly EMPTY is absent, not a label named `wayfinder:`
])
def test_a_child_is_always_labelled_and_an_explicit_type_still_wins(forge, extra, expect):
    """the 4th argument used to default to NO LABEL, and 11 of one map's 39 children
    were filed unlabelled because of it — the split fell exactly on whether the argument was
    passed. `frontier` never reads labels, so nothing downstream noticed.

    WHICH ARM CATCHES WHAT IS MEASURED, NOT ASSERTED. Four mutations were injected and the
    arms that caught each were recorded:

      * M1, restoring the original `else []`      -> caught by omitted, empty
      * M2, hardcoding `wayfinder:task`           -> caught by grilling ONLY
      * M3, dropping the `and args[3]` guard      -> caught by empty ONLY
      * M4, defaulting to something else          -> caught by omitted, empty

    `grilling` and `empty` each uniquely kill a mutation. **`omitted` does not, and that is
    stated rather than hidden**: after the fix, omitted and explicitly-empty travel the same
    `len(args) > 3 and args[3]` branch, so no mutation can separate them and `empty`'s set
    strictly contains `omitted`'s. It is kept anyway, as the named regression for the defect
    actually reported — 11 unlabelled children of one map — because a test that documents the
    reported case earns its place differently from one that catches a mutant. That is an
    exception to "an arm that catches nothing looks like coverage", and an exception is only
    safe while someone has said out loud that it is one.

    M3 is worth its own sentence: without the truthiness guard the client sends `wayfinder:`
    and `ensure_label` CREATES a real label with that name, so the repo grows a label nobody
    chose. The stub keeps every label it is asked to create, so the second assertion sees it.

    A FIFTH ARM WAS PROPOSED AND REMOVED, because injection said it measured nothing. Explicit
    `task` was suggested — by this author, on the ticket — to catch a divergence between the
    default and explicit paths. It caught no mutation the others did not: that divergence IS
    M3, which `empty` catches directly. Structurally it never could, since explicit `task` and
    the default produce an identical label, so nothing separates them short of a mutation
    branching on the literal string.
    """
    m = mk_map(forge)
    r = forge.run("child-create", "o/r", str(m["number"]), "t", "the body", *extra, "--because", "serves this map")
    assert r.returncode == 0, r.stdout + r.stderr
    child = forge.issues[max(forge.issues)]
    assert type_labels(child) == [expect]
    assert "wayfinder:" not in [lab["name"] for lab in forge.labels], \
        "a label named `wayfinder:` was created from an empty type"


def test_a_non_map_issue_is_refused_as_a_parent(forge):
    plain = forge.add(title="just an issue")
    r = forge.run("child-create", "o/r", str(plain["number"]), "t", "--because", "serves this map")
    assert r.returncode == 2
    assert "not labelled wayfinder:map" in r.stderr


# --------------------------------------------------------------------------- blocking edges


def test_a_blocking_edge_uses_the_index_and_points_the_right_way(forge):
    """Both inversions against GitHub in one assertion.

    The stub gives every issue `id == 1000 + number`, so sending the DB id — GitHub's
    requirement — lands as `index: 1002` and 404s instead of quietly working. And the edge
    must be posted under the BLOCKED ticket, naming the blocker in the body: posted the
    other way round it is an equally valid graph pointing backwards.
    """
    blocked = forge.add(title="child")  # #1
    blocker = forge.add(title="blocker")  # #2
    r = forge.run("block", "o/r", str(blocked["number"]), str(blocker["number"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert FORGE.deps == {1: [2]}, FORGE.deps
    posted = [p for m_, p in forge.seen if m_ == "POST" and p.endswith("/dependencies")]
    assert posted == ["/api/v1/repos/o/r/issues/1/dependencies"], posted


def test_unblock_removes_exactly_the_named_edge(forge):
    """The edge is deleted under the BLOCKED ticket with the blocker in the body -- the same two
    inversions as `block` -- and a second blocker on the same ticket survives."""
    for t in ("child", "first", "second"):
        forge.add(title=t)
    FORGE.deps = {1: [2, 3]}
    r = forge.run("unblock", "o/r", "1", "2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert FORGE.deps == {1: [3]}, FORGE.deps
    deleted = [p for m_, p in forge.seen if m_ == "DELETE"]
    assert deleted == ["/api/v1/repos/o/r/issues/1/dependencies"], deleted


def test_unblock_refuses_an_edge_that_is_not_there(forge):
    """Naming the pair backwards is the natural mistake, and a DELETE for it answers 200."""
    forge.add(title="child")
    forge.add(title="blocker")
    FORGE.deps = {1: [2]}
    r = forge.run("unblock", "o/r", "2", "1")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "is not blocked by" in r.stderr
    assert FORGE.deps == {1: [2]}, "an edge was removed anyway"
    assert not [p for m_, p in forge.seen if m_ == "DELETE"], "a DELETE was sent for an absent edge"


def test_unblock_does_not_believe_its_own_delete(forge):
    """A forge that answers 200 and keeps the edge: the verb re-reads and says so."""
    forge.add(title="child")
    forge.add(title="blocker")
    FORGE.deps = {1: [2]}
    FORGE.ignore_dep_delete = True
    r = forge.run("unblock", "o/r", "1", "2")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "still there" in r.stderr


def test_a_self_block_is_refused(forge):
    """Measured on hub 2026-08-12: Forgejo accepts `#n blocked by #n` and the issue is then
    unclosable forever — every close answers 412 "still has open dependencies", including
    the close that would clear the blocker. Nothing but deleting the issue recovers it."""
    forge.add(title="ticket")
    r = forge.run("block", "o/r", "1", "1")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "cannot block itself" in r.stderr
    assert FORGE.deps == {}, "the edge was written anyway"


def test_blockers_counts_only_the_open_ones(forge):
    forge.add(title="child")  # #1
    forge.add(title="open blocker")  # #2
    forge.add(title="closed blocker", state="closed")  # #3
    forge.deps[1] = [2, 3]
    r = forge.run("blockers", "o/r", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "open_blockers=1 #2" in r.stdout


def test_blockers_json_counts_only_the_open_ones(forge):
    """the same verdict as one JSON object."""
    forge.add(title="child")  # #1
    forge.add(title="open blocker")  # #2
    forge.add(title="closed blocker", state="closed")  # #3
    forge.deps[1] = [2, 3]
    r = forge.run("blockers", "o/r", "1", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout) == {"number": 1, "open_blockers": 1, "blockers": [2]}


def test_blockers_json_an_unreadable_listing_is_an_error_object_never_zero(forge):
    """An unreadable dependency listing must not serialise as `open_blockers: 0`."""
    forge.add(title="child")  # #1
    forge.fail["/issues/1/dependencies"] = 500
    r = forge.run("blockers", "o/r", "1", "--json")
    assert r.returncode == 2, r.stdout + r.stderr
    d = json.loads(r.stdout)
    assert "error" in d and "open_blockers" not in d, d


def test_frontier_json_names_every_examined_child_and_what_it_did_not_examine(forge):
    closed = forge.add(title="done", state="closed")  # #1
    claimed = forge.add(title="taken", assignees=["someone"])  # #2
    blocked = forge.add(title="blocked")  # #3
    winner = forge.add(title="takeable")  # #4
    later = forge.add(title="also takeable")  # #5
    blocker = forge.add(title="blocker still open")  # #6
    forge.deps[blocked["number"]] = [blocker["number"]]
    m = mk_map(forge, children=[c["number"] for c in (closed, claimed, blocked, winner, later)])
    r = forge.run("frontier", "o/r", str(m["number"]), "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    d = json.loads(r.stdout)
    assert d["map"] == m["number"] and d["frontier"] == 4 and d["not_examined"] == 1, d
    assert [(c["number"], c["decision"]) for c in d["children"]] == [(1, "skip"), (2, "skip"), (3, "skip"), (4, "take")]
    assert d["children"][0]["reason"] == "closed" and d["children"][2]["reason"] == "blocked by #6", d
    assert d["children"][1]["reason"].startswith("claimed by someone"), d


def test_frontier_json_an_unreadable_blocker_is_an_error_not_a_frontier(forge):
    forge.add(title="candidate")  # #1
    m = mk_map(forge, children=[1])
    forge.fail["/issues/1/dependencies"] = 500
    r = forge.run("frontier", "o/r", str(m["number"]), "--json")
    assert r.returncode == 2, r.stdout + r.stderr
    d = json.loads(r.stdout)
    assert "error" in d and "frontier" not in d, d


# ------------------------------------------------------------------------ claim and resolve


def test_a_claim_that_does_not_stick_is_reported(forge):
    """Forgejo drops an assignee it will not accept and still answers 2xx."""
    forge.add(title="ticket")
    forge.drop_assignees = True
    r = forge.run("claim", "o/r", "1", "alice")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "did not take" in r.stderr


def test_a_claim_that_sticks_passes__control(forge):
    forge.add(title="ticket")
    r = forge.run("claim", "o/r", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert forge.issues[1]["assignees"] == [{"login": "alice"}]


def test_an_unclaim_that_does_not_take_is_reported(forge):
    """leaving a claim is verified exactly as entering one is -- Forgejo can answer 2xx and
    keep the assignee, and a release that reports success without taking is the asymmetry again."""
    forge.add(title="ticket", assignees=["alice"])
    forge.drop_assignees = True
    r = forge.run("unclaim", "o/r", "1")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "did not take" in r.stderr
    assert forge.issues[1]["assignees"] == [{"login": "alice"}]


def test_an_unclaim_that_takes_removes_only_that_login__control(forge):
    forge.add(title="ticket", assignees=["alice", "other"])
    r = forge.run("unclaim", "o/r", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "#1 released by alice" in r.stdout
    assert forge.issues[1]["assignees"] == [{"login": "other"}], "it cleared someone else's claim"


def test_unclaiming_a_ticket_that_login_does_not_hold_writes_nothing(forge):
    forge.add(title="ticket", assignees=["other"])
    r = forge.run("unclaim", "o/r", "1")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "not claimed by alice" in r.stderr
    assert not [m for m, p in forge.seen if m == "PATCH"], "nothing must be written"
    assert forge.issues[1]["assignees"] == [{"login": "other"}]


def test_resolve_comments_closes_and_appends_under_decisions_so_far(forge):
    m = mk_map(forge, body="## Notes\n\nn\n\n## Decisions so far\n\n- #7 an earlier one\n\n")
    m["body"] += "\n## Fog\n\nf\n"
    forge.add(title="the ticket")
    n = max(forge.issues)
    r = forge.run("resolve", "o/r", str(n), str(m["number"]), "the answer\nmore detail")
    assert r.returncode == 0, r.stdout + r.stderr
    assert forge.issues[n]["state"] == "closed"
    assert forge.comments[n][0]["body"].startswith("the answer")
    body = forge.issues[m["number"]]["body"]
    decisions = body.split("## Decisions so far")[1].split("## Fog")[0]
    assert "- #7 an earlier one" in decisions
    # The pointer identifies the ticket by LINK, not by a bare `#N`. Same claim,
    # and stricter: a bare `#2` also matches `#20`, where the full URL cannot.
    assert f"http://forge/o/r/issues/{n}" in decisions and "the answer" in decisions
    assert "more detail" not in decisions, "only the first line belongs in the pointer"
    assert "## Fog\n\nf" in body, "the section after Decisions was clobbered"


# --- `@file:<path>` argument expansion ------------------------------------------------
#
# This exists because two guards each failing closed left no way to pass long prose as argv:
# `pane-input-guard` refuses an unparseable line mentioning the tmux injector, its
# documented answer is a file plus command substitution, and the worktree-isolation check refuses
# command substitution. Removing the substitution moves neither guard.


def test_a_file_argument_is_expanded_to_its_contents(forge, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("line one\n\nline two\n")
    forge.add(title="t")
    n = max(forge.issues)

    r = forge.run("note", "o/r", str(n), "@file:%s" % body)

    assert r.returncode == 0, r.stdout + r.stderr
    posted = forge.comments[n][0]["body"]
    assert "line one" in posted and "line two" in posted
    assert "@file:" not in posted, "the token reached the forge instead of the file's contents"


def _access_config(tmp_path, mode=0o600):
    acc = tmp_path / "hub-access.conf"
    acc.write_text('header = "CF-Access-Client-Id: probe-id"\n'
                   'header = "CF-Access-Client-Secret: probe-secret"\n')
    acc.chmod(mode)
    return acc


def test_an_issue_write_carries_the_access_pair_when_an_access_config_is_named(forge, tmp_path):
    """The issue verbs build their own curl in api(), and it sent the token config alone. From a
    box that reaches the forge through Cloudflare Access, every write then got the Access login
    page (a 302) while the passthrough's GETs, which go through hub_curl(), worked -- measured
    on 2026-09-11."""
    forge.add(title="t")
    n = max(forge.issues)
    forge.env["HUB_ACCESS_CONFIG"] = str(_access_config(tmp_path))

    r = forge.run("note", "o/r", str(n), "a comment through Access")

    assert r.returncode == 0, r.stdout + r.stderr
    posts = [h for m, h in forge.headers_seen if m == "POST"]
    assert posts, "no POST reached the forge"
    assert posts[-1].get("CF-Access-Client-Id") == "probe-id", posts[-1]
    assert posts[-1].get("CF-Access-Client-Secret") == "probe-secret"


def test_an_access_config_readable_by_others_is_refused_before_anything_is_sent(forge, tmp_path):
    forge.add(title="t")
    n = max(forge.issues)
    forge.env["HUB_ACCESS_CONFIG"] = str(_access_config(tmp_path, mode=0o644))
    before = len(forge.seen)

    r = forge.run("note", "o/r", str(n), "must not be sent")

    assert r.returncode != 0
    assert "want 600" in r.stdout + r.stderr
    assert not forge.comments.get(n), "the comment was posted with an unprotected Access config"
    assert len(forge.seen) == before, "a request went out before the refusal"


def test_a_MISSING_file_refuses_rather_than_sending_the_token(forge, tmp_path):
    """The failure this expansion exists to prevent, one level down: a body silently posted as the
    literal string `@file:/tmp/x.md` is worse than no body, because it looks like a comment."""
    forge.add(title="t")
    n = max(forge.issues)

    r = forge.run("note", "o/r", str(n), "@file:%s" % (tmp_path / "absent.md"))

    assert r.returncode != 0
    assert "no such body file" in (r.stdout + r.stderr)
    assert not forge.comments.get(n), "it wrote a comment despite refusing"


def test_only_the_PREFIX_form_expands(forge, tmp_path):
    """`@file:` rather than a bare `@path`, because prose legitimately starts with `@` and a
    prefix that collides with content is a defect waiting for the first person to write one.
    A mention of the token INSIDE a body is content, not an instruction."""
    forge.add(title="t")
    n = max(forge.issues)

    r = forge.run("note", "o/r", str(n), "see @file:/etc/passwd in the docs for how this works")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "see @file:/etc/passwd in the docs" in forge.comments[n][0]["body"]


def test_the_BARE_curl_spelling_refuses_instead_of_posting_the_path(forge, tmp_path):
    """The adjacent spelling fell through as content -- measured on a real PR, 2026-09-03.

    `@file:` was chosen over a bare `@path` so the prefix could not collide with prose, and the
    missing-file branch above refuses "rather than sending the literal token as your body". But that
    refusal only covers tokens the loop RECOGNISES. `-d @file` is curl's spelling and is used
    elsewhere in this same script, so it is the one a caller reaches for -- and `@/tmp/x.md` matched
    nothing, so it was sent verbatim.

    A PR opened with a 105-character body that was the PATH. That is worse than an empty body: an
    empty one looks empty, while this looks filled in and resolves to nothing on any other box.
    """
    body = tmp_path / "body.md"
    body.write_text("the real body\n")
    forge.add(title="t")
    n = max(forge.issues)

    r = forge.run("note", "o/r", str(n), "@%s" % body)

    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "names a readable file" in out
    assert "@file:%s" % body in out, "it must name the spelling that works, not just refuse"
    assert not forge.comments.get(n), "it posted despite refusing"


def test_an_at_mention_that_names_no_file_is_still_ordinary_prose(forge, tmp_path):
    """The discriminator, and the reason this refuses rather than expanding.

    The expansion declined to expand a bare `@` because prose legitimately starts with one. That reasoning
    is intact: the guard fires only when the path RESOLVES, which `@claude` does not. Expanding here
    would install the exact collision that avoided; refusing on a resolvable path does not.
    """
    forge.add(title="t")
    n = max(forge.issues)

    r = forge.run("note", "o/r", str(n), "@claude please look at the release path")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "@claude please look at the release path" in forge.comments[n][0]["body"]


def test_the_expansion_is_uniform_across_arguments(forge, tmp_path):
    """Applied at argv level rather than per-verb, so every long-prose surface gets it at once --
    `pr create`'s body, `issue note`, `issue resolve` -- without each verb growing its own flag."""
    title = tmp_path / "title.txt"
    title.write_text("a title from a file")
    m = mk_map(forge, body="## Decisions so far\n\n")
    forge.add(title="t")
    n = max(forge.issues)

    r = forge.run("resolve", "o/r", str(n), str(m["number"]), "the answer", "@file:%s" % title)

    assert r.returncode == 0, r.stdout + r.stderr
    decisions = forge.issues[m["number"]]["body"].split("## Decisions so far")[1]
    assert "-- a title from a file" in decisions, "the gist argument did not expand"


def test_the_pointer_is_a_markdown_link_not_bare_text(forge):
    """the second defect in the same generated string. `- #N Title -- gist (url)` reads as
    a different KIND of entry beside `- [Title](url) -- gist`, so a scanning reader takes it for a
    stray note rather than a decision -- which is how one sat unnoticed on a map for a day.
    Repaired by hand five times before anyone wrote it down."""
    m = mk_map(forge, body="## Decisions so far\n\n")
    forge.add(title="the ticket")
    n = max(forge.issues)
    forge.run("resolve", "o/r", str(n), str(m["number"]), "the answer")
    decisions = forge.issues[m["number"]]["body"].split("## Decisions so far")[1]
    assert f"- [the ticket](http://forge/o/r/issues/{n}) -- the answer" in decisions
    assert f"- #{n} the ticket" not in decisions, "still emitting the bare form"


def test_a_heading_first_comment_is_REFUSED_BEFORE_anything_is_written(forge):
    """THE INJECTION THE DEFECT ASKS FOR, plus the ordering that makes a refusal useful.

    The house comment style opens with `## Resolution`, and the first line is what gets indexed, so
    the pointer read `-- ## Resolution` and carried zero bits. Refusing is only worth anything
    BEFORE the write: the line used to be built after the comment POST and the close PATCH, so a
    refusal there would have left the ticket closed and the map unwritten -- a worse state than the
    bug it prevents.
    """
    m = mk_map(forge, body="## Decisions so far\n\n")
    forge.add(title="t")
    n = max(forge.issues)

    r = forge.run("resolve", "o/r", str(n), str(m["number"]), "## Resolution\n\nthe real answer")

    assert r.returncode != 0
    assert "HEADING" in r.stderr, r.stderr
    assert forge.issues[n]["state"] != "closed", "refused, but it had already closed the ticket"
    assert not forge.comments.get(n), "refused, but it had already posted the comment"
    assert "## Resolution" not in forge.issues[m["number"]]["body"]


def test_an_explicit_gist_is_used_and_the_heading_style_then_works(forge):
    """Option 1: the caller states the gist, because which sentence is the DECISION is a human
    judgement and the caller is the only party holding it."""
    m = mk_map(forge, body="## Decisions so far\n\n")
    forge.add(title="t")
    n = max(forge.issues)

    r = forge.run("resolve", "o/r", str(n), str(m["number"]),
                  "## Resolution\n\nlong prose", "the one-sentence answer")

    assert r.returncode == 0, r.stdout + r.stderr
    decisions = forge.issues[m["number"]]["body"].split("## Decisions so far")[1]
    assert "-- the one-sentence answer" in decisions
    assert "## Resolution" not in decisions
    assert forge.comments[n][0]["body"].startswith("## Resolution"), (
        "the COMMENT must keep the house style; only the pointer differs")


def test_a_plain_first_line_still_becomes_the_gist__control(forge):
    """THE CONTROL. A fix that refuses everything, or drops the gist, passes the injection above
    and destroys the index -- which is the whole thing the map is for."""
    m = mk_map(forge, body="## Decisions so far\n\n")
    forge.add(title="t")
    n = max(forge.issues)
    r = forge.run("resolve", "o/r", str(n), str(m["number"]), "a plain sentence\n\n## Detail\n\nx")
    assert r.returncode == 0, r.stdout + r.stderr
    decisions = forge.issues[m["number"]]["body"].split("## Decisions so far")[1]
    assert "-- a plain sentence" in decisions
    assert "## Detail" not in decisions


def test_brackets_in_a_title_do_not_break_the_link(forge):
    """An unescaped `]` truncates the link text and the line still RENDERS -- silently wrong output
    that looks fine, which is this ticket's failure mode exactly."""
    m = mk_map(forge, body="## Decisions so far\n\n")
    forge.add(title="fix [the thing] properly")
    n = max(forge.issues)
    forge.run("resolve", "o/r", str(n), str(m["number"]), "done")
    decisions = forge.issues[m["number"]]["body"].split("## Decisions so far")[1]
    assert r"[fix \[the thing\] properly]" in decisions


def test_resolve_creates_the_decisions_section_when_the_map_has_none(forge):
    m = mk_map(forge, body="## Notes\n\nnothing yet\n\n")
    forge.add(title="t")
    n = max(forge.issues)
    r = forge.run("resolve", "o/r", str(n), str(m["number"]), "answer")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "## Decisions so far" in forge.issues[m["number"]]["body"]


# ----------------------------------------------------------------------------- zoom, labels


def test_zoom_reads_the_whole_ticket_including_settled_blockers(forge):
    """`blockers` answers the live question; zoom answers "what happened to this ticket", so
    a closed blocker still belongs in the output."""
    forge.add(title="the ticket", body="the body", labels=["wayfinder:research"],
              assignees=["alice"])
    forge.add(title="open blocker")
    forge.add(title="settled blocker", state="closed")
    forge.deps[1] = [2, 3]
    forge.comments[1] = [{"body": "a comment", "user": {"login": "alice"}}]
    r = forge.run("zoom", "o/r", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    for want in ("#1 the ticket [open]", "wayfinder:research", "alice", "the body",
                 "a comment", "#2(open)", "#3(closed)"):
        assert want in r.stdout, want


def test_a_label_is_created_before_it_is_used(forge):
    """EditIssueOption has no labels field and CreateIssueOption.labels takes IDs, so the
    only route is create-label -> resolve-id -> attach. A repo starts with no labels."""
    forge.add(title="t")
    assert forge.labels == []
    r = forge.run("label", "o/r", "1", "grilling")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [lab["name"] for lab in forge.labels] == ["wayfinder:grilling"]
    assert [lab["name"] for lab in forge.issues[1]["labels"]] == ["wayfinder:grilling"]


def test_map_create_labels_the_map_and_seeds_the_marker_block(forge):
    r = forge.run("map-create", "o/r", "the map", "## Notes\n\nsome notes\n")
    assert r.returncode == 0, r.stdout + r.stderr
    m = forge.issues[1]
    # Every child also gets `wayfinder` and `wayfinder:map-<own number>`; the type label is what this
    # assertion is about, and `wayfinder:map` is not excluded by `type_labels` — it has no hyphen.
    assert type_labels(m) == [MAP_LABEL]
    assert "some notes" in m["body"]
    assert "<!-- wayfinder:children -->" in m["body"]
    assert "<!-- /wayfinder:children -->" in m["body"]


# ------------------------------------------------------------------------ the signed-in trap


def test_a_200_carrying_only_a_message_is_not_data(forge):
    """hub runs REQUIRE_SIGNIN_VIEW=true: an unauthenticated call returns HTTP 200 and
    `{"message": "Only signed in user is allowed to call APIs."}`. A verb that read that as
    an empty issue list would report an empty frontier, which reads as 'no work'."""
    forge.add(title="ticket")
    m = mk_map(forge, children=[1])
    forge.signed_out = "/issues"
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "message, not data" in r.stderr


def test_a_real_payload_is_not_mistaken_for_a_message__control(forge):
    """The guard above rejects a dict whose only keys are message/url/errors. A real issue
    carries a `message`-free payload, but a real ERROR body carries `url` too — so this pins
    that ordinary data still gets through."""
    forge.add(title="ticket")
    m = mk_map(forge, children=[1])
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FRONTIER: #1" in r.stdout


# -------------------------------------------------------------- generic maintainer verbs


def test_file_note_tag_close_and_list(forge):
    # `--no-map`: this ticket genuinely belongs to no map (it is the maintainer
    # run's own alert), which is the case that verb still serves -- it just has to be said.
    r = forge.run("file", "o/r", "--no-map", "maintainer needs attention", "the box is red")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("#1 ")
    n = "1"
    r = forge.run("tag", "o/r", n, "agent:maintainer")
    assert r.returncode == 0, r.stderr
    assert "agent:maintainer" in r.stdout
    r = forge.run("note", "o/r", n, "still red")
    assert r.returncode == 0, r.stderr
    r = forge.run("list", "o/r", "agent:maintainer")
    assert r.returncode == 0, r.stderr
    assert "#1" in r.stdout
    assert any("type=issues" in path for _m, path in forge.seen)
    r = forge.run("close", "o/r", n, "recovered")
    assert r.returncode == 0, r.stderr
    assert forge.issues[1]["state"] == "closed"
    assert forge.comments[1][-1]["body"] == "recovered"

def test_list_skips_pulls_and_honours_label(forge):
    forge.add(title="plain")
    forge.add(title="tagged", labels=["agent:maintainer"])
    forge.add(title="a pr", pull=True, labels=["agent:maintainer"])
    r = forge.run("list", "o/r", "agent:maintainer")
    assert r.returncode == 0, r.stderr
    assert "#2" in r.stdout
    assert "#1" not in r.stdout
    assert "#3" not in r.stdout
    assert any("type=issues" in path for _m, path in forge.seen)


def test_file_refuses_without_the_no_map_marker(forge):
    """Map membership here is carried by the LABEL, so `issue file` produced a real,
    numbered ticket that no map lists and no frontier surfaces -- while printing a number and a URL,
    which looks exactly like success.

    Measured 2026-08-26: four of eight tickets filed that evening were orphaned, every one via this
    verb, every one caught by the operator rather than by any mechanism, twice an hour apart.
    """
    before = len(forge.issues)
    r = forge.run("file", "o/r", "a title", "a body")
    assert r.returncode == 2, f"refusals here exit 2, not 1: {r.returncode}\n{r.stderr}"
    assert "applies NO LABELS" in r.stderr
    assert "child-create" in r.stderr, "the refusal must name the verb for map-bound work"
    assert "--no-map" in r.stderr, "and the way to say you meant it"
    assert len(forge.issues) == before, "a refusal must create nothing"


def test_file_with_no_map_still_files_because_a_mapless_ticket_is_legitimate(forge):
    """THE CONTROL, and the ticket is explicit that it matters: a genuinely map-less issue is a
    legitimate thing to file, so a flat refusal would be the wrong fix. What was wrong is that
    SILENCE AND DECISION were indistinguishable -- the same shape as an empty PR body.

    Run against the STUB. Verifying that a guard does NOT refuse means actually doing the thing it
    guards, so this arm needs a scratch subject; done against the real forge it files real junk,
    which is how a real misfiling happened.
    """
    before = len(forge.issues)
    r = forge.run("file", "o/r", "--no-map", "a title", "a body")
    assert r.returncode == 0, r.stderr
    assert len(forge.issues) == before + 1, "the legitimate path must still file"
    filed = forge.issues[max(forge.issues)]
    assert filed["title"] == "a title", filed
    assert "no labels, no map" in r.stdout, "the outcome is stated, not left to be discovered"


def test_list_excludes_the_map_from_its_own_children(forge):
    """A map carries its own `wayfinder:map-N` label, so it came back in its own
    children's listing and rows differed from children by exactly one.

    It went wrong in BOTH directions in one afternoon: a handoff said 39 children, an auditor
    counted 40 rows and reported the handoff wrong, and the correction was then written up as
    "both right about different questions" -- a definitional story that teaches a reader nothing.
    """
    forge.add(title="the map", labels=["wayfinder:map-1"])
    forge.add(title="child a", labels=["wayfinder:map-1"])
    forge.add(title="child b", labels=["wayfinder:map-1"])
    r = forge.run("list", "o/r", "wayfinder:map-1")
    assert r.returncode == 0, r.stderr
    rows = [l for l in r.stdout.splitlines() if l.startswith("#")]
    assert len(rows) == 2, f"the map was counted as its own child: {rows}"
    assert not any(l.startswith("#1 ") or l.startswith("#1\t") for l in rows), rows
    # SAID, not silently dropped: a quiet exclusion swaps one wrong count for another, because
    # the caller still cannot tell which question was answered.
    assert "2 child(ren) of map #1" in r.stdout, r.stdout
    assert "excluded" in r.stdout


def test_a_non_map_label_excludes_nothing_and_still_states_the_count(forge):
    """THE CONTROL. The exclusion is keyed on `wayfinder:map-N` matching the issue's own number;
    an ordinary label must lose no rows. A fix that dropped the lowest number, or any row whose
    label looked map-ish, would pass the test above and fail here."""
    forge.add(title="one", labels=["agent:maintainer"])
    forge.add(title="two", labels=["agent:maintainer"])
    r = forge.run("list", "o/r", "agent:maintainer")
    assert r.returncode == 0, r.stderr
    rows = [l for l in r.stdout.splitlines() if l.startswith("#")]
    assert len(rows) == 2, rows
    assert "2 row(s)" in r.stdout, r.stdout
    assert "child(ren)" not in r.stdout


def test_list_pages_past_the_first_fifty(forge):
    """`limit=50` CAPPED SILENTLY: a truncated list and a complete one are the same
    object, and nothing in the response says which you are holding.

    Measured on hub 2026-08-25 — a labelling sweep read 50 of 122 issues and reported the clean
    subset as the whole repo. It was caught because a child known to exist was missing from the
    result, which is luck standing in for a check.

    123 is deliberate: two full pages plus a remainder, so an off-by-one in the last page or a
    loop that stops after page 2 both fail. The stub honours `limit`/`page` as of this ticket —
    before that it answered in full however it was asked, which would have made this assertion
    pass against a pager that never paged.
    """
    for i in range(123):
        forge.add(title=f"issue {i}")
    r = forge.run("list", "o/r")
    assert r.returncode == 0, r.stderr
    lines = [l for l in r.stdout.splitlines() if l.startswith("#")]
    assert len(lines) == 123, f"got {len(lines)} of 123 — the listing was capped"
    # Name the boundary rather than only the count: a pager that returned page 1 three times
    # would also produce 123 lines.
    assert all("#%d " % k in r.stdout for k in (50, 51, 123)), r.stdout
    assert len(set(lines)) == 123, "the same page was returned more than once"


@pytest.mark.parametrize("state, expect_in, expect_out", [
    ("open",   ["#1"], ["#2"]),
    ("closed", ["#2"], ["#1"]),
    ("all",    ["#1", "#2"], []),
])
def test_list_takes_a_state_so_the_guarded_verb_can_serve_the_query(forge, state, expect_in, expect_out):
    """The mechanism, and the reason the ticket exists at all.

    `list` is the GUARDED way to filter by label, and it hardcoded `state=open`. So the query
    the guard protects — a map's whole scope, which is `state=all` — could only be asked through
    the raw passthrough, which has no guard. The fail-open was demonstrated on the unguarded
    path BECAUSE the guarded path could not express the question.
    """
    forge.add(title="open one", labels=["wayfinder:task"])
    forge.add(title="closed one", state="closed", labels=["wayfinder:task"])
    r = forge.run("list", "o/r", "wayfinder:task", state)
    assert r.returncode == 0, r.stderr
    for n in expect_in:
        assert n in r.stdout, f"{n} missing from state={state}"
    for n in expect_out:
        assert n not in r.stdout, f"{n} leaked into state={state}"


def test_list_refuses_a_state_forgejo_would_silently_ignore(forge):
    """Forgejo does not reject an unknown `state`, it falls back to its default — so a typo
    answers with a different question's result and looks right. Same family as the unknown
    label this ticket is about: the parameter is dropped, not rejected."""
    forge.add(title="t")
    r = forge.run("list", "o/r", "", "opne")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "not open|closed|all" in r.stderr
    assert not any("state=opne" in path for _m, path in forge.seen), \
        "refused after the call, so the wrong query was still sent"



# ------------------------------------------------------- body-edit and the lock
#
# Standing decisions 16-20 sat on a map as COMMENTS for a day because this client could edit
# only the two machine-owned regions -- `child-create`'s children block and `resolve`'s
# decision pointer -- and nothing could touch the prose around them.
#
# The obvious safety net does not exist. Measured against the LIVE forge while building the lock,
# both arms: an issue GET carries no `ETag` and no `Last-Modified`, and
# `EditIssueOption.updated_at` is a settable timestamp, NOT a precondition -- a PATCH sent with
# a deliberately STALE `updated_at` returned 201 and clobbered the write that had landed in
# between. Swagger's `412` is not reachable that way. So the server will never refuse a lost
# update, and the two tests at the bottom are the ones that matter.


def _texts(tmp_path, tag, old, new):
    o, n = tmp_path / f"old-{tag}", tmp_path / f"new-{tag}"
    o.write_text(old)
    n.write_text(new)
    return str(o), str(n)


def test_body_edit_replaces_a_region_occurring_exactly_once(forge, tmp_path):
    it = forge.add(body="alpha\nBEFORE\nomega\n")
    o, n = _texts(tmp_path, "1", "BEFORE", "AFTER")
    r = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert r.returncode == 0, r.stderr
    assert forge.issues[it["number"]]["body"] == "alpha\nAFTER\nomega\n"


@pytest.mark.parametrize("body,why", [
    ("alpha\nomega\n", "0 matches -- the body moved under you, or it never matched"),
    ("BEFORE\nalpha\nBEFORE\n", "2 matches -- it would edit an arbitrary one of them"),
])
def test_body_edit_refuses_unless_the_match_is_unique(forge, tmp_path, body, why):
    """A naive `.replace()` silently edits the first of two and reports success."""
    it = forge.add(body=body)
    o, n = _texts(tmp_path, "2", "BEFORE", "AFTER")
    r = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert r.returncode != 0, why
    assert "not once" in (r.stderr + r.stdout)
    assert forge.issues[it["number"]]["body"] == body, "a refusal must not write"


def test_body_edit_refuses_an_empty_old_text(forge, tmp_path):
    """`"".count()` is not 1, but the refusal must be its own: an empty `old` would otherwise
    read as a prepend and report success."""
    it = forge.add(body="alpha\n")
    o, n = _texts(tmp_path, "3", "", "AFTER")
    r = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert r.returncode != 0
    assert "refusing to 'replace' nothing" in (r.stderr + r.stdout)
    assert forge.issues[it["number"]]["body"] == "alpha\n"


def test_body_edit_with_an_empty_old_text_FILLS_an_empty_body(forge, tmp_path):
    """The repair path a `--title-only` PR lacked: one opened with no body and could only be
    closed and reopened. An empty old text now fills an EMPTY body -- the test above is the control
    that the same call against a non-empty body is still refused."""
    it = forge.add(body="")
    o, n = _texts(tmp_path, "fill", "", "Ticket: #43\n\nWhy this change.\n")
    r = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert r.returncode == 0, r.stderr + r.stdout
    assert forge.issues[it["number"]]["body"] == "Ticket: #43\n\nWhy this change.\n"


def _race(forge, tmp_path, lock_a, lock_b):
    """Two `body-edit` runs on ONE issue at once, each replacing its own anchor.

    A BARRIER, NOT A SLEEP. The first version bought the read window with a fixed
    0.6 s delay tuned on an idle box, and under load process 1 finished its whole
    read-modify-write before process 2 got far enough to read -- so the injection failed to
    inject and the test went RED on a CORRECT implementation. Measured: 1 failed at ~4x
    contention (suite 1,326 s against the usual ~330 s), 19 passed in 18.8 s on the same tree
    when idle. A false FAIL is the same defect as a false pass, and worse here, because this
    test's whole job is to prove the sibling below can fail.

    The rendezvous makes it independent of machine speed: each reader snapshots, then waits
    until both have arrived, so both snapshots are guaranteed to precede either PATCH. When
    the flock DOES serialise them the second reader cannot arrive, the wait times out, and
    that is the serialised case rather than a failure.
    """
    it = forge.add(body="A1\nA2\n")
    forge.get_barrier = threading.Barrier(2)
    out: dict[str, subprocess.CompletedProcess] = {}

    def go(tag, anchor, lock):
        o, n = _texts(tmp_path, tag, anchor, anchor + "-EDITED")
        env = dict(forge.env)
        env["HUB_API_LOCK"] = lock
        out[tag] = subprocess.run(
            ["sh", str(SCRIPT), "issue", "body-edit", "o/r", str(it["number"]), o, n],
            capture_output=True, text=True, env=env, timeout=60,
        )

    ts = [threading.Thread(target=go, args=a)
          for a in (("r1", "A1", lock_a), ("r2", "A2", lock_b))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return forge.issues[it["number"]]["body"], out


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode this case relies on")
def test_body_edit_takes_a_lock_file_it_can_only_READ(forge, tmp_path):
    """the body lock sits at a fixed name in shared /tmp and belongs to whichever user
    ran first; every other user's body-edit died with PermissionError opening it to append."""
    lock = tmp_path / "another-users.lock"
    lock.touch()
    lock.chmod(0o444)
    it = forge.add(body="alpha\nBEFORE\n")
    o, n = _texts(tmp_path, "ro", "BEFORE", "AFTER")
    r = subprocess.run(["sh", str(SCRIPT), "issue", "body-edit", "o/r", str(it["number"]), o, n],
                       capture_output=True, text=True, env=dict(forge.env, HUB_API_LOCK=str(lock)), timeout=60)
    assert r.returncode == 0, r.stderr
    assert forge.issues[it["number"]]["body"] == "alpha\nAFTER\n"


def test_one_lock_lets_two_concurrent_edits_both_survive(forge, tmp_path):
    lock = str(tmp_path / "shared.lock")
    body, out = _race(forge, tmp_path, lock, lock)
    for tag, r in out.items():
        assert r.returncode == 0, f"{tag}: {r.stderr}"
    assert "A1-EDITED" in body and "A2-EDITED" in body, (
        f"an edit was lost despite the lock: {body!r}")


def test_two_lock_paths_lose_an_update_silently__fault_injection(forge, tmp_path):
    """The injection for the test above, and the exact bug `pr-queue.sh` warns about.

    Locking a path INSIDE each checkout is a different inode per worktree, so both holders
    acquire happily and the lock looks like it worked. Here that is two lock files.

    The assertion worth reading is the second: **both runs exit 0.** The read-back check in
    `edit_body` compares against what THIS process wrote, so the winner and the loser both
    verify successfully and the lost update is silent -- which is why the lock has to prevent
    it rather than a check catching it afterwards.
    """
    body, out = _race(forge, tmp_path, str(tmp_path / "a.lock"), str(tmp_path / "b.lock"))
    assert ("A1-EDITED" in body) != ("A2-EDITED" in body), (
        f"expected exactly one edit to survive two unshared locks, got {body!r}")
    assert all(r.returncode == 0 for r in out.values()), (
        "the loss must be SILENT -- if a run had failed, the lock would not be load-bearing")


def test_body_edit_refuses_a_retry_that_would_double_the_text(forge, tmp_path):
    """Exactly-once is NOT idempotence when `new` contains `old`.

    Found on the LIVE forge, not here: `B4` -> `B4 - edited ...` applied twice, 61 B
    then 120 B, rc=0 both times. `old` survives inside its own replacement, so it still matches
    exactly once and the second run doubles the text instead of matching nothing. An agent
    retrying after an apparent failure compounds the body silently.
    """
    it = forge.add(body="alpha\nB4\nomega\n")
    o, n = _texts(tmp_path, "retry", "B4", "B4 and more")
    first = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert first.returncode == 0, first.stderr
    assert forge.issues[it["number"]]["body"] == "alpha\nB4 and more\nomega\n"

    again = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert again.returncode != 0, "the second run must refuse, not double the text"
    assert "already contains the new text" in (again.stderr + again.stdout)
    assert forge.issues[it["number"]]["body"] == "alpha\nB4 and more\nomega\n"


def test_body_edit_still_allows_an_ordinary_replacement__control(forge, tmp_path):
    """The retry guard must not fire on the normal case, where `new` is not already present."""
    it = forge.add(body="alpha\nBEFORE\nomega\n")
    o, n = _texts(tmp_path, "ctl", "BEFORE", "AFTER")
    r = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert r.returncode == 0, r.stderr
    assert forge.issues[it["number"]]["body"] == "alpha\nAFTER\nomega\n"


def test_body_edit_can_DELETE_text(forge, tmp_path):
    """The retry guard tested `new in body`, and `"" in body` is True for EVERY body --
    so the guard fired on 100% of deletions and could never be satisfied, while explaining a
    doubling hazard that cannot exist for an empty replacement.

    Measured 2026-08-30 pruning merged entries out of a queue ticket. The workaround was to widen
    `old` until `new` was non-empty, which for a fenced block means swallowing the fence markers
    themselves -- so the guard pushed callers toward a strictly MORE dangerous edit than the one
    it refused."""
    it = forge.add(body="alpha\n#13\n#14\nomega\n")
    o, n = _texts(tmp_path, "del", "#13\n#14\n", "")
    r = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert r.returncode == 0, r.stderr + r.stdout
    assert forge.issues[it["number"]]["body"] == "alpha\nomega\n"


def test_a_second_deletion_refuses_as_NOT_FOUND_not_as_already_applied(forge, tmp_path):
    """The two refusals mean different things and must stay distinguishable. A deletion is
    idempotent by construction: once it has run, `old` no longer matches, so the correct refusal
    is the exactly-once one -- not the retry guard, which would be claiming the body already
    contains an empty string."""
    it = forge.add(body="alpha\n#13\nomega\n")
    o, n = _texts(tmp_path, "del2", "#13\n", "")
    assert forge.run("body-edit", "o/r", str(it["number"]), o, n).returncode == 0

    again = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert again.returncode != 0, "a deletion whose text is gone must refuse"
    out = again.stderr + again.stdout
    assert "occurs 0 times" in out, out
    assert "already contains the new text" not in out, (
        "that is the retry guard, and it is the wrong reason for a completed deletion")
    assert forge.issues[it["number"]]["body"] == "alpha\nomega\n"


def test_a_replacement_is_allowed_when_new_appears_elsewhere_but_does_not_recontain_old(forge, tmp_path):
    """The other direction of the same bug. `foo` -> `bar` in a body that already says `bar`
    somewhere is perfectly idempotent -- after it runs `old` no longer matches -- yet the proxy
    refused it because it only asked whether `new` was present."""
    it = forge.add(body="bar\nfoo\nomega\n")
    o, n = _texts(tmp_path, "elsewhere", "foo", "bar")
    r = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert r.returncode == 0, r.stderr + r.stdout
    assert forge.issues[it["number"]]["body"] == "bar\nbar\nomega\n"


def test_appending_to_a_region_is_still_allowed_ONCE(forge, tmp_path):
    """THE ARM THAT RULES OUT THE TICKET'S PREFERRED FIX. The ticket suggested testing `old in new`
    INSTEAD of `new in body`. That refuses this edit outright -- the first application, the one
    the caller wants -- and this file's own guard comment calls it "the commonest edit there is".
    Only the conjunction allows it once and refuses the repeat."""
    it = forge.add(body="alpha\nB4\nomega\n")
    o, n = _texts(tmp_path, "append1", "B4", "B4 - edited")
    r = forge.run("body-edit", "o/r", str(it["number"]), o, n)
    assert r.returncode == 0, r.stderr + r.stdout
    assert forge.issues[it["number"]]["body"] == "alpha\nB4 - edited\nomega\n"


# ------------------------------------------------- the passthrough label guard (part 2)
#
# The VERBS were already guarded. The passthrough was not, and it is the path the defect was
# measured through: `labels=wayfinder:map-99999` returned all 122 issues, because `issue list`
# could not express that query at all until part 1 widened it.
#
# Scope is keyed on the ENDPOINT, not on the substring `labels=`, and these tests exist mostly to
# pin that distinction. Measured by walking `/swagger.v1.json` (326 paths, 8 taking a `labels`
# query param, control `page` -> 105): five `runners/jobs` endpoints take RUN JOB labels
# (`ubuntu-latest`) and `/pulls` takes label IDs as `array of int64`. A substring guard refuses
# every valid call to all six.


def test_passthrough_refuses_a_label_the_repo_does_not_have(forge):
    """The whole point. Forgejo's own spec says "Non existent labels are discarded", so this call
    would answer with the WHOLE repo and read as "everything matched"."""
    forge.add(title="t", labels=["wayfinder:task"])
    r = raw(forge, "/api/v1/repos/o/r/issues?type=issues&labels=wayfinder:map-99999")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSING" in r.stderr and "wayfinder:map-99999" in r.stderr
    assert "wayfinder:task" in r.stderr, "the refusal should name the labels that DO exist"


def test_passthrough_lets_a_real_label_through(forge):
    """The control, and it is what stops this being "disable the passthrough". Same endpoint,
    same shape, only the label differs."""
    forge.add(title="t", labels=["wayfinder:task"])
    r = raw(forge, "/api/v1/repos/o/r/issues?type=issues&labels=wayfinder:task")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REFUSING" not in r.stderr


def test_passthrough_refuses_when_the_repo_cannot_be_read_out_of_the_path(forge):
    """A genuine cannot-determine, and RARE BY CONSTRUCTION -- every well-formed call parses. This
    is the arm nothing exercises by accident, which is exactly why it needs its own test."""
    forge.add(title="t", labels=["wayfinder:task"])
    r = raw(forge, "/api/v1/repos//r/issues?labels=wayfinder:task")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no owner/repo could be" in r.stderr
    assert not any("labels=" in p for _m, p in forge.seen), \
        "refused AFTER the call, so the unverified filter was still sent"


def test_passthrough_proceeds_loudly_on_the_cross_repo_search_endpoint(forge):
    """`/repos/issues/search` takes label NAMES but is cross-repo, so they cannot be resolved
    against any one repo. Refusing a correctly-formed call because it is the wrong shape for this
    check is not failing closed -- it is breaking a working path. So: proceed, and say so."""
    forge.add(title="t", labels=["wayfinder:task"])
    r = raw(forge, "/api/v1/repos/issues/search?labels=wayfinder:map-99999")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CROSS-REPO" in r.stderr
    assert "REFUSING" not in r.stderr


@pytest.mark.parametrize("path", [
    "/api/v1/repos/o/r/actions/runners/jobs?labels=ubuntu-latest",   # RUN JOB labels
    "/api/v1/repos/o/r/pulls?labels=7",                              # label IDs, array of int64
    "/api/v1/repos/o/r/issues",                                      # the listing, no filter at all
])
def test_passthrough_is_silent_where_labels_means_something_else(forge, path):
    """A guard keyed on the substring `labels=` would refuse all of these. `runners/jobs` labels
    are runner tags in a different namespace; `/pulls` takes IDs, not names -- and note it is NOT
    the path `pr-queue.sh merge_requested` uses, which is `issues?type=pulls&labels=<name>`. Same
    parameter name, different type, different endpoint. None of that is deducible from the name."""
    forge.add(title="t", labels=["wayfinder:task"])
    r = raw(forge, path)
    assert "REFUSING" not in r.stderr, r.stderr
    assert "DISCARDS" not in r.stderr and "discards" not in r.stderr, "this guard should be entirely silent here"


def test_passthrough_does_not_ask_for_labels_when_no_filter_is_present(forge):
    """The guard must cost nothing on the overwhelmingly common call. If it fetched `/labels` on
    every passthrough it would double the request count for every read on this box."""
    forge.add(title="t")
    r = raw(forge, "/api/v1/repos/o/r/issues?type=issues&state=open")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(p.endswith("/labels?limit=100") for _m, p in forge.seen), \
        "the guard fetched the label list for a call carrying no filter"
# ---------------------------------------------------------------------------------------------
# child-create and map-create attach ALL THREE wayfinder labels.
#
# An earlier fix made the type label non-optional and stopped there, so the generic `wayfinder` and the
# per-map `wayfinder:map-<N>` were applied by an ad-hoc script in a coordinator's scratchpad —
# outside the repo, dead with that session. Measured 2026-08-25: a child came back with
# `['wayfinder:task']` alone and the OPERATOR noticed; no check did.
#
# The two arms below are the ones the ticket says a naive fix passes by construction. Neither is
# a count assertion: `len(labels) == 3` passes against a version attaching three WRONG labels.

def labels_of(forge, n) -> set:
    return {lab["name"] for lab in forge.issues[n]["labels"]}


def test_a_child_with_no_type_carries_all_three_labels(forge):
    m = mk_map(forge)
    r = forge.run("child-create", "o/r", str(m["number"]), "t", "body", "--because", "serves this map")
    assert r.returncode == 0, r.stdout + r.stderr

    child = max(forge.issues)
    assert labels_of(forge, child) == {
        "wayfinder:task", "wayfinder", f"wayfinder:map-{m['number']}"}


def test_a_child_with_an_EXPLICIT_type_still_carries_both_filter_labels(forge):
    """THE ARM A NAIVE FIX PASSES BY CONSTRUCTION.

    Adding the extras only where the type defaults does not merely miss this case — it recreates
    that exact symptom for the callers who bothered to be specific: the child comes back
    `['wayfinder:research']` alone. So the regression lands on the most deliberate users, which is
    why this arm exists separately from the default one rather than being assumed to follow from it.
    """
    m = mk_map(forge)
    r = forge.run("child-create", "o/r", str(m["number"]), "t", "body", "research", "--because", "serves this map")
    assert r.returncode == 0, r.stdout + r.stderr

    child = max(forge.issues)
    got = labels_of(forge, child)
    assert "wayfinder:research" in got, f"the explicit type was lost: {got}"
    assert "wayfinder" in got, f"the generic filter label was lost on the explicit-type path: {got}"
    assert f"wayfinder:map-{m['number']}" in got, f"the map label was lost: {got}"
    assert "wayfinder:task" not in got, f"the default type leaked in alongside the explicit one: {got}"


def test_the_map_label_follows_the_map_actually_filed_under(forge):
    """THE ONLY ARM THAT CATCHES A HARDCODED NUMBER, and the reason it files under TWO maps.

    A hardcoded `wayfinder:map-268` fails in the ACCEPTING direction: every child carries a
    wrong-but-present map label, so every check asking "is a map label attached?" answers yes. A
    test filing under a single map cannot produce that fault — it would pass against the hardcoded
    value it exists to catch, which is a known shape one level up.

    So: two maps, two children, and the labels must DIFFER and each match its own map.
    """
    m1 = mk_map(forge)
    m2 = mk_map(forge)
    assert m1["number"] != m2["number"], "fixture precondition: two distinct maps"

    assert forge.run("child-create", "o/r", str(m1["number"]), "a", "b", "--because", "serves this map").returncode == 0
    c1 = max(forge.issues)
    assert forge.run("child-create", "o/r", str(m2["number"]), "a", "b", "--because", "serves this map").returncode == 0
    c2 = max(forge.issues)

    assert f"wayfinder:map-{m1['number']}" in labels_of(forge, c1)
    assert f"wayfinder:map-{m2['number']}" in labels_of(forge, c2)
    # The discriminating assertion: neither child may carry the OTHER map's label. A hardcoded
    # number gives both children the same one and fails exactly here.
    assert f"wayfinder:map-{m2['number']}" not in labels_of(forge, c1)
    assert f"wayfinder:map-{m1['number']}" not in labels_of(forge, c2)


def test_map_create_labels_the_map_with_its_own_number(forge):
    """A per-map filter must return the map alongside its children, which is what the by-hand sweep
    produced and what anyone filtering will expect. The map cannot know its number until the forge
    assigns one, so this is attached after the create rather than with it."""
    r = forge.run("map-create", "o/r", "the map", "body")
    assert r.returncode == 0, r.stdout + r.stderr

    n = max(forge.issues)
    got = labels_of(forge, n)
    assert "wayfinder:map" in got, f"the map type label was lost: {got}"
    assert "wayfinder" in got, f"the generic filter label is missing from the map: {got}"
    assert f"wayfinder:map-{n}" in got, f"the map is not labelled with its own number: {got}"


def test_a_child_filed_under_a_map_shares_its_map_label(forge):
    """The property the labels exist FOR: filtering by `wayfinder:map-<N>` returns the map and its
    children together. Asserted as the intersection rather than as two separate memberships, since
    that is the question a filter actually asks."""
    r = forge.run("map-create", "o/r", "the map", "b")
    assert r.returncode == 0, r.stdout + r.stderr
    m = max(forge.issues)

    assert forge.run("child-create", "o/r", str(m), "child", "b", "--because", "serves this map").returncode == 0
    c = max(forge.issues)

    shared = labels_of(forge, m) & labels_of(forge, c)
    assert f"wayfinder:map-{m}" in shared, (
        f"map and child do not share a map label; map={labels_of(forge, m)} child={labels_of(forge, c)}")
    assert "wayfinder" in shared


# ------------------------------------------------------- label-id, the one fetch
#
# Six call sites independently wrote `/labels?limit=100`, in three files and two languages,
# because each author hit Forgejo's label fail-open separately and wrote their own guard. The
# third was written AFTER the first two existed and still shipped without one. `label-id` is the
# seam for callers OUTSIDE this file -- `pr-queue.sh` and `forge.sh` may not import its internals.
#
# The contract these pin is narrow and load-bearing: an id on stdout and exit 0, or NOTHING on
# stdout and exit 2. A diagnostic printed to stdout would hand a caller a truthy "id", which is
# precisely the fail-open the verb exists to close -- and routing `pr-queue.sh` through it
# re-opened exactly that until its digit check went in.


def test_label_id_resolves_a_known_label(forge):
    forge.label("ready-for-agent")
    r = forge.run("label-id", "o/r", "ready-for-agent")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().isdigit(), f"want a bare id on stdout, got {r.stdout!r}"


def test_label_id_refuses_an_unknown_label_with_nothing_on_stdout(forge):
    """The arm that matters. Forgejo DISCARDS a name it does not know and answers with the whole
    repo, so an unresolvable name must not read as a resolved one."""
    forge.label("ready-for-agent")
    r = forge.run("label-id", "o/r", "zz-no-such-label")
    assert r.returncode == 2, f"want the REFUSING exit code, got {r.returncode}"
    assert r.stdout.strip() == "", (
        "a refusal must leave stdout EMPTY -- a caller reads any non-empty stdout as an id, "
        f"which is the fail-open this verb closes. got {r.stdout!r}"
    )
    assert "zz-no-such-label" in r.stderr
    assert "ready-for-agent" in r.stderr, "the refusal must name what IS known, so it is actionable"


def test_label_id_names_the_known_set_when_the_repo_has_none(forge):
    r = forge.run("label-id", "o/r", "anything")
    assert r.returncode == 2
    assert "(none)" in r.stderr


def test_label_id_reads_the_labels_endpoint_once(forge):
    """It is 'the one fetch' by name; this checks the name is honest.

    A helper that re-fetched per lookup would still pass every test above while making the
    duplication it replaced cheaper in lines and more expensive in requests.
    """
    forge.label("ready-for-agent")
    before = len([p for m, p in forge.seen if m == "GET" and "/labels" in p])
    forge.run("label-id", "o/r", "ready-for-agent")
    after = len([p for m, p in forge.seen if m == "GET" and "/labels" in p])
    assert after - before == 1, f"expected exactly one /labels read, saw {after - before}"


def test_a_label_that_exists_but_matches_nothing_is_NOT_a_refusal(forge):
    """The arm that hides this whole bug class, called out in pr-queue.sh's own comment.

    A label that exists and matches zero issues behaves correctly, so a suite covering only the
    happy path and the empty path never reveals the fail-open. `label-id` must resolve it: it
    answers 'does this name exist', not 'does it match anything'.
    """
    forge.label("queue:needs-human-review")          # created, attached to nothing
    r = forge.run("label-id", "o/r", "queue:needs-human-review")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().isdigit()


# --- `pr unhold`: the other half of the brake ----------
#
# `pr hold` existed and nothing lifted it. The label means `queue:needs-human-review`, so its whole
# lifecycle is apply / get a ruling / resume, and only the first third had a verb.

HELD_LABEL = "queue:needs-human-review"


def _pr(forge, held: bool):
    it = forge.add(title="a pr", pull=True, labels=[HELD_LABEL] if held else [])
    return it["number"]


def test_pr_unhold_lifts_the_hold_and_confirms_it_is_gone(forge):
    """THE DEFECT: the brake was one-way from the only client this box is supposed to use."""
    n = _pr(forge, held=True)
    r = client(forge, "pr", "unhold", "o/r", str(n))
    assert r.returncode == 0, r.stdout + r.stderr
    assert HELD_LABEL not in labels_of(forge, n), "the label survived a reported success"
    assert "unheld #%d" % n in r.stdout, r.stdout


def test_pr_unhold_says_NOT_HELD_rather_than_reporting_a_lift(forge):
    """A NO-OP MUST SAY SO. Without this the verb cannot tell "I lifted it" from "there was nothing
    there" -- and those are the two a caller needs to separate: the first means the queue will now
    admit this PR, the second means someone's model of the queue is wrong.

    Measured BEFORE the delete rather than read off its status, because Forgejo answers 204 for
    removing a label that was not attached -- so the status cannot carry this distinction."""
    n = _pr(forge, held=False)
    r = client(forge, "pr", "unhold", "o/r", str(n))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOT HELD" in r.stdout, r.stdout
    assert "unheld #" not in r.stdout, "a no-op reported itself as a lift: " + r.stdout


def test_pr_unhold_REFUSES_when_the_label_survives_the_removal(forge):
    """SYMMETRIC WITH `hold`, AND FOR THE MIRRORED REASON. An unhold that reports success while the
    label is still attached leaves a PR the drain keeps skipping, and the seat that ran it stops
    watching BECAUSE it ran it.

    The injection makes the forge ACCEPT the DELETE and not apply it -- which is the failure that
    a status check alone cannot see, and the reason this verb reads back at all."""
    n = _pr(forge, held=True)
    orig = _Handler._route

    def accept_but_ignore(self, method):          # noqa: ANN001
        if method == "DELETE" and "/labels/" in self.path:
            return self._send(None, 204)          # 204, and the label stays attached
        return orig(self, method)

    _Handler._route = accept_but_ignore
    try:
        r = client(forge, "pr", "unhold", "o/r", str(n))
    finally:
        _Handler._route = orig
    assert r.returncode != 0, "a removal that did not apply was reported as success: " + r.stdout
    assert "STILL on #%d" % n in (r.stdout + r.stderr)
    assert HELD_LABEL in labels_of(forge, n), "the injection did not hold the label; test is vacuous"


def test_both_hold_verbs_are_discoverable_from_the_usage(forge):
    """The second half: `pr hold` worked and was absent from the client's own help, so the
    verb that applies the brake was undiscoverable. Both halves must be listed, or the next seat
    finds a PR it cannot release and no command that says it can."""
    usage = client(forge, "pr").stdout + client(forge, "pr").stderr
    assert "hold|unhold" in usage or ("hold" in usage and "unhold" in usage), usage
    unknown = client(forge, "pr", "nosuchverb", "o/r")
    assert "unhold" in (unknown.stdout + unknown.stderr)


# --- a map number is a decision, and moving a child is a supported operation -----------
#
# THE MISFILING THIS COMES FROM. Four tickets about the SUCCESSION HANDOFF were filed on the map
# for a forge-workflow rebuild, by a session that had been working that map all day. Nothing was wrong
# with the tickets. `child-create` took the map number as free input and checked only that it WAS
# a map -- so "the map I have in mind" and "the map this belongs to" were the same keystroke.


def _child(forge, mapno, because="serves this map"):
    r = forge.run("child-create", "o/r", str(mapno), "t", "body", "--because", because)
    assert r.returncode == 0, r.stdout + r.stderr
    return max(forge.issues)


def test_child_create_refuses_without_a_routing_rationale(forge):
    m = mk_map(forge)
    r = forge.run("child-create", "o/r", str(m["number"]), "t", "body")
    assert r.returncode != 0, "a map number was accepted as free input"
    assert "--because" in r.stderr, r.stderr


def test_the_refusal_shows_the_OTHER_maps_it_could_have_been(forge):
    """THE WHOLE MECHANISM. Requiring a reason alone would only add a sentence to a wrong filing.
    What stops "whatever map is in hand" is being shown, at the moment of choosing, that there IS
    a choice -- every open map and what each is FOR."""
    here = mk_map(forge, body="## Destination\n\nthe map in hand\n\n")
    other = mk_map(forge, body="## Destination\n\na different destination\n\n")
    r = forge.run("child-create", "o/r", str(here["number"]), "t", "body")
    assert f"#{other['number']}" in r.stderr, f"no alternative offered:\n{r.stderr}"
    assert "a different destination" in r.stderr, f"the alternative has no destination:\n{r.stderr}"


def test_the_rationale_lands_on_the_ticket(forge):
    """A routing decision a later reader cannot see is one they must re-derive -- which is how the
    original misfiling stayed invisible until someone read four tickets together."""
    m = mk_map(forge)
    n = _child(forge, m["number"], "serves the destination by fixing X")
    assert "serves the destination by fixing X" in forge.issues[n]["body"]


def test_move_repoints_the_body_the_label_and_BOTH_children_lists(forge):
    """The three bindings. The children block is the one a reader of the TICKET cannot see: miss it
    and the child is either invisible to the new map's frontier or still counted by the old one,
    while the ticket itself shows the right body and the right label."""
    src, dst = mk_map(forge), mk_map(forge)
    n = _child(forge, src["number"])

    r = forge.run("move", "o/r", str(n), str(dst["number"]), "--because", "belongs there")
    assert r.returncode == 0, r.stdout + r.stderr

    child = forge.issues[n]
    assert child["body"].startswith(f"Part of #{dst['number']}"), child["body"]
    labels = [l["name"] if isinstance(l, dict) else l for l in child["labels"]]
    assert f"wayfinder:map-{dst['number']}" in labels, labels
    assert f"wayfinder:map-{src['number']}" not in labels, labels
    assert f"- #{n}\n" in forge.issues[dst["number"]]["body"], "the new map does not list the child"
    assert f"- #{n}\n" not in forge.issues[src["number"]]["body"], "the old map still lists it"
    assert "belongs there" in child["body"], "the move left no rationale on the ticket"


def test_move_refuses_an_issue_that_is_not_a_map_child(forge):
    """Nothing to move it FROM. Guessing a parent here would invent one."""
    dst = mk_map(forge)
    loose = forge.add(title="not a child", body="free-floating", labels=[])
    r = forge.run("move", "o/r", str(loose["number"]), str(dst["number"]), "--because", "x")
    assert r.returncode != 0 and "map child" in r.stderr, r.stderr


def test_move_refuses_a_no_op(forge):
    src = mk_map(forge)
    n = _child(forge, src["number"])
    r = forge.run("move", "o/r", str(n), str(src["number"]), "--because", "x")
    assert r.returncode != 0 and "already on map" in r.stderr, r.stderr


def test_move_also_demands_a_rationale(forge):
    """Moving is a routing decision too, and the map it lands on is the one someone will trust."""
    src, dst = mk_map(forge), mk_map(forge)
    n = _child(forge, src["number"])
    r = forge.run("move", "o/r", str(n), str(dst["number"]))
    assert r.returncode != 0 and "--because" in r.stderr, r.stderr


# --- a resolution that says it is incomplete may not close the ticket ------------------
#
# `resolve` never reads what the ticket owed and cannot judge the work. What it can read is its own
# comment, and the measured instance (2026-09-05) opened "This closes the MECHANICAL half only"
# and closed anyway. The contradiction is internal to the closing artifact.

# The real closing comment, abridged to the sentences that carry the contradiction. It cites
# a PR and no ticket, which is why a `PR #N` must not count as a follow-up.
THE_PARTIAL_CLOSE = (
    "Every string-replacement fault injection now asserts its anchor, and a gate keeps it that way. "
    "Landed in PR #25 (471ad1f024 on main).\n\n"
    "This closes the MECHANICAL half only -- item 1 of what the ticket says it owes.\n\n"
    "WHAT IS NOT DONE, and this ticket should stay open in someone's mind for it: item 2, the "
    "judgement half. The 393 output assertions are UNTOUCHED.\n"
)


def _resolve_fixture(forge):
    m = mk_map(forge, body="## Decisions so far\n\n")
    forge.add(title="the ticket")
    return m, max(forge.issues)


def test_a_resolution_that_says_it_is_incomplete_and_names_no_follow_up_is_refused(forge):
    """THE INJECTION: the comment that closed a ticket. Nothing is written -- no comment, no close, no
    map line -- because a refusal after the POST would leave the ticket closed on the very text
    that says it should not be."""
    m, n = _resolve_fixture(forge)
    before = forge.issues[m["number"]]["body"]

    r = forge.run("resolve", "o/r", str(n), str(m["number"]), THE_PARTIAL_CLOSE)

    assert r.returncode != 0
    assert "names no follow-up ticket" in r.stderr and "incomplete" in r.stderr
    assert "half only" in r.stderr.lower(), "the refusal names the marker it matched"
    assert forge.issues[n]["state"] == "open"
    assert forge.comments.get(n, []) == [], "refused BEFORE any write"
    assert forge.issues[m["number"]]["body"] == before


def test_the_same_comment_naming_the_ticket_that_carries_the_remainder_closes__control(forge):
    """THE CONTROL. A follow-up ticket citation makes the same words consistent with closing: the
    remainder has a home. A refusal that fired here would make every honest partial close
    impossible, which is worse than the defect."""
    m, n = _resolve_fixture(forge)
    r = forge.run("resolve", "o/r", str(n), str(m["number"]),
                  THE_PARTIAL_CLOSE + "\nThe judgement half is carried by #90.\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert forge.issues[n]["state"] == "closed"


def test_the_ticket_and_map_numbers_do_not_count_as_a_follow_up(forge):
    """`Part of #<map>` and `#<self>` appear in nearly every comment; if they counted, the check
    could never fire."""
    m, n = _resolve_fixture(forge)
    r = forge.run("resolve", "o/r", str(n), str(m["number"]),
                  THE_PARTIAL_CLOSE + "\nSee #%d and map #%d.\n" % (n, m["number"]))
    assert r.returncode != 0
    assert forge.issues[n]["state"] == "open"


def test_RESOLVE_PARTIAL_OK_closes_when_the_remainder_is_genuinely_nothing(forge):
    """The other exit: the marker is true and nothing is owed ("not done, and it does not need
    doing"). The variable is the resolver meaning it, and the comment has to say so."""
    m, n = _resolve_fixture(forge)
    forge.env["RESOLVE_PARTIAL_OK"] = "1"
    try:
        r = forge.run("resolve", "o/r", str(n), str(m["number"]),
                      "Item 2 is NOT DONE, and it does not need doing: the files it named were deleted.")
    finally:
        del forge.env["RESOLVE_PARTIAL_OK"]
    assert r.returncode == 0, r.stdout + r.stderr
    assert forge.issues[n]["state"] == "closed"


def test_a_complete_resolution_is_untouched__control(forge):
    """The ordinary case must not pay for the check: no marker, no refusal."""
    m, n = _resolve_fixture(forge)
    r = forge.run("resolve", "o/r", str(n), str(m["number"]),
                  "Fixed at the last remaining site, and the wired hook set re-swept. Landed in PR #12.")
    assert r.returncode == 0, r.stdout + r.stderr
    assert forge.issues[n]["state"] == "closed"


# --- `token-scopes`: the header's security posture, as a verb that can fail ---------

def _declared_scopes() -> list[str]:
    m = re.search(r'^WORKING_TOKEN_SCOPES="([^"]+)"', SCRIPT.read_text(encoding="utf-8"), re.M)
    assert m, "hub-api.sh no longer declares WORKING_TOKEN_SCOPES; the header is back to being prose"
    return m.group(1).split(",")


def _token_scopes(forge):
    return subprocess.run(["sh", str(SCRIPT), "token-scopes"], capture_output=True, text=True,
                          env=forge.env, timeout=60)


def test_token_scopes_passes_when_the_live_token_carries_exactly_the_declared_set(forge):
    """Served in REVERSE order, so a pass here is about the set and not about string equality."""
    forge.tokens = [{"id": 7, "name": "forge-tools-test-1", "token_last_eight": FAKE_TOKEN[-8:],
                     "scopes": list(reversed(_declared_scopes()))}]
    r = _token_scopes(forge)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK:" in r.stdout and "declared :" in r.stdout and "live     :" in r.stdout


def test_token_scopes_fails_when_the_live_token_is_wider_than_the_header_declares(forge):
    """The measured shape exactly: the header asserts a posture, the installed token has another,
    and a reader trusts the header instead of measuring. The verb is the measurement."""
    forge.tokens = [{"id": 7, "name": "forge-tools-test-1", "token_last_eight": FAKE_TOKEN[-8:],
                     "scopes": _declared_scopes() + ["write:admin"]}]
    r = _token_scopes(forge)
    assert r.returncode != 0, "a token wider than declared must fail the verb"
    assert "MISMATCH" in r.stderr and "write:admin" in r.stderr, r.stderr


def test_token_scopes_fails_when_the_live_token_is_narrower_than_the_header_declares(forge):
    """The other direction: a re-mint that DROPPED a scope the header still claims. The header
    is then wrong the same way, just towards safety, and a verb that only caught widening would
    let the declaration drift out of date again."""
    forge.tokens = [{"id": 7, "name": "forge-tools-test-1", "token_last_eight": FAKE_TOKEN[-8:],
                     "scopes": _declared_scopes()[:-1]}]
    r = _token_scopes(forge)
    assert r.returncode != 0
    assert "MISMATCH" in r.stderr


def test_token_scopes_refuses_when_the_installed_token_is_not_in_the_listing(forge):
    """Not a pass: an unidentifiable token is an unmeasured one."""
    forge.tokens = [{"id": 8, "name": "someone-else", "token_last_eight": "zzzzzzzz",
                     "scopes": _declared_scopes()}]
    r = _token_scopes(forge)
    assert r.returncode != 0
    assert "could not identify" in r.stderr


def test_the_declared_scopes_carry_write_user_for_user_owned_repos():
    """Operator ruling, 2026-09-15: `repo create <user>/<repo>` POSTs /user/repos, which the
    forge refuses without write:user, so the scope an earlier cleanup dropped is back. The forge
    stores write:user IN PLACE OF read:user (it implies it): the re-minted token lists exactly these
    four, and `token-scopes` compares sets, so declaring read:user as well would read MISMATCH on a
    correct token. The fake forge never checks scopes, so pin the declaration itself."""
    declared = _declared_scopes()
    assert "write:user" in declared, "repo create for a user-owned repo needs write:user"
    assert "read:user" not in declared, "the forge folds read:user into write:user; declaring both never matches"


# ---- `repo protect`: the forge refuses what a session might not ---------------------

def test_repo_protect_creates_the_rule_and_reads_it_back(forge):
    r = client(forge, "repo", "protect", "o/r", "main", "Test /*", "Fallow /*")
    assert r.returncode == 0, r.stderr
    assert "created protection on main" in r.stdout and "block_on_outdated_branch=True" in r.stdout, r.stdout
    assert [m for m, p in forge.seen if p.endswith("/branch_protections")] == ["GET", "POST"]
    rule = forge.protections[0]
    assert rule["block_on_outdated_branch"] is True and rule["enable_status_check"] is True
    assert sorted(rule["status_check_contexts"]) == ["Fallow /*", "Test /*"]


def test_repo_protect_updates_an_existing_rule_rather_than_adding_a_second(forge):
    forge.protections.append({"rule_name": "main", "branch_name": "main", "enable_status_check": False,
                              "status_check_contexts": [], "block_on_outdated_branch": False})
    r = client(forge, "repo", "protect", "o/r", "main", "Test /*")
    assert r.returncode == 0, r.stderr
    assert "updated protection on main" in r.stdout
    assert len(forge.protections) == 1 and forge.protections[0]["status_check_contexts"] == ["Test /*"]
    assert ("PATCH", "/api/v1/repos/o/r/branch_protections/main") in forge.seen


def test_repo_protect_REFUSES_when_the_read_back_does_not_hold_what_was_sent(forge):
    """A 2xx on the write is a request accepted, not a protection held."""
    forge.protect_drop = True
    r = client(forge, "repo", "protect", "o/r", "main", "Test /*")
    assert r.returncode != 0
    assert "does NOT match" in r.stderr and "UNPROTECTED" in r.stderr, r.stderr


def test_repo_protect_a_branch_name_with_a_slash_is_created_updated_and_read_back(forge):
    """the rule name is a path segment. Raw, `fix/x` became `/branch_protections/fix/x`,
    which the forge 404s -- the rule was created and the read-back died in a JSON traceback, live on
    pytest-test-categories' default branch."""
    r = client(forge, "repo", "protect", "o/r", "fix/x", "Test /*")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "created protection on fix/x" in r.stdout, r.stdout
    r = client(forge, "repo", "protect", "o/r", "fix/x", "Lint /*")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "updated protection on fix/x" in r.stdout, r.stdout
    assert ("PATCH", "/api/v1/repos/o/r/branch_protections/fix%2Fx") in forge.seen, forge.seen
    assert len(forge.protections) == 1 and forge.protections[0]["status_check_contexts"] == ["Lint /*"]


def test_repo_protect_needs_at_least_one_context__control(forge):
    r = client(forge, "repo", "protect", "o/r", "main")
    assert r.returncode != 0 and "at least one status-check context" in r.stderr
    assert not [m for m, p in forge.seen if m in ("POST", "PATCH")], "nothing must be written"


# ---- `repo provision|create|fork`: every repo on the hub comes up gated ------------------

# Read inside the test, never at import: on a base tree without the template a module-level read
# is a collection error that takes every test in this file down with it.
PY_TEMPLATE = REPO / "scripts/templates/forgejo/python.yml"


def _writes(forge):
    return [(m, p) for m, p in forge.seen if m in ("POST", "PATCH", "PUT", "DELETE")]


def test_repo_provision_gates_and_protects_a_bare_repo_on_its_DEFAULT_branch(forge):
    """Not `main`: the default branch is read, and the gate, the read-back and the rule all use it."""
    forge.make_repo("o/r", default_branch="trunk")
    r = client(forge, "repo", "provision", "o/r", "--kind", "python")
    assert r.returncode == 0, r.stdout + r.stderr
    assert forge.files["o/r"][".forgejo/workflows/python.yml"] == PY_TEMPLATE.read_text()
    assert [(p, b) for _, p, b in forge.commits] == [(".forgejo/workflows/python.yml", "trunk")]
    assert forge.repos["o/r"]["has_actions"] is True
    assert forge.collabs["o/r"] == {"carol", "dave"}
    rule = forge.protections[0]
    assert rule["branch_name"] == "trunk" and rule["status_check_contexts"] == ["Test /*"], rule
    assert rule["enable_status_check"] is True and rule["block_on_outdated_branch"] is True
    assert sorted(rule["push_whitelist_usernames"]) == ["carol", "dave"] and rule["enable_push_whitelist"] is True
    assert "push whitelist held: carol, dave" in r.stdout and "Test /*" in r.stdout, r.stdout


def test_repo_provision_without_a_push_whitelist_refuses_naming_the_key_and_writes_nothing(forge):
    forge.make_repo("o/r")
    forge.env.pop("FORGE_TOOLS_PUSH_WHITELIST")
    r = client(forge, "repo", "provision", "o/r", "--kind", "python")
    assert r.returncode != 0 and "FORGE_TOOLS_PUSH_WHITELIST" in r.stderr, r.stdout + r.stderr
    assert not _writes(forge)


def test_repo_provision_never_overwrites_a_gate_and_requires_ITS_contexts(forge):
    forge.make_repo("o/r")
    forge.files["o/r"] = {".forgejo/workflows/ci.yml": "name: CI\non: [pull_request]\n"}
    r = client(forge, "repo", "provision", "o/r", "--kind", "rust")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not forge.commits, "an existing gate must not be overwritten or joined by a template"
    assert forge.files["o/r"] == {".forgejo/workflows/ci.yml": "name: CI\non: [pull_request]\n"}
    assert forge.protections[0]["status_check_contexts"] == ["CI /*"]


def test_repo_provision_no_ci_needs_a_reason(forge):
    forge.make_repo("o/r")
    r = client(forge, "repo", "provision", "o/r", "--no-ci")
    assert r.returncode != 0 and "needs a reason" in r.stderr, r.stderr
    assert not _writes(forge)


def test_repo_provision_no_ci_protects_with_status_checks_off_and_prints_the_reason(forge):
    forge.make_repo("o/r")
    r = client(forge, "repo", "provision", "o/r", "--no-ci", "notes vault, nothing to test")
    assert r.returncode == 0, r.stdout + r.stderr
    rule = forge.protections[0]
    assert rule["enable_status_check"] is False and rule["status_check_contexts"] == []
    assert sorted(rule["push_whitelist_usernames"]) == ["carol", "dave"]
    assert not forge.commits and forge.repos["o/r"]["has_actions"] is False
    assert "notes vault, nothing to test" in r.stdout


def test_repo_provision_FAILS_when_the_forge_drops_the_whitelist(forge):
    """The collaborator add is accepted and not applied, so the forge drops both names from the rule."""
    forge.make_repo("o/r")
    forge.collab_put_ignored = True
    r = client(forge, "repo", "provision", "o/r", "--kind", "python")
    assert r.returncode != 0, r.stdout
    assert "push_whitelist_usernames" in r.stderr and "UNPROTECTED" in r.stderr, r.stderr


def test_repo_provision_refuses_a_mirror(forge):
    forge.make_repo("o/r", mirror=True)
    r = client(forge, "repo", "provision", "o/r", "--kind", "python")
    assert r.returncode == 2 and "mirrors are out of scope" in r.stderr, r.stderr
    assert not _writes(forge)


# ---- the EFFECTIVE workflow directory ------------------------------------------------
# Forgejo runs the first of .forgejo/.gitea/.github/workflows that EXISTS. Provision used to list
# `.forgejo` alone, so a `.github`-gated repo read as ungated and got a template that shadowed its CI.

GH_TEST = "name: Test\non: [pull_request]\n"
GH_DEPLOY = "name: Deploy\non:\n  push:\n    branches: [main]\n"


def _github_gated(forge):
    forge.make_repo("o/r")
    forge.files["o/r"] = {".github/workflows/test.yml": GH_TEST, ".github/workflows/deploy.yml": GH_DEPLOY}


def test_repo_provision_REFUSES_a_github_gated_repo_until_told_whose_gate_it_is(forge):
    _github_gated(forge)
    r = client(forge, "repo", "provision", "o/r", "--kind", "python")
    assert r.returncode == 2, r.stdout + r.stderr
    assert ".github/workflows" in r.stderr and "--existing keep" in r.stderr and "--existing shadow" in r.stderr, r.stderr
    assert not _writes(forge), "nothing may be committed, enabled or protected before the caller answers"


def test_repo_provision_existing_keep_leaves_the_gate_and_requires_only_its_PULL_REQUEST_contexts(forge):
    """A push-only workflow (the deploy) never reports on a PR, so requiring it would block every merge."""
    _github_gated(forge)
    r = client(forge, "repo", "provision", "o/r", "--kind", "python", "--existing", "keep")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not forge.commits and ".forgejo/workflows/python.yml" not in forge.files["o/r"]
    assert forge.protections[0]["status_check_contexts"] == ["Test /*"], forge.protections


def test_repo_provision_existing_shadow_commits_the_template_where_the_forge_will_read_it(forge):
    _github_gated(forge)
    r = client(forge, "repo", "provision", "o/r", "--kind", "python", "--existing", "shadow")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [(p, b) for _, p, b in forge.commits] == [(".forgejo/workflows/python.yml", "main")]


def test_repo_provision_judges_the_FIRST_existing_directory_even_with_no_workflows_in_it(forge):
    """PASSES ON BASE: a pin -- the old `.forgejo`-only listing committed the template here too; it guards the resolver against skipping an existing empty directory (proven by injection).

    An existing `.forgejo/workflows` holding no yml is what the forge reads: nothing runs, so the
    `.github` gate behind it is already shadowed and the template replaces nothing."""
    forge.make_repo("o/r")
    forge.files["o/r"] = {".forgejo/workflows/README.md": "notes\n", ".github/workflows/test.yml": GH_TEST}
    r = client(forge, "repo", "provision", "o/r", "--kind", "python")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [(p, b) for _, p, b in forge.commits] == [(".forgejo/workflows/python.yml", "main")]


def test_repo_provision_an_UNREADABLE_listing_is_not_an_absent_one(forge):
    forge.make_repo("o/r")
    forge.fail[".forgejo/workflows"] = 500
    r = client(forge, "repo", "provision", "o/r", "--kind", "python")
    assert r.returncode != 0 and "could not be read" in r.stderr, r.stdout + r.stderr
    assert not _writes(forge)


def test_repo_provision_names_a_repo_it_cannot_read(forge):
    """Item 3: it refused before, but as a repo that 'reports no default branch'."""
    forge.fail["/repos/o/missing"] = 404
    r = client(forge, "repo", "provision", "o/missing", "--kind", "python")
    assert r.returncode != 0 and "could not read o/missing" in r.stderr, r.stderr
    assert "no default branch" not in r.stderr
    assert not _writes(forge)


def test_repo_provision_REFUSES_a_gate_with_no_pull_request_workflow(forge):
    forge.make_repo("o/r")
    forge.files["o/r"] = {".forgejo/workflows/deploy.yml": GH_DEPLOY}
    r = client(forge, "repo", "provision", "o/r", "--kind", "python")
    assert r.returncode != 0 and "pull_request" in r.stderr, r.stdout + r.stderr
    assert not _writes(forge)


def test_repo_fork_SHADOWS_upstream_github_workflows_without_being_told(forge):
    """PASSES ON BASE: a pin -- the old `.forgejo`-only listing never saw upstream's `.github` and committed the template anyway; it guards the fork default of `--existing shadow` (proven by injection).

    A fresh fork's `.github` is upstream's CI by definition; running it once starved the runner."""
    forge.files["o/proj"] = {".github/workflows/ci.yml": "name: Upstream\non: [pull_request, push]\n"}
    r = client(forge, "repo", "fork", "https://example.invalid/up/proj.git", "o/proj", "--kind", "rust")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [(p, b) for _, p, b in forge.commits] == [(".forgejo/workflows/rust.yml", "main")]
    assert forge.protections[0]["status_check_contexts"] == ["Test /*"]


def test_repo_fork_migrates_as_a_non_mirror_then_provisions(forge):
    r = client(forge, "repo", "fork", "https://example.invalid/up/proj.git", "o/proj", "--kind", "rust")
    assert r.returncode == 0, r.stdout + r.stderr
    mig = forge.migrations[0]
    assert mig["mirror"] is False and mig["service"] == "git" and mig["clone_addr"].endswith("proj.git"), mig
    assert _writes(forge)[0] == ("POST", "/api/v1/repos/migrate")
    assert ".forgejo/workflows/rust.yml" in forge.files["o/proj"]
    assert forge.repos["o/proj"]["has_actions"] is True and forge.protections[0]["status_check_contexts"] == ["Test /*"]


def test_repo_create_creates_under_the_org_with_auto_init_then_provisions(forge):
    r = client(forge, "repo", "create", "o/new", "--kind", "python", "--private")
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("POST", "/api/v1/orgs/o/repos") in forge.seen
    assert forge.created[0]["auto_init"] is True and forge.created[0]["private"] is True
    assert ".forgejo/workflows/python.yml" in forge.files["o/new"]
    assert forge.protections[0]["branch_name"] == "main"


def test_note_refuses_a_bare_flag_where_the_comment_goes(forge):
    """the half `pr create`'s guard does not reach.

    Fifteen writes on this forge landed with a body of literally `-F`: eight through `pr create`
    and SEVEN through `issue note` -- the `gh` habit spells a body file `-F <path>`, this client
    takes the text positionally, so the flag becomes the comment and the filename is dropped. It
    printed `commented #N` every time, which is why nobody read it back. One of the seven was a
    "merging this takes the live site down" analysis.
    """
    n = forge.add(title="t")["number"]
    r = forge.run("note", "o/r", str(n), "-F", "/tmp/body.md")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "is a flag where the comment goes" in r.stderr, r.stderr
    assert not forge.comments.get(n), f"it posted anyway: {forge.comments.get(n)}"


def test_resolve_refuses_a_bare_flag_where_the_resolution_goes(forge):
    """The one with no repair path: a resolution closes the ticket and writes the map's index
    line, so a two-character resolution is both silent and permanent."""
    n = forge.add(title="t")["number"]
    m = mk_map(forge, children=[n])
    r = forge.run("resolve", "o/r", str(n), str(m["number"]), "-F", "/tmp/body.md")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "is a flag where the resolution comment goes" in r.stderr, r.stderr
    assert forge.issues[n]["state"] == "open", "it resolved anyway"


def test_a_comment_beginning_with_a_hyphen_is_still_a_comment__control(forge):
    """The collision the expansion rejected. A flag has no whitespace; prose usually does, so the
    whitespace test comes first and ordinary prose is untouched."""
    n = forge.add(title="t")["number"]
    r = forge.run("note", "o/r", str(n), "-- and that is the finding")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "is a flag where" not in r.stderr, r.stderr


# --- a claim made on another host cannot be judged from this one ---------------------------


def _resolver_on(tmp_path, host, rc):
    p = tmp_path / "session-attest.sh"
    p.write_text('#!/bin/sh\ncase "$1" in\n  host) echo "%s" ;;\n  resolve) exit %d ;;\n  *) exit 2 ;;\nesac\n' % (host, rc))
    p.chmod(0o755)
    return str(p)


def _claimed_on(forge, sid, host, **kw):
    it = forge.add(assignees=["claude"], **kw)
    forge.comments.setdefault(it["number"], []).append(
        {"body": "Claimed by session %s.\n\nclaim-session: %s\nclaim-host: %s" % (sid, sid, host),
         "user": {"login": "claude"}})
    return it


def test_claim_RECORDS_the_claiming_host(forge, tmp_path):
    forge.add(title="ticket")
    forge.env["FORGE_TOOLS_SESSION_ID"] = ENDED_SID
    forge.env["HUB_API_ATTEST"] = _resolver_on(tmp_path, "machine:THISBOX", 0)
    r = forge.run("claim", "o/r", "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert any("claim-host: machine:THISBOX" in c["body"] for c in forge.comments.get(1, [])), forge.comments


def test_frontier_does_NOT_take_a_claim_made_on_ANOTHER_host(forge, tmp_path):
    """The resolver answers for this box only; a session on another box would read as ended."""
    held = _claimed_on(forge, ENDED_SID, "machine:OTHERBOX", title="claimed elsewhere")
    later = forge.add(title="free")
    m = mk_map(forge, children=[held["number"], later["number"]])
    forge.env["HUB_API_ATTEST"] = _resolver_on(tmp_path, "machine:THISBOX", 1)
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: #%d" % later["number"] in r.stdout, r.stdout + r.stderr
    assert "UNKNOWN" in r.stdout, r.stdout


def test_a_claim_made_on_THIS_host_whose_session_ended_is_still_TAKEN__control(forge, tmp_path):
    held = _claimed_on(forge, ENDED_SID, "machine:THISBOX", title="abandoned here")
    later = forge.add(title="free")
    m = mk_map(forge, children=[held["number"], later["number"]])
    forge.env["HUB_API_ATTEST"] = _resolver_on(tmp_path, "machine:THISBOX", 1)
    r = forge.run("frontier", "o/r", str(m["number"]))
    assert "FRONTIER: #%d" % held["number"] in r.stdout, r.stdout + r.stderr
    assert "ENDED" in r.stdout, r.stdout


# ------------------------------------------------------ `adopt`, for an UNMAPPED issue
#
# `move` re-parents a child and refuses one with no `wayfinder:map-N` label -- correctly, there is
# nothing to move it from. Its refusal advises `child-create`, which CREATES a second issue, so
# taking that advice abandons the numbered ticket someone already filed. `issue file` produces that
# shape deliberately and the weekly maintainer run files seven or eight a week; eighteen piled up by
# 2026-09-14 and a whole map was built to absorb them.


def test_adopt_binds_the_body_the_labels_and_the_childrens_list(forge):
    m = mk_map(forge)
    loose = forge.add(title="filed by the maintainer", body="the finding", labels=[])
    n = loose["number"]

    r = forge.run("adopt", "o/r", str(n), str(m["number"]), "--because", "its surfaces are this map's")
    assert r.returncode == 0, r.stdout + r.stderr

    got = forge.issues[n]
    assert got["body"].startswith(f"Part of #{m['number']}"), got["body"]
    assert "the finding" in got["body"], "adoption must not discard the ticket's own body"
    assert "its surfaces are this map's" in got["body"], "no rationale left on the ticket"
    labels = [l["name"] if isinstance(l, dict) else l for l in got["labels"]]
    assert {"wayfinder", f"wayfinder:map-{m['number']}", "wayfinder:task"} <= set(labels), labels
    assert f"- #{n}\n" in forge.issues[m["number"]]["body"], "the map does not list it"


def test_an_adopted_ticket_can_then_be_MOVED(forge):
    """The reason the body line is not cosmetic. `move` rewrites `Part of #<src>` and DIES when the
    line is absent, so an issue adopted by label and children block alone is a child that the
    re-parenting verb refuses to touch -- a one-way door nothing reports. Seven maintainer tickets
    were adopted by hand that way on 2026-09-21 before this verb existed."""
    first, second = mk_map(forge), mk_map(forge)
    loose = forge.add(title="t", body="b", labels=[])
    n = loose["number"]
    assert forge.run("adopt", "o/r", str(n), str(first["number"]), "--because", "x").returncode == 0

    r = forge.run("move", "o/r", str(n), str(second["number"]), "--because", "reconsidered")
    assert r.returncode == 0, "adoption left the ticket unmovable:\n" + r.stdout + r.stderr
    assert forge.issues[n]["body"].startswith(f"Part of #{second['number']}")


def test_adopt_refuses_a_ticket_that_is_already_another_maps_child(forge):
    """Re-parenting is `move`'s job; doing it here would leave two map labels or silently drop one."""
    src, dst = mk_map(forge), mk_map(forge)
    n = _child(forge, src["number"])
    r = forge.run("adopt", "o/r", str(n), str(dst["number"]), "--because", "x")
    # Assert the REASON, not merely non-zero. A review caught the weaker form: with no `adopt` verb
    # the unknown-verb bail also exits non-zero, and it PRINTS THE VERB LIST -- so even `"move" in
    # stderr` matched it. The test passed on the base and proved nothing about the refusal.
    assert r.returncode != 0, r.stdout
    assert "is already a child of" in r.stderr, r.stderr
    assert f"issue move o/r {n} {dst['number']}" in r.stderr, "the refusal must name the verb that does work"


def test_adopt_repairs_a_HALF_adopted_ticket_and_rewrites_nothing_else(forge):
    """The state this has to survive, because it is the state hand-adoption leaves: labels and the
    children entry already right, the body line missing."""
    m = mk_map(forge)
    loose = forge.add(title="t", body="b",
                      labels=["wayfinder", f"wayfinder:map-{m['number']}", "wayfinder:task"])
    n = loose["number"]
    anchor = "<!-- /wayfinder:children -->"
    assert anchor in forge.issues[m["number"]]["body"], (
        f"fault-injection anchor {anchor!r} is gone -- this test would inject nothing")
    forge.issues[m["number"]]["body"] = forge.issues[m["number"]]["body"].replace(
        anchor, f"- #{n}\n" + anchor)

    r = forge.run("adopt", "o/r", str(n), str(m["number"]), "--because", "repairing the body line")
    assert r.returncode == 0, r.stdout + r.stderr
    assert forge.issues[n]["body"].startswith(f"Part of #{m['number']}"), forge.issues[n]["body"]
    assert "already:" in r.stdout, "a repair must say what was already in place: " + r.stdout
    assert forge.issues[m["number"]]["body"].count(f"- #{n}\n") == 1, "listed twice"


def test_adopt_needs_a_because(forge):
    m = mk_map(forge)
    loose = forge.add(title="t", body="b", labels=[])
    r = forge.run("adopt", "o/r", str(loose["number"]), str(m["number"]))
    assert r.returncode != 0 and "--because" in r.stderr, r.stderr


def test_adopt_refuses_a_target_that_is_not_a_map(forge):
    """Before anything is written: a non-map target would otherwise get a children block grafted on."""
    not_a_map = forge.add(title="ordinary", body="b", labels=[])
    loose = forge.add(title="t", body="b", labels=[])
    r = forge.run("adopt", "o/r", str(loose["number"]), str(not_a_map["number"]), "--because", "x")
    assert r.returncode != 0, r.stdout
    # Same correction: "non-zero and nothing written" is also true of an absent verb.
    assert "is not labelled wayfinder:map" in r.stderr, r.stderr
    assert "Part of #" not in (forge.issues[loose["number"]]["body"] or ""), "it wrote before refusing"
