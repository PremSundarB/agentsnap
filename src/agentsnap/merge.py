"""Merging a workspace's changes back into the repo.

Area owner: merge engine (Bhushan).

A merge is all-or-nothing per workspace: either every changed file is
written back, or nothing is. Merges are serialised with a file lock, so two
agents finishing at the same moment cannot interleave their writes.

For each changed path we pick one of:

* ``clean``      - the repo still has the base version: copy the agent's file.
* ``convergent`` - the repo already has exactly the agent's version: no-op.
* ``stale``      - the repo moved on since the snapshot. What happens next
                   depends on the policy.

Policies for stale paths:

``human``  (default) Apply nothing, report the conflicts, let a person decide.
``merge3`` Try a line-level three-way merge (``git merge-file``) of
           base -> repo and base -> agent. If every stale file merges
           without conflict markers, apply; otherwise behave like ``human``.
``rerun``  Apply nothing, and re-snapshot the workspace on top of the
           current repo (the old tree is kept in ``attempts/``) so the agent
           can redo its task on the new code.
``force``  Overwrite the repo with the agent's version. Only for when a
           person has reviewed the conflict and approves.
"""

from __future__ import annotations

import enum
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .conflicts import Conflict, find_stale_reads, is_stale
from .tracking import same_content
from .workspace import Store, Workspace


class Policy(str, enum.Enum):
    HUMAN = "human"
    MERGE3 = "merge3"
    RERUN = "rerun"
    FORCE = "force"


@dataclass
class MergeResult:
    workspace: str
    policy: str
    dry_run: bool = False
    # path -> "added" | "modified" | "deleted" | "merged" | "convergent"
    applied: dict[str, str] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    warnings: list[Conflict] = field(default_factory=list)
    refreshed: bool = False
    commit: str | None = None

    @property
    def merged(self) -> bool:
        return not self.conflicts and not self.dry_run

    def to_dict(self) -> dict:
        return {
            "workspace": self.workspace,
            "policy": self.policy,
            "dry_run": self.dry_run,
            "merged": self.merged,
            "applied": dict(sorted(self.applied.items())),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "warnings": [c.to_dict() for c in self.warnings],
            "refreshed": self.refreshed,
            "commit": self.commit,
        }


@dataclass
class _Write:
    path: str
    how: str
    data: bytes | None = None  # merged content; None = copy the tree file


def merge_workspace(
    store: Store,
    name: str,
    policy: Policy | str = Policy.HUMAN,
    dry_run: bool = False,
    commit: bool = False,
    message: str | None = None,
) -> MergeResult:
    policy = Policy(policy)
    with store.lock():
        ws = store.get(name)
        result = MergeResult(workspace=name, policy=policy.value, dry_run=dry_run)
        changes = ws.changes()
        result.warnings = find_stale_reads(store.repo, name, ws.base_dir, ws.reads.paths(), changes)

        plan: list[_Write] = []
        for path in sorted(changes.paths):
            kind = changes.kind_of(path)
            assert kind is not None
            if same_content(store.repo / path, ws.tree / path):
                plan.append(_Write(path, "convergent"))
            elif not is_stale(store.repo, ws.tree, ws.base_dir, path) or policy is Policy.FORCE:
                plan.append(_Write(path, kind))
            elif policy is Policy.MERGE3 and (merged := _three_way(store, ws, path)) is not None:
                plan.append(_Write(path, "merged", merged))
            else:
                result.conflicts.append(Conflict(path=path, kind="stale-write", workspaces=(name,)))

        if result.conflicts:
            pass  # all-or-nothing: write nothing
        elif dry_run:
            result.applied = {w.path: w.how for w in plan}
        else:
            for w in plan:
                _apply(store, ws, w)
                result.applied[w.path] = w.how
            ws.merges.append({"ts": time.time(), "policy": policy.value, "paths": sorted(result.applied)})
            ws.reads.clear()
            ws.save()
            if commit and result.applied:
                msg = message or f"agentsnap: merge workspace {name}"
                result.commit = _git_commit(store.repo, sorted(result.applied), msg)

    # refresh() takes the lock itself, so it must run after we release ours.
    if result.conflicts and policy is Policy.RERUN and not dry_run:
        store.refresh(name)
        result.refreshed = True
    return result


def _three_way(store: Store, ws: Workspace, path: str) -> bytes | None:
    """Merged bytes, or None if the merge has conflicts or cannot be attempted."""
    ours = store.repo / path  # current repo version
    theirs = ws.tree / path  # agent's version
    base = ws.base_dir / path
    if not (ours.is_file() and theirs.is_file()) or ours.is_symlink() or theirs.is_symlink():
        return None  # deletes and symlinks need a human
    if shutil.which("git") is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        base_file = base if base.is_file() else Path(tmp) / "empty"
        if not base.is_file():
            base_file.write_bytes(b"")
        proc = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "-L",
                "repo",
                "-L",
                "base",
                "-L",
                ws.name,
                str(ours),
                str(base_file),
                str(theirs),
            ],
            capture_output=True,
        )
    # Exit code: 0 clean, >0 number of conflicts, <0 error (e.g. binary file).
    return proc.stdout if proc.returncode == 0 else None


def _apply(store: Store, ws: Workspace, w: _Write) -> None:
    repo_p, tree_p, base_p = store.repo / w.path, ws.tree / w.path, ws.base_dir / w.path

    if w.how == "deleted":
        _remove(repo_p)
        _prune_empty_dirs(repo_p.parent, store.repo)
    elif w.how == "merged":
        _atomic_write(repo_p, w.data or b"", mode_from=tree_p)
        _atomic_write(tree_p, w.data or b"", mode_from=tree_p)  # agent sees merged result
    elif w.how != "convergent":
        _atomic_copy(tree_p, repo_p)

    # The workspace's base now matches the repo for this path, so the change
    # is not merged twice and later merges compare against the new version.
    # copy2 keeps the mtime, so base/ and repo signatures match afterwards.
    _remove(base_p)
    if repo_p.exists() or repo_p.is_symlink():
        _atomic_copy(repo_p, base_p)
    _prune_empty_dirs(base_p.parent, ws.base_dir)


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.agentsnap-tmp")
    _remove(tmp)
    if src.is_symlink():
        os.symlink(os.readlink(src), tmp)
    else:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _atomic_write(dst: Path, data: bytes, mode_from: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.agentsnap-tmp")
    tmp.write_bytes(data)
    if mode_from.exists():
        shutil.copymode(mode_from, tmp)
    os.replace(tmp, dst)


def _remove(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()


def _prune_empty_dirs(d: Path, stop: Path) -> None:
    while d != stop and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


def _git_commit(repo: Path, paths: list[str], message: str) -> str | None:
    if not (repo / ".git").exists() or shutil.which("git") is None:
        return None
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "add", "-A", "--", *paths], check=True, capture_output=True)
    proc = subprocess.run([*git, "commit", "-q", "-m", message, "--", *paths], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
