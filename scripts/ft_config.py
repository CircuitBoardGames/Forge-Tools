"""ft_config -- the Python half of scripts/ft-config.sh; the two must agree (see that file).

Importing it does what sourcing the shell reader does: every FORGE_TOOLS_<KEY> in the config file
${FORGE_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-~/.config}/forge-tools/config} that the environment does
not already set is put into os.environ (the environment wins), so a module that reads os.environ
after this import sees the file too. get("KEY") adds the neutral defaults. The file is parsed,
never executed; names outside FORGE_TOOLS_<A-Z0-9_> are skipped.
"""
import os
import re
import sys

_HOME = os.path.expanduser("~")
_XDG_CONFIG = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_HOME, ".config")
CONFIG_FILE = os.environ.get("FORGE_TOOLS_CONFIG") or os.path.join(_XDG_CONFIG, "forge-tools", "config")
DEFAULTS = {
    "REMOTE": "origin",
    "MERGE_SCRATCH_PREFIX": os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(_HOME, ".cache"),
                                         "forge-tools", "merge"),
    "CREDENTIALS_DIR": os.path.join(_XDG_CONFIG, "forge-tools"),
    "WORKTREE_PREFIX": "CC-",
}


def _file():
    out = {}
    try:
        lines = open(CONFIG_FILE, encoding="utf-8").read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, eq, v = line.partition("=")
        if not eq or k == "FORGE_TOOLS_CONFIG" or not re.fullmatch(r"FORGE_TOOLS_[A-Z0-9_]+", k):
            print("forge-tools: %s: skipping %r -- not a FORGE_TOOLS_<KEY>=value line" % (CONFIG_FILE, line),
                  file=sys.stderr)
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if v.startswith("~/"):
            v = os.path.join(_HOME, v[2:])
        out[k] = v
    return out


for _k, _v in _file().items():
    os.environ.setdefault(_k, _v)


def get(key, default=None):
    return os.environ.get("FORGE_TOOLS_" + key, DEFAULTS.get(key, default))
