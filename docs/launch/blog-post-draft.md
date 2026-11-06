# Ten coding agents, one repo, zero overwrites

*Draft for the November 30 launch. Audience: developers who already run coding agents.*

If you have tried running two coding agents on the same repository at the
same time, you have probably seen this. One agent fixes a bug in `api.py`. A
second agent, working on a feature, rewrites `api.py` from the version it read
ten minutes ago. The bug fix is gone, nobody gets a warning, and you find out
when the tests fail.

The standard fix is one `git worktree` per agent. It works, but every worktree
is a full checkout and needs its own dependency install and build. Ten agents
means ten `npm install`s. And at the end, you are the one merging ten branches.

We built **agentsnap** to make parallel agents cheap and safe on a single
machine.

## The idea: treat agents like database transactions

This is an old problem in a new place. Many workers share one state, and they
need isolation while they work and a safe way to combine their results.
Databases solved it decades ago with *optimistic concurrency control*:

- every transaction works on its own snapshot and never blocks the others;
- at commit time, the database checks whether anything it touched changed
  underneath it;
- the first one to commit wins; the others retry on the new state.

agentsnap does exactly that, with files.

## 1. Snapshots in milliseconds

`agentsnap new fix-login` gives the agent a full copy of your repo, made with
the filesystem's copy-on-write cloning (APFS on macOS, reflinks on btrfs/XFS on
Linux). No data is copied until someone writes to a file. Your `node_modules`
comes along for free, so the agent does not reinstall anything.

On our test repo (109 MiB, 5,000 files, 10 agents):

| | agentsnap | git worktree + install |
|---|---|---|
| time per agent | 54 ms | 716 ms |
| disk for 10 agents | 22 MiB | 1.1 GiB |

## 2. Know who touched what, before merging

agentsnap compares each agent's tree with the snapshot it started from, using
the same trick as git's index (check `stat` first, hash only what looks
changed). `agentsnap conflicts` then tells you, *before* anything is merged:

```
CONFLICT  write-write src/app.py: changed by closer, greeter
          [functions - closer: farewell; greeter: greet]
          (different functions: a 3-way merge will likely succeed)
```

It also tracks what agents *read*, through the MCP server or a Claude Code
hook. If an agent based its work on a file that another agent has since
changed, you get a warning, even though the two never wrote the same file.

## 3. Merges that cannot lose work

`agentsnap merge` is all-or-nothing. It takes a lock, checks that every file it
is about to write still holds the version the agent started from, and writes
them all under an undo journal. If the process crashes half-way, the next
command rolls it back. If someone else got there first, you pick a policy:

- `human`: stop and show me (the default);
- `merge3`: try a line-level three-way merge;
- `rerun`: give the agent a fresh snapshot with the other changes so it can
  redo its task, then wait for a person to approve the result.

The part we like most is `--check`. Two agents can make changes that never
touch the same file and still break each other: one renames a function, the
other adds a new call to the old name. With `--check "pytest -q"`, agentsnap
builds the merged result in a scratch snapshot, runs your tests there, and
refuses the merge if they fail. Your real repo never sees the broken state.

## Works with the agent you already use

agentsnap ships an MCP server, so Claude Code, Cursor, Codex or any other MCP
client can create a workspace, check conflicts and merge on their own:

```bash
claude mcp add agentsnap -- agentsnap-mcp --repo "$(pwd)"
```

Or drive it from the shell: `agentsnap run fix-login -- claude -p "fix the login bug"`.

## Try it

```bash
git clone https://github.com/OWNER/agentsnap && cd agentsnap
pip install -e '.[mcp]'
./examples/demo.sh
```

It is alpha software, built as a course project for Open Source Software
(Fall 2026) by Bhushan, Arvind and Prem. We would love bug reports, especially
from people running several agents on real projects. Tell us what broke.

---

*Before publishing:* replace `OWNER`, settle the package name (the PyPI name
`agentsnap` is taken), re-run the benchmark on the release build, and add a
link to the demo video.
