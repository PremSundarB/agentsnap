"""All-or-nothing merges: type changes, mid-merge failures, and crash recovery."""

import json

import pytest

from agentsnap import journal, merge
from agentsnap.merge import merge_workspace

from .conftest import SAMPLE, read, write


def test_directory_replaced_by_file(store, repo):
    ws = store.create("a")
    (ws.tree / "docs/guide.txt").unlink()
    (ws.tree / "docs").rmdir()
    write(ws.tree, "docs", "docs are now a single file\n")
    res = merge_workspace(store, "a")
    assert res.merged, res.conflicts
    assert (repo / "docs").is_file() and read(repo, "docs") == "docs are now a single file\n"


def test_file_replaced_by_directory(store, repo):
    ws = store.create("a")
    (ws.tree / "README.md").unlink()
    write(ws.tree, "README.md/index.md", "# moved\n")
    assert merge_workspace(store, "a").merged
    assert read(repo, "README.md/index.md") == "# moved\n"


def test_directory_with_other_files_blocks_type_change(store, repo):
    ws = store.create("a")
    (ws.tree / "docs/guide.txt").unlink()
    (ws.tree / "docs").rmdir()
    write(ws.tree, "docs", "file\n")
    write(repo, "docs/other.txt", "someone else's file\n")  # appeared after the snapshot
    res = merge_workspace(store, "a")
    assert not res.merged and [c.kind for c in res.conflicts] == ["type-change"]
    assert read(repo, "docs/guide.txt") == SAMPLE["docs/guide.txt"]  # nothing written


def test_parent_became_a_file_blocks(store, repo):
    ws = store.create("a")
    write(ws.tree, "pkg/mod.py", "x = 1\n")
    write(ws.tree, "README.md", "also changed\n")
    write(repo, "pkg", "a file now\n")
    res = merge_workspace(store, "a")
    assert [(c.kind, c.path) for c in res.conflicts] == [("type-change", "pkg/mod.py")]
    assert read(repo, "README.md") == "# demo\n"


def test_failure_mid_merge_rolls_back(store, repo, monkeypatch):
    ws = store.create("a")
    write(ws.tree, "README.md", "new readme\n")
    write(ws.tree, "src/util.py", "new util\n")
    write(ws.tree, "zzz/new.txt", "new file\n")
    (ws.tree / "docs/guide.txt").unlink()

    real = merge._write_into
    calls = []

    def flaky(root, ws_, w):
        calls.append(w.path)
        if len(calls) == 3:
            raise OSError(28, "No space left on device")
        real(root, ws_, w)

    monkeypatch.setattr(merge, "_write_into", flaky)
    with pytest.raises(OSError):
        merge_workspace(store, "a")
    # Everything is back as it was, and the journal is gone.
    assert read(repo, "README.md") == "# demo\n"
    assert read(repo, "src/util.py") == SAMPLE["src/util.py"]
    assert read(repo, "docs/guide.txt") == SAMPLE["docs/guide.txt"]
    assert not (repo / "zzz").exists()
    assert not store.txn_dir.exists()
    # The workspace still has its changes and can merge once the problem is gone.
    monkeypatch.setattr(merge, "_write_into", real)
    assert merge_workspace(store, "a").merged
    assert read(repo, "zzz/new.txt") == "new file\n"


def test_crash_is_recovered_by_next_lock(store, repo):
    """Simulate a process killed half-way: journal on disk, repo partly written."""
    store.init()
    txn = store.txn_dir
    (txn / "backup/src").mkdir(parents=True)
    (txn / "backup/src/util.py").write_text(SAMPLE["src/util.py"])
    (txn / "journal.json").write_text(
        json.dumps([{"path": "src/util.py", "existed": True}, {"path": "new/file.txt", "existed": False}])
    )
    write(repo, "src/util.py", "half-merged\n")
    write(repo, "new/file.txt", "half-merged\n")

    with store.lock():
        pass

    assert read(repo, "src/util.py") == SAMPLE["src/util.py"]
    assert not (repo / "new").exists()
    assert not txn.exists()


def test_crash_while_taking_backups_touches_nothing(tmp_path):
    repo, txn = tmp_path / "repo", tmp_path / "txn"
    repo.mkdir()
    write(repo, "a.txt", "a\n")
    (txn / "backup").mkdir(parents=True)  # no journal.json yet
    assert journal.rollback(txn, repo) is False
    assert not txn.exists() and read(repo, "a.txt") == "a\n"


def test_journal_refuses_to_overwrite_existing(tmp_path):
    (tmp_path / "txn").mkdir()
    with pytest.raises(RuntimeError):
        journal.run(tmp_path / "txn", tmp_path, [], lambda: None)


def test_snapshot_waits_for_and_sees_consistent_repo(store, repo):
    """create() takes the lock, so it also recovers a crashed merge first."""
    store.init()
    (store.txn_dir / "backup").mkdir(parents=True)
    (store.txn_dir / "backup/README.md").write_text("# demo\n")
    (store.txn_dir / "journal.json").write_text(json.dumps([{"path": "README.md", "existed": True}]))
    write(repo, "README.md", "half-merged\n")
    ws = store.create("a")
    assert read(ws.tree, "README.md") == "# demo\n"
