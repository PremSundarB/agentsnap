"""Conflict detection between agents and against the shared repo.

Area owner: conflict detection (Arvind).

The model is optimistic concurrency control, as in databases:

* Agents never block each other while they work.
* Each workspace remembers the content hash of every file at snapshot time.
* At merge time, a file can only be written back if the repo still holds
  the version the agent started from. If someone else merged a change to
  that file first, the write is *stale* and must not silently overwrite it
  ("first committer wins").

Kinds of conflict:

``write-write``  Two or more unmerged workspaces changed the same file.
                 Reported early so a person can decide merge order.
``stale-write``  The repo changed a file since the workspace's snapshot and
                 the workspace changed it too. Blocks the merge.
``stale-read``   The workspace read a file (reported via the MCP server or
                 a hook) that has changed in the repo since its snapshot.
                 The agent may have made decisions on outdated code. Warning.
``read-write``   One workspace read a file another unmerged workspace
                 changed. Warning.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .symbols import changed_symbols, is_python
from .tracking import ChangeSet, same_content

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Conflict:
    path: str
    kind: str
    workspaces: tuple[str, ...]
    severity: str = ERROR
    # Per-workspace symbols touched, when known (Python files only).
    symbols: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ERROR

    def symbols_disjoint(self) -> bool | None:
        """True if every workspace touched different functions in this file."""
        if len(self.symbols) < 2 or len(self.symbols) != len(self.workspaces):
            return None
        seen: set[str] = set()
        for names in self.symbols.values():
            if seen & set(names):
                return False
            seen |= set(names)
        return True

    def describe(self) -> str:
        who = ", ".join(self.workspaces)
        if self.kind == "write-write":
            msg = f"{self.path}: changed by {who}"
        elif self.kind == "stale-write":
            msg = f"{self.path}: changed in the repo since {who} snapshotted it, and {who} changed it too"
        elif self.kind == "stale-read":
            msg = f"{self.path}: {who} read it, but the repo version changed since"
        elif self.kind == "read-write":
            reader, *writers = self.workspaces
            msg = f"{self.path}: {reader} read it while {', '.join(writers)} changed it"
        else:
            msg = f"{self.path}: {self.kind} ({who})"
        if self.symbols:
            parts = [f"{ws}: {', '.join(names) or '-'}" for ws, names in self.symbols.items()]
            msg += f"  [functions - {'; '.join(parts)}]"
            disjoint = self.symbols_disjoint()
            if disjoint:
                msg += "  (different functions: a 3-way merge will likely succeed)"
            elif disjoint is False:
                msg += "  (same function edited)"
        return msg

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "kind": self.kind,
            "severity": self.severity,
            "workspaces": list(self.workspaces),
            "symbols": {k: list(v) for k, v in self.symbols.items()},
            "description": self.describe(),
        }


def find_overlaps(changes: Mapping[str, ChangeSet]) -> list[Conflict]:
    """Paths changed by more than one workspace."""
    owners: dict[str, list[str]] = defaultdict(list)
    for ws_name, cs in changes.items():
        for path in cs.paths:
            owners[path].append(ws_name)
    return [
        Conflict(path=p, kind="write-write", workspaces=tuple(sorted(names)))
        for p, names in sorted(owners.items())
        if len(names) > 1
    ]


def is_stale(repo: Path, tree: Path, base_dir: Path, path: str) -> bool:
    """True if the repo moved away from the workspace's base for ``path``.

    Convergent edits (the repo already holds exactly what the workspace
    wants, including both sides deleting the file) are not stale.
    """
    current = Path(repo) / path
    return not same_content(current, Path(base_dir) / path) and not same_content(current, Path(tree) / path)


def find_stale_writes(
    repo: Path, ws_name: str, base_dir: Path, changes: ChangeSet, tree: Path
) -> list[Conflict]:
    """Changed paths whose repo version no longer matches the workspace's base."""
    return [
        Conflict(path=p, kind="stale-write", workspaces=(ws_name,))
        for p in sorted(changes.paths)
        if is_stale(repo, tree, base_dir, p)
    ]


def find_stale_reads(
    repo: Path, ws_name: str, base_dir: Path, reads: Iterable[str], changes: ChangeSet
) -> list[Conflict]:
    """Files the workspace read (but did not change) that have since changed in the repo."""
    return [
        Conflict(path=p, kind="stale-read", workspaces=(ws_name,), severity=WARNING)
        for p in sorted(set(reads) - changes.paths)
        if not same_content(Path(repo) / p, Path(base_dir) / p)
    ]


def find_read_write(reads: Mapping[str, Iterable[str]], changes: Mapping[str, ChangeSet]) -> list[Conflict]:
    out = []
    for reader, paths in sorted(reads.items()):
        for p in sorted(set(paths)):
            writers = sorted(w for w, cs in changes.items() if w != reader and p in cs.paths)
            if writers and p not in changes.get(reader, ChangeSet()).paths:
                out.append(
                    Conflict(
                        path=p,
                        kind="read-write",
                        workspaces=(reader, *writers),
                        severity=WARNING,
                    )
                )
    return out


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def annotate_symbols(conflict: Conflict, ws_dirs: Mapping[str, Path]) -> Conflict:
    """Attach per-workspace function names for Python write-write conflicts.

    ``ws_dirs`` maps workspace name -> workspace dir (containing base/ and tree/).
    """
    if not is_python(conflict.path):
        return conflict
    symbols: dict[str, tuple[str, ...]] = {}
    for ws in conflict.workspaces:
        d = ws_dirs.get(ws)
        if d is None:
            return conflict
        old = _read_text(d / "base" / conflict.path) or ""
        new = _read_text(d / "tree" / conflict.path) or ""
        names = changed_symbols(old, new)
        if names is None:
            return conflict
        symbols[ws] = tuple(sorted(names))
    return Conflict(
        path=conflict.path,
        kind=conflict.kind,
        workspaces=conflict.workspaces,
        severity=conflict.severity,
        symbols=symbols,
    )


@dataclass
class Report:
    conflicts: list[Conflict]
    changes: dict[str, ChangeSet]

    @property
    def errors(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.blocking]

    @property
    def warnings(self) -> list[Conflict]:
        return [c for c in self.conflicts if not c.blocking]

    def to_dict(self) -> dict:
        return {
            "workspaces": {k: v.to_dict() for k, v in sorted(self.changes.items())},
            "conflicts": [c.to_dict() for c in self.conflicts],
            "errors": len(self.errors),
            "warnings": len(self.warnings),
        }


def check(store, names: Iterable[str] | None = None) -> Report:
    """Full conflict report for the given workspaces (default: all)."""
    workspaces = store.list() if names is None else [store.get(n) for n in names]
    changes = {ws.name: ws.changes() for ws in workspaces}
    dirs = {ws.name: ws.dir for ws in workspaces}

    conflicts: list[Conflict] = [annotate_symbols(c, dirs) for c in find_overlaps(changes)]
    reads = {}
    for ws in workspaces:
        conflicts += find_stale_writes(store.repo, ws.name, ws.base_dir, changes[ws.name], ws.tree)
        r = ws.reads.paths()
        reads[ws.name] = r
        conflicts += find_stale_reads(store.repo, ws.name, ws.base_dir, r, changes[ws.name])
    conflicts += find_read_write(reads, changes)
    return Report(conflicts=conflicts, changes=changes)
