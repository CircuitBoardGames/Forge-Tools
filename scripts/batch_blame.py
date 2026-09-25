"""Which members of a red batch can have caused it, read from the red itself.

    batch_blame.py <base-sha> <why-red-output-file> <pr>=<ref> [<pr>=<ref>...]

Prints ONE line: `eject: <pr> ...` when the failing tests implicate some members and not others,
else `halve: <reason>`. Run from the checkout that holds the refs; exits 0 either way.

A red batch used to be halved blindly: one five-member batch took three more gate runs to find
culprits the failing test names already pointed at. Here a member is IMPLICATED when its own diff
(`<base>...<ref>`) touches a failing test file, or a path the committed impact map records that
test reading, or anything the map cannot place -- the same fail-toward-running rule a
scoped pytest run applies, so an unplaceable diff can never clear a member. When none or all are
implicated the answer is to halve, as before.
"""
import json
import re
import subprocess
import sys

# Anchored to the shapes pytest and the differential gate print. why-red prefixes each kept line
# with two spaces, which is removed before matching so that pytest's column-0 short summary
# (`FAILED path::test`) stays distinct from the gate's INDENTED per-test listing, whose
# `FAILED ... [proven: FAILED on base]` lines are passes of the gate, not failures. A substring
# match here once made tests NAMED `..._assertion_...` read as failures.
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
_SUMMARY = re.compile(r"^(?:FAILED|ERROR) (\S+?\.py)(?:::|\s|$)")
_UNPROVEN = re.compile(r"^\s*\S+\s+(\S+?\.py)::\S+\s+\[NOT PROVEN\b")


def failing_test_files(why_red_lines):
    files = set()
    for line in why_red_lines:
        p = _STAMP.sub("", line[2:] if line.startswith("  ") else line, count=1)
        m = _SUMMARY.match(p) or _UNPROVEN.match(p)
        if m:
            files.add(m.group(1))
    return files


def _covers(paths, f):
    return f in paths or "." in paths or any(d.endswith("/") and d != "./" and f.startswith(d) for d in paths)


def implicated(changed, failing, tests, machinery=(), tests_dir="scripts/tests/"):
    """Whether a member whose diff is `changed` can reach any `failing` test file. `changed` None
    means the diff could not be read, which implicates. A changed TEST file implicates only when it
    is one of the failing ones: most members add a test, and a new one is in no map yet."""
    if changed is None:
        return True
    for f in changed:
        if f in failing or f in machinery:
            return True
        if any(_covers(tests.get(t, ()), f) for t in failing):
            return True
        if f.startswith(tests_dir) and f.rsplit("/", 1)[-1].startswith("test_"):
            continue
        if not any(_covers(p, f) for p in tests.values()):
            return True     # nothing records reading it: unplaceable, so it cannot be cleared
    return False


def decide(members, failing, tests, machinery=(), tests_dir="scripts/tests/"):
    """members: {pr: changed-files or None}. Returns (eject list, reason)."""
    if not failing:
        return [], "the red names no failing test file"
    hit = [m for m, ch in members.items() if implicated(ch, failing, tests, machinery, tests_dir)]
    if not hit:
        return [], "no member's diff reaches %s" % " ".join(sorted(failing))
    if len(hit) == len(members):
        return [], "every member's diff reaches %s" % " ".join(sorted(failing))
    return hit, "only these members' diffs reach %s" % " ".join(sorted(failing))


def _git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout


def main(argv):
    base, log_path, pairs = argv[0], argv[1], argv[2:]
    try:
        failing = failing_test_files(open(log_path).read().splitlines())
        cfg = json.loads(_git("show", "%s:.ci-scope.json" % base))
        tests = {t: set(p) for t, p in json.loads(_git("show", "%s:%s" % (base, cfg["map"])))["tests"].items()}
        tests_dir = cfg["tests_dir"].rstrip("/") + "/"
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        print("halve: no impact map to attribute by (%s)" % ((str(e).strip() or type(e).__name__).splitlines()[-1][:120]))
        return 0
    members = {}
    for pair in pairs:
        pr, ref = pair.split("=", 1)
        try:
            members[pr] = set(_git("diff", "--name-only", "%s...%s" % (base, ref)).split())
        except subprocess.CalledProcessError:
            members[pr] = None
    hit, why = decide(members, failing, tests, set(cfg.get("machinery", ())), tests_dir)
    print("eject: %s -- %s" % (" ".join(hit), why) if hit else "halve: " + why)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
