"""MCP server exposing agentsnap to coding agents.

Area owner: MCP server (Prem).

Any MCP client (Claude Code, Cursor, Codex, ...) can use it with no changes
to the client. Register it with Claude Code, for example::

    claude mcp add agentsnap -- agentsnap-mcp --repo /path/to/repo

Typical agent flow:

1. ``create_workspace("fix-login")``: returns a directory to work in.
2. Edit files under that directory with the agent's normal tools. Reading
   through ``read_file`` (or a read hook) lets agentsnap warn about stale reads.
3. ``check_conflicts()`` then ``merge_workspace("fix-login")``.

Requires the optional MCP SDK (``pip install mcp``, or install this package with the ``[mcp]`` extra).
"""

from __future__ import annotations

import argparse
import functools
import os
import sys
from pathlib import Path

from . import __version__
from .conflicts import check
from .merge import Policy, merge_workspace
from .workspace import Store, Workspace, WorkspaceError, find_repo

INSTRUCTIONS = """\
agentsnap gives each agent its own copy-on-write snapshot of the repository so
several agents can work in parallel without overwriting each other.

Before editing code, call create_workspace with a short task name and do ALL of
your reads and edits inside the returned directory, never in the main repo.
Git works inside the workspace but is private to it: your commits there are
not merged. agentsnap merges the files you changed.
When done, call check_conflicts and then merge_workspace. If a merge reports
conflicts, do not force it: either retry with policy "merge3", or use policy
"rerun" to get a fresh snapshot with the other agents' changes and redo the
task there, or ask the user.

After a rerun, your next merge reports "awaiting_approval": a person must
review it. Tell the user to run `agentsnap merge <name> --approve`; you cannot
approve it yourself. If the user configured a merge check (for example the test
suite), a merge whose check fails is not applied; read the check output and fix
the problem in your workspace.
"""

MAX_READ_BYTES = 2_000_000


def _sdk():
    """(server class, ToolError class) for whichever MCP SDK is installed."""
    try:  # MCP Python SDK 2.x
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError

        return MCPServer, ToolError
    except ImportError:
        pass
    try:  # MCP Python SDK 1.x
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        return FastMCP, ToolError
    except ImportError as exc:
        raise SystemExit("agentsnap-mcp needs the MCP SDK: pip install mcp") from exc


def _resolve_in(ws: Workspace, rel: str) -> Path:
    """Map a workspace-relative path to an absolute one, refusing to escape the tree."""
    root = ws.tree.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise WorkspaceError(f"path {rel!r} is outside workspace {ws.name!r}")
    return p


def build_server(repo: Path):
    store = Store(repo)
    server_cls, tool_error = _sdk()
    try:
        server = server_cls(name="agentsnap", instructions=INSTRUCTIONS, version=__version__)
    except TypeError:  # older SDKs take no version
        server = server_cls(name="agentsnap", instructions=INSTRUCTIONS)

    def tool(**kwargs):
        """server.tool(), but expected errors reach the agent with their message."""

        def register(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kw):
                try:
                    return fn(*args, **kw)
                except (WorkspaceError, ValueError, OSError) as exc:
                    raise tool_error(str(exc)) from exc

            return server.tool(**kwargs)(wrapper)

        return register

    def info(ws: Workspace) -> dict:
        return {"name": ws.name, "path": str(ws.tree), "method": ws.method, "attempts": ws.attempts}

    @tool()
    def create_workspace(name: str) -> dict:
        """Create an isolated copy-on-write workspace for one task.

        Returns the absolute directory the agent must work in. Name: letters,
        digits, '.', '_' or '-'.
        """
        ws = store.create(name)
        return {**info(ws), "note": "Work only inside `path`. Merge with merge_workspace when done."}

    @tool()
    def list_workspaces() -> dict:
        """List all workspaces with counts of added/modified/deleted files."""
        out = []
        for ws in store.list():
            cs = ws.changes()
            counts = {"added": len(cs.added), "modified": len(cs.modified), "deleted": len(cs.deleted)}
            out.append({**info(ws), **counts})
        return {"workspaces": out}

    @tool()
    def workspace_status(name: str) -> dict:
        """Files this workspace added, modified and deleted relative to its snapshot."""
        ws = store.get(name)
        return {**info(ws), "changes": ws.changes().to_dict(), "reads": sorted(ws.reads.paths())}

    @tool()
    def workspace_diff(name: str, paths: list[str] | None = None) -> str:
        """Unified diff of the workspace against its snapshot (optionally only some paths)."""
        return store.get(name).diff(paths) or "(no changes)"

    @tool()
    def read_file(workspace: str, path: str) -> str:
        """Read a file from a workspace and record the read for stale-read detection.

        `path` is relative to the workspace root.
        """
        ws = store.get(workspace)
        p = _resolve_in(ws, path)
        data = p.read_bytes()[:MAX_READ_BYTES]
        ws.reads.record([p.relative_to(ws.tree.resolve()).as_posix()])
        return data.decode("utf-8", errors="replace")

    @tool()
    def write_file(workspace: str, path: str, content: str) -> str:
        """Write a text file inside a workspace (creates parent directories)."""
        ws = store.get(workspace)
        p = _resolve_in(ws, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {path}"

    @tool()
    def record_reads(workspace: str, paths: list[str]) -> str:
        """Tell agentsnap which workspace files you read with your own tools."""
        ws = store.get(workspace)
        rels = [_resolve_in(ws, p).relative_to(ws.tree.resolve()).as_posix() for p in paths]
        ws.reads.record(rels)
        return f"recorded {len(rels)} read(s)"

    @tool()
    def check_conflicts(workspaces: list[str] | None = None) -> dict:
        """Report write-write overlaps, stale writes and stale reads across workspaces."""
        return check(store, workspaces).to_dict()

    @tool(name="merge_workspace")
    def merge_tool(name: str, policy: str = "human", dry_run: bool = False, commit: bool = False) -> dict:
        """Merge a workspace into the repo, all-or-nothing.

        policy: "human" (stop on conflict), "merge3" (try a line-level 3-way
        merge), "rerun" (on conflict, re-snapshot so you can redo the task),
        "force" (overwrite; only with the user's explicit approval).

        The result says whether it merged. If "awaiting_approval" is set, ask
        the user to run `agentsnap merge <name> --approve`. If "check" shows a
        non-zero returncode, the user's merge check failed on the merged result.
        """
        return merge_workspace(store, name, policy=Policy(policy), dry_run=dry_run, commit=commit).to_dict()

    @tool()
    def refresh_workspace(name: str) -> dict:
        """Re-snapshot a workspace on the latest repo; the old tree is kept under attempts/."""
        return info(store.refresh(name))

    @tool()
    def drop_workspace(name: str, force: bool = False) -> str:
        """Delete a workspace. Refuses if it has unmerged changes unless force=True."""
        ws = store.get(name)
        if ws.changes() and not force:
            raise WorkspaceError(f"{name!r} has unmerged changes; merge first or pass force=True")
        store.drop(name)
        return f"dropped {name}"

    # Keep a handle for tests.
    server.agentsnap_store = store  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentsnap-mcp", description="agentsnap MCP server (stdio)")
    parser.add_argument("--repo", type=Path, default=None, help="repo root (default: $AGENTSNAP_REPO or cwd)")
    args = parser.parse_args(argv)
    repo = args.repo or (
        Path(os.environ["AGENTSNAP_REPO"]) if "AGENTSNAP_REPO" in os.environ else find_repo()
    )
    print(f"agentsnap MCP server for {repo}", file=sys.stderr)
    build_server(repo).run()


if __name__ == "__main__":  # pragma: no cover
    main()
