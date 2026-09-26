"""The premise `scripts/pr-queue.sh` rests on, gated so it cannot rot silently.

Serial PR admission is only worth anything because **pushing a feature branch costs no
CI run** -- every workflow triggers on `pull_request`, and on no `push` at all (nothing runs after
a merge). That is what lets branches pile up for free and be rebased before their PR exists, so a
PR is born up to date and needs exactly one run.

Add `push: branches: ['**']` (or any non-main branch) to one workflow and the premise is
gone: every queued branch would start burning CI on every push, and the queue would be
slower than the thing it replaced while still *looking* correct. Nothing in the queue
script can detect that -- it would keep admitting PRs one at a time, and the waste would
be somewhere else entirely.

So the check is on the WORKFLOWS, not on the script: this repo's own, and the ones
`bootstrap-target-repo.sh` and `hub-api.sh repo provision` commit into every consumer repo, which
is where the queue actually runs.

Read without PyYAML, deliberately: the CI job installs only pytest, so a test that
`importorskip`s yaml would SKIP on the gate and report nothing there. The reader below handles the
shapes a workflow's `on:` takes -- a block mapping, an inline list or a bare event -- and
`test_the_reader_sees_a_planted_push_trigger__control` proves it can fail.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
OWN = REPO / ".forgejo/workflows"
SHIPPED = REPO / "scripts/templates"
WORKFLOWS = sorted(OWN.glob("*.yml")) + sorted(SHIPPED.rglob("*.yml"))


def _push_branches(text: str, name: str = "") -> list[str] | None:
    """The `push:` trigger's branch list, or None when there is no push trigger.

    `[]` means a push trigger with no `branches:` filter -- every branch.
    """
    m = re.search(r"(?m)^on:[ \t]*(.*)$", text)
    assert m, f"{name}: no `on:` block found -- test would be vacuous"
    inline = m.group(1).split("#")[0].strip()
    if inline:   # `on: push` / `on: [push, pull_request]`: no filter is possible
        return [] if "push" in re.findall(r"[\w-]+", inline) else None
    block = []
    for line in text[m.end():].splitlines()[1:]:
        if line.strip() and not line.startswith((" ", "\t", "#")):
            break    # the next top-level key ends the `on:` block
        block.append(line)
    keys = [l for l in block if l.strip() and not l.lstrip().startswith("#")]
    indent = min(len(l) - len(l.lstrip()) for l in keys) if keys else 0
    push = None
    for i, line in enumerate(block):
        if re.match(r"^ {%d}push:" % indent, line):
            push = i
    if push is None:
        return None
    branches: list[str] = []
    rest = block[push + 1:]
    for j, line in enumerate(rest):
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break    # the next trigger
        b = re.match(r"^\s+branches:\s*(.*)$", line)
        if not b:
            continue
        if b.group(1).strip():
            branches += [x.strip(" '\"") for x in b.group(1).strip("[] ").split(",") if x.strip()]
        for item in rest[j + 1:]:
            it = re.match(r"^\s+-\s*(.+?)\s*$", item)
            if not it:
                break
            branches.append(it.group(1).strip("'\""))
    return branches


def _violation(branches: list[str] | None) -> str:
    if branches is None:
        return ""    # no push trigger at all is strictly safer than the premise requires
    if branches == []:
        return "`push:` with no `branches:` filter runs on EVERY branch"
    extra = [b for b in branches if b != "main"]
    return f"push triggers on {extra} as well as main" if extra else ""


def test_workflows_exist_so_this_test_is_not_vacuous():
    names = [p.name for p in WORKFLOWS]
    assert names, "no workflows found -- an empty corpus reports clean"
    assert "python.yml" in [p.name for p in OWN.glob("*.yml")], names
    assert "test.yml" in [p.name for p in SHIPPED.rglob("*.yml")], names


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: str(p.relative_to(REPO)))
def test_no_workflow_runs_on_a_feature_branch_push(workflow: pathlib.Path):
    """A push to anything but `main` must start no CI, or serial admission is pointless."""
    why = _violation(_push_branches(workflow.read_text(), workflow.name))
    assert not why, (
        f"{workflow.name}: {why}. scripts/pr-queue.sh assumes a feature-branch push is free; it "
        "is not any more. Either drop the trigger or stop using serial admission -- the queue "
        "cannot see this.")


def test_the_reader_sees_a_planted_push_trigger__control():
    """The reader is not vacuous: each way of spelling a feature-branch push is caught, and the
    two safe spellings are not."""
    caught = {
        "block, all branches": "on:\n  pull_request:\n  push:\n    branches: ['**']\njobs: {}\n",
        "block, no filter": "on:\n  push:\n  pull_request:\njobs: {}\n",
        "list items": "on:\n  push:\n    branches:\n      - main\n      - 'feat/*'\njobs: {}\n",
        "inline list": "on: [push, pull_request]\njobs: {}\n",
        "bare event": "on: push\njobs: {}\n",
    }
    for label, text in caught.items():
        assert _violation(_push_branches(text)), label
    safe = {
        "no push": "on:\n  pull_request:\n  # No `push:` here.\n  workflow_dispatch:\njobs: {}\n",
        "main only": "on:\n  push:\n    branches: [main]\n  pull_request:\njobs: {}\n",
    }
    for label, text in safe.items():
        assert not _violation(_push_branches(text)), label
