"""handoff.sh's trigger gate and cache budget.

Skills are harness-agnostic: a `/trigger` on a TODO page resolves against configurable
skill roots (`<root>/<name>/SKILL.md`, default `.agents/skills .claude/skills`) and command roots
(`<root>/<name>.md|.toml`, default `.claude/commands .agents/commands`) under the git top level of
the cwd -- the CONSUMER repo -- and the exception list ships with Forge-Tools, extended by
FORGE_TOOLS_TRIGGER_EXCEPTIONS. HANDOFF_CACHE_HOOK has no default path.

Every publish here is `--dry-run` against an unreachable forge, so nothing leaves the box; the gate
runs before any write and its verdict is on stderr.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
HANDOFF = SCRIPTS / "handoff.sh"
REFUSED = 'REFUSING to publish "TODO" -- it names trigger(s)'
UNCHECKED = 'NOT CHECKED -- "TODO" carries roster triggers'
# Printed only by the forge read AFTER every publish gate, so it proves a run got past them: a
# script that died earlier (a missing sibling, say) also prints neither REFUSED nor UNCHECKED.
PAST_THE_GATES = 'CANNOT TELL whether "'


def _consumer(tmp_path, files=()):
    repo = tmp_path / "consumer"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for rel in files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    return repo


def _publish(tmp_path, text, cwd, entry=HANDOFF, page="TODO", **env_extra):
    f = tmp_path / "page.md"
    f.write_text(text)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FORGE_TOOLS_") and k != "HANDOFF_CACHE_HOOK"}
    env.update(HANDOFF_PAGE=page, HANDOFF_ALLOW_FOREIGN_HOLDER="1", HANDOFF_NO_STAMP="1",
               HUB_URL="http://127.0.0.1:9", HUB_API_CONFIG=str(tmp_path / "absent.conf"),
               HOME=str(tmp_path),
               # A consumer's configuration.
               HANDOFF_REPO="acme/app", FORGE_TOOLS_TRIGGER_PAGES="TODO",
               FORGE_TOOLS_INJECTED_PAGES="Session Cache,Hot Cache",
               **env_extra)
    return subprocess.run(["sh", str(entry), "publish", "--dry-run", str(f)], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)


def test_default_roots_resolve_agents_and_claude_skills_and_commands(tmp_path):
    repo = _consumer(tmp_path, [".agents/skills/alpha/SKILL.md", ".claude/skills/beta/SKILL.md",
                                ".claude/commands/gamma.md", ".agents/commands/delta.toml"])
    r = _publish(tmp_path, "use `/alpha`, `/beta`, `/gamma` and `/delta`\n", repo)
    assert REFUSED not in r.stderr and UNCHECKED not in r.stderr and PAST_THE_GATES in r.stderr, r.stderr
    # CONTROL: one unresolvable token among them is named and refused.
    r = _publish(tmp_path, "use `/alpha` and `/nosuch`\n", repo)
    assert r.returncode == 1 and REFUSED in r.stderr and "/nosuch" in r.stderr, r.stderr


def test_a_skill_directory_without_SKILL_md_does_not_resolve(tmp_path):
    repo = _consumer(tmp_path, [".claude/skills/hollow/README.md"])
    r = _publish(tmp_path, "see `/hollow`\n", repo)
    assert REFUSED in r.stderr and "/hollow" in r.stderr, r.stderr


def test_FORGE_TOOLS_SKILL_ROOTS_and_COMMAND_ROOTS_replace_the_defaults(tmp_path):
    repo = _consumer(tmp_path, ["tools/skills/eps/SKILL.md", "tools/cmds/zeta.md"])
    text = "see `/eps` and `/zeta`\n"
    r = _publish(tmp_path, text, repo)
    assert REFUSED in r.stderr, "resolved outside the default roots: " + r.stderr
    r = _publish(tmp_path, text, repo, FORGE_TOOLS_SKILL_ROOTS="tools/skills",
                 FORGE_TOOLS_COMMAND_ROOTS="tools/cmds")
    assert REFUSED not in r.stderr and UNCHECKED not in r.stderr and PAST_THE_GATES in r.stderr, r.stderr


def test_roots_are_the_consumers_not_the_tree_handoff_lives_in(tmp_path):
    """A skill beside handoff.sh must not satisfy a trigger for a repo that lacks it."""
    repo = _consumer(tmp_path)
    tools = tmp_path / "ft" / "scripts"
    tools.mkdir(parents=True)
    for n in ("handoff.sh", "hub-api.sh", "ft-config.sh", "skill_trigger_exceptions.txt"):
        shutil.copy(SCRIPTS / n, tools / n)
    (tools.parent / ".claude/skills/only-in-tools").mkdir(parents=True)
    (tools.parent / ".claude/skills/only-in-tools/SKILL.md").write_text("x\n")
    r = _publish(tmp_path, "see `/only-in-tools`\n", repo, entry=tools / "handoff.sh")
    assert REFUSED in r.stderr, r.stderr


def test_the_shipped_exceptions_apply_and_the_env_list_extends_them(tmp_path):
    repo = _consumer(tmp_path)
    r = _publish(tmp_path, "run `/compact` then look in `/proc`\n", repo)
    assert REFUSED not in r.stderr and UNCHECKED not in r.stderr and PAST_THE_GATES in r.stderr, r.stderr
    r = _publish(tmp_path, "run `/compact` and `/mine`\n", repo)
    assert REFUSED in r.stderr and "/mine" in r.stderr and "/compact" not in r.stderr.split(REFUSED)[1], r.stderr
    r = _publish(tmp_path, "run `/compact` and `/mine`\n", repo, FORGE_TOOLS_TRIGGER_EXCEPTIONS="/other /mine")
    assert REFUSED not in r.stderr and UNCHECKED not in r.stderr and PAST_THE_GATES in r.stderr, r.stderr


def test_through_a_symlink_the_exception_list_is_still_found(tmp_path):
    """Installed as a symlink on PATH: the list is found beside the REAL script, not the link."""
    repo = _consumer(tmp_path)
    link = tmp_path / "bin" / "handoff"
    link.parent.mkdir()
    link.symlink_to(HANDOFF)
    r = _publish(tmp_path, "run `/compact`\n", repo, entry=link)
    assert REFUSED not in r.stderr and UNCHECKED not in r.stderr and PAST_THE_GATES in r.stderr, r.stderr


def test_an_unreadable_exception_list_is_said_loudly_and_not_refused(tmp_path):
    repo = _consumer(tmp_path)
    tools = tmp_path / "ft"
    tools.mkdir()
    for n in ("handoff.sh", "hub-api.sh", "ft-config.sh"):          # no skill_trigger_exceptions.txt
        shutil.copy(SCRIPTS / n, tools / n)
    r = _publish(tmp_path, "run `/compact`\n", repo, entry=tools / "handoff.sh")
    assert UNCHECKED in r.stderr and "skill_trigger_exceptions.txt" in r.stderr, r.stderr
    assert REFUSED not in r.stderr and PAST_THE_GATES in r.stderr, r.stderr


def test_outside_a_git_tree_the_gate_says_it_did_not_check(tmp_path):
    bare = tmp_path / "plain"
    bare.mkdir()
    r = _publish(tmp_path, "see `/anything`\n", bare, GIT_CEILING_DIRECTORIES=str(tmp_path))
    assert UNCHECKED in r.stderr and "not inside a git work tree" in r.stderr, r.stderr
    assert REFUSED not in r.stderr and PAST_THE_GATES in r.stderr, r.stderr


def test_an_injected_page_with_HANDOFF_CACHE_HOOK_unset_is_refused_by_name(tmp_path):
    """No default path. Unset cannot read the budget, so it REFUSES like an unreadable
    hook does -- never publishes an injected page without its ceiling."""
    repo = _consumer(tmp_path)
    r = _publish(tmp_path, "cache\n", repo, page="Session Cache")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "HANDOFF_CACHE_HOOK is not set" in r.stderr and "REFUSING" in r.stderr, r.stderr
    # CONTROL: pointed at a hook that states the budget, the same page gets past the ceiling check.
    hook = tmp_path / "inject.sh"
    hook.write_text("CACHE_CLIFF_CHARS=10000\nCACHE_NOTICE_RESERVE_CHARS=600\n")
    r = _publish(tmp_path, "cache\n", repo, page="Session Cache", HANDOFF_CACHE_HOOK=str(hook))
    assert "HANDOFF_CACHE_HOOK is not set" not in r.stderr and "cannot read" not in r.stderr, r.stderr
    assert "REFUSING to publish \"Session Cache\"" not in r.stderr and PAST_THE_GATES in r.stderr, r.stderr
