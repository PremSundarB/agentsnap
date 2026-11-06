"""Copy-on-write snapshots of a repository.

Area owner: snapshot layer (Bhushan).

A snapshot is a new directory that starts out identical to the repo. We try
the cheapest method the platform supports and fall back if it fails:

1. ``apfs``    - macOS clonefile(2). Near-instant, and data blocks are shared
                 until one side writes to them, so a snapshot costs almost no
                 disk space.
2. ``reflink`` - Linux ``cp --reflink=auto``. Copy-on-write on btrfs/XFS; GNU
                 cp silently falls back to a normal copy on other filesystems.
3. ``copy``    - Portable ``shutil`` copy. Always works, costs full disk space.

Set ``AGENTSNAP_SNAPSHOT_METHOD`` to force one method (handy in tests and
benchmarks).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

METHOD_ENV = "AGENTSNAP_SNAPSHOT_METHOD"

# From <sys/clonefile.h>: clone a symlink itself instead of its target.
_CLONE_NOFOLLOW = 0x0001


class SnapshotError(RuntimeError):
    """Raised when no snapshot method could copy the tree."""


def _load_clonefile() -> Callable[..., int] | None:
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        fn = libc.clonefile
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    fn.restype = ctypes.c_int
    return fn


_clonefile = _load_clonefile()


def _clone_apfs(src: Path, dst: Path) -> None:
    # clonefile() on a directory clones the whole hierarchy in one call.
    assert _clonefile is not None
    if _clonefile(os.fsencode(src), os.fsencode(dst), _CLONE_NOFOLLOW) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(src))


def _clone_reflink(src: Path, dst: Path) -> None:
    try:
        subprocess.run(
            ["cp", "-a", "--reflink=auto", str(src), str(dst)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        raise OSError(exc.stderr.decode(errors="replace").strip()) from exc


def _copy(src: Path, dst: Path) -> None:
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
    elif src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


_CLONERS: dict[str, Callable[[Path, Path], None]] = {
    "apfs": _clone_apfs,
    "reflink": _clone_reflink,
    "copy": _copy,
}


def available_methods() -> list[str]:
    """Methods worth trying on this platform, cheapest first."""
    methods = []
    if _clonefile is not None:
        methods.append("apfs")
    if sys.platform.startswith("linux") and shutil.which("cp"):
        methods.append("reflink")
    methods.append("copy")
    return methods


def clone_path(src: Path, dst: Path, method: str | None = None) -> str:
    """Clone a single file or directory ``src`` to ``dst``. Returns the method used."""
    return _run_methods(lambda m: _CLONERS[m](Path(src), Path(dst)), Path(dst), method)


def create_snapshot(
    src: Path,
    dst: Path,
    exclude: Iterable[str] = (),
    method: str | None = None,
) -> str:
    """Snapshot the directory ``src`` into the new directory ``dst``.

    Top-level entries of ``src`` whose names are in ``exclude`` are skipped
    (for example ``.git`` and the agentsnap state directory, which lives
    inside the repo). Returns the name of the method that succeeded.
    """
    src, dst = Path(src), Path(dst)
    excluded = set(exclude)
    entries = sorted(e for e in src.iterdir() if e.name not in excluded)

    def run(m: str) -> None:
        dst.mkdir(parents=True)
        for entry in entries:
            _CLONERS[m](entry, dst / entry.name)

    return _run_methods(run, dst, method)


def _run_methods(action: Callable[[str], None], dst: Path, method: str | None) -> str:
    # Checked up front: failed attempts delete ``dst``, which must never be
    # something that existed before we started.
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"snapshot destination already exists: {dst}")
    forced = method or os.environ.get(METHOD_ENV)
    if forced:
        if forced not in _CLONERS:
            raise ValueError(f"unknown snapshot method {forced!r}; choose from {sorted(_CLONERS)}")
        candidates = [forced]
    else:
        candidates = available_methods()

    errors = []
    for m in candidates:
        try:
            action(m)
            return m
        except OSError as exc:
            # e.g. clonefile() on a non-APFS volume. Clean up and try the next one.
            errors.append(f"{m}: {exc}")
            _remove(dst)
    raise SnapshotError(f"could not snapshot to {dst}: " + "; ".join(errors))


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
