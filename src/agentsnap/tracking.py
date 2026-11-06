"""Tracking what each agent changed (writes) and looked at (reads).

Area owner: conflict detection and read/write tracking (Arvind).

Writes are found by comparing the agent's tree with the pristine ``base/``
copy taken at snapshot time. Like git's index, we first compare cheap
``lstat`` signatures (type, size, mtime in ns). Clones and copies preserve
mtimes, so untouched files match without being read; only files whose
signature changed are hashed to confirm a real content change. This needs
no filesystem hooks, so it works for any agent and any editor.

Caveat (same as git): a tool that rewrites a file keeping both its size and
its nanosecond mtime is not detected.

Reads cannot be seen from file contents, so they are recorded explicitly:
the MCP server logs every file an agent reads through it, and the
``agentsnap record-read`` command can be wired into an agent's hooks
(for example a Claude Code ``PostToolUse`` hook on the ``Read`` tool).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Names skipped anywhere in the tree when building a manifest. These are VCS
# metadata, our own state, and build/cache output that agents regenerate
# rather than author. Edits under them are never merged back.
DEFAULT_IGNORES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".agentsnap",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".DS_Store",
    }
)

# (kind, size, mtime_ns): kind is "f" for regular files, "l" for symlinks.
Sig = tuple[str, int, int]
Manifest = dict[str, Sig]

_CHUNK = 1 << 20


def hash_file(path: Path) -> str:
    """Content hash of a regular file, or of a symlink's target string."""
    path = Path(path)
    h = hashlib.sha256()
    if path.is_symlink():
        h.update(b"symlink:" + os.fsencode(os.readlink(path)))
        return h.hexdigest()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def hash_path(root: Path, rel: str) -> str | None:
    """Hash ``root/rel``, or ``None`` if it does not exist as a file/symlink."""
    p = Path(root) / rel
    if p.is_symlink() or p.is_file():
        return hash_file(p)
    return None


def file_sig(path: Path) -> Sig | None:
    """Cheap signature from ``lstat``, or None if not a file/symlink."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return ("l", st.st_size, st.st_mtime_ns)
    if stat.S_ISREG(st.st_mode):
        return ("f", st.st_size, st.st_mtime_ns)
    return None


def same_content(a: Path, b: Path) -> bool:
    """True if ``a`` and ``b`` hold the same content (both missing counts as same)."""
    sa, sb = file_sig(a), file_sig(b)
    if sa is None or sb is None:
        return sa is None and sb is None
    if sa == sb:
        return True  # fast path: clones keep size and mtime
    if sa[0] != sb[0] or (sa[0] == "f" and sa[1] != sb[1]):
        return False
    return hash_file(a) == hash_file(b)


def is_ignored(path: str, ignore: Iterable[str] = DEFAULT_IGNORES) -> bool:
    """True if any component of the relative POSIX ``path`` is an ignored name."""
    ignored = frozenset(ignore)
    return any(part in ignored for part in path.split("/"))


def build_manifest(root: Path, ignore: Iterable[str] = DEFAULT_IGNORES) -> Manifest:
    """Map every file under ``root`` (POSIX-style relative path) to its signature."""
    root = Path(root)
    ignored = frozenset(ignore)
    manifest: Manifest = {}
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        keep = []
        for d in dirnames:
            if d in ignored:
                continue
            # os.walk does not descend into symlinked dirs; track the link itself.
            if (base / d).is_symlink():
                filenames.append(d)
            else:
                keep.append(d)
        dirnames[:] = sorted(keep)
        for name in filenames:
            if name in ignored:
                continue
            p = base / name
            sig = file_sig(p)
            if sig is not None:  # skip sockets, fifos, ...
                manifest[p.relative_to(root).as_posix()] = sig
    return manifest


def diff_trees(base_root: Path, root: Path, ignore: Iterable[str] = DEFAULT_IGNORES) -> ChangeSet:
    """Files added, modified or deleted in ``root`` relative to ``base_root``."""
    ignore = frozenset(ignore)
    base, cur = build_manifest(base_root, ignore), build_manifest(root, ignore)
    candidates = diff_manifests(base, cur)
    modified = frozenset(
        p for p in candidates.modified if not same_content(Path(base_root) / p, Path(root) / p)
    )
    return ChangeSet(added=candidates.added, modified=modified, deleted=candidates.deleted)


@dataclass(frozen=True)
class ChangeSet:
    """Files a workspace added, modified or deleted relative to its base."""

    added: frozenset[str] = field(default_factory=frozenset)
    modified: frozenset[str] = field(default_factory=frozenset)
    deleted: frozenset[str] = field(default_factory=frozenset)

    @property
    def paths(self) -> frozenset[str]:
        return self.added | self.modified | self.deleted

    @property
    def written(self) -> frozenset[str]:
        """Paths whose new content must be copied (added or modified)."""
        return self.added | self.modified

    def __bool__(self) -> bool:
        return bool(self.paths)

    def __len__(self) -> int:
        return len(self.paths)

    def kind_of(self, path: str) -> str | None:
        if path in self.added:
            return "added"
        if path in self.modified:
            return "modified"
        if path in self.deleted:
            return "deleted"
        return None

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "added": sorted(self.added),
            "modified": sorted(self.modified),
            "deleted": sorted(self.deleted),
        }


def diff_manifests(base: Mapping[str, object], current: Mapping[str, object]) -> ChangeSet:
    """Diff two manifests by value. With signatures, ``modified`` are only candidates."""
    base_keys, cur_keys = set(base), set(current)
    return ChangeSet(
        added=frozenset(cur_keys - base_keys),
        deleted=frozenset(base_keys - cur_keys),
        modified=frozenset(p for p in base_keys & cur_keys if base[p] != current[p]),
    )


class ReadLog:
    """Append-only JSON-lines log of paths an agent read in its workspace."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def record(self, rel_paths: Iterable[str]) -> None:
        now = time.time()
        with open(self.path, "a", encoding="utf-8") as fh:
            for rel in rel_paths:
                fh.write(json.dumps({"path": rel, "ts": now}) + "\n")

    def paths(self) -> frozenset[str]:
        if not self.path.exists():
            return frozenset()
        out = set()
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.add(json.loads(line)["path"])
                except (json.JSONDecodeError, KeyError):
                    continue  # tolerate a torn last line
        return frozenset(out)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
