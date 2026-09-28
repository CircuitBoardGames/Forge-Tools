"""handoff.sh publish's holder gate with no caller identity.

A session with no FORGE_TOOLS_SESSION_ID was refused over its OWN page as a "LIVE session which is
not this one" and pointed at HANDOFF_ALLOW_FOREIGN_HOLDER=1 -- the override for somebody else's
page. The refusal must say the identity is missing; a truly foreign live holder keeps the old one.

handoff.sh reaches hub-api.sh as a sibling, so the scripts are copied beside a stub that serves a
page naming a holder, and `session-attest` is a stub on PATH that reports that holder LIVE. Every
publish is `--dry-run`: the gate runs before it returns, and nothing is written anywhere.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
HOLDER = "b933c408-66c4-4a9a-a151-b023bba47fbb"
OTHER = "afca151e-78c9-4b3e-a739-221da174aa21"
FOREIGN = "LIVE session which is not this one"
NO_ID = "may be YOUR OWN"


def _publish(tmp_path, me):
    d = tmp_path / "scripts"
    d.mkdir()
    for p in SCRIPTS.iterdir():
        if p.is_file() and p.name != "hub-api.sh":
            (d / p.name).write_bytes(p.read_bytes())
    page = "# Handoff CC 9\n\n- holder session: `%s`\n" % HOLDER
    (d / "pages.json").write_text(json.dumps([{"title": "Handoff CC 9", "sub_url": "Handoff-CC-9"}]))
    (d / "page.json").write_text(json.dumps({"content_base64": base64.b64encode(page.encode()).decode()}))
    (d / "hub-api.sh").write_text(
        '#!/bin/sh\nHERE=$(dirname "$0")\n'
        'case "$1" in */wiki/pages*) cat "$HERE/pages.json" ;; */wiki/page/*) cat "$HERE/page.json" ;; esac\n')
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "session-attest").write_text('#!/bin/sh\n[ "$1" = resolve ] && [ "$2" = %s ]\n' % HOLDER)
    (bin_ / "session-attest").chmod(0o755)
    f = tmp_path / "draft.md"
    f.write_text("# Handoff CC 9\n\nbody\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FORGE_TOOLS_", "HANDOFF_"))}
    env.update(PATH="%s:%s" % (bin_, env.get("PATH", "/usr/bin:/bin")), HOME=str(tmp_path),
               HANDOFF_PAGE="Handoff CC 9", HANDOFF_REPO="acme/app", HANDOFF_NO_STAMP="1",
               HANDOFF_LOCK=str(tmp_path / "handoff.lock"), FORGE_TOOLS_SESSION_ID=me)
    return subprocess.run(["sh", str(d / "handoff.sh"), "publish", "--dry-run", str(f)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)


def test_publish_with_NO_identity_says_the_page_may_be_YOUR_OWN(tmp_path):
    r = _publish(tmp_path, "")
    assert r.returncode == 1, r.stderr
    assert NO_ID in r.stderr and "FORGE_TOOLS_SESSION_ID" in r.stderr, r.stderr
    assert FOREIGN not in r.stderr, "no identity was reported as a foreign holder:\n" + r.stderr


def test_a_FOREIGN_live_holder_still_gets_the_foreign_refusal__control(tmp_path):
    r = _publish(tmp_path, OTHER)
    assert r.returncode == 1, r.stderr
    assert FOREIGN in r.stderr and HOLDER in r.stderr, r.stderr
    assert NO_ID not in r.stderr, r.stderr
