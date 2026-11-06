# Using agentsnap with Claude Code

There are two ways to use agentsnap with Claude Code. You can use both.

## 1. As an MCP server (the agent manages its own workspace)

Register the server once per repo:

```bash
pip install -e '.[mcp]'   # from a clone of this repo
claude mcp add agentsnap -- agentsnap-mcp --repo "$(pwd)"
```

Or commit [`mcp.json`](mcp.json) to the repo as `.mcp.json`.

The server tells the agent to call `create_workspace` before it edits
anything, to work only inside the returned directory, and to finish with
`check_conflicts` and `merge_workspace`. Tools:

| tool | what it does |
|---|---|
| `create_workspace(name)` | new copy-on-write snapshot; returns the directory to work in |
| `list_workspaces()` | all workspaces with change counts |
| `workspace_status(name)` | added / modified / deleted files and recorded reads |
| `workspace_diff(name, paths?)` | unified diff against the snapshot |
| `read_file(workspace, path)` | read a file **and record the read** |
| `write_file(workspace, path, content)` | write a file inside the workspace |
| `record_reads(workspace, paths)` | report reads made with other tools |
| `check_conflicts(workspaces?)` | write-write, stale-write, stale-read, read-write |
| `merge_workspace(name, policy, dry_run, commit)` | all-or-nothing merge |
| `refresh_workspace(name)` | re-snapshot on the latest repo |
| `drop_workspace(name, force)` | delete a workspace |

## 2. From the shell (you orchestrate the agents)

[`parallel_tasks.sh`](parallel_tasks.sh) starts one headless `claude -p` per
task, each with its working directory set to its own workspace, waits for
them, reports conflicts and merges:

```bash
./parallel_tasks.sh "typos:Fix spelling in docs/" "tests:Add tests for src/util.py"
```

## Tracking reads with a hook

Conflicts on *writes* are found automatically. To also get warnings when an
agent made decisions based on a file that another agent has since changed,
agentsnap needs to know what the agent read. Add the `PostToolUse` hook in
[`settings.json`](settings.json) to `.claude/settings.json`. It pipes every
`Read` tool call to `agentsnap record-read`, which logs the file if it is
inside a workspace and ignores everything else. The hook never fails the
tool call.
