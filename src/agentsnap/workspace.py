"""Workspaces: one isolated snapshot of the repo per agent.

On-disk layout (all inside the repo, so APFS/reflink clones stay on one volume)::

    <repo>/.agentsnap/
        .gitignore              # "*": git never sees our state
        config.json             # optional user settings
        lock                    # flock()ed while merging
        workspaces/<name>/
            meta.json           # base manifest, method, timestamps
            base/               # pristine copy of the repo at snapshot time
            tree/               # the agent works here
            reads.jsonl         # files the agent read (if reported)
            attempts/<n>/       # earlier trees kept by ``refresh``

``base/`` costs nothing extra with copy-on-write clones. It lets us show
real diffs and do line-level three-way merges when two agents touch the
same file.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import journal
from .snapshot import clone_path, create_snapshot
from .tracking import DEFAULT_IGNORES, ChangeSet, ReadLog, diff_trees

# Files whose mtime is within this many seconds before the snapshot are
# re-checked by content even if their signature looks unchanged. Generous,
# to cover coarse filesystem timestamps and clock granularity.
RACY_MARGIN_S = 2.0

try:  # POSIX only; Windows is not supported yet.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

STATE_DIRNAME = ".agentsnap"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Never part of a snapshot. Everything else (including build caches such as
# node_modules) is cloned so agents can build without reinstalling.
SNAPSHOT_EXCLUDES = frozenset({".git", STATE_DIRNAME})


class WorkspaceError(Exception):
    """User-facing error (bad name, missing workspace, ...)."""


@dataclass
class Config:
    """Settings from ``.agentsnap/config.json`` (all optional)::

    {
      "extra_ignore": ["generated"],      # more names never tracked or merged
      "snapshot_method": "copy",          # force a snapshot method
      "merge_check": "pytest -q",         # must pass before any merge lands
      "merge_check_timeout": 600,         # seconds
      "workspace_git": "private"          # or "none": block git in workspaces
    }
    """

    ignore: frozenset[str] = DEFAULT_IGNORES
    snapshot_method: str | None = None
    merge_check: str | None = None
    merge_check_timeout: float | None = None
    workspace_git: str = "private"

    @classmethod
    def load(cls, path: Path) -> Config:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            ignore=DEFAULT_IGNORES | frozenset(data.get("extra_ignore", [])),
            snapshot_method=data.get("snapshot_method"),
            merge_check=data.get("merge_check"),
            merge_check_timeout=data.get("merge_check_timeout"),
            workspace_git=data.get("workspace_git", "private"),
        )


@dataclass
class Workspace:
    name: str
    dir: Path
    method: str
    created_at: float
    attempts: int = 0
    merges: list[dict] = field(default_factory=list)
    # Paths that conflicted before a rerun. The agent's new attempt resolves
    # them, and a person must approve before it merges.
    needs_approval: list[str] = field(default_factory=list)
    ignore: frozenset[str] = DEFAULT_IGNORES
    # When tree/ and base/ were last snapshotted (create or refresh).
    snapshot_at: float = 0.0

    @property
    def tree(self) -> Path:
        """Directory the agent should work in."""
        return self.dir / "tree"

    @property
    def base_dir(self) -> Path:
        return self.dir / "base"

    @property
    def reads(self) -> ReadLog:
        return ReadLog(self.dir / "reads.jsonl")

    def changes(self) -> ChangeSet:
        since = self.snapshot_at or self.created_at
        racy_after_ns = int((since - RACY_MARGIN_S) * 1e9)
        return diff_trees(self.base_dir, self.tree, self.ignore, racy_after_ns=racy_after_ns)

    def diff(self, paths: list[str] | None = None) -> str:
        """Unified diff of the agent's tree against its snapshot."""
        cs = self.changes()
        selected = sorted(cs.paths if not paths else set(paths) & cs.paths)
        out: list[str] = []
        for path in selected:
            old, new = _text_lines(self.base_dir / path), _text_lines(self.tree / path)
            if old is None or new is None:
                out.append(f"Binary file {path} differs\n")
                continue
            kind = cs.kind_of(path)
            out.extend(
                difflib.unified_diff(
                    old,
                    new,
                    fromfile="/dev/null" if kind == "added" else f"a/{path}",
                    tofile="/dev/null" if kind == "deleted" else f"b/{path}",
                )
            )
        return "".join(out)

    def save(self) -> None:
        meta = {
            "name": self.name,
            "method": self.method,
            "created_at": self.created_at,
            "snapshot_at": self.snapshot_at,
            "attempts": self.attempts,
            "merges": self.merges,
            "needs_approval": self.needs_approval,
        }
        tmp = self.dir / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.dir / "meta.json")

    @classmethod
    def load(cls, wdir: Path, ignore: frozenset[str] = DEFAULT_IGNORES) -> Workspace:
        meta = json.loads((wdir / "meta.json").read_text(encoding="utf-8"))
        return cls(
            name=meta["name"],
            dir=wdir,
            method=meta["method"],
            created_at=meta["created_at"],
            snapshot_at=meta.get("snapshot_at", meta["created_at"]),
            attempts=meta.get("attempts", 0),
            merges=meta.get("merges", []),
            needs_approval=meta.get("needs_approval", []),
            ignore=ignore,
        )


def find_repo(start: Path | None = None) -> Path:
    """Find the repo root from ``start`` (default: cwd).

    Prefers the nearest ancestor that already has a ``.agentsnap`` directory,
    so running a command from inside a workspace tree resolves to the real
    repo rather than to the snapshot. Otherwise uses the nearest ``.git``,
    and finally ``start`` itself.
    """
    start = Path(start or Path.cwd()).resolve()
    candidates = [start, *start.parents]
    for p in candidates:
        if (p / STATE_DIRNAME).is_dir():
            return p
    for p in candidates:
        if (p / ".git").exists():
            return p
    return start


class Store:
    """All workspaces of one repo."""

    def __init__(self, repo: Path):
        self.repo = Path(repo).resolve()
        self.root = self.repo / STATE_DIRNAME
        self.ws_root = self.root / "workspaces"
        self.config = Config.load(self.root / "config.json")

    # -- setup ---------------------------------------------------------

    def init(self) -> None:
        self.ws_root.mkdir(parents=True, exist_ok=True)
        gi = self.root / ".gitignore"
        if not gi.exists():
            gi.write_text("# agentsnap state; never commit\n*\n", encoding="utf-8")

    # -- workspaces ----------------------------------------------------

    def create(self, name: str, method: str | None = None) -> Workspace:
        _check_name(name)
        self.init()
        wdir = self.ws_root / name
        if wdir.exists():
            raise WorkspaceError(f"workspace {name!r} already exists")
        wdir.mkdir()
        try:
            # Under the lock, so we never snapshot a repo that is half-way
            # through a merge.
            with self.lock():
                started = time.time()
                used = self._snapshot_into(wdir, method)
            ws = Workspace(
                name=name,
                dir=wdir,
                method=used,
                created_at=started,
                snapshot_at=started,
                ignore=self.config.ignore,
            )
            ws.save()
        except BaseException:
            shutil.rmtree(wdir, ignore_errors=True)
            raise
        return ws

    def _snapshot_into(self, wdir: Path, method: str | None) -> str:
        method = method or self.config.snapshot_method
        # Snapshot the repo into tree/, then clone tree/ into base/ before the
        # agent is told about it, so both are identical by construction even
        # if the repo changes meanwhile. base/ is only used for tracked files,
        # so it skips ignored top-level directories (node_modules, .venv, ...):
        # often the bulk of the files, and the bulk of the cloning time.
        used = create_snapshot(self.repo, wdir / "tree", exclude=SNAPSHOT_EXCLUDES, method=method)
        create_snapshot(wdir / "tree", wdir / "base", exclude=self.config.ignore, method=used)
        self._private_git(wdir, used)
        return used

    def _private_git(self, wdir: Path, method: str) -> None:
        """Give the workspace its own git repository.

        Without this, git commands an agent runs inside its workspace would
        walk up the directory tree and act on the *main* repo (workspaces live
        inside it): ``git checkout .`` or ``git reset --hard`` there would
        destroy the user's uncommitted work.

        * copy-on-write filesystems: clone ``.git`` itself (cheap), giving
          the agent a complete private repo.
        * otherwise: ``git clone --shared`` (borrows the main repo's objects,
          has its own refs and index) plus a mixed reset to fill the index.
        * if that is impossible or ``workspace_git`` is ``"none"``: a ``.git``
          file pointing nowhere, so git refuses to run rather than escape.

        ``.git`` is ignored by change tracking, so it is never merged back.
        """
        tree, main_git = wdir / "tree", self.repo / ".git"
        if not (main_git.exists() or main_git.is_symlink()):
            return  # not a git repo; nothing to escape to
        if self.config.workspace_git == "private":
            try:
                if main_git.is_dir() and not main_git.is_symlink() and method in ("apfs", "reflink"):
                    clone_path(main_git, tree / ".git", method=method)
                    return
                if shutil.which("git"):
                    gitdir = wdir / "git"
                    if gitdir.exists():  # left over from before a refresh
                        shutil.rmtree(gitdir)
                    _git("clone", "-q", "--shared", "--no-checkout", str(self.repo), str(gitdir))
                    (tree / ".git").write_text(f"gitdir: {gitdir / '.git'}\n", encoding="utf-8")
                    _git("-C", str(tree), "reset", "-q")
                    return
            except (OSError, subprocess.CalledProcessError):
                for leftover in (tree / ".git", wdir / "git"):
                    if leftover.is_dir() and not leftover.is_symlink():
                        shutil.rmtree(leftover)
                    elif leftover.exists() or leftover.is_symlink():
                        leftover.unlink()
        (tree / ".git").write_text(f"gitdir: {_NO_GIT}\n", encoding="utf-8")

    def get(self, name: str) -> Workspace:
        _check_name(name)
        wdir = self.ws_root / name
        if not (wdir / "meta.json").exists():
            raise WorkspaceError(f"no workspace named {name!r} (see `agentsnap list`)")
        return Workspace.load(wdir, self.config.ignore)

    def list(self) -> list[Workspace]:
        if not self.ws_root.exists():
            return []
        return [
            Workspace.load(d, self.config.ignore)
            for d in sorted(self.ws_root.iterdir())
            if (d / "meta.json").exists()
        ]

    def drop(self, name: str) -> None:
        ws = self.get(name)
        shutil.rmtree(ws.dir)

    def refresh(self, name: str, needs_approval: list[str] | None = None) -> Workspace:
        """Re-snapshot a workspace on top of the current repo.

        The previous tree is kept under ``attempts/<n>/`` so the agent (or a
        person) can see what it did last time and redo it on the new base.
        This is the "rerun the losing agent on top of the winner" policy.
        """
        with self.lock():
            ws = self.get(name)
            ws.attempts += 1
            keep = ws.dir / "attempts" / str(ws.attempts)
            keep.parent.mkdir(exist_ok=True)
            ws.tree.rename(keep)
            shutil.rmtree(ws.base_dir)
            ws.snapshot_at = time.time()
            ws.method = self._snapshot_into(ws.dir, ws.method)
            ws.reads.clear()
            if needs_approval:
                ws.needs_approval = sorted(set(ws.needs_approval) | set(needs_approval))
            ws.save()
            return ws

    def workspace_for_path(self, path: Path) -> tuple[Workspace, str] | None:
        """If ``path`` lies inside a workspace tree, return it and the repo-relative path."""
        try:
            rel = Path(path).resolve().relative_to(self.ws_root.resolve())
        except ValueError:
            return None
        parts = rel.parts
        if len(parts) < 3 or parts[1] != "tree":
            return None
        try:
            ws = self.get(parts[0])
        except WorkspaceError:
            return None
        return ws, Path(*parts[2:]).as_posix()

    # -- concurrency ---------------------------------------------------

    @property
    def txn_dir(self) -> Path:
        return self.root / "txn"

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive, cross-process lock serialising snapshots and merges.

        Not re-entrant. On entry, rolls back any merge that crashed half-way,
        so the repo is always seen in a consistent state.
        """
        self.init()
        with open(self.root / "lock", "w") as fh:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                if self.txn_dir.exists():
                    journal.rollback(self.txn_dir, self.repo)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)


_NO_GIT = ".agentsnap-git-disabled-in-workspaces"


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def _text_lines(p: Path) -> list[str] | None:
    """Lines of a text file ([] if missing), or None if it is binary."""
    if not (p.is_file() or p.is_symlink()):
        return []
    if p.is_symlink():
        return [f"symlink -> {p.readlink()}\n"]
    try:
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n\\ No newline at end of file\n"
    return lines


def _check_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise WorkspaceError(
            f"invalid workspace name {name!r}: use letters, digits, '.', '_' or '-' "
            "(max 64 chars, must start with a letter or digit)"
        )
