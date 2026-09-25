"""Tests for scripts/gate-watch.py, the forge-gate watcher that outlives its session.

Every collaborator is a stub the test controls: `hub-api.sh` answers with a scripted exit code
(the four-way contract `pr await` defines), the `session-notify` stub records what it was asked to send, and
/proc is a directory the test writes, so requester liveness and the recycled-pid case are real
inputs rather than mocked decisions. Nothing here touches the forge or a live session.
"""
import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/gate-watch.py"
SHA = "a" * 40
PID = "4242"


def _stub_hub_api(tmp_path, rc_file):
    s = tmp_path / "hub-api.sh"
    s.write_text('#!/bin/sh\necho "stub pr await $*"\necho "measured $5"\nexit $(cat %s)\n' % rc_file)
    s.chmod(0o755)
    return s


def _stub_peer_send(tmp_path, log):
    s = tmp_path / "peer-send.py"
    s.write_text("#!/usr/bin/env python3\nimport sys\nopen(%r, 'a').write('TO=' + sys.argv[sys.argv.index('--to') + 1]"
                 " + '\\n' + sys.stdin.read() + '\\n---\\n')\n" % str(log))
    s.chmod(0o755)  # called by command name, as `session-notify` is
    return s


def _proc(tmp_path, pid=PID, comm="claude", start="777"):
    d = tmp_path / "proc" / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "comm").write_text(comm + "\n")
    # Real layout: "<pid> (<comm>) <state> <field4> ...", so after the ")" the state is index 0 and
    # starttime (field 22) is index 19 -- which is fields[18] once the state is written separately.
    fields = ["0"] * 50
    fields[18] = start
    (d / "stat").write_text("%s (%s) S %s\n" % (pid, comm, " ".join(fields)))
    return tmp_path / "proc"


def _env(tmp_path, rc="2", **extra):
    rc_file = tmp_path / "rc"
    rc_file.write_text(rc)
    sent = tmp_path / "sent.log"
    env = dict(os.environ,
               GATE_WATCH_REGISTRY=str(tmp_path / "reg.jsonl"),
               GATE_WATCH_HUB_API=str(_stub_hub_api(tmp_path, rc_file)),
               GATE_WATCH_PEER_SEND=str(_stub_peer_send(tmp_path, sent)),
               GATE_WATCH_PROC=str(_proc(tmp_path)),
               FORGE_TOOLS_WAKE_PID=PID)
    env.update(extra)
    return env, rc_file, sent


def run(env, *args):
    return subprocess.run(["python3", str(SCRIPT), *args], env=env, capture_output=True, text=True)


def _events(env):
    return [json.loads(l) for l in Path(env["GATE_WATCH_REGISTRY"]).read_text().splitlines()]


def test_register_refuses_a_short_sha_and_a_bare_shell(tmp_path):
    env, _, _ = _env(tmp_path)
    r = run(env, "register", "o/r", SHA[:12])
    assert r.returncode == 2 and "REFUSING" in r.stderr
    env.pop("FORGE_TOOLS_WAKE_PID")
    r = run(env, "register", "o/r", SHA)
    assert r.returncode == 2 and "FORGE_TOOLS_WAKE_PID" in r.stderr
    assert not Path(env["GATE_WATCH_REGISTRY"]).exists()


def test_green_verdict_is_delivered_once_and_closes_the_watch(tmp_path):
    env, rc_file, sent = _env(tmp_path, rc="2")
    assert run(env, "register", "o/r", SHA, "pr 1").returncode == 0
    # No verdict yet: nothing sent, watch stays open.
    assert run(env, "tick").returncode == 0
    assert not sent.exists()
    assert "open watches: 1" in run(env, "list").stdout
    rc_file.write_text("0")
    assert run(env, "tick").returncode == 0
    body = sent.read_text()
    assert "TO=claude-code:%s" % PID in body and "GREEN" in body and SHA[:10] in body
    assert "Re-measure before acting" in body and "pr checks o/r %s" % SHA in body
    assert [e["event"] for e in _events(env)] == ["register", "verdict"]
    # Closed: a further tick sends nothing more.
    run(env, "tick")
    assert body == sent.read_text()
    assert "open watches: 0" in run(env, "list").stdout


def test_red_is_a_verdict_and_says_so(tmp_path):
    env, rc_file, sent = _env(tmp_path, rc="1")
    run(env, "register", "o/r", SHA)
    run(env, "tick")
    assert "RED" in sent.read_text() and "a verdict, not a delay" in sent.read_text()


def test_giving_up_is_delivered_as_not_a_verdict(tmp_path):
    env, _, sent = _env(tmp_path, rc="2", GATE_WATCH_GIVE_UP_SECS="0")
    run(env, "register", "o/r", SHA)
    run(env, "tick")
    body = sent.read_text()
    assert "GAVE UP" in body and "NOT a verdict" in body and "Do not read this as green" in body
    assert _events(env)[-1]["event"] == "gave-up"


def test_gone_or_recycled_requester_gets_nothing_and_the_verdict_is_kept(tmp_path):
    env, _, sent = _env(tmp_path, rc="0")
    run(env, "register", "o/r", SHA)
    # Recycled: same pid, different start time -- the message would reach a stranger.
    _proc(tmp_path, start="999")
    run(env, "tick")
    assert not sent.exists()
    ev = _events(env)[-1]
    assert ev["event"] == "undelivered" and "recycled" in ev["why"] and "GREEN" in ev["verdict"]
    out = run(env, "list").stdout
    assert "UNDELIVERED verdicts: 1" in out and SHA[:10] in out
    # Gone entirely: same outcome, different reason.
    run(env, "register", "o/r", "b" * 40)
    (tmp_path / "proc" / PID / "comm").unlink()
    run(env, "tick")
    assert not sent.exists()
    assert "is gone" in _events(env)[-1]["why"]


def test_live_requester_control_proves_the_liveness_check_can_pass(tmp_path):
    """Negative control for the test above: with the SAME pid and the registered start time the
    message IS sent, so the refusals there are the check firing rather than delivery being broken."""
    env, _, sent = _env(tmp_path, rc="0")
    run(env, "register", "o/r", SHA)
    run(env, "tick")
    assert sent.exists() and _events(env)[-1]["event"] == "verdict"


# --- the push half: `serve` and `hook-install` -----------------------------------------------------
import hashlib
import hmac
import socket
import time
import urllib.error
import urllib.request


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(tmp_path, env, secret):
    (tmp_path / "secret").write_text(secret + "\n")
    port = _free_port()
    env = dict(env, GATE_WATCH_PORT=str(port), GATE_WATCH_SECRET_FILE=str(tmp_path / "secret"),
               GATE_WATCH_DELIVERY_MARK=str(tmp_path / "mark.json"))
    p = subprocess.Popen(["python3", str(SCRIPT), "serve"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=1).read()
            return p, port, env
        except Exception:
            time.sleep(0.05)
    p.kill()
    raise AssertionError("receiver never came up")


def _post(port, body, headers):
    req = urllib.request.Request("http://127.0.0.1:%d/" % port, data=body, headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_serve_refuses_an_unsigned_or_missigned_delivery_and_ticks_nothing(tmp_path):
    env, rc_file, sent = _env(tmp_path, rc="0")
    run(env, "register", "o/r", SHA)
    p, port, env = _serve(tmp_path, env, "s3cret")
    try:
        body = json.dumps({"run": {"commit_sha": SHA}}).encode()
        assert _post(port, body, {"X-Forgejo-Event": "action_run_success"})[0] == 401
        wrong = hmac.new(b"other", body, hashlib.sha256).hexdigest()
        assert _post(port, body, {"X-Forgejo-Event": "action_run_success", "X-Forgejo-Signature": wrong})[0] == 401
        assert not sent.exists() and not (tmp_path / "mark.json").exists()
        assert "open watches: 1" in run(env, "list").stdout
    finally:
        p.kill()


def test_serve_ticks_on_a_signed_delivery_and_marks_it(tmp_path):
    """Positive control for the refusal above, and the `hook-install` proof mechanism: a correctly
    signed delivery moves the mark and the verdict reaches the requester."""
    env, rc_file, sent = _env(tmp_path, rc="0")
    run(env, "register", "o/r", SHA)
    p, port, env = _serve(tmp_path, env, "s3cret")
    try:
        body = json.dumps({"run": {"commit_sha": SHA}}).encode()
        sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        code, ans = _post(port, body, {"X-Forgejo-Event": "action_run_success", "X-Forgejo-Signature": sig})
        assert code == 200 and ans["ticked"] == 1 and ans["sha"] == SHA
        assert "GREEN" in sent.read_text()
        assert json.loads((tmp_path / "mark.json").read_text())["event"] == "action_run_success"
        # The GitHub-style header with its prefix is accepted too.
        code, _ = _post(port, body, {"X-Hub-Signature-256": "sha256=" + sig})
        assert code == 200
    finally:
        p.kill()


def _stub_hub_api_hooks(tmp_path, existing_url=None, deliver_mark=None):
    """A hub-api.sh that answers the hooks API: list (one hook at `existing_url` if given), create
    (id 77), update, and `tests`, which simulates the forge delivering by touching the mark."""
    log = tmp_path / "hub-calls.log"
    lst = json.dumps([{"id": 5, "config": {"url": existing_url}}] if existing_url else [])
    s = tmp_path / "hub-api-hooks.sh"
    s.write_text('#!/bin/sh\necho "$*" >> %s\ncase "$1 $*" in\n'
                 '  */hooks/*/tests*) %s; echo "{}";;\n'
                 '  */hooks/[0-9]*) echo "{}";;\n'
                 '  */hooks*POST*) echo "{\\"id\\": 77}";;\n'
                 '  */hooks*) echo \'%s\';;\n'
                 'esac\n' % (log, ("touch %s" % deliver_mark) if deliver_mark else "true", lst))
    s.chmod(0o755)
    return s, log


def test_hook_install_creates_once_updates_by_url_and_requires_the_test_delivery(tmp_path):
    env, _, _ = _env(tmp_path)
    p, port, env = _serve(tmp_path, env, "")          # no secret yet: install mints one
    try:
        url = "http://127.0.0.1:%d/" % port
        mark = tmp_path / "mark.json"
        stub, log = _stub_hub_api_hooks(tmp_path, deliver_mark=mark)
        env["GATE_WATCH_HUB_API"] = str(stub)
        r = run(env, "hook-install", "o/r")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "minted" in r.stdout and "created" in r.stdout and "RECEIVED" in r.stdout
        assert oct(os.stat(tmp_path / "secret").st_mode & 0o777) == "0o600"
        calls = log.read_text()
        assert "-X POST" in calls and "/hooks/77/tests" in calls
        # Second run: the hook exists at this URL, so it is PATCHed, never duplicated.
        stub2, log2 = _stub_hub_api_hooks(tmp_path, existing_url=url, deliver_mark=mark)
        env["GATE_WATCH_HUB_API"] = str(stub2)
        r = run(env, "hook-install", "o/r")
        assert r.returncode == 0 and "updated" in r.stdout
        assert "-X PATCH" in log2.read_text() and "/hooks/5/tests" in log2.read_text()
        # No delivery arriving is a FAILURE, not a pass: the stub that never touches the mark.
        stub3, _ = _stub_hub_api_hooks(tmp_path, existing_url=url, deliver_mark=None)
        env["GATE_WATCH_HUB_API"] = str(stub3)
        r = run(env, "hook-install", "o/r")
        assert r.returncode == 3 and "never reached" in r.stderr
    finally:
        p.kill()


# --- subscriptions: a PR-scoped standing interest that mints sha watches ------------------------
#
# A subscription exists because the things that START a gate run are many and none of them is the
# author: `pr create` arms only for a non-draft head, the queue arms for whoever DRAINED, and a
# re-push leaves the new head unwatched. `tick` resolves the PR's head instead, so whoever drains
# and however the head moves, the author is told.

import json as _json


def _pulls_stub(tmp_path, state="open", draft=False, head="b" * 40):
    """A hub-api that answers the one call `arm_subscriptions` makes."""
    s = tmp_path / "hub-api-pulls.sh"
    body = _json.dumps({"state": state, "draft": draft, "head": {"sha": head}})
    s.write_text("#!/bin/sh\ncat <<'JSON'\n" + body + "\nJSON\n")
    s.chmod(0o755)
    return s


def _tick(tmp_path, api, env=None):
    e = dict(os.environ, GATE_WATCH_REGISTRY=str(tmp_path / "reg.jsonl"),
             GATE_WATCH_HUB_API=str(api), FORGE_TOOLS_WAKE_PID=str(os.getpid()), **(env or {}))
    subprocess.run(["python3", str(SCRIPT), "subscribe", "o/r", "77"], capture_output=True, env=e)
    return subprocess.run(["python3", str(SCRIPT), "tick"], capture_output=True, text=True, env=e)


def test_a_subscription_arms_a_watch_on_the_prs_current_head(tmp_path):
    """The positive control. Without it, the draft test below cannot tell 'correctly waited' from
    'subscriptions are inert', which look identical from the outside."""
    r = _tick(tmp_path, _pulls_stub(tmp_path, draft=False))
    assert "armed" in r.stdout, f"a subscription to an open, non-draft PR armed nothing:\n{r.stdout}{r.stderr}"
    assert "b" * 10 in r.stdout, "armed some other head than the one the PR reports"


def test_a_subscription_waits_out_a_draft(tmp_path):
    """A draft skips its suite, so a watch on its head can NEVER win: `pr await` reads the skipped
    `Test / pytest` as no-verdict, the watch burns the give-up window and then reports
    'GAVE UP ... NOT a verdict' -- an alarm about nothing. Measured 2026-09-19 on a real PR.

    PASSES ON BASE: vacuously -- which is the honest reason, not a loophole. On the base there
    are no subscriptions at all, so `tick` arms nothing for ANY pr, draft or not, and this assertion
    holds for a reason that has nothing to do with what it tests. It carries no proof on its own.
    The proof is its paired positive control
    `test_a_subscription_arms_a_watch_on_the_prs_current_head`, which FAILS on base; this test says
    the arming that control proves is suppressed while the pr is a draft. Read the two together or
    neither means anything."""
    r = _tick(tmp_path, _pulls_stub(tmp_path, draft=True))
    assert "armed" not in r.stdout, f"armed a watch on a draft, which cannot produce a verdict:\n{r.stdout}"


def test_a_closed_pr_ends_the_subscription(tmp_path):
    r = _tick(tmp_path, _pulls_stub(tmp_path, state="closed"))
    assert "armed" not in r.stdout
    reg = (tmp_path / "reg.jsonl").read_text()
    assert '"unsubscribe"' in reg, "a closed PR left its subscription open forever"


# --- the receiver ran nine-day-old code, and a failed delivery closed the watch ------------------

def test_the_receiver_ticks_the_code_ON_DISK_and_reexecs_when_its_source_changes(tmp_path):
    """Measured 2026-09-22: `serve` had run since 09-13 and ticked in-process, so after a change deleted
    its delivery tool from the self-updating tree every verdict failed. Changing the script under a
    running receiver must change what the NEXT delivery sends, and the receiver must survive its own
    re-exec (same pid, still answering)."""
    script = tmp_path / "gate-watch.py"
    script.write_text(SCRIPT.read_text())
    (tmp_path / "agent_comms.py").write_text((SCRIPT.parent / "agent_comms.py").read_text())
    (tmp_path / "ft_config.py").write_text((SCRIPT.parent / "ft_config.py").read_text())   # its config reader
    env, rc_file, sent = _env(tmp_path, rc="0")
    subprocess.run(["python3", str(script), "register", "o/r", SHA], env=env, capture_output=True, text=True)
    (tmp_path / "secret").write_text("s3cret\n")
    port = _free_port()
    env = dict(env, GATE_WATCH_PORT=str(port), GATE_WATCH_SECRET_FILE=str(tmp_path / "secret"),
               GATE_WATCH_DELIVERY_MARK=str(tmp_path / "mark.json"))
    p = subprocess.Popen(["python3", str(script), "serve"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def _up():
        for _ in range(100):
            try:
                return urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=1).status
            except Exception:
                time.sleep(0.05)
        raise AssertionError("receiver never came up")
    try:
        _up()
        # The tree moves under the running receiver, as the 20-minute fast-forward does.
        anchor = 'PREFIX = "[machine-nudge: gate-watch.py]"'
        src = SCRIPT.read_text()
        assert anchor in src, f'fault-injection anchor {anchor!r} is gone -- this test would inject nothing'
        script.write_text(src.replace(anchor, 'PREFIX = "[machine-nudge: gate-watch.py NEW-CODE]"'))
        body = json.dumps({"run": {"commit_sha": SHA}}).encode()
        sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        code, _ = _post(port, body, {"X-Forgejo-Event": "action_run_success", "X-Forgejo-Signature": sig})
        assert code == 200
        assert "NEW-CODE" in sent.read_text(), "the tick ran the code the receiver started with:\n" + sent.read_text()
        assert _up() == 200 and p.poll() is None, "the receiver did not survive re-executing itself"
    finally:
        p.kill()


def test_a_LIVE_requester_whose_delivery_fails_is_retried_not_closed(tmp_path):
    """Point 2 keeps `undelivered` for a requester that is GONE. A live one whose delivery tool failed
    (rc=2, as a deleted peer-send.py did) keeps its watch open, and the next tick delivers."""
    env, rc_file, sent = _env(tmp_path, rc="0")
    flaky = tmp_path / "flaky-send"
    flaky.write_text("#!/bin/sh\nif [ ! -f %s ]; then : > %s; echo 'transient failure'; exit 2; fi\nexec %s \"$@\"\n"
                     % (tmp_path / "failed-once", tmp_path / "failed-once", env["GATE_WATCH_PEER_SEND"]))
    flaky.chmod(0o755)
    env["GATE_WATCH_PEER_SEND"] = str(flaky)
    run(env, "register", "o/r", SHA)
    run(env, "tick")
    assert not any(e["event"] == "undelivered" for e in _events(env)), _events(env)
    assert "open watches: 1" in run(env, "list").stdout, "the watch was closed on a failed delivery"
    run(env, "tick")
    assert "GREEN" in sent.read_text() and _events(env)[-1]["event"] == "verdict", _events(env)


def test_a_GONE_requester_still_gets_undelivered__control(tmp_path):
    """The control: the retry must not swallow the case point 2 is for. A requester that no longer
    exists is closed as `undelivered` on the first tick, verdict kept."""
    env, rc_file, sent = _env(tmp_path, rc="0")
    run(env, "register", "o/r", SHA)
    _proc(tmp_path, comm="bash")          # the registered pid is now some other process
    run(env, "tick")
    assert _events(env)[-1]["event"] == "undelivered" and not sent.exists(), _events(env)


# --- a succession dropped every PR subscription -------------------------------------------------

PRED = "1412136"


def _pred_registry(tmp_path, lines):
    reg = tmp_path / "reg.jsonl"
    reg.write_text("".join(_json.dumps(l) + "\n" for l in lines))
    return reg


def _sub(pr, pid=PRED, start="555", event="subscribe", why=None, session=None):
    ev = {"event": event, "repo": "o/r", "pr": str(pr), "pid": int(pid), "proc_start": start}
    if why:
        ev["why"] = why
    if session:
        ev["session"] = session
    return ev


def _open_for(env, pid):
    state = {}
    for ev in _events(env):
        k = (ev.get("repo"), str(ev.get("pr")), str(ev.get("pid")))
        if ev.get("event") == "subscribe":
            state[k] = ev
        elif ev.get("event") == "unsubscribe":
            state.pop(k, None)
    return sorted(pr for (_, pr, p) in state if p == pid)


def test_adopt_takes_over_subscriptions_tick_already_closed_but_not_closed_prs(tmp_path):
    """The measured case, 2026-09-22: 1412136 held three PRs, exited, and `tick` wrote
    'subscriber gone' for all three before its successor asked. A fourth closed while it was subscribed,
    so there is nothing left to hear about it."""
    gone = "subscriber gone (pid recycled or exited)"
    _pred_registry(tmp_path, [_sub(1388), _sub(1394), _sub(1411), _sub(1300),
                              _sub(1300, event="unsubscribe", why="PR closed"),
                              _sub(1388, event="unsubscribe", why=gone),
                              _sub(1394, event="unsubscribe", why=gone),
                              _sub(1411, event="unsubscribe", why=gone)])
    env, _, _ = _env(tmp_path)
    r = run(env, "adopt", PRED)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _open_for(env, PID) == ["1388", "1394", "1411"], r.stdout


def test_adopt_ignores_an_older_session_that_had_the_same_pid(tmp_path):
    """A pid number is reused; the process is the identity. Only the newest process that subscribed
    under this pid -- the predecessor -- is adopted from."""
    _pred_registry(tmp_path, [_sub(900, start="111"), _sub(1388, start="555")])
    env, _, _ = _env(tmp_path)
    run(env, "adopt", PRED)
    assert _open_for(env, PID) == ["1388"]


def test_adopt_with_nothing_to_adopt_subscribes_nothing__control(tmp_path):
    _pred_registry(tmp_path, [_sub(1388, pid="999")])
    env, _, _ = _env(tmp_path)
    r = run(env, "adopt", PRED)
    assert r.returncode == 0 and _open_for(env, PID) == [], r.stdout + r.stderr


# --- ownership is keyed on the SESSION, delivery on the process ----------------------------------

SID = "deadbeef-0000-4000-8000-000000000001"


def test_subscribe_and_register_record_the_session_id_when_the_environment_carries_one(tmp_path):
    env, _, _ = _env(tmp_path, FORGE_TOOLS_SESSION_ID=SID)
    assert run(env, "subscribe", "o/r", "1388").returncode == 0
    assert run(env, "register", "o/r", SHA, "PR #88").returncode == 0
    recorded = [ev.get("session") for ev in _events(env) if ev["event"] in ("subscribe", "register")]
    assert recorded == [SID, SID], recorded


def test_no_session_in_the_environment_records_no_session__control(tmp_path):
    env, _, _ = _env(tmp_path)
    env.pop("FORGE_TOOLS_SESSION_ID", None)
    assert run(env, "subscribe", "o/r", "1388").returncode == 0
    assert all("session" not in ev for ev in _events(env)), list(_events(env))


def test_adopt_by_session_id_takes_over_across_pids_and_a_recycled_pid(tmp_path):
    """The predecessor subscribed from two processes (a `--resume` keeps the session id), and an
    unrelated session once ran under one of those pids. The session, not the pid, is the owner."""
    _pred_registry(tmp_path, [_sub(900, pid="777", start="111", session="someone-else"),
                              _sub(1388, pid="777", start="222", session=SID),
                              _sub(1394, pid="1412136", start="555", session=SID),
                              _sub(1300, pid="1412136", start="555", session=SID),
                              _sub(1300, pid="1412136", start="555", session=SID,
                                   event="unsubscribe", why="PR closed")])
    env, _, _ = _env(tmp_path)
    r = run(env, "adopt", SID)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _open_for(env, PID) == ["1388", "1394"], r.stdout


def test_adopt_by_an_unknown_session_id_subscribes_nothing__control(tmp_path):
    _pred_registry(tmp_path, [_sub(1388, session=SID)])
    env, _, _ = _env(tmp_path)
    r = run(env, "adopt", "no-such-session")
    assert r.returncode == 0 and _open_for(env, PID) == [], r.stdout + r.stderr


# --- a watch that outlived its PR ended in a false GAVE UP (2026-09-23) ----------------------------

def _pr_and_await_stub(tmp_path, state="open", head=SHA):
    """Answers the PR read with a PR object, and `pr await` with rc 2 (no verdict yet)."""
    s = tmp_path / "hub-api-pr.sh"
    body = _json.dumps({"state": state, "draft": False, "head": {"sha": head}})
    s.write_text('#!/bin/sh\ncase "$1" in\n  pr) echo "no verdict"; exit 2 ;;\n'
                 "  *) cat <<'JSON'\n" + body + "\nJSON\n ;;\nesac\n")
    s.chmod(0o755)
    return s


def _watched_tick(tmp_path, **pr):
    env, _, sent = _env(tmp_path)
    assert run(env, "register", "o/r", SHA, "PR #77 (subscription)").returncode == 0
    env["GATE_WATCH_HUB_API"] = str(_pr_and_await_stub(tmp_path, **pr))
    r = run(env, "tick")
    events = [e["event"] for e in _events(env)]
    return r, events, (sent.read_text() if sent.exists() else "")


def test_a_watch_whose_PR_merged_is_retired_not_left_to_give_up(tmp_path):
    r, events, sent = _watched_tick(tmp_path, state="closed")
    assert "retired" in events, f"the watch stayed open on a merged PR:\n{r.stdout}{r.stderr}"
    assert sent == "", "retiring must not message anyone: nothing is owed for a closed PR"


def test_a_watch_whose_PR_moved_to_another_head_is_retired(tmp_path):
    r, events, _ = _watched_tick(tmp_path, head="c" * 40)
    assert "retired" in events, r.stdout + r.stderr
    assert "moved to cccccccccc" in r.stdout, r.stdout


def test_a_watch_on_the_PRs_current_open_head_stays_open__control(tmp_path):
    r, events, _ = _watched_tick(tmp_path)
    assert "retired" not in events, r.stdout
    assert "no verdict yet" in r.stdout, r.stdout


def test_seat_of_labels_a_registered_seat_from_the_registry_and_nothing_otherwise(tmp_path, monkeypatch):
    """`list` prints `pid N (seat CC-1)` when Session-Notify's registry names the pid's seat.
    The function is loaded from the script file; the registry is passed in, then read from a stub
    command on PATH, then absent."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("gw", SCRIPT)
    gw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gw)
    reg = [{"handle": "claude-code:4242", "meta": {"pid": "4242", "seat": "CC-1"}}, {"handle": "omp:x", "meta": {"pid": "5"}}]
    assert gw.seat_of(4242, reg) == " (seat CC-1)"
    assert gw.seat_of("5", reg) == "" and gw.seat_of(6, reg) == ""
    b = tmp_path / "bin"; b.mkdir()
    (b / "session-notify-list").write_text("#!/bin/sh\nprintf '%s' '" + json.dumps(reg) + "'\n")
    (b / "session-notify-list").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:/usr/bin:/bin")
    assert gw.seat_of("4242") == " (seat CC-1)"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert gw.seat_of("4242") == ""
