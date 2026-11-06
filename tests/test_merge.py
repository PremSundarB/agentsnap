import shlex
import subprocess
import sys

import pytest

from agentsnap.merge import Policy, merge_workspace

from .conftest import SAMPLE, read, write

APP = SAMPLE["src/app.py"]
GUIDE = SAMPLE["docs/guide.txt"]


def py(code: str) -> str:
    """Shell command running ``code`` with this interpreter, with src/ importable."""
    code = "import sys; sys.path.insert(0, 'src')\n" + code
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


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
    assert b.needs_approval == ["README.md"]
    # agent redoes its work on top of a's change; it merges cleanly, but the
    # conflict it resolved needs a person's approval first
    write(b.tree, "README.md", "from a\nand b\n")
    waiting = merge_workspace(store, "b")
    assert not waiting.merged and waiting.awaiting_approval == ["README.md"]
    assert read(repo, "README.md") == "from a\n"
    assert merge_workspace(store, "b", approve=True).merged
    assert read(repo, "README.md") == "from a\nand b\n"
    assert store.get("b").needs_approval == []
    assert store.get("b").merges[-1]["approved"] == ["README.md"]


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


def test_commit_with_untracked_deletion_does_not_fail(git_repo):
    from agentsnap.workspace import Store

    write(git_repo, "scratch.txt", "untracked\n")
    store = Store(git_repo)
    ws = store.create("a")
    (ws.tree / "scratch.txt").unlink()  # deleting a file git never tracked
    write(ws.tree, "src/new.py", "x = 1\n")
    res = merge_workspace(store, "a", commit=True)
    assert res.merged and res.commit and res.commit_error is None
    assert not (git_repo / "scratch.txt").exists()


def test_commit_outside_git_reports_error(store):
    ws = store.create("a")
    write(ws.tree, "x.txt", "x\n")
    res = merge_workspace(store, "a", commit=True)
    assert res.merged and res.commit is None and res.commit_error == "not a git repository"


def test_refuses_to_write_through_symlinked_parent(store, repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    a, b = store.create("a"), store.create("b")
    (a.tree / "evil").symlink_to(outside)  # agent a adds a link to outside the repo
    write(b.tree, "evil/payload.txt", "pwned\n")  # agent b writes "into" it
    assert merge_workspace(store, "a").merged
    res = merge_workspace(store, "b")
    assert not res.merged
    assert [c.kind for c in res.conflicts] == ["unsafe-path"]
    assert not (outside / "payload.txt").exists()
    # even force does not bypass this
    assert not merge_workspace(store, "b", policy="force").merged
    assert not (outside / "payload.txt").exists()


def test_empty_merge_does_not_clear_approval(store):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "README.md", "from a\n")
    write(b.tree, "README.md", "from b\n")
    merge_workspace(store, "a")
    merge_workspace(store, "b", policy="rerun")
    res = merge_workspace(store, "b")  # nothing changed yet
    assert res.applied == {} and not res.awaiting_approval
    write(store.get("b").tree, "README.md", "resolved\n")
    assert merge_workspace(store, "b").awaiting_approval == ["README.md"]


def test_check_passes(store, repo):
    ws = store.create("a")
    write(ws.tree, "src/util.py", "def add(a, b):\n    return a + b + 0\n")
    res = merge_workspace(store, "a", check=py("import util; assert util.add(1, 2) == 3"))
    assert res.merged and res.check["returncode"] == 0
    assert read(repo, "src/util.py").endswith("+ 0\n")
    assert not (store.root / "scratch").exists() or not any((store.root / "scratch").iterdir())


def test_check_failure_blocks_merge(store, repo):
    ws = store.create("a")
    write(ws.tree, "src/util.py", "def add(a, b):\n    return a - b\n")
    res = merge_workspace(store, "a", check=py("import util; assert util.add(1, 2) == 3, 'add broke'"))
    assert not res.merged and res.check["returncode"] != 0
    assert "add broke" in res.check["output"]
    assert read(repo, "src/util.py") == SAMPLE["src/util.py"]  # untouched
    assert not any((store.root / "scratch").iterdir())


def test_check_sees_merged_result_not_just_workspace(store, repo):
    """Two clean edits that only break when combined: the check must catch it."""
    a, b = store.create("a"), store.create("b")
    write(a.tree, "src/util.py", "def plus(a, b):\n    return a + b\n")  # renames add -> plus
    write(b.tree, "src/use.py", "from util import add\nassert add(1, 1) == 2\n")  # still calls add
    check = py("import os\nif os.path.exists('src/use.py'): import use")
    assert merge_workspace(store, "a", check=check).merged
    res = merge_workspace(store, "b", check=check)  # no file conflict at all...
    assert res.conflicts == [] and not res.merged  # ...but the check catches it
    assert not (repo / "src/use.py").exists()


def test_check_from_config_and_timeout(repo):
    from agentsnap.workspace import Store

    write(repo, ".agentsnap/config.json", '{"merge_check": "sleep 5", "merge_check_timeout": 0.5}')
    store = Store(repo)
    ws = store.create("a")
    write(ws.tree, "x.txt", "x\n")
    res = merge_workspace(store, "a")
    assert not res.merged and res.check["returncode"] == -1
    assert "timed out" in res.check["output"]


def test_check_not_run_when_nothing_to_merge(store):
    store.create("a")
    res = merge_workspace(store, "a", check="exit 1")
    assert res.check is None and res.applied == {}
