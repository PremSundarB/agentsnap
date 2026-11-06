# Roadmap

The schedule follows the work plan in the October 9 proposal deck
([docs/proposal/](proposal/)). Status as of November 5, 2026.

| due | milestone | owner | status |
|---|---|---|---|
| Sep 25 | Proposal; public repo, license, README, CI skeleton | all | ✅ repo, MIT license, README, CI ready locally. **To do: push to a public GitHub repo** |
| Oct 25 | **Snapshots**: copy-on-write with APFS clones (macOS) and reflink (Linux), plain-copy fallback; tests showing many agents editing in isolation | Bhushan | ✅ done (`snapshot.py`, `tests/test_snapshot.py`, `tests/test_isolation.py`). Overlayfs moved to stretch goals |
| Nov 5 | **Conflict detection**: track files each agent reads and writes; flag overlaps before merging | Arvind | ✅ done (`tracking.py`, `conflicts.py`). Python function-level detection done early (`symbols.py`) |
| Nov 20 | **Merging**: auto-merge clean changes; on conflict an agent reruns on top of the other change and a person approves | Bhushan | ✅ done (`merge.py`: `human`, `merge3`, `rerun`, `force`; approval gate after rerun; merge checks on the merged result) |
| Nov 30 | **Launch**: MCP server for Claude Code, Cursor and Codex; blog post, demo video, forum posts; collect issues from outside users | Prem | 🟡 MCP server done and tested. To do: PyPI release, blog post, demo video, posts |
| Dec 1-4 | **Wrap-up**: final presentation, report, maintenance plan | all | ⬜ not started |

## Next steps, in priority order

1. **Publish the repo.** Create the GitHub repo, push, and replace `OWNER` in
   the README badge and links. Check that CI is green on both OSes.
2. **Pick a package name, then do the first PyPI release (0.1.0).** ⚠️ The
   name `agentsnap` is **already taken on PyPI** by an unrelated project
   ("Deterministic snapshot testing harness for AI agents", checked
   2026-11-05). Names that were free on that date: `agentlanes`, `agentcow`,
   `agent-snap`, `agentshard`, `snapmerge`. Renaming is mechanical: the package
   directory, `name` and scripts in `pyproject.toml`, the `.agentsnap` state
   directory, and docs. Then add the repo as a trusted publisher on PyPI and
   `git tag v0.1.0 && git push --tags` (see `.github/workflows/release.yml`).
3. **Try it with real agents.** Run `examples/claude-code/parallel_tasks.sh` on
   a real project with 3 to 5 Claude Code agents. Record what breaks and file issues.
4. ~~Approval flow for agent-resolved conflicts~~ ✅ done (`merge --approve`;
   not exposed over MCP).
5. ~~Run tests before accepting a merge~~ ✅ done (`merge --check`,
   `merge_check` in config).
6. **Launch materials**: blog post, a 2-minute demo video (use
   `examples/demo.sh`), posts to relevant forums. Collect outside issues.
7. **Final report**: include `docs/benchmarks.md` numbers on macOS and Linux.
8. **Latency on large repos**: get the 13k-file case under 100 ms (pre-warmed
   workspace pool, or lazy `base/` in the MCP server).

## Stretch goals

- Overlayfs (rootless) or `fuse-overlayfs` for real CoW on ext4.
- Kernel-level read tracking (fanotify / Endpoint Security).
- Tree-sitter for function-level conflicts in JS/TS, Go, Rust and others.
- Windows support.
- A small TUI or web dashboard showing live workspaces and conflicts.

## Maintenance plan after the course

- Keep `main` releasable. Every change goes through a PR with green CI and
  one review.
- Triage issues weekly. Label `good first issue` generously.
- Semantic versioning. Record changes in `CHANGELOG.md`.
- Revisit MCP SDK compatibility on each major SDK release (we already support
  1.x and 2.x).
