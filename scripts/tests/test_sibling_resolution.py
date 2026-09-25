"""Forge-Tools finds its OWN files from its real directory, never under the consumer repo.

`$HUB` / `PR_QUEUE_HUB` / `DOCTOR_HUB` name the CONSUMER checkout a command operates on. The pruner,
gate-watch, agent_comms, live-drains, batch_blame, forge.sh and hub-api.sh are siblings of the
script, and it is installed as a symlink on PATH -- so `$HUB/scripts/...` and a bare `dirname $0`
both find the wrong file (or none).
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]


def test_pr_queue_runs_its_own_pruner_not_the_consumers_through_a_symlink(tmp_path):
    consumer = tmp_path / "consumer"
    (consumer / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(consumer)], check=True)
    # A decoy where the old lookup went: `$HUB/scripts/`. It must NOT be what runs.
    decoy = consumer / "scripts" / "prune-landed-branches-forgejo.sh"
    decoy.write_text("#!/bin/sh\necho DECOY PRUNER RAN\n")
    decoy.chmod(0o755)
    link = tmp_path / "bin" / "pr-queue"
    link.parent.mkdir()
    link.symlink_to(SCRIPTS / "pr-queue.sh")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PR_QUEUE_")}
    # The real pruner stops at its first check, so no forge is asked: its client is absent.
    env.update(PR_QUEUE_HUB=str(consumer), PR_QUEUE_REPO="o/r", HUB_API_SH=str(tmp_path / "no-client"))
    r = subprocess.run(["sh", str(link), "prune-merged"], cwd=tmp_path, env=env, input="",
                       capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert "DECOY PRUNER RAN" not in out, "pr-queue ran the consumer's pruner:\n" + out
    assert f"hub-api.sh not executable at {tmp_path / 'no-client'}" in out, (
        "the Forge-Tools pruner beside the real pr-queue.sh did not run:\n" + out)


# `$HUB/scripts/` (a Forge-Tools file looked for in the consumer), a bare `dirname "$0"` or
# `dirname -- "$0"` (the symlink's directory), and `abspath(__file__)` on sys.path (same, in Python).
_WRONG = re.compile(r'\$\{?HUB\}?/scripts/|dirname (-- )?"\$0"|dirname "\$\{BASH_SOURCE\[0\]\}"'
                    r'|sys\.path\.insert\(0, os\.path\.dirname\(os\.path\.abspath\(__file__\)\)\)')


def test_no_core_script_finds_a_sibling_through_the_consumer_or_the_link():
    files = subprocess.run(["git", "ls-files", "scripts"], cwd=SCRIPTS.parent,
                           capture_output=True, text=True, check=True).stdout.split()
    scanned, hits = 0, []
    for f in files:
        if f.startswith(("scripts/tests/", "scripts/templates/")) or not f.endswith((".sh", ".py", "suites")):
            continue
        scanned += 1
        for n, line in enumerate((SCRIPTS.parent / f).read_text(errors="replace").splitlines(), 1):
            if not line.lstrip().startswith("#") and _WRONG.search(line):
                hits.append(f"{f}:{n}: {line.strip()}")
    assert scanned >= 10, f"scanned only {scanned} files, so this measured little"
    assert not hits, "a sibling is found through the consumer repo or the symlink:\n" + "\n".join(hits)
