"""End-to-end checks for the success metrics in the proposal:

* Isolation: 10 agents editing the same repo at once, 0 overwrites.
* Conflict detection: 100% of overlapping edits caught before merge,
  0 conflicts merged without approval.
"""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from agentsnap.conflicts import check
from agentsnap.merge import merge_workspace

from .conftest import read, write

N = 10


def agent(store, i):
    """Simulated agent: edits a shared file and its own file, many times."""
    ws = store.get(f"agent-{i}")
    for step in range(20):
        write(ws.tree, "shared.txt", f"agent {i} step {step}\n")
        write(ws.tree, f"own/agent_{i}.txt", f"agent {i} step {step}\n")
    return i


def test_parallel_agents_never_overwrite_each_other(store, repo):
    write(repo, "shared.txt", "original\n")
    for i in range(N):
        store.create(f"agent-{i}")
    with ThreadPoolExecutor(N) as pool:
        list(pool.map(lambda i: agent(store, i), range(N)))

    # Every agent sees exactly its own final writes; the repo is untouched.
    for i in range(N):
        ws = store.get(f"agent-{i}")
        assert read(ws.tree, "shared.txt") == f"agent {i} step 19\n"
        assert ws.changes().paths == {"shared.txt", f"own/agent_{i}.txt"}
    assert read(repo, "shared.txt") == "original\n"

    # Every overlap is caught before anything merges.
    report = check(store)
    (overlap,) = [c for c in report.errors if c.kind == "write-write"]
    assert overlap.path == "shared.txt" and len(overlap.workspaces) == N

    # Merging: first committer wins, the other N-1 are refused, nothing lost.
    results = [merge_workspace(store, f"agent-{i}") for i in range(N)]
    assert [r.merged for r in results] == [True] + [False] * (N - 1)
    assert read(repo, "shared.txt") == "agent 0 step 19\n"
    assert all(not (repo / f"own/agent_{i}.txt").exists() for i in range(1, N))


def test_concurrent_merges_from_separate_processes(store, repo):
    """The merge lock must hold across processes, not just threads."""
    for i in range(N):
        ws = store.create(f"agent-{i}")
        write(ws.tree, f"own/agent_{i}.txt", f"{i}\n")  # disjoint
        write(ws.tree, "shared.txt", f"agent {i}\n")  # everyone adds the same new file
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "agentsnap", "-C", str(repo), "merge", f"agent-{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(N)
    ]
    codes = [p.wait(timeout=60) for p in procs]
    assert sorted(codes) == [0] + [1] * (N - 1)  # exactly one winner
    winner = codes.index(0)
    assert read(repo, "shared.txt") == f"agent {winner}\n"
    assert read(repo, f"own/agent_{winner}.txt") == f"{winner}\n"
    # losers were all-or-nothing: their disjoint files were not merged either
    assert sorted(p.name for p in (repo / "own").iterdir()) == [f"agent_{winner}.txt"]


def test_disjoint_agents_all_merge(store, repo):
    for i in range(N):
        ws = store.create(f"agent-{i}")
        write(ws.tree, f"own/agent_{i}.txt", f"{i}\n")
    assert check(store).conflicts == []
    with ThreadPoolExecutor(N) as pool:
        results = list(pool.map(lambda i: merge_workspace(store, f"agent-{i}"), range(N)))
    assert all(r.merged for r in results)
    assert len(list((repo / "own").iterdir())) == N


def test_merge3_lets_all_agents_land_disjoint_line_edits(store, repo):
    # Each agent edits its own line. Lines are 3 apart because git merge-file
    # (like git itself) treats edits to adjacent lines as one conflicting hunk.
    lines = [f"line {k}\n" for k in range(3 * N)]
    write(repo, "doc.txt", "".join(lines))
    for i in range(N):
        ws = store.create(f"agent-{i}")
        mine = list(lines)
        mine[3 * i + 1] = f"line {3 * i + 1} edited by agent {i}\n"
        write(ws.tree, "doc.txt", "".join(mine))
    results = [merge_workspace(store, f"agent-{i}", policy="merge3") for i in range(N)]
    assert all(r.merged for r in results)
    assert [r.applied["doc.txt"] for r in results] == ["modified"] + ["merged"] * (N - 1)
    text = read(repo, "doc.txt")
    assert all(f"edited by agent {i}" in text for i in range(N))
    assert len(text.splitlines()) == 3 * N
