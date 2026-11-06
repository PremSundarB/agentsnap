"""Benchmark: how fast and how small are agent workspaces?

Measures the proposal's latency and storage metrics on a synthetic repo:

* time to create one workspace (median / p95 / max over N agents)
* extra disk space used by N workspaces (free-space delta on the volume)

and compares three ways of giving N agents their own copy:

* ``agentsnap`` with the platform's best snapshot method (APFS clone/reflink)
* ``agentsnap`` forced to plain copies (the fallback)
* ``git worktree add`` (today's recommended approach). Worktrees don't
  include untracked build caches like node_modules, so we also report the
  time it takes to copy that cache in, which is what a real
  ``npm install`` / build per worktree would replace.

Usage::

    python benchmarks/bench_workspaces.py                # defaults
    python benchmarks/bench_workspaces.py --agents 10 --src-files 3000 --cache-files 5000
    python benchmarks/bench_workspaces.py --json results.json

The scratch directory must be on the volume you care about (APFS on macOS,
btrfs/XFS on Linux for reflinks). Free-space deltas are approximate: other
processes writing to the same disk add noise.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentsnap.snapshot import available_methods  # noqa: E402
from agentsnap.workspace import Store  # noqa: E402


def make_repo(root: Path, src_files: int, src_kb: int, cache_files: int, cache_kb: int) -> int:
    """Create a git repo with source files and an untracked build cache. Returns bytes written."""
    rng = os.urandom
    total = 0
    for i in range(src_files):
        p = root / f"src/pkg{i % 40}/mod{i}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        data = rng(src_kb * 512).hex().encode()  # text-ish, src_kb KiB
        p.write_bytes(data)
        total += len(data)
    for i in range(cache_files):
        p = root / f"node_modules/dep{i % 100}/file{i}.js"
        p.parent.mkdir(parents=True, exist_ok=True)
        data = rng(cache_kb * 1024)
        p.write_bytes(data)
        total += len(data)
    (root / ".gitignore").write_text("node_modules/\n.agentsnap/\n")
    git = [
        "git",
        "-C",
        str(root),
        "-c",
        "user.email=b@b",
        "-c",
        "user.name=bench",
        "-c",
        "commit.gpgsign=false",
    ]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    # Pack objects, as in any cloned or gc'd repo. Otherwise .git holds one
    # loose file per object, which inflates the cost of giving each workspace
    # its private copy of .git.
    subprocess.run([*git, "gc", "-q"], check=True)
    return total


def free_bytes(path: Path) -> int:
    os.sync()
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def settled_free_bytes(path: Path, tolerance: int = 4 << 20, timeout: float = 30.0) -> int:
    """Free space once it stops moving.

    Filesystems such as APFS reclaim deleted files asynchronously, so right
    after cleaning up a previous run the free space keeps growing. Measuring
    then would under-count the next run's usage.
    """
    deadline = time.monotonic() + timeout
    last = free_bytes(path)
    while time.monotonic() < deadline:
        time.sleep(0.5)
        now = free_bytes(path)
        if abs(now - last) <= tolerance:
            return now
        last = now
    return last


def summarize(times_ms: list[float]) -> dict:
    s = sorted(times_ms)
    return {
        "median_ms": round(statistics.median(s), 1),
        "p95_ms": round(s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))], 1),
        "max_ms": round(s[-1], 1),
    }


def bench_agentsnap(repo: Path, agents: int, method: str | None) -> dict:
    store = Store(repo)
    store.init()
    before = settled_free_bytes(repo)
    times = []
    for i in range(agents):
        t0 = time.perf_counter()
        ws = store.create(f"agent-{i}", method=method)
        times.append((time.perf_counter() - t0) * 1000)
    used = before - settled_free_bytes(repo)
    result = {"method": ws.method, **summarize(times), "disk_used_bytes": used}
    # cleanup
    shutil.rmtree(store.ws_root)
    return result


def bench_worktree(repo: Path, agents: int) -> dict:
    wt_root = repo.parent / "worktrees"
    wt_root.mkdir()
    before = settled_free_bytes(repo)
    checkout, cache = [], []
    for i in range(agents):
        dst = wt_root / f"agent-{i}"
        t0 = time.perf_counter()
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(dst)],
            check=True,
            capture_output=True,
        )
        t1 = time.perf_counter()
        # Stand-in for reinstalling dependencies / rebuilding in each worktree.
        if (repo / "node_modules").exists():
            shutil.copytree(repo / "node_modules", dst / "node_modules")
        t2 = time.perf_counter()
        checkout.append((t1 - t0) * 1000)
        cache.append((t2 - t0) * 1000)
    used = before - settled_free_bytes(repo)
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], check=False)
    shutil.rmtree(wt_root)
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], check=False)
    return {
        "method": "git worktree",
        "checkout_only": summarize(checkout),
        **summarize(cache),
        "disk_used_bytes": used,
    }


def fmt_bytes(n: int) -> str:
    n = max(n, 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agents", type=int, default=10)
    ap.add_argument("--src-files", type=int, default=2000)
    ap.add_argument("--src-kb", type=int, default=8, help="KiB per source file")
    ap.add_argument("--cache-files", type=int, default=3000, help="untracked build-cache files")
    ap.add_argument("--cache-kb", type=int, default=32, help="KiB per cache file")
    ap.add_argument("--dir", type=Path, default=None, help="scratch directory (default: system temp)")
    ap.add_argument("--skip-copy", action="store_true", help="skip the slow plain-copy run")
    ap.add_argument("--json", type=Path, help="also write results as JSON")
    args = ap.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="agentsnap-bench-", dir=args.dir))
    try:
        repo = scratch / "repo"
        repo.mkdir()
        size = make_repo(repo, args.src_files, args.src_kb, args.cache_files, args.cache_kb)
        print(
            f"repo: {args.src_files} source files + {args.cache_files} cache files, {fmt_bytes(size)}; "
            f"{args.agents} agents; {platform.system()} {platform.machine()}"
        )

        runs = []
        best = available_methods()[0]
        runs.append(("agentsnap", bench_agentsnap(repo, args.agents, None)))
        if best != "copy" and not args.skip_copy:
            runs.append(("agentsnap (copy)", bench_agentsnap(repo, args.agents, "copy")))
        runs.append(("git worktree + cache", bench_worktree(repo, args.agents)))

        full_copies = size * args.agents
        print()
        n = args.agents
        print(f"| approach | method | median | p95 | max | disk for {n} agents | % of {n} full copies |")
        print("|---|---|---|---|---|---|---|")
        for label, r in runs:
            share = 100 * r["disk_used_bytes"] / full_copies if full_copies else 0
            print(
                f"| {label} | {r['method']} | {r['median_ms']} ms | {r['p95_ms']} ms | {r['max_ms']} ms "
                f"| {fmt_bytes(r['disk_used_bytes'])} | {share:.0f}% |"
            )
        wt = runs[-1][1]["checkout_only"]
        print(f"\n(git worktree checkout alone, without the build cache: median {wt['median_ms']} ms)")

        if args.json:
            args.json.write_text(
                json.dumps(
                    {
                        "params": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                        "repo_bytes": size,
                        "platform": platform.platform(),
                        "results": dict(runs),
                    },
                    indent=2,
                )
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
