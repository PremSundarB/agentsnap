import os
import stat
import tempfile
from pathlib import Path

import pytest

from agentsnap import snapshot
from agentsnap.snapshot import SnapshotError, available_methods, clone_path, create_snapshot, probe_methods

from .conftest import contents, read, write

# Methods that work on the filesystem pytest uses (e.g. no reflinks on ext4).
METHODS = probe_methods(Path(tempfile.gettempdir()))


@pytest.mark.parametrize("method", METHODS)
def test_snapshot_is_identical(repo, tmp_path, method):
    (repo / ".git").mkdir()
    write(repo, ".git/HEAD", "ref: refs/heads/main\n")
    dst = tmp_path / "snap"
    used = create_snapshot(repo, dst, exclude={".git"}, method=method)
    assert used == method
    assert contents(dst, ignore=()) == contents(repo, ignore={".git"})
    assert not (dst / ".git").exists()
    # symlink kept as a link, executable bit preserved
    assert (dst / "app_link.py").is_symlink()
    assert os.readlink(dst / "app_link.py") == "src/app.py"
    assert (dst / "src/util.py").stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("method", METHODS)
def test_snapshot_is_isolated_both_ways(repo, tmp_path, method):
    dst = tmp_path / "snap"
    create_snapshot(repo, dst, method=method)
    write(dst, "src/util.py", "changed in snapshot\n")
    write(repo, "README.md", "changed in repo\n")
    assert read(repo, "src/util.py") == "def add(a, b):\n    return a + b\n"
    assert read(dst, "README.md") == "# demo\n"


def test_default_method_is_cheapest_available(repo, tmp_path, monkeypatch):
    monkeypatch.delenv(snapshot.METHOD_ENV, raising=False)
    assert create_snapshot(repo, tmp_path / "snap") == METHODS[0]


def test_env_var_forces_method(repo, tmp_path, monkeypatch):
    monkeypatch.setenv(snapshot.METHOD_ENV, "copy")
    assert create_snapshot(repo, tmp_path / "snap") == "copy"


def test_unknown_method_rejected(repo, tmp_path):
    with pytest.raises(ValueError):
        create_snapshot(repo, tmp_path / "snap", method="nope")


def test_falls_back_when_a_method_fails(repo, tmp_path, monkeypatch):
    def broken(src, dst):
        dst.mkdir(exist_ok=True)  # leave debris that must be cleaned up
        raise OSError(45, "Operation not supported")

    monkeypatch.delenv(snapshot.METHOD_ENV, raising=False)
    monkeypatch.setattr(snapshot, "available_methods", lambda: ["apfs", "copy"])
    monkeypatch.setitem(snapshot._CLONERS, "apfs", broken)
    assert create_snapshot(repo, tmp_path / "snap") == "copy"
    assert read(tmp_path / "snap", "README.md") == "# demo\n"


def test_all_methods_failing_raises(repo, tmp_path, monkeypatch):
    def broken(src, dst):
        raise OSError("nope")

    monkeypatch.setitem(snapshot._CLONERS, "copy", broken)
    with pytest.raises(SnapshotError):
        create_snapshot(repo, tmp_path / "snap", method="copy")
    assert not (tmp_path / "snap").exists()


@pytest.mark.parametrize("method", METHODS)
def test_existing_destination_is_left_untouched(repo, tmp_path, method):
    write(tmp_path, "snap/precious.txt", "keep me\n")
    with pytest.raises(FileExistsError):
        create_snapshot(repo, tmp_path / "snap", method=method)
    with pytest.raises(FileExistsError):
        clone_path(repo / "src", tmp_path / "snap", method=method)
    assert read(tmp_path, "snap/precious.txt") == "keep me\n"


@pytest.mark.parametrize("method", METHODS)
def test_clone_path_directory(repo, tmp_path, method):
    clone_path(repo / "src", tmp_path / "src-copy", method=method)
    assert read(tmp_path / "src-copy", "util.py") == read(repo, "src/util.py")


def test_failed_method_is_skipped_next_time(repo, tmp_path, monkeypatch):
    calls = []

    def broken(src, dst):
        calls.append(src)
        raise OSError(45, "Operation not supported")

    monkeypatch.delenv(snapshot.METHOD_ENV, raising=False)
    monkeypatch.setattr(snapshot, "available_methods", lambda: ["apfs", "copy"])
    monkeypatch.setitem(snapshot._CLONERS, "apfs", broken)
    assert create_snapshot(repo, tmp_path / "one") == "copy"
    assert create_snapshot(repo, tmp_path / "two") == "copy"
    assert len(calls) == 1  # not retried on the same device


def test_probe_methods(tmp_path):
    works = probe_methods(tmp_path)
    assert works and works[-1] == "copy"
    assert set(works) <= set(available_methods())
    assert list(tmp_path.iterdir()) == []  # probe cleans up
