import subprocess

import pytest

from agentsnap.merge import Policy, merge_workspace

from .conftest import SAMPLE, read, write

APP = SAMPLE["src/app.py"]
GUIDE = SAMPLE["docs/guide.txt"]


def test_clean_merge_add_modify_delete(store, repo):
    ws = store.create("a")
    write(ws.tree, "src/util.py", "def add(a, b):\n    return b + a\n")
    write(ws.tree, "pkg/sub/new.py", "x = 1\n")
    (ws.tree / "docs/guide.txt").unlink()
    res = merge_workspace(store, "a")
    assert res.merged
    assert res.applied == {"src/util.py": "modified", "pkg/sub/new.py": "added", "docs/guide.txt": "deleted"}
    assert read(repo, "src/util.py").endswith("b + a\n")
    assert read(repo, "pkg/sub/new.py") == "x = 1\n"
    assert not (repo / "docs").exists()  # emptied directory pruned
    # merging again is a no-op
    again = merge_workspace(store, "a")
    assert again.merged and again.applied == {}
    assert not store.get("a").changes()


def test_file_mode_and_symlinks_merge(store, repo):
    ws = store.create("a")
    (ws.tree / "app_link.py").unlink()
    (ws.tree / "app_link.py").symlink_to("src/util.py")
    (ws.tree / "src/app.py").chmod(0o755)
    write(ws.tree, "src/app.py", APP + "# x\n")
    assert merge_workspace(store, "a").merged
    assert (repo / "app_link.py").is_symlink()
    assert (repo / "app_link.py").readlink().as_posix() == "src/util.py"
    assert (repo / "src/app.py").stat().st_mode & 0o100


def test_dry_run_changes_nothing(store, repo):
    ws = store.create("a")
    write(ws.tree, "README.md", "new\n")
    res = merge_workspace(store, "a", dry_run=True)
    assert not res.merged and res.applied == {"README.md": "modified"}
    assert read(repo, "README.md") == "# demo\n"


def test_first_committer_wins(store, repo):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "README.md", "from a\n")
    write(b.tree, "README.md", "from b\n")
    assert merge_workspace(store, "a").merged
    res = merge_workspace(store, "b")
    assert not res.merged
    assert [c.path for c in res.conflicts] == ["README.md"]
    assert read(repo, "README.md") == "from a\n"  # never silently overwritten


def test_merge_is_all_or_nothing(store, repo):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "README.md", "from a\n")
    write(b.tree, "README.md", "from b\n")
    write(b.tree, "src/util.py", "clean change from b\n")
    merge_workspace(store, "a")
    res = merge_workspace(store, "b")
    assert not res.merged and res.applied == {}
    assert read(repo, "src/util.py") == SAMPLE["src/util.py"]


def test_merge3_resolves_disjoint_edits(store, repo):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "docs/guide.txt", GUIDE.replace("line 1\n", "line ONE\n"))
    write(b.tree, "docs/guide.txt", GUIDE.replace("line 8\n", "line EIGHT\n"))
    merge_workspace(store, "a")
    assert not merge_workspace(store, "b").merged
    res = merge_workspace(store, "b", policy="merge3")
    assert res.merged and res.applied == {"docs/guide.txt": "merged"}
    text = read(repo, "docs/guide.txt")
    assert "line ONE" in text and "line EIGHT" in text
    # the workspace now sees the merged result and has nothing left to merge
    assert read(b.tree, "docs/guide.txt") == text
    assert not store.get("b").changes()


def test_merge3_python_different_functions(store, repo):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "src/app.py", APP.replace("hello", "hi"))
    write(b.tree, "src/app.py", APP.replace("self.n += 1", "self.n += 2"))
    merge_workspace(store, "a")
    assert merge_workspace(store, "b", policy=Policy.MERGE3).merged
    text = read(repo, "src/app.py")
    assert "hi {name}" in text and "self.n += 2" in text


def test_merge3_same_lines_still_conflicts(store, repo):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "docs/guide.txt", GUIDE.replace("line 4\n", "four-a\n"))
    write(b.tree, "docs/guide.txt", GUIDE.replace("line 4\n", "four-b\n"))
    merge_workspace(store, "a")
    res = merge_workspace(store, "b", policy="merge3")
    assert not res.merged
    assert "<<<<<<<" not in read(repo, "docs/guide.txt")


def test_merge3_cannot_merge_delete(store):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "README.md", "edited\n")
    (b.tree / "README.md").unlink()
    merge_workspace(store, "a")
    assert not merge_workspace(store, "b", policy="merge3").merged


def test_rerun_policy_resnapshots(store, repo):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "README.md", "from a\n")
    write(b.tree, "README.md", "from b\n")
    merge_workspace(store, "a")
    res = merge_workspace(store, "b", policy="rerun")
    assert not res.merged and res.refreshed
    b = store.get("b")
    assert b.attempts == 1
    assert read(b.tree, "README.md") == "from a\n"
    assert read(b.dir, "attempts/1/README.md") == "from b\n"
    # agent redoes its work on top of a's change; now it merges cleanly
    write(b.tree, "README.md", "from a\nand b\n")
    assert merge_workspace(store, "b").merged
    assert read(repo, "README.md") == "from a\nand b\n"


def test_rerun_dry_run_does_not_refresh(store):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "README.md", "from a\n")
    write(b.tree, "README.md", "from b\n")
    merge_workspace(store, "a")
    res = merge_workspace(store, "b", policy="rerun", dry_run=True)
    assert not res.refreshed and store.get("b").attempts == 0


def test_force_overwrites(store, repo):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "README.md", "from a\n")
    write(b.tree, "README.md", "from b\n")
    merge_workspace(store, "a")
    assert merge_workspace(store, "b", policy="force").merged
    assert read(repo, "README.md") == "from b\n"


def test_stale_read_is_a_warning_not_a_blocker(store, repo):
    a = store.create("a")
    a.reads.record(["src/util.py"])
    write(a.tree, "README.md", "a\n")
    write(repo, "src/util.py", "upstream\n")
    res = merge_workspace(store, "a")
    assert res.merged
    assert [w.kind for w in res.warnings] == ["stale-read"]
    assert store.get("a").reads.paths() == frozenset()  # reset after merge


def test_unknown_policy(store):
    store.create("a")
    with pytest.raises(ValueError):
        merge_workspace(store, "a", policy="yolo")


def test_commit(git_repo):
    from agentsnap.workspace import Store

    store = Store(git_repo)
    ws = store.create("a")
    write(ws.tree, "src/new.py", "x = 1\n")
    (ws.tree / "README.md").unlink()
    write(git_repo, "unrelated.txt", "should not be committed\n")
    res = merge_workspace(store, "a", commit=True, message="add new")
    assert res.commit
    log = subprocess.run(
        ["git", "-C", str(git_repo), "show", "--stat", "--format=%s", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "add new" in log and "src/new.py" in log and "README.md" in log
    assert "unrelated.txt" not in log
