#!/usr/bin/env python3
"""A forge-gate watcher that OUTLIVES the session that asked.

    gate-watch register <owner/repo> <full-40-sha> [note ...]   from inside a session
    gate-watch adopt <predecessor-pid|predecessor-session-id>    from a successor: take over its PR subscriptions
    gate-watch tick                                              from the systemd timer (fallback)
    gate-watch serve                                             the webhook receiver, a systemd service
    gate-watch hook-install <owner/repo>                         mint the secret, register the forge webhook, prove delivery
    gate-watch list                                              open watches, undelivered verdicts

WHY. Observing the forge used to need a live turn: a session pinned itself to a sleep-and-poll loop
for 8 minutes per gate and burned its 5h window doing nothing (measured: 37 minutes
for two PRs). A harness background task fixed the in-session half -- the session goes idle and is
woken when `pr await` exits -- but a background task DIES WITH ITS SESSION, so a PR whose session
ended has no observer (measured: three green-or-pending PRs, nobody watching). This is the
other half: the watcher is a systemd timer, so it belongs to the box, not to a session.

THE SHAPE, and the four decisions the ticket left open, as taken here:

1. The registry is `$XDG_STATE_HOME/forge-tools/gate-watch.jsonl` (`~/.local/state` when XDG_STATE_HOME
   is unset), append-only, one event per line -- the shape of
   `succession.jsonl`, the existing precedent for state that must outlive a party. A watch is OPEN
   when its `register` line has no later closing line for the same (repo, sha, pid).
2. A requester that is gone gets no message and the verdict is NOT dropped: it is written to the
   registry as `undelivered`, with the verdict text, and `list` prints it until someone acts.
   Liveness is /proc comm AND process start time (stat field 22), the same test `_wake` makes, so a
   recycled pid cannot receive a message meant for the session it replaced.
3. REPORT-ONLY. On a verdict the watcher peer-messages the requester and stops. It never merges,
   never approves, never re-runs anything: landing is `pr-queue drain`'s job, and the session
   that asked re-measures before it acts (the message says so). A watcher that acts would inherit
   the unresolved attribution ceilings of acting on someone else's behalf; one that only wakes a session inherits none.
4. One `pr checks` per open watch per tick, through `pr await <repo> <sha> 1 0` so the exit-code
   contract that verb already enforces (0 green, 1 red, 2 no verdict, 3 abort) is reused rather
   than re-derived. A watch older than GIVE_UP_SECS closes as `gave-up`, delivered to the requester
   AS NOT A VERDICT, in the words `pr await` uses -- running out of patience must never read as
   green.

DELIVERY IS BY PEER MESSAGE, never typed input: `session-notify` (Session-Notify) arrives wrapped and attributed and
cannot forge the operator's column-0 line. From a timer there is no FORGE_TOOLS_WAKE_PID, so
the frame names this process's own pid and says it is not a session -- measured deliverable.
Written is not delivered: a bypass-mode receiver without `crossSessionInbound: accept`
holds the frame for its operator, and nothing on this side can see that.

PUSH, NOT POLL, IS THE FAST PATH. Forgejo 16 emits `action_run_success` / `action_run_failure` /
`action_run_recover` webhook events per workflow run, so `serve` listens on loopback for them and runs
`tick` on each authenticated delivery: a verdict reaches the requester seconds after the last run
ends instead of up to a timer interval later. The receiver verifies the HMAC the forge signs every
delivery with (hex SHA-256 over the raw body, keyed by the hook secret) and refuses anything else
with 401, so the only thing an unsigned caller can do is be logged. `hook-install` is idempotent:
one secret file, one hook per receiver URL (found by URL, updated in place), then a forge-side test
delivery that must reach the receiver before it reports success -- which is also the measurement
that this forge allows webhooks to loopback at all. The timer stays as the fallback for a delivery
the forge dropped; nothing here depends on the hook firing.

WHY NOT a workflow engine such as n8n: bringing one up to receive one webhook and run one command would replace ~40
lines of stdlib Python with a Node service, its database, a UI, a credential store and a workflow
JSON -- less code in this file, far more to keep alive on a CPU-shared box.

ponytail: an `undelivered` verdict is only surfaced by `list`. The upgrade, if a dropped PR ever
recurs, is a SessionStart hook that prints them -- not built until it is needed.
"""
import calendar
import hashlib
import hmac
import http.server
import json
import shutil
import os
import re
import secrets
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))   # real dir, through a symlink
import ft_config  # noqa: E402  -- site configuration: loads the config file's FORGE_TOOLS_<KEY>s FIRST
from agent_comms import AGENT_COMMS  # noqa: E402  -- which comms are agent sessions (FORGE_TOOLS_AGENT_COMMS)

HERE = os.path.dirname(os.path.realpath(__file__))
# State lives in a provisioned, harness-neutral dir; `append` and `serve` create it on first write.
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "forge-tools")
REGISTRY = os.environ.get("GATE_WATCH_REGISTRY") or os.path.join(STATE_DIR, "gate-watch.jsonl")
HUB_API = os.environ.get("GATE_WATCH_HUB_API") or os.path.join(HERE, "hub-api.sh")
# Session-Notify's sender, BY COMMAND NAME; the override is for tests.
PEER_SEND = os.environ.get("GATE_WATCH_PEER_SEND") or "session-notify"
PROC = os.environ.get("GATE_WATCH_PROC") or "/proc"
GIVE_UP_SECS = int(os.environ.get("GATE_WATCH_GIVE_UP_SECS") or 7200)
PORT = int(os.environ.get("GATE_WATCH_PORT") or 8958)
SECRET_FILE = os.environ.get("GATE_WATCH_SECRET_FILE") or os.path.join(ft_config.get("CREDENTIALS_DIR"), "gate-watch.secret")
# Written by `serve` on every authenticated delivery; `hook-install` waits for it to move, which is
# how "the forge can reach loopback" becomes a measured fact rather than an assumption.
DELIVERY_MARK = os.environ.get("GATE_WATCH_DELIVERY_MARK") or os.path.join(STATE_DIR, "gate-watch-last-delivery.json")
# `push` is included because the forge's TEST delivery is a push event and PrepareWebhook drops any event
# the hook is not subscribed to -- measured 2026-09-13: with the three action_run_* events alone, hook
# 2's test delivery produced no connection at all. A push tick is harmless (no open watch, no call).
HOOK_EVENTS = ["push", "action_run_success", "action_run_failure", "action_run_recover"]
CLOSING = ("verdict", "gave-up", "undelivered", "retired")
FROM_NAME = "gate-watch.py (forge gate watcher)"
PREFIX = "[machine-nudge: gate-watch.py]"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def die(msg, rc=2):
    print("gate-watch: %s" % msg, file=sys.stderr)
    sys.exit(rc)


def proc_start(pid):
    """/proc/<pid>/stat field 22, or None. The comm may contain ')' so split after the LAST one."""
    try:
        stat = open("%s/%s/stat" % (PROC, pid)).read()
        return stat[stat.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _session():
    """The harness's own session id, when this environment carries one. The pid and
    proc_start above are what a wake is DELIVERED to and what liveness is judged on; the session id
    is what OWNERSHIP is keyed on, because it survives pid reuse, a reboot and a `--resume`, and it
    is what a handoff page's lineage block names. Absent (a cron, a bare shell): nothing recorded."""
    sid = os.environ.get("FORGE_TOOLS_SESSION_ID")
    return {"session": sid} if sid else {}


def comm(pid):
    try:
        return open("%s/%s/comm" % (PROC, pid)).read().strip()
    except OSError:
        return None


def events():
    try:
        with open(REGISTRY) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def append(ev):
    ev = dict(ev, at=now())
    os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    with open(REGISTRY, "a") as f:
        f.write(json.dumps(ev, sort_keys=True) + "\n")
    return ev


def key(ev):
    return (ev.get("repo"), ev.get("sha"), str(ev.get("pid")))


def open_watches():
    """Registrations with no later closing event. Later lines win, so a re-register after a close
    reopens the watch -- which is what a re-push of the same sha would want."""
    state = {}
    for ev in events():
        if ev.get("event") == "register":
            state[key(ev)] = ev
        elif ev.get("event") in CLOSING:
            state.pop(key(ev), None)
    return list(state.values())


def undelivered():
    """Closing events nobody received, still standing: an `undelivered` line with no later register
    for the same key (a re-register means somebody came back for it)."""
    state = {}
    for ev in events():
        if ev.get("event") == "undelivered":
            state[key(ev)] = ev
        elif ev.get("event") == "register":
            state.pop(key(ev), None)
    return list(state.values())


def register(argv):
    if len(argv) < 2:
        die("usage: register <owner/repo> <full-40-sha> [note ...]")
    repo, sha, note = argv[0], argv[1], " ".join(argv[2:])
    if "/" not in repo:
        die("%r is not owner/repo" % repo)
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        die("REFUSING: %r is not a full 40-char sha -- a short one never matches a check-run" % sha)
    pid = os.environ.get("FORGE_TOOLS_WAKE_PID")
    if not pid:
        die("no FORGE_TOOLS_WAKE_PID: run this from inside the session that wants waking, not from a bare shell")
    start = proc_start(pid)
    if start is None:
        die("cannot read %s/%s/stat -- nothing here could be woken later" % (PROC, pid))
    for w in open_watches():
        if key(w) == (repo, sha, str(pid)):
            print("gate-watch: already watching %s %s for pid %s (since %s)" % (repo, sha[:10], pid, w["at"]))
            return 0
    append({"event": "register", "repo": repo, "sha": sha, "pid": int(pid), "proc_start": start,
            "note": note, "cwd": os.getcwd(), **_session()})
    print("gate-watch: registered %s %s for pid %s. Go idle; the timer polls every minute and\n"
          "  peer-messages this session on a verdict, or after %ds as NOT a verdict.\n"
          "  Registry: %s" % (repo, sha[:10], pid, GIVE_UP_SECS, REGISTRY))
    return 0


def subscribe(argv):
    """Subscribe the calling session to a PR's gate verdicts, for as long as that PR is open.

    WHY A SUBSCRIPTION AND NOT A WATCH. A watch is sha-scoped and one-shot -- decision 3, report
    only, and still correct. What it cannot express is "tell me whenever MY pr runs the gate",
    because the things that START a gate run are many and are not the author:

      * `pr create` arms a watch, but only for a NON-DRAFT head -- a draft has no suite to wait for;
      * the queue arms one when it UN-DRAFTS, for the session THE DRAIN answers to. Any session may
        drain, so that is routinely not the author. Measured 2026-09-19: a PR was authored by one
        session, drained by another, went RED, and only the drainer was told. A human relayed it;
      * a re-push moves the head, and the verdict that closed the old watch leaves the new head
        unwatched.

    Fixing those one trigger site at a time is how the gap keeps reappearing: each site has to
    remember the author, and a site that forgets is silent. So the interest is recorded ONCE, here,
    against the PR NUMBER, and `tick` -- the one thing that already polls -- arms the sha watches.
    Whoever drains, however the head moves, the author is told.

    The subscription itself never delivers and never polls. It is a standing instruction that mints
    ordinary one-shot watches, so every guarantee below it is unchanged."""
    if len(argv) < 2:
        die("usage: subscribe <owner/repo> <pr-number>")
    repo, pr = argv[0], str(argv[1])
    if "/" not in repo:
        die("%r is not owner/repo" % repo)
    pid = os.environ.get("FORGE_TOOLS_WAKE_PID")
    if not pid:
        return 0                       # a cron or bare shell authored it; nobody is owed a message
    start = proc_start(pid)
    if start is None:
        return 0                       # the pid is already gone; recording it would name a corpse
    for s in open_subscriptions():
        if (s.get("repo"), str(s.get("pr")), str(s.get("pid"))) == (repo, pr, str(pid)):
            print("gate-watch: already subscribed to %s #%s for pid %s" % (repo, pr, pid))
            return 0
    append({"event": "subscribe", "repo": repo, "pr": pr, "pid": int(pid), "proc_start": start, **_session()})
    print("gate-watch: subscribed to %s #%s for pid %s -- every head this PR gates, until it closes."
          % (repo, pr, pid))
    return 0


def adopt(argv):
    """Subscribe the calling session to every PR its predecessor was subscribed to.

    A subscription is keyed on a pid, so a succession dropped all of them: measured 2026-09-22,
    a predecessor held three PRs, exited at ~22:29, `arm_subscriptions` wrote
    "subscriber gone" for all three, and the first one's GREEN verdict at 22:31 went `undelivered` while
    its successor -- which had inherited those PRs in the handoff -- was subscribed to none.

    READS THE CLOSED ONES TOO. By the time a successor asks, the predecessor may already be gone and
    `tick` may already have unsubscribed it, so the last subscribe/unsubscribe per PR is what counts:
    only an unsubscribe because the PR itself closed is skipped. A PR that closes after this is
    unsubscribed by the next tick, as for any subscriber.

    ONE PROCESS, NOT ONE PID NUMBER: only events carrying the proc_start of that pid's newest
    subscribe are read, so an older, unrelated session that once had the same pid adds nothing.

    BY SESSION ID, PREFERABLY. A pid is meaningful only on this box and only until it is
    reused; the predecessor's SESSION id is what the handoff page's lineage block names, and events
    carry it. Given one (anything not all digits), every subscribe/unsubscribe it
    recorded counts, whatever pid it ran under -- a `--resume` keeps the id across processes."""
    if len(argv) != 1 or not argv[0]:
        die("usage: adopt <predecessor-pid|predecessor-session-id>")
    pred = argv[0]
    if not os.environ.get("FORGE_TOOLS_WAKE_PID"):
        die("no FORGE_TOOLS_WAKE_PID: run this from inside the successor session")
    last = {}
    if pred.isdigit():
        mine = [ev for ev in events() if str(ev.get("pid")) == pred and ev.get("event") in ("subscribe", "unsubscribe")]
        starts = [ev.get("proc_start") for ev in mine if ev.get("event") == "subscribe"]
        for ev in mine:
            if starts and ev.get("proc_start") == starts[-1]:
                last[(ev.get("repo"), str(ev.get("pr")))] = ev
    else:
        for ev in events():
            if ev.get("session") == pred and ev.get("event") in ("subscribe", "unsubscribe"):
                last[(ev.get("repo"), str(ev.get("pr")))] = ev
    for (repo, pr), ev in sorted(last.items()):
        if ev.get("event") == "unsubscribe" and str(ev.get("why", "")).startswith("PR "):
            continue                                   # the PR closed: nothing left to hear
        subscribe([repo, pr])
    return 0


def open_subscriptions():
    """Subscriptions with no later unsubscribe. Same later-lines-win shape as `open_watches`."""
    state = {}
    for ev in events():
        k = (ev.get("repo"), str(ev.get("pr")), str(ev.get("pid")))
        if ev.get("event") == "subscribe":
            state[k] = ev
        elif ev.get("event") == "unsubscribe":
            state.pop(k, None)
    return list(state.values())


def _pr_state(repo, pr):
    """(state, head_sha, is_draft) for a PR, or (None, None, None) if it cannot be read. A forge that is down or a
    token that 403s must leave the subscription ALONE rather than close it: an unreadable PR is not
    a finished one, and closing here would silence the author permanently on a transient error."""
    p = subprocess.run(["sh", HUB_API, "/api/v1/repos/%s/pulls/%s" % (repo, pr)],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if p.returncode != 0:
        return None, None, None
    try:
        d = json.loads(p.stdout)
        return d.get("state"), (d.get("head") or {}).get("sha"), bool(d.get("draft"))
    except Exception:
        # Not a PR object -- an error body, or HTML from a proxy. Same reasoning as above: unknown
        # is not finished, so the caller leaves the subscription open and tries again next tick.
        return None, None, None


def _delivered_shas(repo, pid):
    """Heads this pid has already had a verdict (or a give-up) for. A watch is one-shot, so without
    this every tick would re-arm the same head forever and re-deliver the same verdict."""
    return {ev.get("sha") for ev in events()
            if ev.get("event") in CLOSING and ev.get("repo") == repo and str(ev.get("pid")) == str(pid)}


def arm_subscriptions():
    """Turn standing PR interests into ordinary sha watches. Called at the top of every tick."""
    subs = open_subscriptions()
    if not subs:
        return
    open_keys = {key(w) for w in open_watches()}
    for s in subs:
        repo, pr, pid = s.get("repo"), str(s.get("pr")), str(s.get("pid"))
        # LIVENESS IS SPLIT ON PURPOSE, AND THE FIRST VERSION GOT IT WRONG TWICE. Arming asks only
        # "is this the same process?" -- the start time answers that, and a recycled pid fails it.
        # Whether the pid is a SESSION at all is `deliver`'s question, and it already asks it.
        #
        # Duplicating the check here was wrong in both directions: it demanded comm == "claude"
        # while `deliver` accepts every AGENT_COMMS entry (then "claude", "omp"), so an omp subscriber would have been dropped
        # as "subscriber gone" by the stricter half of its own mechanism; and it made the arming
        # path untestable, which is how the divergence stayed invisible. Arming permissively costs
        # at most one watch that `deliver` then declines with a stated reason and records
        # undelivered -- the loud failure, not the silent one.
        if proc_start(pid) != s.get("proc_start"):
            append(dict(s, event="unsubscribe", why="subscriber gone (pid recycled or exited)"))
            continue
        state, head, is_draft = _pr_state(repo, pr)
        if state is None:
            continue                                   # unreadable: try again next tick
        if state != "open":
            append(dict(s, event="unsubscribe", why="PR %s" % state))
            continue
        # A DRAFT HAS NO SUITE, so a watch on its head can never win: `pr await` reads the skipped
        # `Test / pytest` as no-verdict, the watch burns the full give-up window and then
        # reports "GAVE UP ... NOT a verdict" -- an alarm about nothing, which is how a watcher
        # teaches its readers to ignore it. Measured 2026-09-19: the first version of this armed
        # a PR while it was drafted and did exactly that, 7200s later.
        #
        # Waiting is not a gap. Spanning the draft -> un-draft transition is the whole point of a
        # subscription: the tick after it is un-drafted arms the head that will actually gate.
        if is_draft:
            continue
        if not head or len(head) != 40:
            continue
        if (repo, head, pid) in open_keys or head in _delivered_shas(repo, pid):
            continue                                   # already watching it, or already told them
        append({"event": "register", "repo": repo, "sha": head, "pid": int(pid),
                "proc_start": s.get("proc_start"), "note": "PR #%s (subscription)" % pr,
                "cwd": s.get("cwd", "")})
        print("gate-watch: armed %s %s for pid %s from the subscription to #%s"
              % (repo, head[:10], pid, pr))


def measure(repo, sha):
    """One poll through `pr await`, whose exit code already separates the four outcomes."""
    p = subprocess.run(["sh", HUB_API, "pr", "await", repo, sha, "1", "0"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout.strip()


def age_secs(ev):
    try:
        return time.time() - calendar.timegm(time.strptime(ev["at"], "%Y-%m-%dT%H:%M:%SZ"))
    except (KeyError, ValueError):  # missing or unparseable timestamp reads as age 0, i.e. fresh
        return 0


def deliver(w, text):
    """Peer-message the requester if it is the process that registered; say why not otherwise.
    Returns (delivered, why, requester_alive) -- the third says whether a failure is a GONE requester
    (the verdict is kept as `undelivered`) or a live one whose delivery tool failed (retried)."""
    pid = str(w["pid"])
    if comm(pid) not in AGENT_COMMS:
        return False, "pid %s is gone (no %s process)" % (pid, "/".join(AGENT_COMMS)), False
    if proc_start(pid) != str(w.get("proc_start")):
        return False, "pid %s was recycled: start time %s is not the registered %s" % (
            pid, proc_start(pid), w.get("proc_start")), False
    try:
        p = subprocess.run([PEER_SEND, "--to", "claude-code:" + pid, "--from-name", FROM_NAME],
                           input=text, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:  # not installed, or not executable: a missing REPO, not a dead session
        return False, "%s cannot run (%s) -- install Session-Notify, which provides it" % (PEER_SEND, e), True
    if p.returncode != 0:
        return False, "session-notify rc=%d: %s" % (p.returncode, p.stdout.strip()[-300:]), True
    return True, "written (not proven delivered: the receiver decides)", True


def close(w, event, verdict, body):
    text = "%s %s\n\n%s\n\nThis watcher only reports. Re-measure before acting:\n  hub-api pr checks %s %s" % (
        PREFIX, verdict, body, w["repo"], w["sha"])
    ok, why, alive = deliver(w, text)
    # A LIVE REQUESTER WHOSE DELIVERY FAILED IS RETRIED, NOT CLOSED. Point 2 above keeps
    # `undelivered` for a requester that is GONE. Writing it for a live one closed the watch on a
    # broken tool: measured 2026-09-22, every verdict since the delivery tool it called was deleted was
    # closed as `undelivered` to sessions that were running. Left open, the next tick measures and
    # delivers again; the give-up age still bounds it.
    if not ok and alive and age_secs(w) <= GIVE_UP_SECS:
        print("gate-watch: %s %s pid %s: delivery FAILED to a live requester, watch kept open for the next tick: %s"
              % (w["repo"], w["sha"][:10], w["pid"], why))
        return
    rec = {"event": event if ok else "undelivered", "repo": w["repo"], "sha": w["sha"], "pid": w["pid"],
           "verdict": verdict, "note": w.get("note", ""), "why": why, "closed_as": event}
    append(rec)
    print("gate-watch: %s %s pid %s -> %s: %s" % (w["repo"], w["sha"][:10], w["pid"], rec["event"], why))


def retire_if_outlived(w):
    """Close, without a message, a watch whose PR no longer needs it. True when it did.

    A watch is armed on a head at one moment and polled for two hours. When the PR it is for has
    since CLOSED, or its head is no longer this sha, no verdict for this head is owed to anyone: a
    batch landing reports through its own integration run, and a subscription arms the new head.
    Left open, such a watch ends in a false GAVE UP. Measured 2026-09-23: five in one hour, on four
    PRs (one twice), every one of them already merged -- two were armed
    five seconds after their batch landed, in the window where the batch un-drafts a member to mark
    it merged. The PR number is read from the note, which both arming paths write as "PR #N ...".
    An unreadable PR is not a finished one: the watch is measured as before."""
    m = re.match(r"PR #(\d+)\b", w.get("note") or "")
    if not m:
        return False
    state, head, _ = _pr_state(w["repo"], m.group(1))
    if state is None:
        return False
    if state != "open":
        why = "PR #%s is %s" % (m.group(1), state)
    elif head and head != w["sha"]:
        why = "PR #%s moved to %s" % (m.group(1), head[:10])
    else:
        return False
    append({"event": "retired", "repo": w["repo"], "sha": w["sha"], "pid": w["pid"],
            "note": w.get("note", ""), "why": why})
    print("gate-watch: %s %s pid %s -> retired: %s" % (w["repo"], w["sha"][:10], w["pid"], why))
    return True


def tick():
    arm_subscriptions()          # BEFORE the early return: a tick with no open watches is exactly
    watches = open_watches()     # when a subscription has one to mint.
    if not watches:
        return 0
    for w in watches:
        if retire_if_outlived(w):
            continue
        rc, out = measure(w["repo"], w["sha"])
        tail = "\n".join(out.splitlines()[-25:])
        if rc == 0:
            close(w, "verdict", "GREEN: %s %s" % (w["repo"], w["sha"][:10]), tail)
        elif rc == 1:
            close(w, "verdict", "RED: %s %s -- a verdict, not a delay" % (w["repo"], w["sha"][:10]), tail)
        elif age_secs(w) > GIVE_UP_SECS:
            close(w, "gave-up",
                  "GAVE UP on %s %s after %ds. NOT a verdict -- the gate never produced one. Do not read this as green."
                  % (w["repo"], w["sha"][:10], GIVE_UP_SECS),
                  "last reading (pr await rc=%d):\n%s" % (rc, tail))
        else:
            print("gate-watch: %s %s pid %s: no verdict yet (rc=%d, age %ds)" % (
                w["repo"], w["sha"][:10], w["pid"], rc, int(age_secs(w))))
    return 0


def seat_of(pid, registry=None):
    """The seat the session with this pid registered, as " (seat CC-1)", or "". The registry
    is `session-notify-list --json` (Session-Notify); absent or unreadable, the pid stands alone --
    a seat is a label for the reader, never a condition."""
    if registry is None:
        cmd = shutil.which("session-notify-list")
        if not cmd:
            return ""
        try:
            registry = json.loads(subprocess.run([cmd, "--json"], capture_output=True, text=True, timeout=15).stdout or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""
    for r in registry:
        m = r.get("meta") or {}
        if str(m.get("pid")) == str(pid) and m.get("seat"):
            return " (seat %s)" % m["seat"]
    return ""


def list_():
    ws = open_watches()
    print("open watches: %d" % len(ws))
    for w in ws:
        print("  %s %s pid %s%s since %s %s" % (w["repo"], w["sha"][:10], w["pid"], seat_of(w["pid"]), w["at"], w.get("note", "")))
    us = undelivered()
    print("UNDELIVERED verdicts: %d" % len(us))
    for u in us:
        print("  %s %s pid %s%s at %s: %s\n    %s" % (u["repo"], u["sha"][:10], u["pid"], seat_of(u["pid"]), u["at"], u["verdict"], u["why"]))
    return 0


def read_secret():
    try:
        return open(SECRET_FILE).read().strip()
    except OSError:
        return ""


def signed_ok(secret, body, headers):
    """Forgejo signs the raw body with HMAC-SHA256 and sends the hex digest; older header names carry
    the same digest, `X-Hub-Signature-256` with a `sha256=` prefix. Any one matching is enough; the
    comparison is constant-time. No secret on disk means NOTHING verifies."""
    if not secret:
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    for h in ("X-Forgejo-Signature", "X-Gitea-Signature", "X-Gogs-Signature", "X-Hub-Signature-256"):
        got = headers.get(h, "")
        if got.startswith("sha256="):
            got = got[7:]
        if got and hmac.compare_digest(got, want):
            return True
    return False


class Receiver(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("gate-watch serve: " + fmt % args, flush=True)

    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply(200, {"ok": True, "open_watches": len(open_watches())})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not signed_ok(read_secret(), body, self.headers):
            self._reply(401, {"error": "bad or missing signature"})
            return
        event = self.headers.get("X-Forgejo-Event") or self.headers.get("X-Gitea-Event") or "?"
        self.log_message("event=%s headers=%s", event, ",".join(sorted(h for h in self.headers if "Signature" in h or "Event" in h)))
        try:
            sha = (json.loads(body).get("run") or {}).get("commit_sha", "")
        except (ValueError, AttributeError):
            sha = ""
        mark = {"at": now(), "event": event, "sha": sha}
        os.makedirs(os.path.dirname(DELIVERY_MARK), exist_ok=True)
        with open(DELIVERY_MARK, "w") as f:
            json.dump(mark, f)
        n = len(open_watches())
        if n:
            # THE TICK IS A FRESH PROCESS OF THE FILE ON DISK. This receiver lives for days
            # in a tree that fast-forwards itself every 20 minutes; ticking in-process ran the code it
            # started with. Measured 2026-09-22: nine days old, calling a delivery tool that had
            # since been deleted, so every verdict failed. The timer's own ticks were current; this one was not.
            subprocess.run([sys.executable, os.path.abspath(__file__), "tick"])
        self._reply(200, dict(mark, ticked=n))
        # And the receiver itself: when its source changed, re-exec in place -- same pid, so systemd
        # keeps supervising it. After the reply, so the forge's delivery is never left hanging.
        if _source_stamp() != SOURCE_STAMP:
            self.log_message("source changed on disk -- re-executing the receiver")
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])


def _source_stamp():
    try:
        st = os.stat(os.path.abspath(__file__))
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


SOURCE_STAMP = _source_stamp()


def serve():
    srv = http.server.HTTPServer(("127.0.0.1", PORT), Receiver)
    print("gate-watch serve: listening on 127.0.0.1:%d, secret %s, %d open watch(es)" % (
        PORT, "present" if read_secret() else "MISSING -- every delivery will be refused", len(open_watches())), flush=True)
    srv.serve_forever()
    return 0


def hub(path, *curl_args):
    p = subprocess.run(["sh", HUB_API, path, *curl_args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        die("hub-api %s failed rc=%d: %s" % (path, p.returncode, p.stdout.strip()[-300:]))
    try:
        return json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        die("hub-api %s answered non-JSON: %s" % (path, p.stdout.strip()[:200]))


def hook_install(argv):
    if len(argv) != 1 or "/" not in argv[0]:
        die("usage: hook-install <owner/repo>")
    repo = argv[0]
    url = "http://127.0.0.1:%d/" % PORT
    secret = read_secret()
    if not secret:
        os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
        # O_TRUNC, not O_EXCL: an EMPTY secret file is the same as no secret (nothing verifies), and
        # refusing to replace it would leave the receiver rejecting every delivery for ever.
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32) + "\n")
        os.chmod(SECRET_FILE, 0o600)
        secret = read_secret()
        print("gate-watch: minted %s (mode 600)" % SECRET_FILE)
    # The receiver must be up before the test delivery, or "nothing arrived" says nothing.
    try:
        import urllib.request
        urllib.request.urlopen(url, timeout=3).read()
    except Exception as e:
        die("no receiver at %s (%s) -- start gate-watch-receiver.service first" % (url, e))
    spec = {"type": "forgejo", "active": True, "events": HOOK_EVENTS,
            "config": {"url": url, "content_type": "json", "secret": secret, "http_method": "post"}}
    existing = [h for h in (hub("/api/v1/repos/%s/hooks" % repo) or []) if (h.get("config") or {}).get("url") == url]
    if existing:
        hid = existing[0]["id"]
        hub("/api/v1/repos/%s/hooks/%d" % (repo, hid), "-X", "PATCH", "-H", "Content-Type: application/json", "-d", json.dumps(spec))
        print("gate-watch: hook %d on %s updated (events %s)" % (hid, repo, ",".join(HOOK_EVENTS)))
    else:
        hid = hub("/api/v1/repos/%s/hooks" % repo, "-X", "POST", "-H", "Content-Type: application/json", "-d", json.dumps(spec))["id"]
        print("gate-watch: hook %d on %s created (events %s)" % (hid, repo, ",".join(HOOK_EVENTS)))
    # PROVE DELIVERY: a forge-side test push must reach the receiver. This is the only measurement
    # of whether the forge permits webhooks to loopback (its ALLOWED_HOST_LIST default is external).
    before = os.path.getmtime(DELIVERY_MARK) if os.path.exists(DELIVERY_MARK) else 0
    hub("/api/v1/repos/%s/hooks/%d/tests" % (repo, hid), "-X", "POST")
    for _ in range(40):
        if os.path.exists(DELIVERY_MARK) and os.path.getmtime(DELIVERY_MARK) > before:
            print("gate-watch: test delivery RECEIVED and verified: %s" % open(DELIVERY_MARK).read().strip())
            return 0
        time.sleep(0.25)
    die("hook %d exists but the forge's test delivery never reached %s in 10s. The forge is refusing loopback "
        "targets or the secret differs: check the hook's delivery log in the web UI, and the forge's "
        "[webhook] ALLOWED_HOST_LIST (must include loopback)." % (hid, url), rc=3)


def main():
    verb = sys.argv[1] if len(sys.argv) > 1 else ""
    if verb == "register":
        return register(sys.argv[2:])
    if verb == "tick":
        return tick()
    if verb == "list":
        return list_()
    if verb == "subscribe":
        return subscribe(sys.argv[2:])
    if verb == "adopt":
        return adopt(sys.argv[2:])
    if verb == "serve":
        return serve()
    if verb == "hook-install":
        return hook_install(sys.argv[2:])
    die("usage: gate-watch register <owner/repo> <full-sha> [note] | subscribe <owner/repo> <pr> | adopt <predecessor-pid|session-id> | tick | serve | hook-install <owner/repo> | list")


if __name__ == "__main__":
    sys.exit(main())
