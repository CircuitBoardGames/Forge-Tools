"""A printed hint names the INSTALLED command, never a source path.

provision.json installs each tool as a command on PATH (`scripts/hub-api.sh` -> `hub-api`), and a
consumer repo holds no `scripts/hub-api.sh` of its own. A hint such as "re-measure with `sh
scripts/hub-api.sh pr checks ...`" is therefore a command that fails wherever it is read (the
consumer that used to carry copies deleted them). Comment lines may name source files; nothing printed may.
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCES = sorted({Path(c["source"]).name
                  for c in json.loads((REPO / "provision.json").read_text())["commands"]})
_SRC = "|".join(re.escape(s) for s in SOURCES)
# Two shapes, both runnable: an interpreter in front of the file, or the file followed by a verb.
VERBS = ("pr|issue|repo|approve|drain|thaw|freeze|prune-merged|merge-requested|list|register|"
         "subscribe|adopt|tick|serve|hook-install|publish|show|delete|slot|mint|revoke|/api/")
HINT = re.compile(r"\b(?:sh|python3)\s+(?:\./)?(?:scripts/)?(?:%s)(?![\w.-])"
                  r"|(?<![\w/$-])(?:scripts/)?(?:%s)\s+(?:%s)" % (_SRC, _SRC, VERBS))


def _hits(text, name="<text>"):
    return ["%s:%d: %s" % (name, n, line.strip())
            for n, line in enumerate(text.splitlines(), 1)
            if not line.lstrip().startswith("#") and HINT.search(line)]


def test_the_pattern_catches_both_shapes_and_spares_prose():
    assert _hits('log "  merge it with:  sh scripts/pr-queue.sh approve 7"')
    assert _hits('say "re-run scripts/hub-api.sh repo provision o/r --kind node"')
    assert _hits('die("usage: hub-api.sh issue " + usage)')
    assert _hits('echo "Repair: python3 scripts/gate-watch.py register o/r sha"')
    assert not _hits('log "  merge it with:  pr-queue approve 7"')
    assert not _hits('bail "hub-api.sh not executable at $HUB_API"')
    assert not _hits('log "STOPPING: scripts/pr-queue.sh on hub/main changed"')
    assert not _hits('# comment: sh scripts/pr-queue.sh approve 7')


def test_no_printed_hint_names_a_source_path():
    files = [p for p in (REPO / "scripts").rglob("*")
             if p.is_file() and "tests" not in p.relative_to(REPO / "scripts").parts
             and p.suffix != ".pyc"]
    assert REPO / "scripts/pr-queue.sh" in files and len(SOURCES) >= 10  # scanned something
    hits = [h for p in files for h in _hits(p.read_text(errors="replace"), str(p.relative_to(REPO)))]
    assert hits == [], "print the installed command name (see provision.json), not the file:\n" + "\n".join(hits)
