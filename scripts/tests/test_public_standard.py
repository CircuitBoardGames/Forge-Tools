"""The public standard: no line in the repository names a private deployment.

Every tracked file, tests included, is scanned for the marks of one site's internals: ticket
numbers, session ids, host names and addresses, home directories, private repository and account
names. The one exception is the fallow action's PUBLIC source, the bootstrap template's default.

Each literal below is written with a character class so that this file does not match itself.
Run `python3 scripts/tests/test_public_standard.py [file...]` to print the hits.

Ceiling: a one- or two-digit ticket number (`#7`) is indistinguishable from a test fixture's issue
number, so only three or more digits count as a ticket cite.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PATTERNS = [
    r"\bhub ?[#]\d+",                                   # a ticket on the private forge
    r"(?<![\w&{$/#])[#]\d{3,}\b",                       # a bare ticket cite, three digits or more
    r"\b(?:map|case) [#]?\d{2,}\b",                    # wayfinder maps, case-file numbers
    r"\bclaudecod[e]-\d+",                              # session handles
    # session ids: 8+ hex with a letter AND a digit, so a fixture may use `deadbeef` or `12345678`
    r"\bsession (?=[0-9]*[a-f])(?=[a-f]*[0-9])[0-9a-f]{8}",
    r"Claud[e]Code", r"claudeadmi[n]", r"(?i:airobor[o]s)", r"(?i:tierhiv[e])", r"(?i:agento[s])",
    r"198\.2[7]\.", r"10\.4[2]\.0\.2", r"127\.0\.0\.1:300[0]", r"hub-gi[t]\b",
    r"Wiki-Vaul[t]", r"(?i:gotch[a])", r"Box-Op[s]", r"CircuitBoardGame[s]", r"(?i:\bjak[e]\b)", r"jiggyc[o]",
    r"Bots-AcesDeckbuilde[r]", r"rpytes[t]", r"hindsight-determinis[m]",
]
SCAN = re.compile("|".join("(?:%s)" % p for p in PATTERNS))

# Allowed, each for a stated reason. A regex removed from the line before it is scanned.
ALLOW = [
    # The fallow action's public source: the one site a target repo's fallow.yml may name by default.
    re.compile(r"https://github\.com/CircuitBoardGame[s]/Fallow-Forgejo@[0-9a-f]{40}"),
]


def tracked_files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [f for f in out.decode().split("\0") if f]


def hits(files=None):
    files = files if files is not None else tracked_files()
    found = []
    for f in files:
        try:
            text = (REPO / f).read_text(errors="replace")
        except (IsADirectoryError, FileNotFoundError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            scrubbed = line
            for a in ALLOW:
                scrubbed = a.sub("", scrubbed)
            m = SCAN.search(scrubbed)
            if m:
                found.append("%s:%d: [%s] %s" % (f, n, m.group(0), line.strip()[:160]))
    return found


def test_no_line_names_a_private_deployment():
    files = tracked_files()
    assert len(files) > 40, "the scan found almost nothing to scan: %r" % files
    found = hits(files)
    assert not found, "%d internal reference(s):\n%s" % (len(found), "\n".join(found))


def test_the_scan_sees_a_planted_reference():
    """The scan is not vacuous: each class of mark, planted in an otherwise clean line, is found."""
    for planted in ("see hub%s123" % "#", "fixed in %s1234" % "#", "/home/claude%s/x" % "admin",
                    "measured by session %s" % "0123abcd", "https://github.com/CircuitBoardGame%s/x" % "s"):
        assert SCAN.search(planted), planted
    for clean in ("subject (#42)", "PR #7 is open", "${#var}", "#!/bin/sh"):
        assert not SCAN.search(clean), clean



# --- provision.json and the README ---------------------------------------------------------------

def _manifest():
    return json.loads((REPO / "provision.json").read_text())


def manifest_problems(m):
    """Every problem with a provision.json manifest, [] when it is installable as written."""
    out = []
    names = [c.get("name") for c in m.get("commands", [])]
    if not m.get("name") or not names:
        out.append("no name, or no commands")
    if len(set(names)) != len(names):
        out.append("duplicate command names: %r" % names)
    for c in m.get("commands", []):
        src = REPO / c.get("source", "")
        if not src.is_file():
            out.append("%s: source %s does not exist" % (c.get("name"), c.get("source")))
        elif not os.access(str(src), os.X_OK):
            out.append("%s: source %s is not executable" % (c.get("name"), c.get("source")))
        for b in c.get("beside", []):
            if not (REPO / b).is_file() or not os.access(str(REPO / b), os.R_OK):
                out.append("%s: beside file %s is missing or unreadable" % (c.get("name"), b))
    if m.get("privileged") is not False:
        out.append("privileged must be false")
    return out


def test_provision_json_is_installable_as_written():
    m = _manifest()
    assert not manifest_problems(m), manifest_problems(m)
    assert m["state_dir"]["path"].endswith("/forge-tools")


def test_the_manifest_check_sees_a_missing_source():
    """Not vacuous: one command pointed at a file that does not exist is reported, by name."""
    m = _manifest()
    m["commands"][0] = dict(m["commands"][0], source="scripts/no-such-script.sh")
    assert any("no-such-script.sh does not exist" in p for p in manifest_problems(m)), manifest_problems(m)


def test_the_readme_names_every_installed_command():
    readme = (REPO / "README.md").read_text()
    missing = [c["name"] for c in _manifest()["commands"] if "`%s`" % c["name"] not in readme]
    assert not missing, "README.md does not name: %r" % missing

if __name__ == "__main__":
    out = hits(sys.argv[1:] or None)
    print("\n".join(out))
    print("%d hit(s)" % len(out), file=sys.stderr)
    sys.exit(1 if out else 0)
