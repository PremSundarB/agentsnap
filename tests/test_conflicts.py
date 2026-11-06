from agentsnap.conflicts import Conflict, check
from agentsnap.merge import merge_workspace

from .conftest import SAMPLE, write

APP = SAMPLE["src/app.py"]


def kinds(report):
    return sorted((c.kind, c.path, c.workspaces) for c in report.conflicts)


def test_no_conflicts_when_disjoint(store):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "src/util.py", "a\n")
    write(b.tree, "README.md", "b\n")
    report = check(store)
    assert report.conflicts == []
    assert report.to_dict()["errors"] == 0


def test_write_write_overlap(store):
    a, b, c = store.create("a"), store.create("b"), store.create("c")
    write(a.tree, "README.md", "a\n")
    write(b.tree, "README.md", "b\n")
    (c.tree / "README.md").unlink()  # deletes count as writes
    report = check(store)
    assert kinds(report) == [("write-write", "README.md", ("a", "b", "c"))]
    assert report.errors and not report.warnings


def test_overlap_only_among_selected(store):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "README.md", "a\n")
    write(b.tree, "README.md", "b\n")
    assert check(store, ["a"]).conflicts == []


def test_python_overlap_reports_functions(store):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "src/app.py", APP.replace("hello", "hi"))
    write(b.tree, "src/app.py", APP.replace("bye", "ciao"))
    (c,) = check(store).conflicts
    assert c.symbols == {"a": ("greet",), "b": ("farewell",)}
    assert c.symbols_disjoint() is True
    assert "different functions" in c.describe()


def test_python_overlap_same_function(store):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "src/app.py", APP.replace("hello", "hi"))
    write(b.tree, "src/app.py", APP.replace("hello", "hey"))
    (c,) = check(store).conflicts
    assert c.symbols_disjoint() is False
    assert "same function" in c.describe()


def test_stale_write_after_other_merge(store):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "README.md", "a\n")
    write(b.tree, "README.md", "b\n")
    assert merge_workspace(store, "a").merged
    assert kinds(check(store)) == [("stale-write", "README.md", ("b",))]


def test_convergent_edit_is_not_stale(store):
    a, b = store.create("a"), store.create("b")
    write(a.tree, "README.md", "same\n")
    write(b.tree, "README.md", "same\n")
    assert merge_workspace(store, "a").merged
    assert check(store).conflicts == []


def test_stale_read_warning(store, repo):
    a = store.create("a")
    a.reads.record(["src/util.py"])
    write(a.tree, "README.md", "a\n")
    write(repo, "src/util.py", "changed upstream\n")
    report = check(store)
    assert kinds(report) == [("stale-read", "src/util.py", ("a",))]
    assert report.warnings and not report.errors


def test_read_write_warning(store):
    a, b = store.create("a"), store.create("b")
    a.reads.record(["src/util.py"])
    write(b.tree, "src/util.py", "b\n")
    assert kinds(check(store)) == [("read-write", "src/util.py", ("a", "b"))]


def test_describe_and_dict_cover_all_kinds():
    for kind in ["write-write", "stale-write", "stale-read", "read-write", "other"]:
        c = Conflict(path="p", kind=kind, workspaces=("a", "b"))
        assert "p" in c.describe()
        assert c.to_dict()["kind"] == kind


def test_reads_of_ignored_paths_are_not_reported(store, repo):
    a = store.create("a")
    a.reads.record(["node_modules/pkg/index.js"])
    write(repo, "node_modules/pkg/index.js", "upgraded\n")
    assert check(store).conflicts == []
