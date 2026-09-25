# Forge-Tools

Forge-Tools is a forge client for agent sessions, built for Forgejo first. It covers:

- authenticated API access that keeps the token out of every transcript;
- a PR queue that rebases, gates and lands PRs, and batches waiting drafts into one integration PR;
- a gate watcher that outlives the session that asked for it;
- handoff pages on the repo's wiki, each read back after it is written;
- worktree creation and reaping;
- bootstrapping a target repo's CI gate.

It also reaches GitHub through `gh`, for forks of repositories you do not own.

The core is POSIX `sh` plus the Python standard library, and it reads **no** harness variable. A
session's identity is passed to it as `FORGE_TOOLS_SESSION_ID` and `FORGE_TOOLS_WAKE_PID`, and an
adapter maps a harness's own names onto those. The Claude Code adapter ships here.

## What is here

| Path | What it is |
|---|---|
| `scripts/` | The commands (below) and the helpers they load from their own directory. |
| `scripts/templates/` | The CI gate workflows and fallow config that `hub-api repo provision` and `bootstrap-target-repo` place in a repo. |
| `scripts/tests/` | The test suite. |
| `adapters/claude-code/` | The Claude Code plugin: one SessionStart hook that maps the session's identity. |
| `.claude-plugin/marketplace.json` | Lists that plugin, so this repository can be registered as a plugin marketplace. |
| `config.example` | Every site setting, documented. |
| `provision.json` | The commands this repository installs, the commands it requires, and its state directory. |

## Commands

Each command is installed by name (see [Installing](#installing)) from the `scripts/` file shown.

| Command | Source | What it does |
|---|---|---|
| `hub-api` | `hub-api.sh` | Authenticated forge API access, plus the `pr`, `issue` and `repo` verbs: open, check, await and merge PRs; claim, file, resolve and list wayfinder issues; provision a repo's CI gate and branch protection. |
| `pr-queue` | `pr-queue.sh` | The merge queue. It opens and rebases PRs, waits for the gate and lands them in order: `approve`, `drain [--batch]`, `merge-requested`, `freeze`/`thaw`, `merge-paths`, `prune-merged`. Waiting drafts land as one integration PR, split in half on red. |
| `forge` | `forge.sh` | One forge-agnostic surface over `hub-api` (Forgejo) and `gh` (GitHub): `where`, `pr open\|gate\|land\|list`, `label`, `review`, `branch rm`, `was-cancelled`. |
| `gate-watch` | `gate-watch.py` | Watches a PR's gate after the session has gone idle or ended, and peer-messages the verdict: `register`, `subscribe`, `adopt`, `tick`, `serve`, `hook-install`, `list`. |
| `forge-doctor` | `doctor.sh` | One read-only diagnosis for a confused agent: the queue, holds, stale trees and mid-flight rebases. `--fix` applies only the two fixes that are individually safe. |
| `handoff` | `handoff.sh` | Publishes a handoff or cache page to the repo's wiki and reads it back to prove it landed. Also `show`, `verify`, `url`, `slot` (the per-terminal page registry) and `delete`. |
| `worktree-create` | `worktree-create.sh` | Creates a sibling worktree on the remote's `main`, applies `.worktreeinclude`, and checks the result before handing it over. `--check` verifies an existing worktree. |
| `worktree-reap` | `worktree-reap.sh` | Reports worktrees whose owning session is gone, and removes the clean ones with `--delete`. It refuses dirty trees and trees whose work has not landed. |
| `resource-lock` | `resource-lock.sh` | Holds a named shared resource (for example `suite`) for the lifetime of a command, queueing by default. |
| `running-suites` | `running-suites` | Prints one `<pid><TAB><tree>` row for each pytest run over `scripts/tests` that is live on this machine. |
| `bootstrap-target-repo` | `bootstrap-target-repo.sh` | Places the CI gate and fallow config in a target repo, provisions its protection, and prints the steps a human or agent still owes. |
| `prune-landed-branches-forgejo` | `prune-landed-branches-forgejo.sh` | Deletes local branches whose forge PR is merged, asking the forge rather than git. It runs after a `pr merge` as a hook, or by hand with `--dry-run` or `--delete`. |
| `live-drains` | `live-drains.sh` | Prints every `pr-queue` run live on this machine. Exits 0 if there is one, 1 if there is none. |

The helpers loaded from beside the commands are:

- `ft-config.sh` and `ft_config.py`: the config readers;
- `close_ref.py`: the close-keyword matcher;
- `agent_comms.py`: agent detection from `/proc`;
- `bash_cmd_parse.py`: the command-position matcher;
- `batch_blame.py`: which members of a red batch could have caused it;
- `skill_trigger_exceptions.txt`.

Commands print their instructions under their script names (`hub-api.sh pr merge ...`), because
that is how they call each other.

## Configuration

Every site value is a `FORGE_TOOLS_<KEY>`, taken from the environment or from
`${FORGE_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/forge-tools/config}`. The file holds plain
`KEY=VALUE` lines and is parsed, never sourced. **The environment wins.** `config.example` documents
every key.

**Required:** `FORGE_TOOLS_FORGE_URL` (the forge's base URL) and either `FORGE_TOOLS_REPO`
(`owner/name`) or `FORGE_TOOLS_OWNER`. With only the owner set, the repo name is read off the
remote's URL. A command that needs one of these and finds it unset refuses, naming the key and the
file. Neither has a default, so a command never guesses somebody else's forge.

**Optional:** `FORGE_TOOLS_REMOTE` (default `origin`), `FORGE_TOOLS_FORGE_HOSTS`,
`FORGE_TOOLS_CHECKOUT`, `FORGE_TOOLS_MERGE_SCRATCH_PREFIX`, `FORGE_TOOLS_CREDENTIALS_DIR` (default
`${XDG_CONFIG_HOME:-$HOME/.config}/forge-tools`), `FORGE_TOOLS_INJECTED_PAGES`,
`FORGE_TOOLS_TRIGGER_PAGES`, `FORGE_TOOLS_FALLOW_ACTION`, `FORGE_TOOLS_SKILL_ROOTS`,
`FORGE_TOOLS_COMMAND_ROOTS`, `FORGE_TOOLS_TRIGGER_EXCEPTIONS` and `FORGE_TOOLS_AGENT_COMMS`.

**Credentials** live in the credentials directory:

- `hub-api.conf` holds the API token, as a curl config line, mode 600.
- `hub-access.conf` is an optional Cloudflare Access pair.
- `gate-watch.secret` is the webhook secret.

Nothing prints the token. `hub-api fingerprint` names which token is installed without printing it.

**State:** gate-watch keeps its registry and delivery mark under
`${XDG_STATE_HOME:-$HOME/.local/state}/forge-tools`.

**Per session**, set by an adapter and never put in the file:

- `FORGE_TOOLS_SESSION_ID`: used for claim ownership, the handoff holder and signature, and gate-watch's session;
- `FORGE_TOOLS_WAKE_PID`: the process that gate-watch and a detached `pr-queue` run peer-message.

## Commands this repository relies on, by name

| Command | Repository | Used for |
|---|---|---|
| `session-notify` | Session-Notify | Delivering a message to a session: gate-watch verdicts, and a detached `pr-queue` run's exit and owner nudges. |
| `session-attest` | Session-Attest | Signing handoff pages (`stamp`, `parse`), host keys, and whether a claim's session is still live (`resolve`). |
| `session-succeed` | Session-Succession | Optional. `handoff publish` verifies an operator-instruction block verbatim with `session-succeed operator-lines`. |
| `forge-token-admin` | Forge-Token-Admin | Optional. `hub-api mint` and `hub-api revoke` delegate to it, because minting needs root. |
| `gh` | GitHub CLI | Optional. Only `forge`'s GitHub arm uses it. |
| `git`, `curl`, `flock`, `setsid`, `bash` | system packages | Throughout. `bash` is needed only by `bootstrap-target-repo`. |

If one of these commands is missing, the call that needs it says so by name, naming the repository
that provides it. A detached `pr-queue` run refuses to detach without `session-notify` (exit 2),
because it could never report its end.

## Hooks

The core requires none. The Claude Code adapter is a plugin whose root is `adapters/claude-code`
(`.claude-plugin/plugin.json`, with the wiring in `hooks/hooks.json`):

| Event | Matcher | Runs | What it does |
|---|---|---|---|
| `SessionStart` | `startup\|resume\|clear\|compact` | `hooks/map-identity.sh` | Appends `export FORGE_TOOLS_SESSION_ID=<CLAUDE_CODE_SESSION_ID>` and `export FORGE_TOOLS_WAKE_PID=<CLAUDE_PID>` to `$CLAUDE_ENV_FILE`, so every later Bash tool call carries the session's identity. It writes nothing outside Claude Code and never fails a session start. |

Two commands can also be wired as hooks by a deployment; the plugin does not wire them:

- `prune-landed-branches-forgejo` takes a `PostToolUse` (`Bash`) payload on stdin. It fires only
  when the command in position is `hub-api.sh pr merge`, and then deletes the local branches whose
  PRs are merged.
- `gate-watch tick` and `gate-watch serve` are meant for a timer and a service owned by the host.
  The unit files are the deployment's.

Install the plugin by registering this repository as a local plugin marketplace, then installing
it:

    claude plugin marketplace add <this repository>
    claude plugin install forge-tools@forge-tools --scope <user|project|local>

A local-directory plugin runs in place, so its hook sees this checkout's current files without a
reinstall. For one session only, `claude --plugin-dir <this repository>/adapters/claude-code` loads
it. Do not also wire the same hook in a `settings.json`, or it runs twice.

## Installing

`provision.json` lists each command's name and source, the files it loads from beside that source,
the commands it requires and its state directory. An installer that reads the manifest symlinks
each command onto `PATH`. By hand, the same thing is:

    for c in hub-api:hub-api.sh pr-queue:pr-queue.sh forge:forge.sh gate-watch:gate-watch.py \
             forge-doctor:doctor.sh handoff:handoff.sh worktree-create:worktree-create.sh \
             worktree-reap:worktree-reap.sh resource-lock:resource-lock.sh running-suites:running-suites \
             bootstrap-target-repo:bootstrap-target-repo.sh \
             prune-landed-branches-forgejo:prune-landed-branches-forgejo.sh live-drains:live-drains.sh; do
        ln -sf "$PWD/scripts/${c#*:}" "$HOME/.local/bin/${c%%:*}"
    done

Every command resolves its siblings from its real path, so a symlink is enough. The checkout must
stay where it is. Then write your config (`config.example`) and the token file.

## Tests

    python3 -B -m pytest scripts/tests

The suite runs the real `session-notify` and `session-attest`, so both must be on `PATH`. CI checks
them out at pinned tags (`.forgejo/workflows/python.yml`).

`test_public_standard.py` scans every tracked file for the marks of a private deployment (ticket
numbers, session ids, hosts, home directories, private names). It fails on any it finds. It also
checks that `provision.json` is installable as written, and that this README names every command
the manifest installs.
