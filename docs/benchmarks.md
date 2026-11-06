# Benchmarks

Produced with [`benchmarks/bench_workspaces.py`](../benchmarks/bench_workspaces.py).
Each run builds a synthetic git repo with tracked source files plus an
untracked build cache (`node_modules/`), then gives 10 agents their own copy
in three ways:

- **agentsnap**: the best snapshot method for the platform (APFS clone on macOS).
- **agentsnap (copy)**: forced plain copies, the fallback on filesystems without CoW.
- **git worktree + cache**: `git worktree add` plus copying the build cache
  in. The copy stands in for the `npm install` / build each new worktree needs.
  A real install or build would usually be *slower* than a copy, so this
  baseline is generous to worktrees.

"Disk" is the drop in free space on the volume after creating all 10
(measured once free space stops moving, because APFS reclaims space lazily).
"% of full copies" compares that with 10 × the repo size.

Reproduce:

```bash
python benchmarks/bench_workspaces.py                                   # default repo
python benchmarks/bench_workspaces.py --src-files 5000 --cache-files 8000 --skip-copy   # larger
```

## macOS 26.3, Apple M4, APFS (November 5, 2026)

Includes giving each workspace its private git repo. The synthetic repo's
objects are packed (`git gc`), as in any cloned repo.

### Default repo: 2,000 source files + 3,000 cache files, 109 MiB

| approach | median | p95 | disk for 10 agents | % of 10 full copies |
|---|---|---|---|---|
| **agentsnap (APFS clone)** | **54 ms** | 81 ms | **22 MiB** | **2%** |
| agentsnap (plain copy) | 1,123 ms | 1,187 ms | 1,290 MiB | 118% |
| git worktree + cache | 634 ms | 701 ms | 1,122 MiB | 103% |
| *git worktree, checkout only* | *198 ms* | *256 ms* | | |

### Larger repo: 5,000 source files + 8,000 cache files, 289 MiB

| approach | median | p95 | disk for 10 agents | % of 10 full copies |
|---|---|---|---|---|
| **agentsnap (APFS clone)** | **146 ms** | 292 ms | **55 MiB** | **2%** |
| git worktree + cache | 2,285 ms | 2,367 ms | 2,947 MiB | 102% |
| *git worktree, checkout only* | *506 ms* | *580 ms* | | |

## Linux (Debian 13, Python 3.11, Docker Desktop VM on the same Mac)

These ran in a Linux VM, so absolute timings are only indicative. The
comparison between rows is what matters.

### XFS with reflink (loop device, `mkfs.xfs -m reflink=1`): default repo

| approach | median | p95 | disk for 10 agents | % of 10 full copies |
|---|---|---|---|---|
| **agentsnap (reflink)** | **99 ms** | 128 ms | **42 MiB** | **4%** |
| agentsnap (plain copy) | 194 ms | 338 ms | 1.2 GiB | 118% |
| git worktree + cache | 182 ms | 283 ms | 1.1 GiB | 103% |
| *git worktree, checkout only* | *93 ms* | | | |

### overlayfs (Docker's default; no CoW): default repo

`agentsnap doctor` reports `reflink` as unavailable here, so agentsnap uses
`cp -a`.

| approach | median | p95 | disk for 10 agents | % of 10 full copies |
|---|---|---|---|---|
| agentsnap (`cp -a`) | 142 ms | 186 ms | 1.2 GiB | 115% |
| agentsnap (Python copy) | 196 ms | 230 ms | 1.2 GiB | 115% |
| git worktree + cache | 175 ms | 223 ms | 1.1 GiB | 101% |
| *git worktree, checkout only* | *95 ms* | | | |

On filesystems without CoW, agentsnap still gives isolation and conflict
safety, and is about as fast as a worktree with its dependencies, but it does
not save disk. Overlayfs-based snapshots (on the roadmap) would fix that.

## What the numbers say

- **Storage target met (≥ 90% less disk) wherever the filesystem has CoW:**
  10 workspaces cost 2% (APFS) to 4% (XFS reflink) of 10 full copies. The cost
  is directory metadata, not file data. ext4 and overlayfs have no CoW, so
  there it costs full copies.
- **Latency target (< 100 ms) met for the default repo; not yet for 13k files.**
  Cloning cost grows with the *number of files*, not their size. The larger
  repo takes 146 ms. Parallel `clonefile` calls did not help in our tests
  (APFS serialises them). Ideas to get under 100 ms: create `base/` lazily
  after the agent starts (MCP server only), or pre-warm a pool of spare
  workspaces.
- Against `git worktree`, agentsnap is about **12×** faster on the default repo
  and **16×** on the larger one once the build cache is included, and it is
  still faster than a bare checkout.
- The plain-copy fallback is slower than a worktree, because it copies the
  build cache too, but agents then need no install or build step. It also
  stores `base/` (tracked files only), hence 118%.

## History

- *v0.1 development*: hashing every file at creation took about 900 ms on the
  larger repo. Switching to git-style `lstat` signatures brought it to about
  210 ms. Skipping ignored directories (such as `node_modules`) in the pristine
  `base/` copy brought it to 142 ms. Adding a private git repo per workspace
  costs little when objects are packed (146 ms). With 5,000 *loose* objects it
  rose to about 190 ms.
