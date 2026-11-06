"""Undo journal that makes a merge all-or-nothing, even if it crashes.

Area owner: merge engine (Bhushan).

A merge writes many files. If writing one of them fails (disk full, a
permission error, the process is killed), the repo must not be left
half-merged. Before touching the repo we:

1. copy every file the merge will overwrite or delete into
   ``.agentsnap/txn/backup/``;
2. write ``.agentsnap/txn/journal.json`` listing every path in the order it
   will be applied. Its presence means "a merge is in progress".

On success the whole ``txn/`` directory is removed. On an exception we roll
back immediately. If the process died, the next process to take the lock
finds the journal and rolls back before doing anything else.

This is a classic undo log, the same idea databases use to roll back
uncommitted transactions after a crash.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

JOURNAL = "journal.json"


def _exists(p: Path) -> bool:
    return p.is_symlink() or p.exists()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
    else:
        shutil.copy2(src, dst)


def _unlink(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()


def _prune(d: Path, stop: Path) -> None:
    while d != stop and d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


def run(txn: Path, repo: Path, paths: Iterable[str], apply: Callable[[], None]) -> None:
    """Back up ``paths`` in ``repo``, then call ``apply``; roll back if it raises."""
    if txn.exists():
        raise RuntimeError(f"a merge journal already exists at {txn}; recover() first")
    backup = txn / "backup"
    backup.mkdir(parents=True)
    entries = []
    for rel in paths:
        target = repo / rel
        # Directories are never backed up as such: a directory can only be
        # replaced by a file once every file in it is deleted by the same
        # merge, and those files are journaled one by one.
        existed = _exists(target) and not (target.is_dir() and not target.is_symlink())
        if existed:
            _copy(target, backup / rel)
        entries.append({"path": rel, "existed": existed})
    tmp = txn / (JOURNAL + ".tmp")
    tmp.write_text(json.dumps(entries), encoding="utf-8")
    os.replace(tmp, txn / JOURNAL)  # from here on, a crash means "roll back"

    try:
        apply()
    except BaseException:
        rollback(txn, repo)
        raise
    shutil.rmtree(txn)


def rollback(txn: Path, repo: Path) -> bool:
    """Undo an interrupted merge. Returns True if there was anything to undo."""
    journal = txn / JOURNAL
    if not journal.exists():
        # Crashed while taking backups: nothing in the repo was touched yet.
        shutil.rmtree(txn, ignore_errors=True)
        return False
    entries = json.loads(journal.read_text(encoding="utf-8"))
    for e in reversed(entries):
        target = repo / e["path"]
        _unlink(target)
        if e["existed"]:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)  # a directory now sits where our file was
            _copy(txn / "backup" / e["path"], target)
        else:
            _prune(target.parent, repo)
    shutil.rmtree(txn)
    return True
