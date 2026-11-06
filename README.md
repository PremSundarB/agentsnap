# agentsnap

**Run many coding agents on one repo, safely.**
Each agent gets its own copy-on-write snapshot of the repository in milliseconds.
agentsnap tracks what each agent changed, catches overlapping edits before
anything is merged, and merges clean work back automatically.

[![CI](https://github.com/OWNER/agentsnap/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/agentsnap/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

> Status: alpha (v0.1). This is a course project for Open Source Software, Fall 2026,
> by Bhushan, Arvind and Prem. See the [proposal](docs/proposal/).

---

## The problem

Coding agents like Claude Code, Codex and Cursor can run several tasks at once.
By default they share one working directory, so two agents can edit the same
file and one silently overwrites the other.

The usual fix is one full `git worktree` per agent. That means a full checkout,
a full dependency install and a full build for every agent. Ten agents means
ten builds, and a person still has to merge the branches by hand afterwards.

This is a classic distributed systems problem: many workers share one state and
need **isolation**, **concurrency control** and **reconciliation**. agentsnap
applies the database answer, optimistic concurrency control, to agent workspaces.

## How it works

```
                 ┌──────────── your repo ────────────┐
                 │  src/  docs/  node_modules/  ...  │
                 └───────┬──────────┬──────────┬─────┘
       copy-on-write     │          │          │      (APFS clone / reflink:
       snapshot, ~ms     ▼          ▼          ▼       no data copied)
                     agent-1    agent-2    agent-3
                      edits      edits      edits
                         │          │          │
                         ▼          ▼          ▼
                   ┌──────────────────────────────┐
                   │ conflict check               │  write-write, stale-write,
                   │ (which files/functions each  │  stale-read, read-write
                   │  agent changed or read)      │
                   └──────────────┬───────────────┘
                                  ▼
                   ┌──────────────────────────────┐
                   │ merge (all-or-nothing, first │  policy on conflict:
                   │ committer wins, file lock)   │  human | merge3 | rerun | force
                   └──────────────────────────────┘
```

1. **Snapshots.** `agentsnap new NAME` clones the repo into
   `.agentsnap/workspaces/NAME/tree`. On macOS this uses APFS `clonefile`, on
   Linux reflinks (CoW on btrfs/XFS), and a plain copy anywhere else.
   Build caches like `node_modules` come along for free, so agents don't
   reinstall or rebuild. Each workspace also gets its **own private git repo**,
   so an agent running `git checkout .` or `git reset --hard` in its workspace
   cannot touch your main repo.
2. **Conflict detection.** agentsnap compares each workspace with a pristine
   copy of its starting point, the same way git's index works: cheap `stat`
   checks first, content hashes only for files that look changed. It reports
   files changed by several agents (and, for Python, *which functions* each one
   touched), files the repo changed underneath an agent, and files an agent
   *read* that have since changed.
3. **Merge.** A workspace merges all-or-nothing under a lock, with an undo
   journal so even a crash half-way leaves the repo unchanged. Clean changes are
   applied. If someone else merged a change to the same file first, the policy
   decides: stop for a person (`human`, the default), try a line-level
   three-way merge (`merge3`), re-snapshot so the agent can redo its task on the
   latest code (`rerun`), or overwrite after review (`force`). After a rerun,
   the agent's resolution waits for a person to approve it (`--approve`).
   Optionally, a **merge check** such as `pytest -q` runs on the merged result
   in a scratch snapshot, and the merge only lands if it passes. That catches
   two "clean" edits that break each other.

## Install

> **Not on PyPI yet.** Note: the PyPI package called `agentsnap` is an
> unrelated project, so do not `pip install agentsnap`. Install from source:

```bash
git clone https://github.com/OWNER/agentsnap && cd agentsnap
python -m venv .venv && source .venv/bin/activate
pip install -e .            # CLI only, no dependencies
pip install -e '.[mcp]'     # plus the MCP server for Claude Code / Cursor / Codex
pip install -e '.[dev,mcp]' && pytest   # for development
```

## Quick start

```bash
cd your-repo
agentsnap new fix-login add-tests          # two workspaces, milliseconds each
cd "$(agentsnap path fix-login)"           # ...or point an agent at this directory
# agents edit their own trees in parallel

agentsnap list                             # who changed what
agentsnap conflicts                        # overlaps, before anything is merged
agentsnap diff fix-login                   # review
agentsnap merge fix-login add-tests        # all-or-nothing, first committer wins
agentsnap merge add-tests --policy merge3  # if it overlapped, try a 3-way merge
agentsnap merge add-tests --check "pytest -q"   # only land it if tests pass on the result
agentsnap drop fix-login add-tests
```

A full walkthrough with scripted agents is in [`examples/demo.sh`](examples/demo.sh).
Its second scene shows `--check` catching two edits that never touch the same
file but break each other. Excerpt from the first scene:

```
$ agentsnap conflicts
CONFLICT  write-write src/app.py: changed by closer, greeter  [functions - closer: farewell; greeter: greet]  (different functions: a 3-way merge will likely succeed)

$ agentsnap merge docs greeter
docs: merged 1 file(s)
greeter: merged 1 file(s)

$ agentsnap merge closer
closer: NOT merged, 1 conflict(s) (policy: human)
  CONFLICT src/app.py: changed in the repo since closer snapshotted it, and closer changed it too

$ agentsnap merge closer --policy merge3
closer: merged 2 file(s)
  merged     src/app.py
  added      src/test_app.py
```

## Use with Claude Code, Cursor and Codex

agentsnap ships an MCP server, so any MCP client can use it without changes
to the client:

```bash
claude mcp add agentsnap -- agentsnap-mcp --repo "$(pwd)"
```

The server tells the agent to create a workspace before editing, to work only
inside it, and to check conflicts and merge at the end. To run several
headless agents from the shell, or to track reads with a hook, see
[`examples/claude-code/`](examples/claude-code/).

## Commands

| command | description |
|---|---|
| `agentsnap new NAME...` | create workspaces (`-q` prints only the path, `--method apfs\|reflink\|cp\|copy`) |
| `agentsnap list` | workspaces with `+added ~modified -deleted` counts |
| `agentsnap path NAME` | print a workspace's directory |
| `agentsnap status [NAME]` | changed files per workspace |
| `agentsnap diff NAME [PATH...]` | unified diff against the snapshot |
| `agentsnap conflicts [NAME...]` | conflict report; exit code 1 if blocking conflicts |
| `agentsnap merge NAME... [--policy P] [-n] [--commit] [--check CMD] [--approve]` | merge back; exit code 1 if not merged |
| `agentsnap refresh NAME` | re-snapshot on the latest repo, keeping the old tree in `attempts/` |
| `agentsnap run NAME -- CMD...` | run a command (e.g. an agent) inside a workspace |
| `agentsnap drop NAME... [-f]` | delete workspaces (refuses unmerged changes without `-f`) |
| `agentsnap record-read [PATH...]` | record reads; with no args, reads a Claude Code hook payload from stdin |
| `agentsnap doctor` | which snapshot methods work on this filesystem, and the active config |
| `agentsnap mcp` | run the MCP server on stdio |

`new`, `list`, `status`, `conflicts` and `merge` accept `--json` for scripting.

## Results so far

From [`benchmarks/bench_workspaces.py`](benchmarks/bench_workspaces.py) on an
Apple M4 Mac (APFS), giving 10 agents their own copy of a 109 MiB synthetic
repo (2,000 source files plus 3,000 untracked build-cache files). Full numbers,
including a larger repo, are in [docs/benchmarks.md](docs/benchmarks.md).

| | agentsnap (APFS clone) | agentsnap (plain copy) | git worktree + build cache |
|---|---|---|---|
| time to a ready workspace (median) | **54 ms** | 1,016 ms | 716 ms |
| extra disk for 10 agents | **22 MiB** (2% of 10 copies) | 1.3 GiB | 1.1 GiB |

How this compares with the proposal's success metrics:

| metric (from the proposal) | target | status |
|---|---|---|
| Isolation | 10 agents, 0 overwrites | ✅ tested (`tests/test_isolation.py`) |
| Latency | workspace ready in < 100 ms | ✅ 54 ms on the default repo (APFS), 99 ms (XFS reflink, Linux VM). ⚠️ 142 ms on a 13k-file repo (cost grows with file count) |
| Storage | ≥ 90% less than 10 full copies | ✅ 98% less on APFS, 96% less on XFS with reflinks. ⚠️ No savings on ext4/overlayfs (no CoW) |
| Conflict detection | 100% of overlapping edits caught; 0 merged without approval | ✅ tested, including 10 processes racing to merge |
| Integration | one `pip install`; works end to end from Claude Code via MCP | ✅ MCP server tested over stdio. ⬜ PyPI release pending |

## Limitations

- **POSIX only** (macOS, Linux). Windows is not supported yet.
- **Linux without btrfs/XFS** copies files instead of cloning them. Overlayfs
  support is on the roadmap.
- **Reads are only known if reported**, via the MCP `read_file` tool or a hook.
  Write tracking needs no cooperation from the agent.
- **Function-level conflict reports cover Python only.** Other languages are
  reported at file level.
- Paths under `.git`, `node_modules`, `.venv`, `__pycache__` and similar are
  never merged back (see `DEFAULT_IGNORES`).

## Configuration

Optional, in `<repo>/.agentsnap/config.json`:

```json
{
  "extra_ignore": ["generated", "coverage"],
  "merge_check": "pytest -q -x",
  "merge_check_timeout": 600,
  "snapshot_method": "copy",
  "workspace_git": "private"
}
```

`merge_check` applies to every merge, from the CLI and from agents over MCP.
Agents cannot skip it or approve their own conflict resolutions.
`workspace_git: "none"` disables git inside workspaces entirely instead of
giving each one a private repo.

## Project layout

```
src/agentsnap/
  snapshot.py    copy-on-write snapshots (APFS / reflink / copy)     Bhushan
  merge.py       all-or-nothing merges and policies                  Bhushan
  journal.py     undo journal: roll back failed or crashed merges    Bhushan
  tracking.py    what changed (stat + hash) and what was read        Arvind
  conflicts.py   write-write, stale-write, stale-read, read-write    Arvind
  symbols.py     which Python functions an edit touched              Arvind
  workspace.py   workspace store, layout, locking
  cli.py         `agentsnap` command                                 Prem
  mcp_server.py  `agentsnap-mcp` for Claude Code / Cursor / Codex    Prem
tests/           pytest suite (unit, CLI, MCP, multi-agent isolation)
benchmarks/      latency and disk benchmarks
examples/        demo script and Claude Code integration
docs/            design notes, roadmap, benchmarks, original proposal
```

## Documentation

- [Design](docs/design.md): the concurrency model and the decisions behind it
- [Roadmap](docs/roadmap.md): semester milestones and what's next
- [Benchmarks](docs/benchmarks.md)
- [Contributing](CONTRIBUTING.md)

## License

[MIT](LICENSE)
