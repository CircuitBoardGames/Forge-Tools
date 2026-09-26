"""`scripts/hub-api.sh` is load-bearing and was ungated.

It holds the only credential this box has for the self-hosted forge, it is what `git push
hub` authenticates through, and `pr checks` is the gate primitive for the cut-over. Three
of its behaviours are the kind that fail silently:

  * the credential guard, which already leaked a live token once by being false in the
    environment it protects -- a check that cannot fail is not a check;
  * `pr checks`, which must REFUSE a commit carrying zero check-runs -- Forgejo answers
    that case with `total_count: 0` and an EMPTY state, so every natural test reads it as
    a pass;
  * config validation, which must not treat an ssh error message on disk as a credential.

These drive the real script over a local HTTP server rather than mocking its internals,
because the bugs worth catching here live in the plumbing between curl, the JSON and the
exit code -- exactly what a mock of that plumbing would assume away.
"""

from __future__ import annotations

import json
import pathlib
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "hub-api.sh"

# 40 hex chars: the shape valid_config() insists on. Not a real credential.
FAKE_TOKEN = "0123456789abcdef0123456789abcdef01234567"

# `pr checks` and `pr create` take a FULL 40-char sha and refuse every other shape, because a
# ref silently resolves to a moving tip and a short sha cannot be compared to a PR head later.
# These tests used "deadbeef"/"x" before that rule existed; the shape is incidental
# to what each of them is actually about, so they get a valid one rather than an exemption.
SHA = "1111111111111111111111111111111111111111"
OTHER_SHA = "2222222222222222222222222222222222222222"


def write_config(tmp_path: Path, token: str = FAKE_TOKEN, mode: int = 0o600) -> Path:
    cfg = tmp_path / "hub-api.conf"
    cfg.write_text(f'header = "Authorization: token {token}"\n')
    cfg.chmod(mode)
    return cfg


class _Handler(BaseHTTPRequestHandler):
    payload: dict = {}
    received: str | None = None  # raw request body of the last POST, for encoding checks
    # Every Content-Type that arrived on the last write, in order. A LIST, not a string:
    # The client defaults the header when the caller named none, and the failure mode of doing
    # that carelessly is sending it TWICE alongside an explicit one -- which a scalar holding
    # "the last value" would report as a clean single header.
    received_cts: list = []
    # Path fragments this stand-in answers 404 to. `pr checks` now asks a SECOND question
    # when a commit carries no check-runs -- does the commit exist at all -- and the two
    # answers produce different refusals, so a server that says 200 to everything can only
    # ever exercise one of them.
    missing: tuple = ()

    # Workflow runs for `/actions/runs?head_sha=`, which `was_cancelled` asks when a gate reads
    # red. None means the route is not wired for this test and answers zero runs -- which the
    # helper reports as "nothing measured" (rc 2), NOT as "not cancelled". That default is what
    # keeps every red test written before cancellation detection still measuring a red.
    runs: list | None = None

    # The status this stand-in answers with, and the body to answer it with. Default 200 +
    # `payload`, which every test written before refusal reporting relies on. A refusal is what `pr merge`
    # has to REPORT, so it cannot be exercised by a server that only ever agrees.
    status: int = 200
    error_body: str | None = None

    # Directory listings per git ref, for the gate-parity arm: `pr checks` asks what
    # workflows `main` defines and what the measured HEAD carries. Default empty, which makes the
    # arm silent -- every test written before this one relies on that.
    contents: dict = {}

    # The wiki listing, one list per page, for the queue-freeze arm of `pr merge`: page N
    # is wiki[N-1], every later page is empty, and "BROKEN" is not JSON. Default empty -- no freeze
    # -- which every merge test written before that arm relies on.
    wiki: list | str = []

    # The OPEN PR listing, for `pr create`'s draft decision: a PR that is not at the
    # front of the queue opens as a draft. Default EMPTY -- "nothing else is open", so this PR is
    # the front and opens ready, which is what every `pr create` test written before this relies
    # on. "BROKEN" is not JSON, for the arm that refuses rather than reading an unread listing as
    # an empty queue.
    pulls: list | str = []

    # Label listings for `pr create --serial`: the repo's labels (`/labels?limit=100`, what
    # `issue label-id` resolves against) and one PR's labels (`/issues/N/labels`, the read-back).
    # OPT-IN -- None falls through to `payload`, which an existing merge test reads its label
    # readback from. A default list here would silently change what that test measures.
    repo_labels: list | None = None
    pr_labels: list | None = None

    # A PR's commits for `pr body --from-commits`, NEWEST FIRST as the real forge lists them
    # (measured on the real forge). None falls through to `payload`, as every older test expects.
    commits: list | None = None

    def _respond(self):
        if "/wiki/pages" in self.path:
            query = dict(kv.split("=", 1) for kv in self.path.partition("?")[2].split("&") if "=" in kv)
            n = int(query.get("page", "1"))
            if self.wiki == "BROKEN":
                raw = b"<html>not json</html>"
            elif self.wiki in ("UNINIT", "OTHER404", "UNINIT500"):
                # The real forge's answer for a wiki never initialised, and two near-misses.
                errs = ["user does not exist"] if self.wiki == "OTHER404" else ["no such file or directory"]
                raw = json.dumps({"message": "The target couldn't be found.", "errors": errs}).encode()
                self.send_response(500 if self.wiki == "UNINIT500" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            else:
                raw = json.dumps(self.wiki[n - 1] if n <= len(self.wiki) else []).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if any(frag in self.path for frag in self.missing):
            self.send_error(404, "not found")
            return
        if "/commits" in self.path and self.commits is not None:
            raw = json.dumps(self.commits).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if "/actions/runs" in self.path:
            raw = json.dumps({"workflow_runs": self.runs or []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if "/labels" in self.path and self.command == "GET":
            listing = self.pr_labels if "/issues/" in self.path else self.repo_labels
            if listing is not None:
                raw = json.dumps(listing).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
        # THE QUERY IS THE DISCRIMINATOR, not the word `pulls`: `pr create` POSTs to
        # `/api/v1/repos/o/r/pulls` with no query, and matching on the bare path would answer the
        # creation itself with a listing instead of the created PR.
        if "/pulls?" in self.path:
            raw = (b"<html>not json</html>" if self.pulls == "BROKEN"
                   else json.dumps(self.pulls).encode())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if "/contents/" in self.path:
            ref = self.path.split("ref=", 1)[1] if "ref=" in self.path else ""
            # A `"<ref>|<dir>"` key answers one directory at one ref (the forge runs the
            # FIRST existing of .forgejo/.gitea/.github, so a test must be able to tell them apart);
            # a bare `"<ref>"` key still answers every directory, as every older test relies on.
            directory = self.path.split("/contents/", 1)[1].split("?", 1)[0]
            listing = self.contents.get("%s|%s" % (ref, directory), self.contents.get(ref))
            if listing == "MISSING":
                # The REAL forge's answer for a path that does not exist (measured 2026-09-15).
                raw = json.dumps({"message": "GetContentsOrList", "errors": ["object does not exist"]}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if listing is None:
                # An object carrying `message`, not a list. The REAL forge sends that body with a
                # 404 (measured 2026-09-15); a test that needs the 404 lists the path in `missing`.
                raw = json.dumps({"message": "path does not exist"}).encode()
            elif listing == "BROKEN":
                raw = b"<html>not json</html>"
            else:
                raw = json.dumps([{"name": n, "type": "file"} for n in listing]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.status != 200 and "/merge" in self.path:
            raw = (self.error_body or "").encode()
            self.send_response(self.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        _Handler.received_headers = dict(self.headers)
        self._respond()

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        _Handler.received = self.rfile.read(n).decode() if n else ""
        _Handler.received_cts = self.headers.get_all("Content-Type") or []
        # Every POST, in order, with its path: a fast-forward merge is followed by a label POST,
        # and `received` alone would then hold the label body rather than the merge payload.
        _Handler.received_all.append((self.path, _Handler.received))
        self._respond()

    # The content-type default covers every passthrough write, and a server that only answers POST can only
    # ever demonstrate a third of it.
    do_PATCH = do_POST
    do_PUT = do_POST

    def log_message(self, *a):  # silence the per-request stderr line
        pass


@pytest.fixture
def server():
    """A local stand-in for the forge, so the exit-code path runs for real."""
    _Handler.missing = ()   # per-test state, or one test's 404s leak into the next
    _Handler.runs = None    # ditto: a cancelled run left set here would un-red every later gate test
    _Handler.status = 200   # ditto: a refusal left set here would redden every later merge test
    _Handler.received_all = []
    _Handler.error_body = None
    _Handler.contents = {}
    _Handler.wiki = []      # no queue freeze unless a test lists one
    _Handler.pulls = []     # nothing else open, so `pr create` opens at the front
    _Handler.repo_labels = None   # opt-in label listings; None falls through to payload
    _Handler.pr_labels = None
    _Handler.commits = None
    # Cleared per-test for a sharper reason than tidiness: left set, a test whose request never
    # reached the server at all would read the PREVIOUS test's headers and pass on them.
    _Handler.received = None
    _Handler.received_cts = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    # poll_interval: shutdown() waits for the loop's next poll, 0.5 s by default -- half a second
    # of idle teardown per test, 131 s across a full CI-like run.
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    yield httpd
    httpd.shutdown()


# The tests written for the two-parent merge keep their meaning under that style; the
# fast-forward tests below name their own. An EMPTY value exercises the client's default.
MERGE = {"HUB_API_MERGE_STYLE": "merge"}
FF = {"HUB_API_MERGE_STYLE": ""}


def _env(cfg: Path, url: str | None = None, env_extra: dict | None = None) -> dict:
    """The environment `run` gives the script: one builder, so a test that must invoke the script
    through `sh -c` (to close its stdout under it) gets exactly the same seams."""
    env = dict(os.environ)
    env["HUB_API_CONFIG"] = str(cfg)
    # No site config file leaks in: on a configured box it names the REAL forge.
    env["FORGE_TOOLS_CONFIG"] = str(cfg.parent / "no-forge-tools-config")
    if url:
        env["HUB_URL"] = url
        # HEAD, not hub/main: the currency guard otherwise runs a real `git fetch hub main` over the
        # network on EVERY write verb -- 1.4 s per test (measured 2026-09-16).
        env["HUB_API_CURRENCY_REF"] = "HEAD"
    env.pop("HUB_API_CRED_INTENT", None)
    # A non-draft `pr create` registers a gate-watch for the session named by FORGE_TOOLS_WAKE_PID.
    # Run from inside a live session, that would write into the REAL gate-watch registry and
    # get a test session peer-messaged by the box's timer. Tests that exercise the watch pass both.
    env.pop("FORGE_TOOLS_WAKE_PID", None)
    env.pop("GATE_WATCH_REGISTRY", None)
    env.update(env_extra or {})
    return env


def run(args, cfg: Path, url: str | None = None, env_extra: dict | None = None):
    env = _env(cfg, url, env_extra)
    if args[:2] == ["pr", "merge"] and "HUB_API_MERGE_STYLE" not in (env_extra or {}):
        env["HUB_API_MERGE_STYLE"] = "merge"
    return subprocess.run(
        ["sh", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60
    )


def url_of(httpd) -> str:
    return f"http://127.0.0.1:{httpd.server_port}"


# --------------------------------------------------------------------------- Access path


def _access_get(server, tmp_path, access: Path | None):
    _Handler.received_headers = None   # a request that never arrived must not read a stale set
    extra = {"HOME": str(tmp_path)}    # no real hub-access.conf under ~/.config leaks in
    if access is not None:
        extra["HUB_ACCESS_CONFIG"] = str(access)
    return run(["/api/v1/version"], write_config(tmp_path), url_of(server), extra)


def _access_config(tmp_path: Path, mode: int = 0o600) -> Path:
    p = tmp_path / "hub-access.conf"
    p.write_text('header = "CF-Access-Client-Id: probe-id"\n')
    p.chmod(mode)
    return p


def test_the_access_pair_reaches_the_forge_beside_the_token(server, tmp_path):
    r = _access_get(server, tmp_path, _access_config(tmp_path))
    assert r.returncode == 0, r.stderr
    h = _Handler.received_headers or {}
    assert h.get("CF-Access-Client-Id") == "probe-id"
    assert h.get("Authorization") == f"token {FAKE_TOKEN}", "the second config must not displace the token"


def test_no_access_config_sends_no_access_header__control(server, tmp_path):
    r = _access_get(server, tmp_path, None)
    assert r.returncode == 0, r.stderr
    assert _Handler.received_headers is not None, "the request never arrived -- this control measured nothing"
    assert "CF-Access-Client-Id" not in _Handler.received_headers


def test_a_group_readable_access_config_is_refused(server, tmp_path):
    r = _access_get(server, tmp_path, _access_config(tmp_path, 0o644))
    assert r.returncode != 0 and "want 600" in r.stderr
    assert _Handler.received_headers is None, "refused must mean nothing was sent"


# --------------------------------------------------------------------------- pr checks


def test_zero_check_runs_is_refused_not_passed(server, tmp_path):
    """The whole reason this verb exists.

    Forgejo answers a commit with no check-runs with `total_count: 0` and `state: ""`.
    Measured on hub 2026-08-12. A workflow that never triggered looks exactly like this,
    and it must not read as green.
    """
    _Handler.payload = {"state": "", "total_count": 0, "statuses": []}
    r = run(["pr", "checks", "o/r", SHA], write_config(tmp_path), url_of(server))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSING" in r.stdout
    assert "has NO check-runs registered" in r.stdout
    assert SHA in r.stdout, "the verdict must name the commit it measured"


def _json_lines(stdout: str) -> list[dict]:
    """Every stdout line under `--json` is one JSON record; a line that is not JSON fails here."""
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_pr_checks_json_green_is_one_checks_record_naming_its_commit(server, tmp_path):
    """A verdict an agent can branch on without matching prose. Same exit codes as text."""
    _Handler.payload = {"state": "success", "total_count": 2,
                        "statuses": [{"context": "Test / pytest", "status": "success"},
                                     {"context": "Lint / eslint", "status": "skipped"}]}
    r = run(["pr", "checks", "o/r", SHA, "--json"], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    checks = [x for x in _json_lines(r.stdout) if x["kind"] == "checks"]
    assert len(checks) == 1, r.stdout
    assert checks[0]["verdict"] == "green" and checks[0]["sha"] == SHA
    assert checks[0]["total_count"] == 2 and checks[0]["skipped"] == 1
    assert {"context": "Lint / eslint", "status": "skipped"} in checks[0]["statuses"]


def test_pr_checks_json_zero_check_runs_is_a_refusal_record_and_exit_2(server, tmp_path):
    _Handler.payload = {"state": "", "total_count": 0, "statuses": []}
    r = run(["pr", "checks", "o/r", SHA, "--json"], write_config(tmp_path), url_of(server))
    assert r.returncode == 2, r.stdout + r.stderr
    recs = _json_lines(r.stdout)
    assert [x["verdict"] for x in recs if x["kind"] == "checks"] == ["no_checks"], r.stdout
    assert [x["reason"] for x in recs if x["kind"] == "refusal"] == ["no_check_runs"], r.stdout


def test_pr_checks_json_red_is_not_green_and_exit_1(server, tmp_path):
    _Handler.payload = {"state": "failure", "total_count": 1,
                        "statuses": [{"context": "Test / pytest", "status": "failure"}]}
    r = run(["pr", "checks", "o/r", SHA, "--json"], write_config(tmp_path), url_of(server))
    assert r.returncode == 1, r.stdout + r.stderr
    assert [x["verdict"] for x in _json_lines(r.stdout) if x["kind"] == "checks"] == ["not_green"]


def test_pr_await_REFUSES_a_short_sha_BEFORE_it_starts_waiting(tmp_path):
    """The short-sha measurement, SECOND INSTANCE -- and this test is the part that was missing.

    The first instance recorded: *"a gate poller spun every 30s for ~7 minutes on a 10-char sha while the
    PR it watched went green in about two."* It answered by giving `refuse()` exit code 2, so a
    caller can tell "this input will never work" from "not ready yet".

    IT RECURRED ON 2026-09-02 anyway, because a convention only helps a caller that reads the exit
    code. A hand-rolled loop passed a 12-char sha -- truncated by its own debug print, then fed back
    in -- and matched on STDOUT text for `state='success'`. The refusal goes to STDERR and carries
    none of those tokens, so eighteen polls measured nothing and it reported "not terminal" about a
    PR that had been green the whole time.

    So the sha is validated BEFORE the first sleep: the permanent case that produced both instances
    is a short sha, and catching it up front costs one call and removes the class. No server fixture
    here on purpose -- if this ever reaches the network, the front-validation has been lost.
    """
    r = run(["pr", "await", "o/r", "a7ad0d16c04a"], write_config(tmp_path), "http://127.0.0.1:1")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "short sha" in r.stderr, r.stderr
    assert "REFUSING" in r.stderr, "a refusal must announce itself, or a poller reads it as patience"


def test_pr_await_treats_a_RED_gate_as_a_verdict_not_a_delay(server, tmp_path):
    """A red gate is an ANSWER. A poller that keeps waiting for it to improve is the same defect as
    one that waits through a refusal -- it converts a finished measurement into an open question."""
    _Handler.payload = {"state": "failure", "total_count": 1,
                        "statuses": [{"context": "Test / pytest", "status": "failure"}]}
    r = run(["pr", "await", "o/r", SHA, "5", "1"], write_config(tmp_path), url_of(server))
    assert r.returncode == 1, r.stdout + r.stderr
    # It names the STATE it measured rather than a generic "RED": `failure` and `error` are
    # different answers and a reader acts on which one it was.
    assert "failure after 1 poll" in r.stderr, r.stderr


def test_pr_await_does_not_call_a_CANCELLED_run_a_verdict(server, tmp_path):
    """The commit-STATUS API renders a cancelled job as `failure`, and Forgejo cancels the
    in-flight run every time a PR head is force-pushed -- so this red is routinely one the driver
    caused itself. gate-watch.py relays this verb's rc=1 to a session as a verdict, which is how a
    cancellation reached a requester as "your PR is RED".

    NO exemption marker on purpose: this MUST fail on the base, where the runs route is never
    consulted and the red exits 1 on the first poll. Writing the exemption token followed by the
    word "no" would have read as an exemption -- the gate matches the token, not the sentence, and
    quoting the token to say that would trip it again (wiki cases 217, 218).
    """
    _Handler.payload = {"state": "failure", "total_count": 1,
                        "statuses": [{"context": "Test / pytest", "status": "failure"}]}
    _Handler.runs = [{"status": "cancelled"}, {"status": "success"}]
    r = run(["pr", "await", "o/r", SHA, "2", "0"], write_config(tmp_path), url_of(server))
    # Exhaustion, never green and never red: nothing has been measured about this head yet.
    assert r.returncode == 2, r.stdout + r.stderr
    assert "CANCELLED, not failed" in r.stderr, r.stderr
    assert "a verdict, not a delay" not in r.stderr, (
        "a cancellation was reported as a verdict:\n" + r.stderr)


def test_pr_await_maps_a_cancellation_STILL_IN_FLIGHT_to_not_a_verdict(server, tmp_path):
    """The one probe (forge.sh) answers 3 for `cancelled,running` -- a re-queue
    may still be coming -- where this client's own copy used to answer 0. `pr await` maps 3 the way
    it maps 0: not a verdict, keep polling. A mapping that let only 0 divert would call this red."""
    _Handler.payload = {"state": "failure", "total_count": 1,
                        "statuses": [{"context": "Test / pytest", "status": "failure"}]}
    _Handler.runs = [{"status": "cancelled"}, {"status": "running"}]
    r = run(["pr", "await", "o/r", SHA, "2", "0"], write_config(tmp_path), url_of(server))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "CANCELLED, not failed" in r.stderr, r.stderr
    assert "a verdict, not a delay" not in r.stderr, r.stderr


def test_pr_await_still_calls_an_UNCANCELLED_red_a_verdict__control(server, tmp_path):
    """THE CONTROL ON THE CHECK ABOVE. Same red, same route consulted, runs present and none of them
    cancelled -- so the diversion must NOT fire. Without this, a `was_cancelled` that returned 0
    unconditionally would pass the cancellation test and silently convert every red into patience."""
    _Handler.payload = {"state": "failure", "total_count": 1,
                        "statuses": [{"context": "Test / pytest", "status": "failure"}]}
    _Handler.runs = [{"status": "success"}, {"status": "failure"}]
    r = run(["pr", "await", "o/r", SHA, "2", "0"], write_config(tmp_path), url_of(server))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "failure after 1 poll" in r.stderr, r.stderr
    assert "CANCELLED" not in r.stderr, r.stderr


def test_pr_await_treats_an_UNMEASURABLE_runs_api_as_still_red__control(server, tmp_path):
    """FAIL CLOSED. Zero runs is not "not cancelled" -- it is "nothing was measured", and the same
    false absence the client-side scan produced. A red that cannot be shown to be a cancellation
    stays a verdict, so a runs API that is down cannot turn a real red into an open question."""
    _Handler.payload = {"state": "failure", "total_count": 1,
                        "statuses": [{"context": "Test / pytest", "status": "failure"}]}
    _Handler.runs = []          # the route answers, and answers nothing
    r = run(["pr", "await", "o/r", SHA, "2", "0"], write_config(tmp_path), url_of(server))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "failure after 1 poll" in r.stderr, r.stderr


def test_pr_await_KEEPS_WAITING_through_pending(server, tmp_path):
    """THE CASE THE FIRST VERSION GOT WRONG, and no test could have caught it.

    `pr checks` exits 1 for ANY state that is not success, and `pending` is one of them. The first
    version read exit 1 as RED and gave up on its first poll against a gate that had barely started
    -- the exact mirror of the bug this verb exists to fix. There a refusal read as patience; here
    patience read as a verdict.

    IT WAS FOUND BY USING THE VERB, not by testing it. The fixture only ever produced `failure` and
    empty, so `pending` was a state the suite could not reach. That absence is the finding: a test
    file that cannot produce a state is a test file that certifies nothing about it.
    """
    _Handler.payload = {"state": "pending", "total_count": 8,
                        "statuses": [{"context": "Test / pytest", "status": "pending"}]}
    r = run(["pr", "await", "o/r", SHA, "2", "1"], write_config(tmp_path), url_of(server))
    # It must EXHAUST rather than call it red -- and exhaustion is 2, never 0.
    assert r.returncode == 2, r.stdout + r.stderr
    assert "state=pending" in r.stderr, r.stderr
    assert "gave up after 2 poll(s)" in r.stderr, r.stderr
    assert "RED" not in r.stderr and "verdict, not a delay" not in r.stderr, (
        "pending was reported as a verdict:\n" + r.stderr)


def test_pr_await_ABORTS_on_a_state_it_cannot_classify(server, tmp_path):
    """THE CONTROL ON THE STATE PARSE. Reading a field from another tool's output is the thing this
    verb otherwise refuses to do, so the unrecognised case must STOP rather than default to waiting.
    A format change then surfaces as an abort instead of an infinite poll -- which is how the
    original defect stayed invisible for eighteen rounds."""
    _Handler.payload = {"state": "moonphase", "total_count": 3, "statuses": []}
    r = run(["pr", "await", "o/r", SHA, "3", "1"], write_config(tmp_path), url_of(server))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "unrecognised state moonphase" in r.stderr, r.stderr


def test_pr_await_gives_up_LOUDLY_and_never_reads_as_green(server, tmp_path):
    """Zero check-runs is briefly true after a push, so a bounded retry is right -- but the give-up
    must not be silent or ambiguous. `pr checks` refuses with 2 here, and 2 is what this returns:
    the caller cannot mistake exhaustion for success."""
    _Handler.payload = {"state": "", "total_count": 0, "statuses": []}
    r = run(["pr", "await", "o/r", SHA, "2", "1"], write_config(tmp_path), url_of(server))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "gave up after 2 poll(s)" in r.stderr, r.stderr
    assert "Do not read this as green" in r.stderr, r.stderr


def test_pr_log_REFUSES_a_sha_no_run_carries(server, tmp_path):
    """THE PROPERTY THIS VERB EXISTS FOR.

    Before it, reading a job log meant guessing an id across three integer namespaces that all
    resolve: `/actions/tasks` ids, `/actions/runs` ids, and a run's `index_in_repo`. A session read
    the run NUMBER off a commit status `target_url`, passed it where an ID goes, and got a real,
    well-formed log from twelve days earlier reporting a green suite. Nothing 404s -- the number was
    a valid id belonging to an older run -- and a full diagnosis was spent on the wrong subject.

    So the verb binds on `commit_sha` and REFUSES when nothing matches. The refusal is the whole
    design: falling back to "the newest run" would reproduce the original failure exactly, and it is
    the fallback any reasonable person would write.
    """
    _Handler.payload = {"workflow_runs": [
        {"id": 4432, "index_in_repo": 3995, "workflow_id": "test.yml",
         "commit_sha": "0" * 40, "status": "success"},
    ]}
    r = run(["pr", "log", "o/r", SHA], write_config(tmp_path), url_of(server))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no run in the last 50 carries commit_sha" in r.stderr, r.stderr
    assert SHA in r.stderr, "the refusal must name the sha it could not find"
    assert "NOT falling back" in r.stderr, (
        "the refusal has to say what it declined to do, or the next author adds the fallback back")


def test_a_failing_state_is_refused(server, tmp_path):
    _Handler.payload = {
        "state": "failure",
        "total_count": 2,
        "statuses": [
            {"context": "Test / pytest", "status": "failure"},
            {"context": "Lint / eslint", "status": "success"},
        ],
    }
    r = run(["pr", "checks", "o/r", SHA], write_config(tmp_path), url_of(server))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSING" in r.stdout


def test_success_passes_and_reports_registered_and_skipped(server, tmp_path):
    """A pass must not let the registered count imply coverage.

    Skipped contexts count toward `total_count` -- on the real repo a green push read
    `total_count=7` with 3 skipped. Reporting only the 7 would overstate what was checked.
    """
    _Handler.payload = {
        "state": "success",
        "total_count": 3,
        "statuses": [
            {"context": "a", "status": "success"},
            {"context": "b", "status": "skipped"},
            {"context": "c", "status": "success"},
        ],
    }
    r = run(["pr", "checks", "o/r", SHA], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "3 registered, 1 skipped" in r.stdout
    # NOT "2 actually ran". A job can start, skip every step internally and report
    # success, and this reads the commit-status API, which cannot see inside a job. The count was
    # dropped rather than renamed because the NUMBER was the overstatement: `total - skipped` reads
    # as "ran" under any label short enough for a summary line.
    assert "actually ran" not in r.stdout, "the verb claims coverage it cannot measure"
    assert "registered is not RAN" in r.stdout, "and it must say why the number is absent"


def test_a_job_that_skipped_itself_is_not_counted_as_coverage(server, tmp_path):
    """Measured on a real PR's green head: the line said "4 actually ran" and the
    fourth was eslint, whose log reads `SKIPPED - 0 of 3 changed file(s) are TS/JS anywhere`.

    THE API CANNOT DISTINGUISH THESE TWO, which is the whole point: both statuses below are
    `success`, and one of them measured nothing. No amount of reading this endpoint separates them,
    so the honest verdict is to not claim it. `forge.sh` already refuses to inherit the claim; this
    is the verb the overclaim was actually found in.
    """
    _Handler.payload = {
        "state": "success", "total_count": 2,
        "statuses": [{"context": "pytest", "status": "success"},
                     {"context": "eslint", "status": "success"}],   # started, skipped its steps
    }
    r = run(["pr", "checks", "o/r", SHA], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 registered, 0 skipped" in r.stdout
    assert "actually ran" not in r.stdout
    assert "cannot see inside" in r.stdout, "the reason belongs in the output, not only a ticket"


def test_the_check_can_fail__control(server, tmp_path):
    """Positive control for the three tests above.

    Each asserts on an exit code, and an exit code is exactly the kind of signal that can
    become constant without anyone noticing. This pins that the SAME invocation returns
    different codes for different payloads -- i.e. that the verb is reading the payload at
    all, rather than always refusing (which would make the refusal tests vacuous).
    """
    cfg = write_config(tmp_path)
    codes = []
    for payload in (
        {"state": "", "total_count": 0, "statuses": []},
        {"state": "success", "total_count": 1, "statuses": [{"context": "a", "status": "success"}]},
    ):
        _Handler.payload = payload
        codes.append(run(["pr", "checks", "o/r", SHA], cfg, url_of(server)).returncode)
    assert codes == [2, 0], f"the verb returned {codes} -- it is not reading the payload"


# ------------------------------------------------------------------ credential handling


def test_git_credential_refuses_without_the_intent_variable(tmp_path):
    """Case 35: the original guard used `[ -t 1 ]` and leaked a token on first run.

    Under pytest, subprocess pipes are not ttys -- the same condition that defeated the
    old guard -- so this test would have PASSED against the broken version had it asserted
    on a tty. It asserts on the credential instead: the token must not appear in stdout.
    """
    cfg = write_config(tmp_path)
    r = run(["git-credential", "get"], cfg)
    assert r.returncode != 0
    assert FAKE_TOKEN not in r.stdout
    assert FAKE_TOKEN not in r.stderr
    assert "never directly" in r.stderr


def test_git_credential_emits_the_token_when_intent_is_set(tmp_path):
    """The other direction, so the refusal above is not just a broken subcommand."""
    cfg = write_config(tmp_path)
    r = run(["git-credential", "get"], cfg, env_extra={"HUB_API_CRED_INTENT": "1"})
    assert r.returncode == 0, r.stderr
    assert f"password={FAKE_TOKEN}" in r.stdout
    assert "username=" in r.stdout


# ------------------------------------------------------------------- config validation


def test_an_ssh_error_on_disk_is_not_a_credential(tmp_path):
    """The mint path writes ssh's stdout to the config. If a mint fails and the file is
    left holding an error message, that must not be used as a token."""
    cfg = tmp_path / "hub-api.conf"
    cfg.write_text("ssh: connect to host hubvps port 22: Connection refused\n")
    cfg.chmod(0o600)
    r = run(["fingerprint"], cfg)
    assert r.returncode != 0
    assert "no valid token" in r.stderr


def test_a_group_readable_credential_is_refused(tmp_path):
    r = run(["fingerprint"], write_config(tmp_path, mode=0o644))
    assert r.returncode != 0
    assert "refusing to use it" in r.stderr


def test_a_valid_config_is_accepted__control(tmp_path):
    """Control for the two refusals above: they must not pass for an unrelated reason."""
    r = run(["fingerprint"], write_config(tmp_path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("sha256:")
    assert FAKE_TOKEN not in r.stdout, "fingerprint must not disclose the token"


# ----------------------------------------------------------------------- json building


@pytest.mark.parametrize(
    "title",
    [
        'a "quoted" title',
        "an apostrophe's worth",
        "a\\backslash",
        "a\ttab and trailing space ",
    ],
)
def test_a_hostile_title_reaches_the_forge_intact(server, tmp_path, title):
    """PR titles routinely contain quotes and apostrophes; hand-rolled JSON breaks on them
    and surfaces as an opaque 400.

    This asserts on the bytes the SERVER received. An earlier version of this test asserted
    `json.loads(json.dumps(title)) == title`, which exercises the standard library and not
    this script -- it would have passed against any implementation, including a broken one.
    Testing the encoder by encoding with the encoder is not a test.
    """
    _Handler.received = None
    # One payload serves both GETs the verb now makes: the branch lookup reads `commit.id`,
    # the POST response reads `number`. HUB_API_EXPECT_HEAD states the tree this PR is meant
    # to be about, which is what keeps the head verification from needing a real branch here.
    _Handler.payload = {"number": 1, "state": "open", "mergeable": True, "commit": {"id": SHA}}
    _Handler.contents = {"main": ["test.yml"]}  # the target has CI, or `pr create` refuses first
    r = run(
        ["pr", "create", "o/r", "head", "main", title, "body"],
        write_config(tmp_path),
        url_of(server),
        env_extra={"HUB_API_EXPECT_HEAD": SHA},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert _Handler.received is not None, "the server saw no POST body"
    assert json.loads(_Handler.received)["title"] == title


# ----------------------------------------------------------------------------- pr body


def _create(server, tmp_path, *tail):
    _Handler.received = None
    _Handler.payload = {"number": 1, "state": "open", "mergeable": True, "commit": {"id": SHA}}
    _Handler.contents = {"main": ["test.yml"]}  # the target has CI, or `pr create` refuses first
    return run(
        ["pr", "create", "o/r", "head", "main", "t: an imperative", *tail],
        write_config(tmp_path),
        url_of(server),
        env_extra={"HUB_API_EXPECT_HEAD": SHA},
    )


def test_pr_create_refuses_a_missing_body_rather_than_defaulting_it(server, tmp_path):
    """The body was `${7:-}`, so forgetting the argument opened a bodyless PR silently at
    exit 0 -- done twice in one evening, hours after this repo merged the skill stating the rule.

    THERE IS NO REPAIR PATH, which is why this refuses rather than warns: `issue body-edit` is a
    literal exactly-once region replacement, and an empty body has no region to match. A bodyless PR
    has to be closed and reopened, so the only cheap moment is before it exists.
    """
    r = _create(server, tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSING" in r.stderr
    assert "--title-only" in r.stderr, "the refusal must offer the opt-out it demands"
    assert "Closes #N" in r.stderr, "and must name the closing keyword, measured at 5 of 25"
    assert _Handler.received is None, "nothing may be created on a refusal"


def test_title_only_is_an_accepted_answer_not_an_error(server, tmp_path):
    """THE CONTROL, and the reason this is not just `${7:?}`. A body is NOT always
    earned -- the median PR here is 55 lines across 2 files and for those a body is ceremony. A
    mandatory body would buy filler, which is the failure that skill measured at 17 of 25 PRs
    carrying one identical 122-character body. `--title-only` keeps the judgement available while
    making it a statement rather than a silence.
    """
    r = _create(server, tmp_path, "--title-only")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(_Handler.received)["body"] == ""


def test_an_ordinary_body_still_reaches_the_forge_unchanged(server, tmp_path):
    """The guard must not eat the payload it is guarding. Asserted on the bytes the SERVER received,
    for the reason the title test above gives: checking the argument we passed proves nothing."""
    body = "Closes #63.\n\nReasoning the diff cannot show.\n"
    r = _create(server, tmp_path, body)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(_Handler.received)["body"] == body


def test_pr_create_refuses_an_EMPTY_body_argument(server, tmp_path):
    """The hole one spelling along from the missing-body refusal: a body argument that EXPANDS to nothing -- a
    missing file under "$(cat f)", an unset $BODY, an empty @file: -- fell through every arm and
    opened a bodyless PR at exit 0. Whitespace-only is the same silence."""
    for empty in ("", "  \n\t"):
        r = _create(server, tmp_path, empty)
        assert r.returncode == 2, (repr(empty), r.stdout + r.stderr)
        assert "EMPTY" in r.stderr and "--from-commits" in r.stderr, r.stderr
        assert _Handler.received is None, "nothing may be created on a refusal"


def _commit(sha, message):
    return {"sha": sha, "commit": {"message": message}}


COMMITS_NEWEST_FIRST = [
    _commit("b" * 40, "second: the fix\n\nWhy the fix is shaped this way.\n\nCo-Authored-By: A Bot <bot@example.com>\n"),
    _commit("a" * 40, "first: the probe\n\nThe measurement.\n"),
]
FROM_COMMITS = ("### first: the probe\n\nThe measurement.\n\n"
                "### second: the fix\n\nWhy the fix is shaped this way.")


def _body(server, tmp_path, *args, read_back=None):
    _Handler.commits = COMMITS_NEWEST_FIRST
    _Handler.payload = {"number": 7, "body": FROM_COMMITS if read_back is None else read_back}
    return run(["pr", "body", "o/r", "7", *args], write_config(tmp_path), url_of(server))


def test_pr_body_from_commits_writes_them_oldest_first_without_trailers(server, tmp_path):
    """The operator's verb (2026-09-23): the commit messages ARE the PR's record, so the body is
    built from them rather than the PR reading as empty beside them. The forge lists commits newest
    first; a body reads oldest first. Asserted on the bytes the forge received."""
    r = _body(server, tmp_path, "--from-commits")
    assert r.returncode == 0, r.stdout + r.stderr
    patches = [json.loads(b) for p, b in _Handler.received_all if p.endswith("/pulls/7")]
    assert patches == [{"body": FROM_COMMITS}], _Handler.received_all
    assert "read back identical" in r.stdout


def test_pr_body_refuses_when_the_read_back_differs(server, tmp_path):
    """A 2xx is a request accepted, not a body held."""
    r = _body(server, tmp_path, "--from-commits", read_back="something else")
    assert r.returncode == 1 and "did NOT stick" in r.stderr, r.stdout + r.stderr


def test_pr_body_shares_create_s_reading_of_the_body_argument(server, tmp_path):
    """One reading for both verbs: the refusals `pr create` makes, `pr body` makes too."""
    for arg, needle in (("", "EMPTY"), ("-F", "is a flag"), ("--title-only", "OPENED without a body")):
        r = _body(server, tmp_path, arg)
        assert r.returncode == 2 and needle in r.stderr, (arg, r.stdout + r.stderr)
    body = "Closes #1.\n\nThe reasoning."
    r = _body(server, tmp_path, body, read_back=body)
    assert r.returncode == 0, r.stdout + r.stderr


def test_pr_create_from_commits_writes_the_body_after_opening(server, tmp_path):
    """`pr create ... --from-commits` opens the PR, then writes its body through `pr body`, the one
    code path that builds it: the create POST carries an empty body, the PATCH that follows carries
    the commits. `payload` serves the create response AND the read-back, so it holds both."""
    _Handler.received = None
    _Handler.payload = {"number": 1, "state": "open", "mergeable": True, "commit": {"id": SHA},
                        "body": FROM_COMMITS}
    _Handler.contents = {"main": ["test.yml"]}
    _Handler.commits = COMMITS_NEWEST_FIRST
    r = run(["pr", "create", "o/r", "head", "main", "t: an imperative", "--from-commits"],
            write_config(tmp_path), url_of(server), env_extra={"HUB_API_EXPECT_HEAD": SHA})
    assert r.returncode == 0, r.stdout + r.stderr
    creates = [json.loads(b) for p, b in _Handler.received_all if p.endswith("/pulls")]
    patches = [json.loads(b) for p, b in _Handler.received_all if p.endswith("/pulls/1")]
    assert [c["body"] for c in creates] == [""], _Handler.received_all
    assert patches == [{"body": FROM_COMMITS}], _Handler.received_all


def test_pr_create_from_commits_writes_the_body_even_when_stdout_closes_after_one_line(server, tmp_path):
    """Three PRs landed with EMPTY bodies, all opened by
    `pr create ... --from-commits 2>&1 | head -1`. The verb printed "PR #N open" first, `head` then
    closed the pipe, and the NEXT print killed the script by SIGPIPE -- before the body was written
    and before gate-watch was registered. A verb that has done the irreversible part must finish the
    record before it says anything on stdout, because stdout is the one channel a caller can close
    under it. Asserted on the bytes the forge received, with the pipe really closed after one line."""
    _Handler.received = None
    _Handler.payload = {"number": 1, "state": "open", "mergeable": True, "commit": {"id": SHA},
                        "body": FROM_COMMITS}
    _Handler.contents = {"main": ["test.yml"]}
    _Handler.commits = COMMITS_NEWEST_FIRST
    env = _env(write_config(tmp_path), url_of(server), {"HUB_API_EXPECT_HEAD": SHA})
    subprocess.run(
        ["sh", "-c", f'sh "{SCRIPT}" pr create o/r head main "t: an imperative" --from-commits 2>/dev/null | head -1'],
        capture_output=True, text=True, env=env, timeout=60,
    )
    patches = [json.loads(b) for p, b in _Handler.received_all if p.endswith("/pulls/1")]
    assert patches == [{"body": FROM_COMMITS}], (
        "the body PATCH never reached the forge: the create died on its first stdout line after "
        f"`head` closed the pipe -- {_Handler.received_all}")


# ------------------------------------------------------------------------- merge subject


def merge_subject(server, tmp_path, subject: str, num: str = "42"):
    """Run the real `pr merge` and return (returncode, MergeTitleField-the-server-got)."""
    _Handler.received = None
    _Handler.payload = {}
    r = run(["pr", "merge", "o/r", num, subject], write_config(tmp_path), url_of(server))
    sent = json.loads(_Handler.received)["MergeTitleField"] if _Handler.received else None
    return r, sent


BODY_COMMIT = [
    {"commit": {"message": (
        "prune-merged: report a merged branch that has no local ref\n"
        "\n"
        "The sweep enumerates refs/heads/, so a merged branch with no local\n"
        "ref is invisible.\n"
        "\n"
        "Closes #43\n"
        "\n"
        "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
    )}}
]


def test_the_merge_style_is_merge_not_squash(server, tmp_path):
    """An operator ruling. THE ONLY AUTOMATED PIN ON THE MERGE STYLE.

    Squash rewrites every branch commit into a new sha, which is the sole reason
    `git merge-base --is-ancestor`, `branch -d`, `branch --contains` and `main..branch` all
    answer "not landed" about landed work -- and so the sole reason
    `prune-landed-branches-forgejo.sh` must ask the forge instead of git. Nothing else in the
    suite asserts on `Do=`, so without this a future edit flips the style back in silence and
    the only symptom is advisories quietly resuming their false negatives months later.

    Asserts on the bytes the SERVER received, not on what the client says it sent.
    """
    _Handler.received = None
    _Handler.payload = BODY_COMMIT
    r = run(["pr", "merge", "o/r", "42", "a subject"], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _Handler.received is not None, "the server saw no POST body"
    sent = json.loads(_Handler.received)
    assert sent.get("Do") == "merge", (
        f"merge style is {sent.get('Do')!r}, not 'merge' -- squash destroys the shas CI tested "
        f"and re-breaks every git-side 'did this land' query: {sent}"
    )


# ------------------------------------------------------------------- fast-forward
def test_the_default_style_is_fast_forward_only_with_no_subject_and_no_body(server, tmp_path):
    """An EMPTY HUB_API_MERGE_STYLE is the client's default: `Do` is
    fast-forward-only and the payload carries neither MergeTitleField nor MergeMessageField --
    there is no merge commit for either to land on. The expected head still travels."""
    _Handler.payload = [{"name": "merge-path:hub-api-direct"}]     # the labels readback
    r = run(["pr", "merge", "o/r", "42", "a subject", SHA], write_config(tmp_path), url_of(server), env_extra=FF)
    assert r.returncode == 0, r.stdout + r.stderr
    merge_posts = [(p, b) for p, b in _Handler.received_all if p.endswith("/pulls/42/merge")]
    assert merge_posts, _Handler.received_all
    sent = json.loads(merge_posts[0][1])
    assert sent == {"Do": "fast-forward-only", "head_commit_id": SHA}, sent
    assert "merged: http=200" in r.stdout


def test_a_fast_forward_records_its_path_as_a_label_and_says_so(server, tmp_path):
    """No merge commit, no trailer: the record is `merge-path:<path>` on the PR, applied AFTER the
    merge and read back."""
    _Handler.payload = [{"name": "merge-path:pr-queue"}]
    r = run(["pr", "merge", "o/r", "42", "a subject"], write_config(tmp_path), url_of(server),
            env_extra={**FF, "HUB_API_MERGE_PATH": "pr-queue"})
    assert r.returncode == 0, r.stdout + r.stderr
    label_posts = [(p, b) for p, b in _Handler.received_all if p.endswith("/issues/42/labels")]
    assert label_posts and json.loads(label_posts[-1][1]) == {"labels": ["merge-path:pr-queue"]}, _Handler.received_all
    order = [p for p, _ in _Handler.received_all]
    assert order.index(next(p for p in order if p.endswith("/merge"))) < order.index(next(p for p in order if p.endswith("/labels"))), (
        "the label must be applied AFTER the merge, never before a merge that might be refused")
    assert "merge-path: pr-queue (label on #42)" in r.stdout, r.stdout


def test_a_label_that_does_not_stick_is_reported_and_the_merge_stands(server, tmp_path):
    _Handler.payload = []                                   # the readback shows no label
    r = run(["pr", "merge", "o/r", "42", "a subject"], write_config(tmp_path), url_of(server), env_extra=FF)
    assert r.returncode == 0
    assert "merged: http=200" in r.stdout and "did NOT stick" in r.stdout, r.stdout


def test_a_refused_fast_forward_applies_no_label(server, tmp_path):
    _Handler.status = 500
    _Handler.payload = {"message": "Merge DivergingFastForwardOnly Error: Not possible to fast-forward"}
    r = run(["pr", "merge", "o/r", "42", "a subject"], write_config(tmp_path), url_of(server), env_extra=FF)
    assert "merged: http=500" in r.stdout, r.stdout
    merge_posts = [b for p, b in _Handler.received_all if p.endswith("/pulls/42/merge")]
    assert merge_posts and json.loads(merge_posts[0]).get("Do") == "fast-forward-only", merge_posts
    assert not [p for p, _ in _Handler.received_all if p.endswith("/labels")], "a refused merge must not be recorded as a merge"


# ------------------------------------------------------------------- queue freeze
FREEZE_LISTED = [{"title": "Home", "sub_url": "Home"}, {"title": "Queue Freeze", "sub_url": "Queue-Freeze"}]


def _merge_posts():
    return [p for p, _ in _Handler.received_all if p.endswith("/merge")]


def test_a_hand_merge_refuses_while_the_queue_is_frozen(server, tmp_path):
    _Handler.wiki = [FREEZE_LISTED]
    r = run(["pr", "merge", "o/r", "42", "a subject", SHA], write_config(tmp_path), url_of(server), env_extra=FF)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "the queue is FROZEN" in r.stderr and "HUB_API_FREEZE_OVERRIDE" in r.stderr, r.stderr
    assert not _merge_posts(), "a frozen queue must send no merge"


def test_the_same_merge_proceeds_when_no_freeze_is_listed__control(server, tmp_path):
    _Handler.wiki = [[{"title": "Home", "sub_url": "Home"}]]
    _Handler.payload = [{"name": "merge-path:hub-api-direct"}]
    r = run(["pr", "merge", "o/r", "42", "a subject", SHA], write_config(tmp_path), url_of(server), env_extra=FF)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _merge_posts() and "FROZEN" not in r.stderr, r.stderr


def test_a_freeze_on_a_later_listing_page_is_still_found(server, tmp_path):
    """The per-page cap is unmeasured, so the client reads until an EMPTY page, not a short one."""
    _Handler.wiki = [[{"title": "p%d" % i, "sub_url": "p%d" % i} for i in range(50)], FREEZE_LISTED]
    r = run(["pr", "merge", "o/r", "42", "a subject", SHA], write_config(tmp_path), url_of(server), env_extra=FF)
    assert r.returncode == 2 and "the queue is FROZEN" in r.stderr, r.stdout + r.stderr
    assert not _merge_posts()


def test_an_unreadable_wiki_listing_refuses_the_merge_as_cannot_tell(server, tmp_path):
    _Handler.wiki = "BROKEN"
    r = run(["pr", "merge", "o/r", "42", "a subject", SHA], write_config(tmp_path), url_of(server), env_extra=FF)
    assert r.returncode == 2 and "cannot tell whether the queue is frozen" in r.stderr, r.stdout + r.stderr
    assert not _merge_posts()


def test_a_never_initialised_wiki_is_no_freeze_and_the_merge_proceeds(server, tmp_path):
    """A fresh fork's wiki listing 404s with "no such file or directory", which made every
    new repo unmergeable. That exact answer is an empty wiki, not an unread one."""
    _Handler.wiki = "UNINIT"
    _Handler.payload = [{"name": "merge-path:hub-api-direct"}]
    r = run(["pr", "merge", "o/r", "42", "a subject", SHA], write_config(tmp_path), url_of(server), env_extra=FF)
    assert r.returncode == 0, r.stdout + r.stderr
    # The refusal's own words, not bare "cannot tell": the client-currency NOTE says "cannot tell whether
    # this client is current" wherever `hub/main` does not resolve -- the CI checkout -- and matched it.
    assert _merge_posts() and "cannot tell whether the queue is frozen" not in r.stderr, r.stderr


@pytest.mark.parametrize("wiki", ["OTHER404", "UNINIT500"])
def test_any_other_wiki_failure_still_refuses_as_cannot_tell__control(server, tmp_path, wiki):
    _Handler.wiki = wiki
    r = run(["pr", "merge", "o/r", "42", "a subject", SHA], write_config(tmp_path), url_of(server), env_extra=FF)
    assert r.returncode == 2 and "cannot tell whether the queue is frozen" in r.stderr, r.stdout + r.stderr
    assert not _merge_posts()


def test_the_override_merges_through_a_freeze_and_prints_its_reason(server, tmp_path):
    _Handler.wiki = [FREEZE_LISTED]
    _Handler.payload = [{"name": "merge-path:hub-api-direct"}]
    r = run(["pr", "merge", "o/r", "42", "a subject", SHA], write_config(tmp_path), url_of(server),
            env_extra={**FF, "HUB_API_FREEZE_OVERRIDE": "operator: hotfix during acceptance"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _merge_posts(), "the override must reach the merge"
    assert "FREEZE OVERRIDDEN" in r.stderr and "operator: hotfix during acceptance" in r.stderr, r.stderr


def test_squash_is_refused_and_an_unknown_style_is_refused(server, tmp_path):
    r = run(["pr", "merge", "o/r", "42", "a subject"], write_config(tmp_path), url_of(server), env_extra={"HUB_API_MERGE_STYLE": "squash"})
    assert r.returncode != 0 and "squash -- retired" in r.stderr and not _Handler.received_all
    r = run(["pr", "merge", "o/r", "42", "a subject"], write_config(tmp_path), url_of(server), env_extra={"HUB_API_MERGE_STYLE": "rebase"})
    assert r.returncode != 0 and "not a landing style" in r.stderr and not _Handler.received_all


def test_the_commit_body_survives_the_merge(server, tmp_path):
    """Under `Do=squash` this guarded against DATA LOSS.

    Measured on the last 25 commits of main before this was fixed: every `parents=1` squash
    carried a 0-byte body, while the `parents=2` merge-commit era kept 491-3180 B beneath it. So
    `Closes #N` never reached main to be honoured (a ticket stayed open and was closed by hand),
    `Co-Authored-By:` was written by every session and deleted at merge, and the repo's convention
    of putting the reasoning in the commit was writing into a field the merge path discarded.
    Nothing failed and nothing warned -- only a diff of a pre-merge commit against its merged
    counterpart showed it.

    UNDER `Do=merge` THE STAKES CHANGED AND THIS TEST DID NOT. The branch's commits now survive
    as parents carrying their own messages, so the reasoning can no longer be destroyed here.
    What MergeMessageField buys now is the `--first-parent` view -- which shows merge commits
    ONLY, so an empty merge-commit body would render the one-entry-per-PR history unreadable
    with the detail one parent away. Still worth asserting; the reason is different.

    Asserts on the bytes the SERVER received, not on what the client says it sent.
    """
    _Handler.received = None
    _Handler.payload = BODY_COMMIT
    r = run(["pr", "merge", "o/r", "42", "a subject"], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _Handler.received is not None, "the server saw no POST body"
    sent = json.loads(_Handler.received)
    body = sent.get("MergeMessageField")
    assert body, f"no MergeMessageField sent; the body would be discarded again: {sent}"
    assert "Closes #43" in body, body
    assert "Co-Authored-By" in body, body
    # The subject travels in MergeTitleField; repeating it here lands it twice in the commit.
    assert not body.startswith("prune-merged: report a merged branch"), (
        f"the subject was duplicated into the body:\n{body}"
    )


# ------------------------------------------------------------------------------------------
# `head_commit_id` -- the compare-and-swap on the merge itself.
#
# `pr-queue.sh` verifies a PR is green on a sha, then POSTs the merge. Between those two the
# head can move, and its 405 repair arm exists because that race is measured rather than
# theoretical. Re-reading does not close it: any read-then-act leaves a window that ends only
# when the write lands. `head_commit_id` moves the comparison INTO the write, so the forge
# refuses atomically.
#
# This is the one race `flock` cannot cover. The lock makes THIS BOX a single operator; a human
# merging from the web UI never takes it (a PR landed exactly that way, no session in the loop).
#
# The field is real on THIS hub rather than assumed: `swagger.v1.json` ->
# MergePullRequestOption lists `head_commit_id`, measured on 16.0.2+gitea-1.22.0 with a
# nonexistent field name as the negative control.
#
# WHAT THESE TESTS CANNOT REACH: whether the FORGE honours it. That is forge behaviour and no
# local server can speak for it -- these assert the client SENDS it, exactly as the rebase-style
# test above says of its own parameter. Do not read a pass here as evidence a moved head is
# actually refused.
# ------------------------------------------------------------------------------------------


def test_the_expected_head_is_sent_as_head_commit_id(server, tmp_path):
    """Asserts on the bytes the SERVER received, not on what the client says it sent."""
    _Handler.received = None
    _Handler.payload = BODY_COMMIT
    r = run(["pr", "merge", "o/r", "42", "a subject", SHA],
            write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _Handler.received is not None, "the server saw no POST body"
    sent = json.loads(_Handler.received)
    assert sent.get("head_commit_id") == SHA, (
        f"the expected head was not sent, so the merge is a read-then-act again: {sent}"
    )


def test_no_expected_head_sends_no_head_commit_id__control(server, tmp_path):
    """The negative control, and the reason the test above can fail.

    Without this, a client that unconditionally stuffed some sha into every payload would pass
    the positive test while pinning merges to the wrong tree. It also pins the compatibility
    promise: the argument is OPTIONAL, and an omitted one must reproduce the exact request sent
    before this existed -- a signature change discovered at merge time is the worst place to
    discover one.
    """
    _Handler.received = None
    _Handler.payload = BODY_COMMIT
    r = run(["pr", "merge", "o/r", "42", "a subject"], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    sent = json.loads(_Handler.received)
    assert "head_commit_id" not in sent, (
        f"a head was pinned that no caller asked for: {sent}"
    )
    # The rest of the request must be untouched, or "optional" is not what happened.
    assert sent.get("Do") == "merge" and sent.get("MergeTitleField"), sent


@pytest.mark.parametrize(
    "arg, wanted",
    [
        ("main", "is a ref, not a commit sha"),
        ("agent/290-which-tree", "is a ref with a slash"),
        ("87c8f8f2bd", "short sha"),
    ],
)
def test_pr_merge_refuses_an_expected_head_that_is_not_a_full_sha(server, tmp_path, arg, wanted):
    """Same shapes `pr checks` refuses, same reasons -- and the stakes are higher here.

    A ref silently resolves to its CURRENT tip, so `head_commit_id=main` would pin the merge to
    whatever main points at at that instant, which is a CAS that always passes: the check would
    be present, cost a round trip, and guarantee nothing. That is worse than not sending it,
    because the payload then looks protected.
    """
    _Handler.received = None
    _Handler.payload = BODY_COMMIT
    r = run(["pr", "merge", "o/r", "42", "a subject", arg],
            write_config(tmp_path), url_of(server))
    assert r.returncode != 0, f"{arg!r} was accepted as an expected head: {r.stdout}"
    assert wanted in r.stderr, r.stderr
    assert _Handler.received is None, (
        "the merge was POSTed anyway -- a refusal after the write is not a refusal"
    )


def test_an_unusable_commits_response_falls_back_to_the_old_request(server, tmp_path):
    """The negative arm, and it is the one that keeps a merge from ever being blocked.

    Without it the test above passes for an implementation that fetches the body and dies when it
    cannot. Losing the body again is the OLD bug; refusing the merge would be a NEW one, so an
    unreachable endpoint, unparseable JSON or an empty list must never stop the POST.

    THE ASSERTION CHANGED WITH THE MERGE-PATH TRAILER AND THE PROPERTY DID NOT. This used to assert
    `MergeMessageField` was absent entirely, which was the old *implementation* of "do not invent a
    body". Since then the field always carries a `Merge-Path:` trailer, and that is not an
    invention: it is a locally-known fact about which code path is running, needing no request and
    unable to fail. Provenance that vanished whenever the commits read failed would go missing in
    exactly the degraded conditions where knowing the path matters most.

    So the two things this test is actually for are asserted directly, and more strictly than
    before: the merge still goes through, and NO PROSE IS FABRICATED -- the body is the trailer and
    nothing else.
    """
    _Handler.received = None
    _Handler.payload = {}          # an object, not a list of commits: unusable
    r = run(["pr", "merge", "o/r", "42", "a subject"], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    sent = json.loads(_Handler.received)
    assert sent.get("MergeMessageField") == "Merge-Path: hub-api-direct", (
        f"an unusable commits response produced a body that is not JUST the trailer: {sent}")
    assert sent["MergeTitleField"] == "a subject (#42)", sent


def test_a_subject_without_the_number_gets_it_appended(server, tmp_path):
    """This client always sends MergeTitleField, so Forgejo's default
    "<title> (#N)" can never apply and the number exists only if someone typed it -- and
    `main` refuses force push for admins, so the omission is permanent. Measured instance:
    a merge commit landed without its number. The verb has the number in argv; it must use it."""
    r, sent = merge_subject(server, tmp_path, "prune-merged: name the side it deleted")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sent == "prune-merged: name the side it deleted (#42)"


def test_a_correct_number_is_not_doubled(server, tmp_path):
    """`pr-queue.sh` already appends `(#N)` itself, so the common call arrives complete."""
    r, sent = merge_subject(server, tmp_path, "docs: a title (#42)")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sent == "docs: a title (#42)"


def test_a_different_number_is_refused_not_rewritten(server, tmp_path):
    """A typed `(#7)` on PR #42 is a claim about another PR. Only the caller knows which
    half is wrong, and a commit pointing at the wrong PR is as permanent as one pointing at
    none -- so this refuses BEFORE any POST rather than guessing."""
    r, sent = merge_subject(server, tmp_path, "docs: a title (#7)")
    assert r.returncode != 0
    assert "(#7)" in r.stderr and "#42" in r.stderr, r.stderr
    assert sent is None, f"it merged anyway, sending {sent!r}"


def test_parentheses_that_are_not_a_pr_ref_do_not_look_like_one(server, tmp_path):
    """The refusal above must key on a trailing `(#<digits>)`, not on parentheses."""
    r, sent = merge_subject(server, tmp_path, "hub-api: fix the merge subject (again)")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sent == "hub-api: fix the merge subject (again) (#42)"


# --------------------------------------------- a refusal must carry its reason
#
# `pr merge` used to run `-o /dev/null -w 'merged: http=%{http_code}\n'`, so every refusal
# arrived as three digits with the forge's sentence discarded. **405 covers several distinct
# refusals that need OPPOSITE responses** -- an outdated branch means update and retry, failed
# required checks means do not update and go read the log -- and the code separates none of
# them. Measured on a real PR, where recovering "outdated branch" took a separate
# `branch_protections` read.
#
# These assert the emitted text NAMES THE CASE. Asserting the reason is merely non-empty would
# pass on a client that printed the wrong body, and grepping the script's source would be
# satisfied by the comment above describing the fix.

OUTDATED_BODY = json.dumps(
    {"message": "Branch is outdated. Please update the branch and try again.", "url": ""}
)
CHECKS_BODY = json.dumps(
    {"message": "not allowed to merge [reason: Not all required status checks successful]"}
)


def merge_refusal(server, tmp_path, status: int, body: str | None):
    _Handler.status = status
    _Handler.error_body = body
    _Handler.received = None
    _Handler.payload = {}
    return run(["pr", "merge", "o/r", "42", "docs: a title (#42)"],
               write_config(tmp_path), url_of(server))


def test_an_outdated_branch_refusal_names_the_branch_not_just_405(server, tmp_path):
    r = merge_refusal(server, tmp_path, 405, OUTDATED_BODY)
    assert r.returncode != 0, "a refused merge must not exit 0"
    assert "merged: http=405" in r.stdout, (
        "the status line is parsed by `forge.sh pr land` and asserted by the seam tests; "
        "the reason is added beside it, never in place of it\n" + r.stdout)
    assert "Branch is outdated" in r.stdout, (
        "405 alone cannot tell a caller to update-and-retry rather than go read a log\n"
        + r.stdout)


def test_a_failed_checks_refusal_names_the_checks(server, tmp_path):
    """The control for the test above, and the reason 'non-empty' is not good enough.

    Same status code, opposite correct response. If both refusals printed the same text the
    fix would have changed nothing that matters.
    """
    r = merge_refusal(server, tmp_path, 405, CHECKS_BODY)
    assert r.returncode != 0
    assert "required status checks" in r.stdout, r.stdout
    assert "outdated" not in r.stdout.lower(), (
        "the two 405s must not print interchangeable text\n" + r.stdout)


def test_a_dependency_blocked_500_also_carries_its_reason(server, tmp_path):
    """Not only 405. A dependency-blocked merge answers 500 with its reason in the body the
    same way, and that is the drain's most common expected refusal."""
    r = merge_refusal(server, tmp_path, 500, json.dumps({"message": "blocked by dependency"}))
    assert r.returncode != 0
    assert "merged: http=500" in r.stdout, r.stdout
    assert "blocked by dependency" in r.stdout, r.stdout


def test_an_unparseable_body_is_printed_raw_rather_than_swallowed(server, tmp_path):
    """FAILS OPEN ON THE REPORTING PATH. The lesson of the stale-client guard: failing closed is safe only where
    the closed state is rare. Text we cannot parse is still the only explanation there is."""
    r = merge_refusal(server, tmp_path, 405, "<html>502 Bad Gateway</html>")
    assert r.returncode != 0
    assert "merged: http=405" in r.stdout, r.stdout
    assert "Bad Gateway" in r.stdout, "an unparseable body must print, not vanish\n" + r.stdout


def test_a_refusal_with_no_body_says_so_rather_than_printing_nothing(server, tmp_path):
    """"The forge explained itself and we dropped it" and "the forge said nothing" are
    different bugs, and silence renders them identically."""
    r = merge_refusal(server, tmp_path, 405, "")
    assert r.returncode != 0
    assert "merged: http=405" in r.stdout, r.stdout
    assert "no body" in r.stdout, r.stdout


def test_a_successful_merge_does_not_dump_the_merge_object(server, tmp_path):
    """The negative control. On 200 the body is the whole merge object, and burying the one
    line callers read would be a regression dressed as a fix -- `forge.sh pr land` parses
    this output."""
    r, sent = merge_subject(server, tmp_path, "docs: a title (#42)")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip() == "merged: http=200", (
        "success must print exactly the line it printed before\n" + repr(r.stdout))


# ------------------------------------------------- which tree did you mean
#
# One defect from two sides: the client could not express WHICH TREE a verb was about. On the
# write side `pr create` takes a branch name, so the forge resolved it to whatever the remote
# pointed at and an unpushed commit was invisible by construction. On the read side a ref
# handed to `pr checks` silently resolved to a moving tip. Both now pin a sha and name it.


@pytest.mark.parametrize(
    "arg, wanted",
    [
        ("main", "is a ref, not a commit sha"),                       # RESOLVES, silently
        ("agent/290-which-tree", "is a ref with a slash"),            # 404 -> traceback
        ("87c8f8f2bd", "short sha"),                                  # resolves, unpinnable
    ],
)
def test_pr_checks_refuses_every_shape_that_is_not_a_full_sha(server, tmp_path, arg, wanted):
    """Each rejected shape fails differently, so each gets its own sentence.

    Measured on the real repo before this guard existed: `pr checks <repo> main` printed
    `OK: 7 registered, 3 skipped, 4 actually ran` byte-identically to passing the resolved
    sha, and named no commit anywhere. That is the one this exists for -- a verdict that
    re-aims itself as the branch moves, with no event marking the substitution.
    """
    _Handler.payload = {"state": "success", "total_count": 1,
                        "statuses": [{"context": "a", "status": "success"}]}
    r = run(["pr", "checks", "o/r", arg], write_config(tmp_path), url_of(server))
    # 2, not merely non-zero: this is a refusal to MEASURE, and 1 is what a
    # measurement that came back red uses. `!= 0` would pass with both at 1, which is the state
    # that made a poller treat a permanent input error as a transient one.
    assert r.returncode == 2, f"{arg!r}: refusals exit 2, got {r.returncode}: {r.stderr}"
    assert wanted in r.stderr, r.stderr
    assert "OK:" not in r.stdout, "a refused argument must not also print a verdict"


def test_a_refusal_and_a_red_gate_use_DIFFERENT_exit_codes(server, tmp_path):
    """THE CONTRACT, and the assertion the parametrized test above cannot make alone.

    "Refusals exit 2" is satisfied by making EVERYTHING exit 2. What a caller needs is that the two
    are distinguishable: a refusal to measure is 2, a gate measured red is 1. Without that a poller
    cannot tell "this input will never work" from "not ready yet" -- measured, it spun every 30s for
    ~7 minutes on a 10-char sha while the PR it watched went green in about two.
    """
    _Handler.payload = {"state": "failure", "total_count": 1,
                        "statuses": [{"context": "a", "status": "failure"}]}
    red = run(["pr", "checks", "o/r", SHA], write_config(tmp_path), url_of(server))
    assert red.returncode == 1, f"a measured red gate should be 1: {red.returncode}\n{red.stderr}"

    refusal = run(["pr", "checks", "o/r", SHA[:10]], write_config(tmp_path), url_of(server))
    assert refusal.returncode == 2, refusal.stderr
    assert refusal.returncode != red.returncode, "refusal and red gate are indistinguishable again"


def test_pr_checks_refuses_before_asking_the_forge(server, tmp_path):
    """The refusal must not depend on what the forge would have said.

    A validator that ran after the call would still be correct on this repo today and would
    silently start passing against a forge that answers differently -- and the whole point is
    that the bad shapes ANSWER rather than fail.
    """
    _Handler.received = None
    _Handler.payload = {"state": "success", "total_count": 1, "statuses": []}
    run(["pr", "checks", "o/r", "main"], write_config(tmp_path), url_of(server))
    assert _Handler.received is None, "a POST reached the server"


def test_pr_checks_names_the_commit_it_measured(server, tmp_path):
    """A verdict pasted into a ticket must carry its own expiry.

    Two tickets asked for this from opposite directions: a report
    written against a head that has since moved reads as current, because nothing in it says
    which tree it was about.
    """
    _Handler.payload = {"state": "success", "total_count": 1,
                        "statuses": [{"context": "a", "status": "success"}]}
    r = run(["pr", "checks", "o/r", SHA], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"measured {SHA}" in r.stdout


def test_an_unknown_commit_is_not_reported_as_the_registration_race(server, tmp_path):
    """`total_count=0` conflated two conditions and the refusal asserted the wrong one.

    The old text -- "no check-runs registered for this commit" -- claims the commit exists, so
    a typo'd sha read as the registration race, whose correct response is to WAIT. The refusal
    was right and its reason was wrong, and the wrong reason is the actionable one.
    """
    _Handler.payload = {"state": "", "total_count": 0, "statuses": []}
    _Handler.missing = ("/git/commits/",)
    r = run(["pr", "checks", "o/r", SHA], write_config(tmp_path), url_of(server))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "is not a commit on" in r.stdout
    assert "NOT the registration race" in r.stdout


def test_a_known_commit_with_no_runs_still_reads_as_the_race(server, tmp_path):
    """The other arm, and the reason the test above is not vacuous.

    Same payload, same exit code, different sentence -- decided only by whether the commit
    exists. Without this pair, a discriminator that always printed one branch would pass.
    """
    _Handler.payload = {"state": "", "total_count": 0, "statuses": []}
    _Handler.missing = ()
    r = run(["pr", "checks", "o/r", SHA], write_config(tmp_path), url_of(server))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "has NO check-runs registered" in r.stdout
    assert "NOT the registration race" not in r.stdout


def test_pr_create_refuses_a_head_that_is_not_the_tree_you_have(server, tmp_path):
    """Measured live: a PR opened against an unpushed head.

    Three check-runs registered against a tree missing roughly a third of the change and
    `mergeable` read True. The runs were accurate -- they measured the pushed tree. Green was
    earned and misleading at once, which is why this refuses rather than warns.
    """
    _Handler.received = None
    _Handler.payload = {"number": 1, "state": "open", "mergeable": True,
                        "commit": {"id": OTHER_SHA}}
    r = run(
        ["pr", "create", "o/r", "head", "main", "t", "b"],
        write_config(tmp_path), url_of(server),
        env_extra={"HUB_API_EXPECT_HEAD": SHA},
    )
    assert r.returncode != 0, r.stdout
    assert "REFUSING" in r.stderr
    assert OTHER_SHA in r.stderr and SHA in r.stderr, "name both trees, or it cannot be acted on"
    assert _Handler.received is None, "the PR was created despite the mismatch"


def test_pr_create_proceeds_and_names_the_head_when_they_agree(server, tmp_path):
    """Control for the refusal above: the same invocation must SUCCEED when the trees match.

    Without this, a `pr create` that refused unconditionally would pass the test above while
    making the verb useless -- and it is the client every session on this box opens PRs with.
    """
    _Handler.received = None
    _Handler.payload = {"number": 7, "state": "open", "mergeable": True,
                        "commit": {"id": SHA}}
    _Handler.contents = {"main": ["test.yml"]}  # the target has CI, or `pr create` refuses first
    r = run(
        ["pr", "create", "o/r", "head", "main", "t", "b"],
        write_config(tmp_path), url_of(server),
        env_extra={"HUB_API_EXPECT_HEAD": SHA},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PR #7" in r.stdout
    assert f"head {SHA}" in r.stdout, "the created PR must name the tree it was opened against"
    assert _Handler.received is not None, "no POST reached the server"


def test_the_expect_variable_supplies_a_value_it_never_skips_the_check(server, tmp_path):
    """An override that could turn the comparison OFF would be a fail-open with the same
    output as a verified pass -- the exact shape of the bug. Setting it to a sha that does not
    match must still refuse."""
    _Handler.payload = {"number": 1, "state": "open", "mergeable": True,
                        "commit": {"id": OTHER_SHA}}
    r = run(
        ["pr", "create", "o/r", "head", "main", "t", "b"],
        write_config(tmp_path), url_of(server),
        env_extra={"HUB_API_EXPECT_HEAD": SHA},
    )
    assert r.returncode != 0 and "REFUSING" in r.stderr


# --------------------------------------------------- pr create needs a target with CI
#
# Operator, 2026-09-14: a PR to a repo with no workflow merges ungated, because the forge registers
# zero checks and nothing says so. `pr create` refuses unless the base OR the head carries one.


def _create_with_ci(server, tmp_path, contents, env=None):
    _Handler.received = None
    _Handler.payload = {"number": 3, "state": "open", "mergeable": True, "commit": {"id": SHA}}
    _Handler.contents = contents
    return run(
        ["pr", "create", "o/r", "head", "main", "t", "b"],
        write_config(tmp_path), url_of(server),
        env_extra={"HUB_API_EXPECT_HEAD": SHA, **(env or {})},
    )


def test_pr_create_refuses_a_target_with_no_ci(server, tmp_path):
    r = _create_with_ci(server, tmp_path, {})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "has no CI" in r.stderr and "(bootstrap-target-repo)" in r.stderr
    assert _Handler.received is None, "the PR was created on a target with no CI"


def test_pr_create_opens_the_pr_that_adds_ci__control(server, tmp_path):
    """Control, and the head arm: `main` has no workflow yet, the branch adds one. Passes on a base
    without the check by design -- it is what keeps the refusal tests from passing on a verb that
    refuses everything."""
    r = _create_with_ci(server, tmp_path, {SHA: ["test.yml"]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PR #3" in r.stdout and _Handler.received is not None


def test_pr_create_no_ci_override_opens_it_and_says_why(server, tmp_path):
    r = _create_with_ci(server, tmp_path, {}, env={"HUB_API_NO_CI_REASON": "probe repo, nothing to test"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NO CI OVERRIDDEN" in r.stderr and "probe repo, nothing to test" in r.stderr
    assert _Handler.received is not None


def test_pr_create_an_unreadable_listing_is_not_ci(server, tmp_path):
    r = _create_with_ci(server, tmp_path, {"main": "BROKEN", SHA: "BROKEN"})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "cannot tell whether o/r has CI" in r.stderr
    assert _Handler.received is None


# ------------------------------------------- pr create drafts what is not at the front
#
# Operator, 2026-09-15: a PR that is not at the front of the queue is opened as a DRAFT (`WIP: `
# title), so Forgejo skips its suite and it stops paying for CI and rebases on a base
# that is about to move. Measured on the forge 2026-09-15: a PR titled `WIP: probe ...`
# reads `draft=true` from the API while non-prefixed PRs read false -- so the prefix IS the draft
# flag here, and drafting really does keep a PR off the contended runner.
#
# THE RULE IS "NOT THE FRONT", NOT "SOMETHING NON-DRAFT IS RUNNING". The queue order derives from
# the open PRs, oldest first (operator, 2026-09-16), so a new PR is at the BACK whenever anything
# else is open -- including when everything open is a draft. Opening it ready in that case would
# let the newest PR take the runner ahead of drafts that have been waiting, which is the churn
# this exists to remove.


def _create_with_queue(server, tmp_path, pulls, title="t", flags=(), env=None):
    _Handler.received = None
    _Handler.payload = {"number": 5, "state": "open", "mergeable": True, "commit": {"id": SHA}}
    _Handler.contents = {"main": ["test.yml"]}   # the target has CI, or this refuses first
    _Handler.pulls = pulls
    return run(
        ["pr", "create", "o/r", "head", "main", title, "b", *flags],
        write_config(tmp_path), url_of(server),
        env_extra={"HUB_API_EXPECT_HEAD": SHA, **(env or {})},
    )


def _watch_lines(registry: Path) -> list[dict]:
    return [json.loads(l) for l in registry.read_text().splitlines() if l.strip()] if registry.exists() else []


def test_pr_create_registers_a_gate_watch_for_a_non_draft_pr_from_a_session(server, tmp_path):
    """gate-watch was built to watch a PR past its session and measured unused, so the verb
    that opens the PR registers it. The pid is this test process -- alive, readable, a real /proc."""
    registry = tmp_path / "gate-watch.jsonl"
    r = _create_with_queue(server, tmp_path, [],
                           env={"FORGE_TOOLS_WAKE_PID": str(os.getpid()), "GATE_WATCH_REGISTRY": str(registry)})
    assert r.returncode == 0, r.stdout + r.stderr
    regs = [w for w in _watch_lines(registry) if w.get("event") == "register"]
    assert [(w["repo"], w["sha"], w["pid"]) for w in regs] == [("o/r", SHA, os.getpid())], registry.read_text() if registry.exists() else "no registry written"
    assert "is watched by gate-watch" in r.stderr, r.stderr


def test_pr_create_subscribes_a_pr_it_opens_as_a_draft(server, tmp_path):
    """The sha watch is for a head whose suite runs; the SUBSCRIPTION is what re-arms on every later
    head, and it is owed to every PR. It was nested inside the non-draft arm, so a PR opened as a
    draft was never subscribed -- and that is most PRs, since anything behind the front opens as one.
    Measured 2026-09-21 on six PRs: every non-draft subscribed at creation, every draft NEVER, and a
    session pushed two heads of an un-drafted PR with nothing watching either."""
    registry = tmp_path / "gate-watch.jsonl"
    r = _create_with_queue(server, tmp_path, [{"number": 4, "draft": False, "title": "ahead"}],
                           env={"FORGE_TOOLS_WAKE_PID": str(os.getpid()), "GATE_WATCH_REGISTRY": str(registry)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "opening as a DRAFT" in r.stderr, "the scenario needs a draft: " + r.stderr
    events = [(w.get("event"), str(w.get("pr") or w.get("number") or "")) for w in _watch_lines(registry)]
    assert ("subscribe", "5") in events, "a draft was opened with no subscription: %s" % events
    assert not [e for e in events if e[0] == "register"], "a draft's head must still not be sha-watched: %s" % events


def test_pr_create_still_subscribes_a_non_draft__control(server, tmp_path):
    registry = tmp_path / "gate-watch.jsonl"
    r = _create_with_queue(server, tmp_path, [],
                           env={"FORGE_TOOLS_WAKE_PID": str(os.getpid()), "GATE_WATCH_REGISTRY": str(registry)})
    assert r.returncode == 0, r.stdout + r.stderr
    events = [(w.get("event"), str(w.get("pr") or w.get("number") or "")) for w in _watch_lines(registry)]
    assert ("subscribe", "5") in events and [e for e in events if e[0] == "register"], events


def test_pr_create_does_not_watch_a_draft(server, tmp_path):
    """PASSES ON BASE: base registers nothing at all; proven instead by removing the draft guard, which reds it.

    A draft's suite is skipped, so a watch on its head would wait on a run that never starts."""
    registry = tmp_path / "gate-watch.jsonl"
    r = _create_with_queue(server, tmp_path, [{"number": 4, "draft": False, "title": "ahead"}],
                           env={"FORGE_TOOLS_WAKE_PID": str(os.getpid()), "GATE_WATCH_REGISTRY": str(registry)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _posted_title() == "WIP: t"
    # NOT `== []`: a draft IS subscribed (the test above). What it must not get is a SHA watch.
    assert not [w for w in _watch_lines(registry) if w.get("event") == "register"], (
        "a draft's suite is skipped; watching its head waits on nothing: %s" % _watch_lines(registry))


def test_pr_create_outside_a_session_says_not_watched_and_still_succeeds(server, tmp_path):
    registry = tmp_path / "gate-watch.jsonl"
    r = _create_with_queue(server, tmp_path, [], env={"GATE_WATCH_REGISTRY": str(registry)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOT watched -- no FORGE_TOOLS_WAKE_PID" in r.stderr and f"pr await o/r {SHA}" in r.stderr, r.stderr
    assert _watch_lines(registry) == []


def test_a_failed_register_is_said_and_the_create_still_succeeds(server, tmp_path):
    """Never fatal, never silent: the PR is already open when the watch is attempted."""
    r = _create_with_queue(server, tmp_path, [], env={"FORGE_TOOLS_WAKE_PID": "999999999",
                                                      "GATE_WATCH_REGISTRY": str(tmp_path / "gw.jsonl")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOT watched -- gate-watch register failed" in r.stderr, r.stderr


def _posted_title() -> str:
    return json.loads(_Handler.received or "{}").get("title", "")


def test_pr_create_drafts_a_pr_that_is_not_at_the_front(server, tmp_path):
    r = _create_with_queue(server, tmp_path, [{"number": 4, "draft": False, "title": "ahead"}])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _posted_title() == "WIP: t", _Handler.received
    assert "DRAFT" in r.stdout or "DRAFT" in r.stderr, "a rewritten title must be said out loud"


def test_pr_create_drafts_behind_other_DRAFTS_too(server, tmp_path):
    """Everything open is a draft, so the runner is free -- and this PR is still not the front.
    Opening it ready would jump it ahead of drafts already waiting to be batched."""
    r = _create_with_queue(server, tmp_path, [{"number": 4, "draft": True, "title": "WIP: ahead"}])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _posted_title() == "WIP: t", _Handler.received


def test_pr_create_opens_ready_when_nothing_else_is_open__control(server, tmp_path):
    """The control that keeps the drafting tests from passing on a verb that drafts everything."""
    r = _create_with_queue(server, tmp_path, [])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _posted_title() == "t", _Handler.received


def test_pr_create_no_draft_opens_ready_behind_an_open_pr(server, tmp_path):
    """BOTH HALVES IN ONE TEST, because the flag's whole meaning is the difference it makes.

    Asserting only that `--no-draft` yields a bare title passes on a client that has never heard
    of the flag -- which is exactly what it did the first time it was run, against the unmodified
    verb. The second half is the same invocation without the flag: if that does not draft, the
    first half is measuring the absence of the feature rather than the flag.
    """
    queue = [{"number": 4, "draft": False, "title": "ahead"}]
    r = _create_with_queue(server, tmp_path, queue, flags=("--no-draft",))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _posted_title() == "t", _Handler.received

    r = _create_with_queue(server, tmp_path, queue)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _posted_title() == "WIP: t", "without the flag this must draft, or the flag proves nothing"


def test_pr_create_draft_forces_the_prefix_on_an_empty_queue(server, tmp_path):
    """The burst case: several PRs opened in quick succession, drafted on purpose so they batch."""
    r = _create_with_queue(server, tmp_path, [], flags=("--draft",))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _posted_title() == "WIP: t", _Handler.received


def test_pr_create_does_not_double_prefix_a_title_already_drafted(server, tmp_path):
    """Paired for the same reason as the test above: `WIP: t` staying `WIP: t` is also what a
    client that prefixes NOTHING produces. The second half proves the prefixing arm was live
    for this queue state, so the first half is about idempotence rather than absence."""
    queue = [{"number": 4, "draft": False, "title": "ahead"}]
    r = _create_with_queue(server, tmp_path, queue, title="WIP: t")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _posted_title() == "WIP: t", _Handler.received

    r = _create_with_queue(server, tmp_path, queue, title="t")
    assert _posted_title() == "WIP: t", "the prefixing arm must be live, or idempotence is vacuous"


def _create_serial(server, tmp_path, repo_labels, pr_labels, flags=("--serial",)):
    _Handler.received = None
    _Handler.received_all = []
    _Handler.payload = {"number": 5, "state": "open", "mergeable": True, "commit": {"id": SHA}}
    _Handler.contents = {"main": ["test.yml"]}
    _Handler.pulls = []
    _Handler.repo_labels = repo_labels
    _Handler.pr_labels = pr_labels
    return run(
        ["pr", "create", "o/r", "head", "main", "t", "b", *flags],
        write_config(tmp_path), url_of(server),
        env_extra={"HUB_API_EXPECT_HEAD": SHA},
    )


def _create_post():
    posts = [b for p, b in _Handler.received_all if p.endswith("/pulls")]
    return json.loads(posts[-1]) if posts else None


SERIAL = [{"id": 9, "name": "queue:serial"}]


def test_pr_create_serial_marks_the_pr_in_the_create_request_itself(server, tmp_path):
    """Operator, 2026-09-16: `--serial` keeps a PR out of every batch. The mark rides the
    CREATE, not a follow-up write -- a label added after would leave a window in which the PR exists
    unmarked and a drain could batch it."""
    r = _create_serial(server, tmp_path, SERIAL, SERIAL)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (_create_post() or {}).get("labels") == [9], _Handler.received_all


def test_pr_create_without_serial_sends_no_labels__control(server, tmp_path):
    """The control: the same repo, the label resolvable, no flag -- the create carries no labels.
    Without it, a client that marked every PR serial would pass the test above."""
    r = _create_serial(server, tmp_path, SERIAL, SERIAL, flags=())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "labels" not in (_create_post() or {}), _Handler.received_all


def test_pr_create_serial_refuses_when_the_label_does_not_exist(server, tmp_path):
    """No label means no mark, and an unmarked PR can be batched -- the one thing the flag exists
    to prevent. So it refuses BEFORE creating anything, and names the label."""
    r = _create_serial(server, tmp_path, [{"id": 1, "name": "other"}], [])
    assert r.returncode == 2, r.stdout + r.stderr
    assert "queue:serial" in r.stderr, r.stderr
    assert _create_post() is None, "a PR was opened without the mark it asked for"


def test_pr_create_serial_fails_loudly_when_the_mark_did_not_stick(server, tmp_path):
    """The create's response is the request echoed back, not the forge's state. Read the labels
    back; a PR that opened unmarked is named, with the command that marks it."""
    r = _create_serial(server, tmp_path, SERIAL, [])
    assert r.returncode != 0, r.stdout + r.stderr
    assert "#5" in r.stderr and "did NOT stick" in r.stderr, r.stderr


def test_pr_create_refuses_an_unreadable_open_pr_listing(server, tmp_path):
    """An unread listing is not an empty queue. Reading it as one opens a non-draft PR that takes
    the runner from whatever is actually ahead of it -- the fail-open this whole file keeps
    filing cases about, so it refuses like the CI arm above."""
    r = _create_with_queue(server, tmp_path, "BROKEN")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "cannot tell" in r.stderr and "open PR" in r.stderr
    assert _Handler.received is None, "a PR was opened on a queue that could not be read"


# ------------------------------------------------- passthrough content type
#
# These assert on what arrived AT THE SERVER, not on an exit code. A passthrough write with a
# form-encoded body still returns 2xx from a forge that simply saw no fields -- that is the whole
# reason the form-encoding bug was silent -- so `returncode == 0` is exactly the evidence that was already
# available while the bug was live. The stand-in records the request, which is stronger than the
# read-back the ticket asked for: read-back infers the transport from the resulting state, and
# this observes the transport.


def _ct(cts):
    """The bare media types that arrived, lowercased, parameters stripped."""
    return [c.split(";")[0].strip().lower() for c in cts]


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT"])
def test_a_passthrough_body_arrives_as_json_not_a_form(server, tmp_path, method):
    """The bug: curl defaults -d to x-www-form-urlencoded, so the forge parses JSON as a form
    and reads every field as ABSENT -- then reports a fault that is true of the request and
    wrong about its cause."""
    r = run(
        ["/api/v1/repos/o/r/issues/1/comments", "-X", method, "-d", '{"body":"hello"}'],
        write_config(tmp_path), url_of(server),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert _Handler.received is not None, f"no {method} reached the server"
    assert _ct(_Handler.received_cts) == ["application/json"]
    assert json.loads(_Handler.received) == {"body": "hello"}


def test_an_explicit_content_type_still_wins(server, tmp_path):
    """The escape hatch. The passthrough is how this client reaches endpoints that want form
    encoding or an upload; defaulting must not become forcing (failing closed is
    safe only when the closed state is rare)."""
    r = run(
        ["/api/v1/repos/o/r/issues/1/comments", "-X", "POST",
         "-H", "Content-Type: application/x-www-form-urlencoded", "-d", "body=hello"],
        write_config(tmp_path), url_of(server),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert _ct(_Handler.received_cts) == ["application/x-www-form-urlencoded"]
    assert _Handler.received == "body=hello"


def test_the_pr_queue_call_site_is_not_broken_or_doubled(server, tmp_path):
    """scripts/pr-queue.sh:478 -- a live in-tree passthrough write already carrying the manual
    workaround this ticket removes. It sits on the hold-labelling path, which fails SOFT: a
    regression there resolves nothing, logs, and exits 0. It cannot announce itself, so it is
    asserted here instead. Exactly ONE header, not the explicit one plus a defaulted duplicate."""
    r = run(
        ["/api/v1/repos/o/r/issues/12/labels", "-X", "POST",
         "-H", "Content-Type: application/json", "--data-binary", '{"labels":[3]}'],
        write_config(tmp_path), url_of(server),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert _ct(_Handler.received_cts) == ["application/json"], "one header, not two"
    assert json.loads(_Handler.received) == {"labels": [3]}


def test_a_read_gets_no_content_type__control(server, tmp_path):
    """The negative control for the whole family: no body, so nothing to default. Without this,
    a helper that attached the header unconditionally would pass every test above."""
    _Handler.payload = {"ok": True}
    r = run(["/api/v1/repos/o/r/issues/1"], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _Handler.received_cts == [], "a GET must not acquire a request content type"


# --------------------------------------------------------------- gate parity
#
# Forgejo reads workflow files from the PR's HEAD, not its base, so a gate that LANDS does not run
# on any already-open PR. The branch gates without it and reports a smaller check count that reads
# as ordinary diff-dependent variation. Measured on a real PR across that boundary -- same PR, same
# content, only the base moved: 7 registered before the rebase, 8 after, `Hold / admission`
# appearing. Nothing in `state=success total_count=7` said the PR was unprotected.

def _green_status():
    return {"total_count": 1, "state": "success",
            "statuses": [{"context": "Test / pytest", "status": "success"}]}


def test_a_workflow_main_has_and_the_head_lacks_is_REPORTED(server, tmp_path):
    """The defect. The check count is identical either way, so this line is the only signal."""
    _Handler.payload = _green_status()
    _Handler.contents = {"main": ["test.yml", "hold.yml"], "a" * 40: ["test.yml"]}
    r = run(["pr", "checks", "o/r", "a" * 40], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GATE PARITY" in r.stdout, (
        f"a workflow present on main and absent from the head was not reported:\n{r.stdout}")
    assert "hold.yml" in r.stdout, "the report must NAME the workflow, not just count it"
    assert "Rebase onto main before trusting this reading" in r.stdout


def test_a_head_carrying_every_workflow_is_SILENT(server, tmp_path):
    """The control, and the reason the arm above is not 'it always warns'.

    Differs from it by one element of one list. Without this a client that printed the warning
    unconditionally would pass the test above and be ignored within a day.
    """
    _Handler.payload = _green_status()
    _Handler.contents = {"main": ["test.yml", "hold.yml"], "b" * 40: ["test.yml", "hold.yml"]}
    r = run(["pr", "checks", "o/r", "b" * 40], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GATE PARITY" not in r.stdout, f"warned about a head that carries everything:\n{r.stdout}"


def test_an_unreadable_listing_says_NOT_CHECKED_rather_than_going_quiet(server, tmp_path):
    """An unreadable listing is not 'no difference'.

    This is the fail-safe-looks-like-success shape the repo keeps re-earning: if the comparison
    cannot run, silence is indistinguishable from parity, and silence is what a reader treats as
    permission. The two must print differently.
    """
    _Handler.payload = _green_status()
    _Handler.contents = {"main": "BROKEN", "c" * 40: ["test.yml"]}
    r = run(["pr", "checks", "o/r", "c" * 40], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOT checked" in r.stdout, (
        f"an unreadable listing went quiet, which reads exactly like parity:\n{r.stdout}")
    assert "GATE PARITY" not in r.stdout, "an unreadable listing must not be reported as a finding"


def test_a_repo_WITHOUT_a_forgejo_workflows_dir_still_gets_a_verdict(server, tmp_path):
    """Measured 2026-09-15 on a target repo that keeps its CI in
    `.github/workflows` only: the forge answers the missing `.forgejo/workflows` with a 404, `api`
    exits 22 on it, and `set -eu` killed the verb after the table printed -- so `pr checks` exited 22
    and `pr await` aborted on every poll, for every target repo shaped like that one."""
    _Handler.payload = _green_status()
    _Handler.contents = {"main": ["test.yml"], "e" * 40: ["test.yml"]}
    _Handler.missing = ("/contents/.forgejo/workflows",)
    r = run(["pr", "checks", "o/r", "e" * 40], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, f"a missing workflow directory killed the verdict (rc {r.returncode}):\n{r.stdout}{r.stderr}"
    assert "OK: 1 registered" in r.stdout


def test_a_gitea_gated_repo_has_its_gate_COMPARED(server, tmp_path):
    """The forge runs the FIRST EXISTING of .forgejo/.gitea/.github. The arm
    compared only .forgejo and .github, so a repo gated by `.gitea/workflows` had no comparison at
    all, and a head missing its gate read exactly like parity."""
    sha = "f" * 40
    _Handler.payload = _green_status()
    _Handler.contents = {"main|.forgejo/workflows": "MISSING", sha + "|.forgejo/workflows": "MISSING",
                         "main|.gitea/workflows": ["test.yml", "hold.yml"], sha + "|.gitea/workflows": ["test.yml"]}
    r = run(["pr", "checks", "o/r", sha], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GATE PARITY" in r.stdout and "hold.yml" in r.stdout, r.stdout
    assert ".gitea/workflows" in r.stdout, "the report must name the directory the forge runs: " + r.stdout


def test_a_gitea_gated_head_carrying_every_workflow_is_SILENT__control(server, tmp_path):
    sha = "9" * 40
    _Handler.payload = _green_status()
    _Handler.contents = {"main|.forgejo/workflows": "MISSING", sha + "|.forgejo/workflows": "MISSING",
                         "main|.gitea/workflows": ["test.yml", "hold.yml"], sha + "|.gitea/workflows": ["test.yml", "hold.yml"]}
    r = run(["pr", "checks", "o/r", sha], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GATE PARITY" not in r.stdout, r.stdout


def test_a_shadowed_directory_is_NOT_compared(server, tmp_path):
    """The other half of the precedence: a `.github/workflows` behind an existing `.forgejo/workflows`
    never runs, so its files are not a gate. Comparing it reported a gate the head could not lack --
    a false finding, which a reader learns to ignore along with the true ones."""
    sha = "8" * 40
    _Handler.payload = _green_status()
    _Handler.contents = {"main|.forgejo/workflows": ["test.yml"], sha + "|.forgejo/workflows": ["test.yml"],
                         "main|.github/workflows": ["legacy.yml"], sha + "|.github/workflows": []}
    r = run(["pr", "checks", "o/r", sha], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GATE PARITY" not in r.stdout, "a shadowed directory was compared: " + r.stdout


def test_pr_create_accepts_a_repo_gated_only_by_gitea(server, tmp_path):
    """The same precedence in `pr create`'s CI check: a `.gitea/workflows` gate is CI."""
    r = _create_with_ci(server, tmp_path, {"main|.forgejo/workflows": "MISSING", SHA + "|.forgejo/workflows": "MISSING",
                                           "main|.gitea/workflows": ["ci.yml"]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _Handler.received is not None, "a .gitea-gated repo was refused as having no CI"


def test_pr_create_an_empty_forgejo_dir_shadows_a_github_gate(server, tmp_path):
    """An existing `.forgejo/workflows` with no yml is what the forge reads, so the `.github` gate behind
    it never runs (measured): the PR would merge ungated, and this refuses it."""
    r = _create_with_ci(server, tmp_path, {"main|.forgejo/workflows": [], SHA + "|.forgejo/workflows": [],
                                           "main|.github/workflows": ["ci.yml"], SHA + "|.github/workflows": ["ci.yml"]})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "has no CI" in r.stderr, r.stderr
    assert _Handler.received is None, "opened a PR whose only gate is shadowed"


def test_the_parity_arm_does_not_change_the_verdict(server, tmp_path):
    """It reports; it never refuses. `block_on_outdated_branch` already forces a rebase before a
    merge can land, so refusing here would duplicate a protection the forge enforces and would
    break every legitimately-behind PR at the moment of merging."""
    _Handler.payload = _green_status()
    _Handler.contents = {"main": ["test.yml", "hold.yml"], "d" * 40: []}
    r = run(["pr", "checks", "o/r", "d" * 40], write_config(tmp_path), url_of(server))
    assert "GATE PARITY" in r.stdout
    assert r.returncode == 0, "a missing gate must not turn a green reading into a refusal"
    assert "OK: 1 registered" in r.stdout, "the original verdict must survive unchanged"


def test_pr_create_refuses_a_bare_flag_where_the_body_goes(tmp_path):
    """`-F` is the `gh` spelling and this client has no such flag, so the flag lands in
    the body slot and the filename in the one after it -- accepted, PR opened, exit 0. Eight of
    the last fifty PRs on this repo carry a body of literally `-F`."""
    r = run(["pr", "create", "o/r", "somebranch", "main", "a title", "-F", "/tmp/body.md"],
            write_config(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "is a flag where the BODY goes" in r.stderr, r.stderr
    assert "@file:" in r.stderr and "--title-only" in r.stderr, r.stderr


def test_prose_beginning_with_a_hyphen_is_a_body_not_a_flag__control(tmp_path):
    """The control that version 1 of this guard failed. A flag has no whitespace; prose usually
    does, so whitespace is tested first. Refusing this would be the collision the `@file:` design rejected
    when it chose `@file:` over a bare `@` -- it gets no further than the branch check, which is
    the point: it passed the body slot."""
    r = run(["pr", "create", "o/r", "somebranch", "main", "a title", "-- and that is the finding"],
            write_config(tmp_path))
    assert "is a flag where the BODY goes" not in (r.stderr or ""), r.stderr


def test_title_only_is_not_refused_as_a_flag__control(tmp_path):
    """The other half: the one flag that IS meaningful in this slot must keep working. Version 1
    of the guard sat in the generic argument loop and broke exactly this."""
    r = run(["pr", "create", "o/r", "somebranch", "main", "a title", "--title-only"],
            write_config(tmp_path))
    assert "is a flag where the BODY goes" not in (r.stderr or ""), r.stderr



# -------------------------------------------------------------- mint and revoke are delegated
# Minting needs root, so it lives in the Forge-Token-Admin repo; its own tests cover the forge CLI, the
# ssh mode and the sweep. What stays here is the WIRING: the client hands it the credential's path.


def _no_real_root(tmp_path):
    """`sudo` and `ssh` that REFUSE and record, first on PATH, for every mint test.

    NOT OPTIONAL, AND MEASURED THE HARD WAY (2026-09-22): these tests are run
    against the PREVIOUS client too -- by the fail-on-base gate step, and by any differential -- and
    that client minted with real `sudo`. On a box whose user has passwordless root it minted a real
    token into a temp file, and its revoke sweep then deleted every other `<prefix>-<host>-` token,
    including the live one every session used. Whatever client runs, root must be unreachable here."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "root-calls.log"
    for name in ("sudo", "ssh"):
        (bindir / name).write_text('#!/bin/sh\necho "%s $*" >> "%s"\nexit 1\n' % (name, log))
        (bindir / name).chmod(0o755)
    return bindir, log


def test_mint_delegates_to_forge_token_admin_with_the_clients_config_path(tmp_path):
    bindir, root_log = _no_real_root(tmp_path)
    log = tmp_path / "calls.log"
    (bindir / "forge-token-admin").write_text('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\n' % log)
    (bindir / "forge-token-admin").chmod(0o755)
    cfg = tmp_path / "cfg" / "hub-api.conf"
    r = run(["mint", "write:issue"], cfg, env_extra={"PATH": f"{bindir}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.read_text().splitlines() == [f"mint --install {cfg} --scopes write:issue"]
    assert not root_log.exists(), "the client itself must never reach for root: " + root_log.read_text()


def test_a_missing_forge_token_admin_is_named__control(tmp_path):
    """No forge-token-admin on PATH: the refusal must name the repo, and nothing may reach for root."""
    bindir, root_log = _no_real_root(tmp_path)
    cfg = tmp_path / "cfg" / "hub-api.conf"
    r = run(["mint"], cfg, env_extra={"PATH": f"{bindir}:/usr/bin:/bin"})
    assert r.returncode != 0 and "Forge-Token-Admin" in r.stderr, r.stderr
    assert not root_log.exists(), "the client itself must never reach for root: " + root_log.read_text()

def test_pr_await_does_not_call_a_skipped_must_run_suite_GREEN(server, tmp_path):
    """A draft skips `Test / pytest` by design, and the commit-status API still says
    `state='success'`. gate-watch measures through this verb, so a watch on a draft head -- which
    every queue un-draft now registers -- delivered "GREEN" and closed on a suite that never
    ran. Measured 2026-09-16 on a real PR's first un-draft. `pr-queue.sh` already refuses this shape in
    `wait_for_green`; the verb that other tools wait on must refuse it too."""
    _Handler.payload = {"state": "success", "total_count": 2,
                        "statuses": [{"context": "Test / pytest (scripts/) (pull_request)", "status": "skipped"},
                                     {"context": "Lint / eslint (first-party JS/TS) (pull_request)", "status": "success"}]}
    r = run(["pr", "await", "o/r", SHA, "2", "0"], write_config(tmp_path), url_of(server))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "GREEN" not in r.stdout + r.stderr, "a skipped suite was reported as a verdict:\n" + r.stdout + r.stderr
    assert "Test / pytest" in r.stderr and "SKIPPED" in r.stderr and "not a verdict" in r.stderr, r.stderr
    assert "gave up after 2 poll(s)" in r.stderr, r.stderr


def test_pr_await_calls_a_RUN_must_run_suite_GREEN__control(server, tmp_path):
    """PASSES ON BASE: the same shape with the suite reported `success` is still a verdict, so the
    skip clause above cannot have turned every green into a wait."""
    _Handler.payload = {"state": "success", "total_count": 2,
                        "statuses": [{"context": "Test / pytest (scripts/) (pull_request)", "status": "success"},
                                     {"context": "Lint / eslint (first-party JS/TS) (pull_request)", "status": "success"}]}
    r = run(["pr", "await", "o/r", SHA, "2", "0"], write_config(tmp_path), url_of(server))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pr await: GREEN after 1 poll(s)" in r.stdout, r.stdout + r.stderr


# --- a RED verdict must be readable without a local suite ------------------------------

def _shipped_marks():
    """The filter's marks, READ OUT OF THE SHIPPED SCRIPT.

    The first version of this test re-declared the tuple inline and asserted on its own local list.
    It therefore measured nothing about hub-api.sh and passed on the merge base -- which the
    differential gate caught and refused, correctly. Parsing the shipped literal is what
    makes the assertion about the code rather than about a copy of it.
    """
    import ast
    import re
    text = (pathlib.Path(__file__).resolve().parents[1] / "hub-api.sh").read_text()
    m = re.search(r"^\s*marks = \((.*?)\)\s*$", text, re.M | re.S)
    assert m, "no `marks = (...)` tuple in hub-api.sh -- the filter was renamed and this test " \
              "would silently measure nothing"
    return ast.literal_eval("(" + m.group(1) + ")")


def _shipped_kept(log):
    """The shipped filter, applied: `marks` AND the `ids` pattern, both read out of
    hub-api.sh. A copy of either here would be a test of the copy."""
    import re
    text = (pathlib.Path(__file__).resolve().parents[1] / "hub-api.sh").read_text()
    m = re.search(r'^\s*ids = re\.compile\(r"(.*?)"\)\s*$', text, re.M)
    ids = re.compile(m.group(1)) if m else None
    marks = _shipped_marks()
    return [l for l in log if any(k in (ids.sub("", l) if ids else l) for k in marks)]


def test_why_red_does_not_list_a_PASSING_test_because_its_name_says_assert_or_FAILED():
    """Every red read on 2026-09-22 printed the same ten innocent tests -- their NAMES
    contain `assert` or `FAILED` -- as if they had failed, beside the real failure."""
    log = ["2026-09-23T00:14:00.1Z scripts/tests/test_attach_local_reaps_browser.py::test_the_assertion_can_fail__trap_removed",
           "2026-09-23T00:14:00.1Z 3.15s call     scripts/tests/test_fault_injection_anchors_are_asserted.py::test_every_fault_injection_asserts_its_anchor",
           "2026-09-23T00:14:00.1Z PASSED scripts/tests/test_stop_reminder.py::test_a_FAILED_emission_still_reaches_the_session",
           "2026-09-23T00:14:00.1Z FAILED scripts/tests/test_x.py::test_y - AssertionError: boom",
           "2026-09-23T00:14:00.1Z E       AssertionError: assert 8.0 == 88"]
    kept = _shipped_kept(log)
    assert not any("trap_removed" in l or "anchors_are_asserted" in l or "a_FAILED_emission" in l for l in kept), (
        "a passing test was listed as a failure because of its name:\n" + "\n".join(kept))
    assert any("FAILED scripts/tests/test_x.py::test_y" in l for l in kept), "the real FAILED line was dropped"
    assert any("AssertionError: assert 8.0" in l for l in kept), "the traceback line was dropped"


def test_why_red_surfaces_the_VERDICT_line_not_only_tracebacks():
    """THE ASSERTION THAT MATTERS, and the one a `FAILED` grep fails.

    A real job failed on the fail-on-base differential, whose verdict carries neither the word FAILED
    nor a traceback. A reader that greps only for failures reports "no failure marker" on the exact
    case this verb exists for, so the differential verdict must survive the shipped filter.
    """
    marks = _shipped_marks()
    log = ["2026-09-21T18:35:53.5Z   PASSED  x  [NOT PROVEN -- passed on the base]",
           "2026-09-21T18:35:53.5Z verdict: 1 of 2 new test(s) PASSED on the merge base and claim "
           "no exemption.",
           "2026-09-21T18:35:53.5Z quiet unrelated line"]
    kept = _shipped_kept(log)
    assert any("verdict:" in l for l in kept), "the differential verdict was not kept by %r" % (marks,)
    assert any("NOT PROVEN" in l for l in kept), "the per-test NOT PROVEN marker was not kept"
    assert not any("quiet unrelated" in l for l in kept), "the filter kept an unrelated line"


def test_a_FAILED_only_filter_would_have_missed_1281__control(tmp_path):
    """PASSES ON BASE: this asserts a property of the OLD filter, to show the new marks earn their
    place. It is the negative half of the test above and cannot fail on an unfixed tree."""
    log = ["verdict: 1 of 2 new test(s) PASSED on the merge base and claim no exemption."]
    assert not [l for l in log if "FAILED" in l], (
        "if this ever matches, the differential verdict started saying FAILED and the narrow "
        "filter would have been enough")


def test_why_red_is_in_the_usage_and_the_unknown_subcommand_list():
    text = (pathlib.Path(__file__).resolve().parents[1] / "hub-api.sh").read_text()
    assert "pr create|body|checks|why-red|await|merge|log|hold|unhold" in text, "usage does not list it"
    assert "(create|body|checks|why-red|await|merge|log|hold|unhold)" in text, (
        "the unknown-subcommand error does not list it, so a session probing verbs is told it "
        "does not exist -- the unlisted-verb trap, in the other direction")


# ---------------------------------------------------------------------------------------------
# `pr await` REFUSES A WAIT THE WATCHER IS ALREADY PERFORMING.
#
# The verb is not removable: `gate-watch.py` measures every reading through
# `pr await <repo> <sha> 1 0`, so it owns the one definition of the four-way exit contract. What
# was wrong is that a BLOCKING call still worked from inside a live session whose PR was already
# watched, and two instruction files recommended exactly that. Measured 2026-09-21: six polls on
# 63d2b9ce0c while gate-watch delivered the same verdict to the same session mid-wait.
AW_SHA = "63d2b9ce0c1c42dbc4cb4bd567115f58d79df981"


def _watched(tmp_path, sha=AW_SHA, repo="o/r", pid="4242"):
    """A registry holding one open watch. Never the real registry."""
    reg = tmp_path / "gate-watch.jsonl"
    reg.write_text(json.dumps({"event": "register", "repo": repo, "sha": sha, "pid": int(pid),
                               "proc_start": "777", "note": "PR #13", "at": "2026-09-21T23:00:00Z",
                               "cwd": "/tmp"}, sort_keys=True) + "\n")
    return {"GATE_WATCH_REGISTRY": str(reg), "FORGE_TOOLS_WAKE_PID": pid}


def test_a_blocking_await_on_an_ALREADY_WATCHED_sha_refuses_instead_of_polling(tmp_path):
    r = run(["pr", "await", "o/r", AW_SHA, "20", "30"], write_config(tmp_path),
            env_extra=_watched(tmp_path))
    assert r.returncode == 4, (r.returncode, r.stderr)
    assert "REFUSING to block" in r.stderr and "GO IDLE" in r.stderr, r.stderr
    # it must NAME the watch, or the caller cannot tell which wait it is being sent to
    assert "PR #13" in r.stderr, r.stderr


def test_rc_4_is_its_own_code_and_not_the_abort_code(tmp_path):
    """3 means "I could not classify the gate"; 4 means "the verdict is already coming to you".
    Opposite fixes -- fixing the instrument versus ending the turn -- so they cannot share a
    number. This is the rc-6-means-RED lesson from the queue, one verb over."""
    r = run(["pr", "await", "o/r", AW_SHA, "20", "30"], write_config(tmp_path),
            env_extra=_watched(tmp_path))
    assert r.returncode != 3, r.stderr


def test_ONE_poll_is_never_refused_because_gate_watch_measures_through_it__control(tmp_path):
    """The discriminator, not an exemption: `gate-watch.py` runs `pr await <repo> <sha> 1 0`, so a
    refusal here would break the watcher the refusal points at -- and would recurse, since the
    guard shells out to gate-watch. Keyed on max, which makes that impossible rather than
    unlikely. Any rc but 4 passes: this asserts it was not REFUSED, not that a gate answered."""
    r = run(["pr", "await", "o/r", AW_SHA, "1", "0"], write_config(tmp_path),
            env_extra=_watched(tmp_path))
    assert r.returncode != 4, (r.returncode, r.stderr)
    assert "REFUSING to block" not in r.stderr, r.stderr


def test_with_NO_session_to_wake_a_blocking_await_still_runs__control(tmp_path):
    """cron has no FORGE_TOOLS_WAKE_PID and a batch integration head is nobody's PR, so for them blocking is
    the only instrument there is. Refusing everywhere would remove the verb, not fix it."""
    env = _watched(tmp_path)
    env.pop("FORGE_TOOLS_WAKE_PID")
    r = run(["pr", "await", "o/r", AW_SHA, "1", "0"], write_config(tmp_path), env_extra=env)
    assert r.returncode != 4, (r.returncode, r.stderr)


def test_an_UNWATCHED_sha_is_not_refused_even_inside_a_session__control(tmp_path):
    """The refusal is for a REDUNDANT wait. With no watch on this sha nothing will wake the
    session, so blocking is correct and must survive."""
    other = "0000000000000000000000000000000000000000"
    r = run(["pr", "await", "o/r", other, "1", "0"], write_config(tmp_path),
            env_extra=_watched(tmp_path))
    assert r.returncode != 4, (r.returncode, r.stderr)


def test_the_override_exists_for_a_deliberate_block__control(tmp_path):
    """PASSES ON BASE by construction, like the three controls above: it asserts the guard does
    NOT fire. On the base there is no guard, so nothing could fire -- which is exactly why the
    name carries the suffix and the claim is 'was not refused', never 'the fix works'."""
    r = run(["pr", "await", "o/r", AW_SHA, "1", "0"], write_config(tmp_path),
            env_extra={**_watched(tmp_path), "HUB_API_AWAIT_BLOCKING": "1"})
    assert r.returncode != 4, (r.returncode, r.stderr)


# ---------------------------------------------------------------------------------------------
# A LONE TOKEN NAMING AN EXISTING FILE IS NOT A BODY.
#
# A PR opened with a body that was the 117-character path to the file it meant. The entry point
# was the ARITY refusal, not the `gh` habit: <base> was omitted, the client printed a usage line,
# and supplying the missing argument slid the path one slot along into the body.
def test_a_body_that_NAMES_AN_EXISTING_FILE_is_refused(tmp_path):
    f = tmp_path / "pr-body.md"
    f.write_text("the real body, 2422 characters in the measured case\n")
    r = run(["pr", "create", "o/r", "b", "main", "t", str(f)], write_config(tmp_path))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "names a readable file" in r.stderr, r.stderr
    assert "@file:" in r.stderr, "the refusal must name the way out"


def test_the_refusal_names_the_POSITIONAL_ORDER_because_arity_is_the_entry_point(tmp_path):
    f = tmp_path / "pr-body.md"
    f.write_text("x\n")
    r = run(["pr", "create", "o/r", "b", "main", "t", str(f)], write_config(tmp_path))
    assert "<owner/repo> <head> <base> <title>" in r.stderr, r.stderr


def test_a_missing_ARGUMENT_says_which_one_and_prints_the_order(tmp_path):
    """The measured entry point. This used to say 'base branch' and nothing else, so a caller
    that mis-counted got no help counting."""
    r = run(["pr", "create", "o/r", "b"], write_config(tmp_path))
    assert r.returncode != 0
    assert "missing <base>" in r.stderr and "argument 3 of 5" in r.stderr, r.stderr
    assert "<owner/repo> <head> <base> <title>" in r.stderr, r.stderr


def test_a_PATH_SHAPED_body_that_does_not_exist_is_still_ACCEPTED__control(server, tmp_path):
    """THE CONTROL THE TICKET ASKS FOR. Existence is the discriminator, not shape: a guard keyed
    on shape alone would refuse ordinary prose that mentions a path, and a test that only proved
    the refusal fires would pass against one that refuses far too much."""
    body = "/tmp/no/such/file/anywhere-" + "z" * 12 + ".md"
    r = run(["pr", "create", "o/r", "b", "main", "t", body], write_config(tmp_path), url_of(server),
            env_extra={"HUB_API_EXPECT_HEAD": SHA})
    assert "names a readable file" not in r.stderr, r.stderr


def test_ordinary_prose_that_MENTIONS_a_real_path_is_still_accepted__control(server, tmp_path):
    """The whitespace arm already takes prose as-is, and this pins that the new guard did not
    reach past it: the body names a file that really exists, inside a sentence."""
    real = tmp_path / "pr-queue.sh"
    real.write_text("#!/bin/sh\n")
    r = run(["pr", "create", "o/r", "b", "main", "t", "see %s for the retry" % real],
            write_config(tmp_path), url_of(server), env_extra={"HUB_API_EXPECT_HEAD": SHA})
    assert "names a readable file" not in r.stderr, r.stderr
