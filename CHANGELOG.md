# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-05

First version.

### Added
- Copy-on-write workspaces: APFS `clonefile` on macOS, reflinks on Linux
  (btrfs/XFS), with `cp -a` and portable-copy fallbacks.
- Change tracking with `lstat` signatures plus content-hash confirmation.
- Read tracking through the MCP server and a Claude Code hook helper
  (`agentsnap record-read`).
- Conflict detection: write-write, stale-write, stale-read and read-write,
  with function-level detail for Python.
- All-or-nothing merges under a cross-process lock, with `human`, `merge3`,
  `rerun` and `force` policies, and optional `git commit`.
- Approval gate: after a `rerun`, a person approves the agent's resolution
  (`merge --approve`).
- Merge checks: `merge --check CMD` / `merge_check` runs a command on the
  merged result in a scratch snapshot and refuses the merge if it fails.
- Refuses to merge through symlinked parent directories that point outside
  the repo.
- `agentsnap` CLI and `agentsnap-mcp` MCP server (MCP SDK 1.x and 2.x).
- Benchmarks, demo script, Claude Code integration examples, CI for macOS and Linux.
