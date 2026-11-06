"""Command line interface: ``agentsnap <command>``.

Area owner: CLI (Prem).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .conflicts import check
from .merge import Policy, merge_workspace
from .workspace import Store, Workspace, WorkspaceError, find_repo

EXIT_OK = 0
EXIT_CONFLICT = 1
EXIT_USAGE = 2


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _ws_dict(ws: Workspace, with_changes: bool = True) -> dict:
    d = {
        "name": ws.name,
        "path": str(ws.tree),
        "method": ws.method,
        "created_at": ws.created_at,
        "attempts": ws.attempts,
        "merges": len(ws.merges),
    }
    if with_changes:
        d["changes"] = ws.changes().to_dict()
    return d


def _age(ts: float) -> str:
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
    return f"{s}s ago"


# -- commands ----------------------------------------------------------------


def cmd_init(store: Store, args) -> int:
    store.init()
    print(f"initialised {store.root}")
    return EXIT_OK


def cmd_new(store: Store, args) -> int:
    for name in args.names:
        t0 = time.perf_counter()
        ws = store.create(name, method=args.method)
        ms = (time.perf_counter() - t0) * 1000
        if args.json:
            _print_json({**_ws_dict(ws, with_changes=False), "elapsed_ms": round(ms, 1)})
        elif args.quiet:
            print(ws.tree)
        else:
            print(f"created {ws.name} ({ws.method}, {ms:.0f} ms)")
            print(f"  work in: {ws.tree}")
    return EXIT_OK


def cmd_list(store: Store, args) -> int:
    wss = store.list()
    if args.json:
        _print_json([_ws_dict(ws) for ws in wss])
        return EXIT_OK
    if not wss:
        print("no workspaces (create one with `agentsnap new NAME`)")
        return EXIT_OK
    width = max(len(ws.name) for ws in wss)
    for ws in wss:
        cs = ws.changes()
        summary = f"+{len(cs.added)} ~{len(cs.modified)} -{len(cs.deleted)}"
        print(f"{ws.name:<{width}}  {summary:<14} {ws.method:<7} {_age(ws.created_at):<9} {ws.tree}")
    return EXIT_OK


def cmd_path(store: Store, args) -> int:
    print(store.get(args.name).tree)
    return EXIT_OK


def cmd_status(store: Store, args) -> int:
    wss = [store.get(args.name)] if args.name else store.list()
    if args.json:
        _print_json([_ws_dict(ws) for ws in wss])
        return EXIT_OK
    for ws in wss:
        cs = ws.changes()
        print(f"{ws.name}: {len(cs)} changed file(s)")
        for path in sorted(cs.paths):
            mark = {"added": "A", "modified": "M", "deleted": "D"}[cs.kind_of(path) or ""]
            print(f"  {mark} {path}")
        reads = ws.reads.paths()
        if reads:
            print(f"  ({len(reads)} file(s) read)")
    return EXIT_OK


def cmd_diff(store: Store, args) -> int:
    sys.stdout.write(store.get(args.name).diff(args.paths or None))
    return EXIT_OK


def cmd_conflicts(store: Store, args) -> int:
    report = check(store, args.names or None)
    if args.json:
        _print_json(report.to_dict())
    else:
        if not report.conflicts:
            n = len(report.changes)
            print(f"no conflicts across {n} workspace(s)")
        for c in report.errors:
            print(f"CONFLICT  {c.kind:<11} {c.describe()}")
        for c in report.warnings:
            print(f"warning   {c.kind:<11} {c.describe()}")
    return EXIT_CONFLICT if report.errors else EXIT_OK


def cmd_merge(store: Store, args) -> int:
    rc = EXIT_OK
    for name in args.names:
        try:
            res = merge_workspace(
                store,
                name,
                policy=args.policy,
                dry_run=args.dry_run,
                commit=args.commit,
                message=args.message,
                approve=args.approve,
                check=args.check,
                check_timeout=args.check_timeout,
            )
        except OSError as exc:
            print(f"{name}: merge failed and was rolled back, repo unchanged: {exc}", file=sys.stderr)
            rc = EXIT_CONFLICT
            continue
        if args.json:
            _print_json(res.to_dict())
        else:
            _print_merge(res)
        if res.conflicts or res.awaiting_approval or not res.check_passed:
            rc = EXIT_CONFLICT
    return rc


def _print_merge(res) -> None:
    name = res.workspace
    verb = "would merge" if res.dry_run else "merged"
    if res.conflicts:
        print(f"{name}: NOT merged, {len(res.conflicts)} conflict(s) (policy: {res.policy})")
        for c in res.conflicts:
            print(f"  CONFLICT {c.describe()}")
        if res.refreshed:
            print("  workspace re-snapshotted on the latest repo; previous tree kept in attempts/.")
            print("  re-run the agent's task in the workspace, then merge again (a person approves).")
        elif res.policy == "human":
            print("  options: --policy merge3 (line-level merge), --policy rerun (redo on latest),")
            print("           --policy force (overwrite; only after review), or edit by hand.")
    elif res.awaiting_approval:
        print(f"{name}: NOT merged, waiting for approval")
        print(f"  this workspace resolved earlier conflicts in: {', '.join(res.awaiting_approval)}")
        print(f"  review with `agentsnap diff {name}`, then `agentsnap merge {name} --approve`")
    elif not res.check_passed:
        assert res.check is not None
        print(f"{name}: NOT merged, check failed (exit {res.check['returncode']}): {res.check['command']}")
        tail = res.check["output"].strip().splitlines()[-15:]
        for line in tail:
            print(f"  | {line}")
    elif not res.applied:
        print(f"{name}: nothing to merge")
    else:
        print(f"{name}: {verb} {len(res.applied)} file(s)")
        for path, how in sorted(res.applied.items()):
            print(f"  {how:<10} {path}")
        if res.check:
            print(f"  check passed: {res.check['command']}")
        if res.commit:
            print(f"  committed {res.commit[:12]}")
        elif res.commit_error:
            print(f"  not committed: {res.commit_error}")
    for c in res.warnings:
        print(f"  warning {c.describe()}")


def cmd_refresh(store: Store, args) -> int:
    ws = store.refresh(args.name)
    print(
        f"{ws.name}: re-snapshotted (attempt {ws.attempts} kept in {ws.dir / 'attempts' / str(ws.attempts)})"
    )
    return EXIT_OK


def cmd_drop(store: Store, args) -> int:
    rc = EXIT_OK
    for name in args.names:
        ws = store.get(name)
        cs = ws.changes()
        if cs and not args.force:
            print(f"{name}: has {len(cs)} unmerged change(s); merge it or use --force", file=sys.stderr)
            rc = EXIT_CONFLICT
            continue
        store.drop(name)
        print(f"dropped {name}")
    return rc


def cmd_run(store: Store, args) -> int:
    ws = store.get(args.name)
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        print("usage: agentsnap run NAME -- COMMAND [ARGS...]", file=sys.stderr)
        return EXIT_USAGE
    env = {**os.environ, "AGENTSNAP_WORKSPACE": ws.name, "AGENTSNAP_REPO": str(store.repo)}
    return subprocess.call(cmd, cwd=ws.tree, env=env)


def cmd_record_read(store: Store, args) -> int:
    """Record reads. With no paths, parse a Claude Code hook payload from stdin."""
    paths = list(args.paths)
    cwd = Path.cwd()
    if not paths:
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError:
            return EXIT_OK  # never break the agent's tool call
        tool_input = payload.get("tool_input") or {}
        cwd = Path(payload.get("cwd") or cwd)
        for key in ("file_path", "path", "notebook_path"):
            if isinstance(tool_input.get(key), str):
                paths.append(tool_input[key])
    recorded = 0
    for raw in paths:
        p = Path(raw)
        p = p if p.is_absolute() else cwd / p
        if args.workspace:
            ws = store.get(args.workspace)
            try:
                rel = p.resolve().relative_to(ws.tree.resolve()).as_posix()
            except ValueError:
                continue
        else:
            hit = store.workspace_for_path(p)
            if hit is None:
                continue
            ws, rel = hit
        if (ws.tree / rel).is_file():
            ws.reads.record([rel])
            recorded += 1
    if args.verbose:
        print(f"recorded {recorded} read(s)")
    return EXIT_OK


def cmd_doctor(store: Store, args) -> int:
    """Report which snapshot methods work for this repo and what config is active."""
    from .snapshot import available_methods, probe_methods

    print(f"repo:         {store.repo}")
    print(f"state dir:    {store.root} ({'exists' if store.root.exists() else 'not created yet'})")
    works = probe_methods(store.repo)
    for m in available_methods():
        mark = "ok " if m in works else "-- "
        print(f"  {mark} {m}")
    best = works[0] if works else "none"
    cow = best in ("apfs", "reflink")
    print(f"snapshots:    {best} ({'copy-on-write' if cow else 'full copies: no CoW on this filesystem'})")
    if store.config.snapshot_method:
        print(f"forced:       {store.config.snapshot_method} (config)")
    print(f"merge check:  {store.config.merge_check or '(none)'}")
    print(f"workspaces:   {len(store.list())}")
    return EXIT_OK


def cmd_mcp(store: Store, args) -> int:
    from .mcp_server import main as mcp_main

    mcp_main(["--repo", str(store.repo)])
    return EXIT_OK


# -- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentsnap",
        description="Run many coding agents on one repo: copy-on-write workspaces, "
        "conflict detection and safe merges.",
    )
    p.add_argument("--version", action="version", version=f"agentsnap {__version__}")
    p.add_argument("-C", "--repo", type=Path, help="repo root (default: discovered from cwd)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, "create the .agentsnap state directory")

    sp = add("new", cmd_new, "create workspace(s): a copy-on-write snapshot of the repo")
    sp.add_argument("names", nargs="+", metavar="NAME")
    sp.add_argument("--method", choices=["apfs", "reflink", "cp", "copy"], help="force a snapshot method")
    sp.add_argument("-q", "--quiet", action="store_true", help="print only the workspace path")
    sp.add_argument("--json", action="store_true")

    sp = add("list", cmd_list, "list workspaces and their change counts")
    sp.add_argument("--json", action="store_true")

    sp = add("path", cmd_path, "print a workspace's working directory")
    sp.add_argument("name")

    sp = add("status", cmd_status, "show files a workspace added/modified/deleted")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--json", action="store_true")

    sp = add("diff", cmd_diff, "unified diff of a workspace against its snapshot")
    sp.add_argument("name")
    sp.add_argument("paths", nargs="*")

    sp = add("conflicts", cmd_conflicts, "check workspaces for overlapping or stale changes")
    sp.add_argument("names", nargs="*", metavar="NAME")
    sp.add_argument("--json", action="store_true")

    sp = add("merge", cmd_merge, "merge workspace(s) into the repo (all-or-nothing each)")
    sp.add_argument("names", nargs="+", metavar="NAME")
    sp.add_argument("--policy", choices=[x.value for x in Policy], default=Policy.HUMAN.value)
    sp.add_argument("-n", "--dry-run", action="store_true")
    sp.add_argument("--commit", action="store_true", help="git commit the merged files")
    sp.add_argument("-m", "--message", help="commit message (with --commit)")
    sp.add_argument(
        "--approve", action="store_true", help="approve a workspace that resolved earlier conflicts"
    )
    sp.add_argument(
        "--check",
        metavar="CMD",
        help="shell command that must pass on the merged result first (default: merge_check in config)",
    )
    sp.add_argument("--check-timeout", type=float, metavar="SECS")
    sp.add_argument("--json", action="store_true")

    sp = add("refresh", cmd_refresh, "re-snapshot a workspace on the latest repo (keeps old tree)")
    sp.add_argument("name")

    sp = add("drop", cmd_drop, "delete workspace(s)")
    sp.add_argument("names", nargs="+", metavar="NAME")
    sp.add_argument("-f", "--force", action="store_true", help="drop even with unmerged changes")

    sp = add("run", cmd_run, "run a command (e.g. an agent) inside a workspace")
    sp.add_argument("name")
    sp.add_argument("cmd", nargs=argparse.REMAINDER)

    sp = add("record-read", cmd_record_read, "record files an agent read (hook helper)")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("-w", "--workspace")
    sp.add_argument("-v", "--verbose", action="store_true")

    add("doctor", cmd_doctor, "show which snapshot methods work here and the active config")
    add("mcp", cmd_mcp, "run the MCP server on stdio")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Store(args.repo if args.repo else find_repo())
    try:
        return args.fn(store, args)
    except WorkspaceError as exc:
        print(f"agentsnap: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
