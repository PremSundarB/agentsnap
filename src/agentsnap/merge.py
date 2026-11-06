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

Two gates can stop an otherwise clean merge:

* **Approval.** After ``rerun``, the workspace remembers which paths had
  conflicted. Its next merge waits until a person passes ``approve=True``
  (``agentsnap merge NAME --approve``). The MCP server never approves.
* **Check.** With ``check="pytest -q"`` (or ``merge_check`` in the config),
  the merged result is first assembled in a scratch copy-on-write snapshot of
  the repo and the command runs there. The merge only lands if it exits 0.
  This catches semantic conflicts, such as two clean edits that break each
  other, that file-level checks cannot see.
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

from . import journal
from .conflicts import Conflict, find_stale_reads, is_stale
from .snapshot import create_snapshot
from .tracking import same_content
from .workspace import SNAPSHOT_EXCLUDES, Store, Workspace

CHECK_OUTPUT_LIMIT = 4000


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
    commit_error: str | None = None
    # Paths that need a person's approval before this workspace can merge.
    awaiting_approval: list[str] = field(default_factory=list)
    # {"command", "returncode", "output"} if a merge check ran.
    check: dict | None = None

    @property
    def check_passed(self) -> bool:
        return self.check is None or self.check["returncode"] == 0

    @property
    def merged(self) -> bool:
        return not self.conflicts and not self.dry_run and not self.awaiting_approval and self.check_passed

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
            "commit_error": self.commit_error,
            "awaiting_approval": self.awaiting_approval,
            "check": self.check,
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
    approve: bool = False,
    check: str | None = None,
    check_timeout: float | None = None,
) -> MergeResult:
    """Merge workspace ``name`` into the repo. See the module docstring."""
    policy = Policy(policy)
    check = check if check is not None else store.config.merge_check
    check_timeout = check_timeout if check_timeout is not None else store.config.merge_check_timeout
    with store.lock():
        ws = store.get(name)
        result = MergeResult(workspace=name, policy=policy.value, dry_run=dry_run)
        changes = ws.changes()
        result.warnings = find_stale_reads(
            store.repo, name, ws.base_dir, ws.reads.paths(), changes, ws.ignore
        )

        plan: list[_Write] = []
        for path in sorted(changes.paths):
            kind = changes.kind_of(path)
            assert kind is not None
            if not _inside_repo(store.repo, path):
                # A parent directory in the repo is a symlink leading outside it.
                result.conflicts.append(Conflict(path=path, kind="unsafe-path", workspaces=(name,)))
            elif same_content(store.repo / path, ws.tree / path):
                plan.append(_Write(path, "convergent"))
            elif not is_stale(store.repo, ws.tree, ws.base_dir, path) or policy is Policy.FORCE:
                plan.append(_Write(path, kind))
            elif policy is Policy.MERGE3 and (merged := _three_way(store, ws, path)) is not None:
                plan.append(_Write(path, "merged", merged))
            else:
                result.conflicts.append(Conflict(path=path, kind="stale-write", workspaces=(name,)))
        if not result.conflicts:
            result.conflicts = [
                Conflict(path=p, kind="type-change", workspaces=(name,)) for p in _blocked(store.repo, plan)
            ]

        if not result.conflicts and plan and ws.needs_approval and not approve:
            result.awaiting_approval = list(ws.needs_approval)
        if not result.conflicts and check and plan:
            result.check = _run_check(store, ws, plan, check, check_timeout)

        if result.conflicts or result.awaiting_approval or not result.check_passed or not plan:
            pass  # all-or-nothing: write nothing (or there is nothing to write)
        elif dry_run:
            result.applied = {w.path: w.how for w in plan}
        else:
            ordered = _ordered(plan)
            writes = [w for w in ordered if w.how != "convergent"]
            # Phase 1: the repo, all-or-nothing via the undo journal.
            journal.run(
                store.txn_dir,
                store.repo,
                [w.path for w in writes],
                lambda: [_write_into(store.repo, ws, w) for w in writes],
            )
            # Phase 2: the workspace's own bookkeeping (base/, merged files in tree/).
            for w in ordered:
                _sync_workspace(store, ws, w)
                result.applied[w.path] = w.how
            ws.merges.append(
                {
                    "ts": time.time(),
                    "policy": policy.value,
                    "paths": sorted(result.applied),
                    "approved": sorted(ws.needs_approval),
                    "check": check if result.check else None,
                }
            )
            ws.needs_approval = []
            ws.reads.clear()
            ws.save()
            if commit and result.applied:
                msg = message or f"agentsnap: merge workspace {name}"
                result.commit, result.commit_error = _git_commit(store.repo, sorted(result.applied), msg)

    # refresh() takes the lock itself, so it must run after we release ours.
    if result.conflicts and policy is Policy.RERUN and not dry_run:
        store.refresh(name, needs_approval=[c.path for c in result.conflicts])
        result.refreshed = True
    return result


def _run_check(store: Store, ws: Workspace, plan: list[_Write], command: str, timeout: float | None) -> dict:
    """Run ``command`` on a scratch snapshot of the repo with ``plan`` applied."""
    scratch = store.root / "scratch" / f"{ws.name}-{os.getpid()}-{time.monotonic_ns()}"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    try:
        create_snapshot(store.repo, scratch, exclude=SNAPSHOT_EXCLUDES, method=ws.method)
        for w in _ordered(plan):
            _write_into(scratch, ws, w)
        env = {**os.environ, "AGENTSNAP_CHECK": "1", "AGENTSNAP_WORKSPACE": ws.name}
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=scratch,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            code, output = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            out = out.decode(errors="replace") if isinstance(out, bytes) else out
            code, output = -1, out + f"\n[agentsnap] check timed out after {timeout} s"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"command": command, "returncode": code, "output": output[-CHECK_OUTPUT_LIMIT:]}


def _ordered(plan: list[_Write]) -> list[_Write]:
    """Deletions first, so a directory emptied by the merge can become a file."""
    return [w for w in plan if w.how == "deleted"] + [w for w in plan if w.how != "deleted"]


def _blocked(repo: Path, plan: list[_Write]) -> list[str]:
    """Paths the filesystem would refuse to write, even after the plan's deletions.

    * the target is a directory that still has files the merge does not delete;
    * a parent of the target is a file (or link) that the merge does not delete.
    """
    deleted = {w.path for w in plan if w.how == "deleted"}
    blocked = []
    for w in plan:
        if w.how in ("deleted", "convergent"):
            continue
        target = repo / w.path
        if target.is_dir() and not target.is_symlink():
            for dirpath, dirnames, filenames in os.walk(target):
                leftovers = [
                    n
                    for n in filenames + [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
                    if (Path(dirpath) / n).relative_to(repo).as_posix() not in deleted
                ]
                if leftovers:
                    blocked.append(w.path)
                    break
            continue
        parent = Path(w.path).parent
        while parent.parts:
            p = repo / parent
            if (p.is_symlink() or p.exists()) and not p.is_dir() and parent.as_posix() not in deleted:
                blocked.append(w.path)
                break
            parent = parent.parent
    return blocked


def _inside_repo(repo: Path, path: str) -> bool:
    """True if writing ``repo/path`` cannot escape the repo through a symlinked parent."""
    root = repo.resolve()
    parent = (repo / path).parent.resolve()
    return parent == root or root in parent.parents


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


def _write_into(root: Path, ws: Workspace, w: _Write) -> None:
    """Apply one planned write to the tree at ``root`` (the repo or a scratch copy)."""
    target, source = root / w.path, ws.tree / w.path
    if w.how == "deleted":
        _remove(target)
        _prune_empty_dirs(target.parent, root)
    elif w.how == "merged":
        _atomic_write(target, w.data or b"", mode_from=source)
    elif w.how != "convergent":
        if target.is_dir() and not target.is_symlink():
            target.rmdir()  # emptied by this merge's deletions (checked by _blocked)
        _atomic_copy(source, target)


def _sync_workspace(store: Store, ws: Workspace, w: _Write) -> None:
    """After the repo is written: make the workspace's base/ match the repo for ``w``."""
    repo_p, tree_p, base_p = store.repo / w.path, ws.tree / w.path, ws.base_dir / w.path

    if w.how == "merged":
        _atomic_write(tree_p, w.data or b"", mode_from=tree_p)  # agent sees merged result

    # The workspace's base now matches the repo for this path, so the change
    # is not merged twice and later merges compare against the new version.
    # copy2 keeps the mtime, so base/ and repo signatures match afterwards.
    _remove(base_p)
    if base_p.is_dir():  # a directory became a file: clear what is left of it
        shutil.rmtree(base_p)
    if repo_p.is_symlink() or repo_p.is_file():
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


def _git_commit(repo: Path, paths: list[str], message: str) -> tuple[str | None, str | None]:
    """Commit exactly ``paths``. Returns (sha, error). Never raises: the merge already happened."""
    if not (repo / ".git").exists():
        return None, "not a git repository"
    if shutil.which("git") is None:
        return None, "git not found"
    git = ["git", "-C", str(repo)]
    present = [p for p in paths if (repo / p).exists() or (repo / p).is_symlink()]
    gone = [p for p in paths if p not in present]
    try:
        if present:
            subprocess.run([*git, "add", "-A", "--", *present], check=True, capture_output=True)
        if gone:  # deletions of tracked files; untracked ones are simply ignored
            subprocess.run(
                [*git, "rm", "-q", "--cached", "--ignore-unmatch", "--", *gone],
                check=True,
                capture_output=True,
            )
        staged = subprocess.run(
            [*git, "diff", "--cached", "--name-only", "-z", "--", *paths],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split("\0")
        staged = [p for p in staged if p]
        if not staged:
            return None, "nothing to commit"
        subprocess.run([*git, "commit", "-q", "-m", message, "--", *staged], check=True, capture_output=True)
        sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
        return sha.stdout.strip(), None
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return None, err.strip() or str(exc)
