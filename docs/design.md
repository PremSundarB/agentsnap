# Design

This document explains how agentsnap works and why it is built this way. It is
written for contributors and for anyone reviewing the project.

## Goals and non-goals

**Goals**

- Many agents work on one repo on one machine without overwriting each other.
- A new agent workspace is ready in about 100 ms and costs almost no disk.
- No change is merged over someone else's change without a person agreeing.
- Works with any agent: no plugin API, no filesystem driver, no daemon.

**Non-goals (for now)**

- Distributed or multi-machine coordination.
- Replacing git. agentsnap sits *below* git: it manages working files, and
  can optionally `git commit` what it merges.
- Real-time collaboration inside one file.

## The model: optimistic concurrency control

We borrow the model databases use for transactions that rarely conflict:

| database | agentsnap |
|---|---|
| transaction | one agent's task |
| snapshot isolation | a copy-on-write snapshot of the repo per agent |
| read set / write set | files the agent read / changed |
| validation at commit | conflict check at merge |
| first committer wins | the first workspace to merge a file wins; later ones are *stale* |
| abort and retry | `rerun` policy: re-snapshot and redo the task |

Agents never block each other while they work. All coordination happens at
merge time, which is short and is serialised with a file lock (`flock`) on
`.agentsnap/lock`. Because the lock is a kernel lock, it is released if a
merging process crashes.

## Snapshots

A workspace lives in `<repo>/.agentsnap/workspaces/<name>/`:

```
meta.json     method, timestamps, merge history
base/         pristine snapshot of the repo at creation time
tree/         the agent's working directory
reads.jsonl   files the agent reported reading
attempts/N/   earlier trees kept by `refresh` / the rerun policy
```

The state lives *inside* the repo for one reason: copy-on-write clones only
work within a single volume. `.agentsnap/.gitignore` contains `*`, so git
never sees it.

**Why two copies (`base/` and `tree/`)?** `tree/` is what the agent edits.
`base/` is a frozen copy of what the agent started from. With CoW clones,
`base/` costs only directory metadata. It gives us three things:

1. *Exact change detection* without storing hashes up front (see below).
2. *Real diffs* (`agentsnap diff`), even after the repo has moved on.
3. *Three-way merges*: `git merge-file repo base tree` needs the base text.

`tree/` is snapshotted from the repo first, and `base/` is cloned from `tree/`
before the workspace is handed to the agent. The two are therefore identical
by construction, even if the repo changes during creation. `base/` skips
ignored top-level directories such as `node_modules` and `.venv`, because
ignored paths are never diffed or merged. On repos with a large dependency
directory this removes most of the second clone's cost.

**Snapshot methods**, tried in order (`snapshot.py`):

| method | where | cost |
|---|---|---|
| `apfs` | macOS. `clonefile(2)` via ctypes, one call per top-level entry (it clones directory trees recursively) | metadata only |
| `reflink` | Linux. `cp -a --reflink=always` | CoW on btrfs/XFS; fails on ext4/overlayfs, so we move on |
| `cp` | Linux. `cp -a` | full copy, faster than Python's |
| `copy` | anywhere. `shutil.copytree` | full copy |

If a method fails (for example `clonefile` on a non-APFS volume) the partial
output is removed and the next method is tried. A method that failed on a
device is not retried there for the rest of the process. The destination is checked
up front, so cleanup can never delete something that existed before.

`.git` and `.agentsnap` are not snapshotted. Everything else is, *including*
build caches such as `node_modules`. That is how agents avoid reinstalling
dependencies. Those caches are, however, excluded from change tracking and are
never merged back (`DEFAULT_IGNORES`).

## Tracking changes (writes)

`Workspace.changes()` diffs `tree/` against `base/` (`tracking.diff_trees`):

1. Walk both trees and record each file's `lstat` signature
   `(kind, size, mtime_ns)`. No file is read.
2. Paths present on one side only are *added* or *deleted*.
3. Paths whose signatures differ are *candidates*. Each one is confirmed by
   comparing content (sha256, or the link target for symlinks). A file that was
   touched but not changed is not reported.

This is the approach git's index uses. It is fast because clones and copies
preserve mtimes, so unchanged files match on the first check. On a 10k-file
repo this cut workspace creation from about 900 ms (hashing everything) to
about 210 ms, and skipping ignored directories in `base/` brought it to about
140 ms (see [benchmarks](benchmarks.md)). Like git, it cannot see an edit that keeps both the size and the
nanosecond mtime, which in practice means a tool that deliberately restores
timestamps.

Symlinks are tracked as links (by target), never followed.

## Tracking reads

File contents tell us what an agent wrote, not what it *read*. Reads matter:
an agent that read `api.py`, saw a function signature and wrote code calling
it is affected if another agent changes `api.py`, even if they never touch
the same file.

Reads are therefore reported, through either:

- the MCP server's `read_file` tool (records automatically) or `record_reads`;
- `agentsnap record-read`, which accepts a Claude Code hook payload on stdin.
  A `PostToolUse` hook on `Read` gives full read tracking with no change to
  the agent's behaviour.

The log is append-only JSON lines. A torn last line from a crash is ignored.

Kernel-level tracking (fanotify on Linux, Endpoint Security / FSEvents on
macOS) would be complete without cooperation, but needs privileges. It is
listed as future work.

## Conflict detection

`conflicts.check()` produces four kinds of conflict:

| kind | meaning | severity |
|---|---|---|
| `write-write` | two or more unmerged workspaces changed the same file | error |
| `stale-write` | the repo changed a file since the workspace's snapshot, and the workspace changed it too | error, blocks merge |
| `stale-read` | the workspace read a file that has since changed in the repo | warning |
| `read-write` | one workspace read a file another unmerged workspace changed | warning |

`write-write` is an early warning ("these two will collide"), reported before
anyone merges. Once one of them merges, the other's write becomes
`stale-write`, which is what the merge actually checks.

A *convergent* edit, where the repo already holds exactly the bytes the
workspace wants (including both deleting a file), is not a conflict.

**Function-level detail.** For Python files in a `write-write` conflict,
`symbols.py` parses the base and new versions with `ast`, maps changed line
ranges to the innermost enclosing `def`/`class`, and reports for example
`closer: farewell; greeter: greet (different functions: a 3-way merge will
likely succeed)`. Blank lines are ignored so that adding a function does not
also count as a module-level change. Other languages fall back to file level.
Tree-sitter would extend this to other languages.

## Merging

`merge.merge_workspace()`, under the lock:

1. Compute the workspace's changes.
2. For each changed path, decide:
   - **convergent**: repo already equals tree. Nothing to write.
   - **clean**: repo still equals base. Copy tree to repo.
   - **stale**: apply the policy (below).
3. Check the plan against the filesystem: a file cannot replace a directory
   that still holds other files, and a path cannot be written under something
   that is now a file. Such paths are `type-change` conflicts.
4. If any path is unresolved, write **nothing** (all-or-nothing) and report.
5. Otherwise write the repo inside an **undo journal** (below): deletions
   first, so a directory the merge empties can become a file, then every
   write (temp file + `rename`), pruning emptied directories.
6. Update the workspace's `base/` to the merged version, so the same change is
   never merged twice and later merges compare against the right base.
7. Optionally `git commit` exactly the merged paths (`--commit`). Unrelated
   staged or unstaged work in the repo is left alone. A commit failure is
   reported, never raised, because the merge has already happened.

**Undo journal (crash safety).** Writing many files is not atomic, and a disk
can fill up or a process can be killed half-way. `journal.py` first copies
every repo file the merge will overwrite or delete into `.agentsnap/txn/backup/`,
then writes `txn/journal.json` listing the paths in apply order. Once that file
exists, the merge counts as in progress. If applying raises, the journal is
replayed backwards immediately. If the process dies, the next process to take
the lock (any merge, refresh *or* workspace creation) finds the journal and
rolls back first. A crash *before* the journal is written leaves only backups,
which are discarded. This is an undo log in the database sense, so the repo is
always either fully before or fully after a merge. Workspace creation takes the
same lock, so a snapshot never captures a half-merged repo.

**Policies for stale paths**

- `human` (default): stop and report. Nothing is merged without a person.
- `merge3`: run `git merge-file -p repo base tree`. If every stale file merges
  with no conflict markers, apply the result (also written into the agent's
  tree, so it sees what landed). Deletions, symlinks and binaries are never
  auto-merged.
- `rerun`: write nothing, move `tree/` to `attempts/N/`, and re-snapshot the
  workspace from the current repo. The agent redoes its task on top of the
  winner's change, which is the proposal's "rerun the losing agent" policy.
  The old attempt stays available for reference.
- `force`: overwrite. Only for after a person has reviewed the conflict.

**Approval of agent-resolved conflicts.** The proposal asks that, on overlap,
"an agent resolves it and a person approves." `rerun` records the conflicted
paths in the workspace's `needs_approval`. Once the agent has redone its work,
the next merge stops with `awaiting_approval` until a person runs
`agentsnap merge NAME --approve` (after `agentsnap diff NAME`). The MCP
`merge_workspace` tool has no `approve` parameter, so an agent cannot approve
itself. A merge that writes nothing does not clear the flag.

**Merge check (semantic conflicts).** File-level checks cannot see two clean
edits that break each other: agent A renames `add()` to `plus()` while agent B
adds a new caller of `add()`. With `--check CMD` or `merge_check` in the
config, the merge (still under the lock) clones the repo into
`.agentsnap/scratch/`, applies the planned writes there, and runs `CMD` in it.
Only exit code 0 lets the real merge proceed. The scratch copy has no `.git`.
Its output tail is returned to the caller, so an agent can fix the problem.
Holding the lock while the check runs keeps merges serialisable: the check
always sees exactly what the repo will become.

Why all-or-nothing? A task's changes usually depend on each other (a function
and its caller, code and its test). Merging half of them can leave the repo
broken in ways neither agent would have produced.

## Integration surfaces

- **CLI** (`cli.py`): argparse, no dependencies. Every reporting command has
  `--json`. Exit codes: 0 ok, 1 conflict, 2 usage error.
- **MCP server** (`mcp_server.py`): works with MCP Python SDK 1.x (`FastMCP`)
  and 2.x (`MCPServer`). Its instructions tell the agent to create a workspace
  first, work only inside it, and never force a conflicted merge without asking.
  File tools refuse paths that resolve outside the workspace. Errors reach the
  agent as readable tool errors.

## Testing

- Unit tests for each module, run against every snapshot method available on
  the platform.
- `tests/test_isolation.py` checks the proposal's success metrics: 10
  concurrent agents produce 0 overwrites, every overlap is caught, exactly one of
  10 *separate processes* racing to merge the same file wins, and losers merge
  nothing.
- MCP tools are exercised through the SDK's `call_tool`. The stdio transport
  was smoke-tested by hand with raw JSON-RPC.
- CI runs on Ubuntu (ext4: the reflink-or-copy path) and macOS (APFS), on
  Python 3.10 and 3.13, and also runs the demo and a small benchmark.

## Future work

- Overlayfs (rootless via user namespaces, or `fuse-overlayfs`) for true CoW
  on ext4.
- Kernel-level read tracking.
- Tree-sitter for function-level detection beyond Python.
- Windows support (a `msvcrt` lock, ReFS block cloning).
