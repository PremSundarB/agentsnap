import pytest

from agentsnap.workspace import Store, WorkspaceError, find_repo

from .conftest import contents, read, write


def test_create_workspace(store, repo):
    ws = store.create("agent-1")
    assert ws.tree.is_dir() and ws.base_dir.is_dir()
    assert ws.tree == repo / ".agentsnap" / "workspaces" / "agent-1" / "tree"
    assert read(ws.tree, "src/app.py") == read(repo, "src/app.py")
    assert contents(ws.base_dir) == contents(repo) == contents(ws.tree)
    assert not ws.changes()
    # state dir hides itself from git
    assert read(repo, ".agentsnap/.gitignore").strip().endswith("*")


def test_snapshot_excludes_git_and_state(git_repo):
    store = Store(git_repo)
    store.create("a")
    ws = store.create("b")
    assert not (ws.tree / ".git").exists()
    assert not (ws.tree / ".agentsnap").exists()


def test_build_cache_dirs_are_cloned_but_not_tracked(store):
    ws = store.create("a")
    assert (ws.tree / "node_modules/pkg/index.js").exists()
    write(ws.tree, "node_modules/pkg/index.js", "rebuilt\n")
    assert not ws.changes()


def test_changes_detected(store):
    ws = store.create("a")
    write(ws.tree, "src/util.py", "def add(a, b):\n    return b + a\n")
    write(ws.tree, "src/new.py", "x = 1\n")
    (ws.tree / "README.md").unlink()
    cs = ws.changes()
    assert cs.modified == {"src/util.py"}
    assert cs.added == {"src/new.py"}
    assert cs.deleted == {"README.md"}


def test_diff(store):
    ws = store.create("a")
    write(ws.tree, "src/util.py", "def add(a, b):\n    return b + a\n")
    write(ws.tree, "new.txt", "hello")
    d = ws.diff()
    assert "--- a/src/util.py" in d and "+    return b + a" in d
    assert "--- /dev/null" in d and "+++ b/new.txt" in d
    assert "No newline at end of file" in d
    assert ws.diff(["new.txt"]).count("+++") == 1


def test_get_list_drop(store):
    store.create("b")
    store.create("a")
    assert [w.name for w in store.list()] == ["a", "b"]
    assert store.get("a").name == "a"
    store.drop("a")
    assert [w.name for w in store.list()] == ["b"]
    with pytest.raises(WorkspaceError):
        store.get("a")


def test_list_when_uninitialised(store):
    assert store.list() == []


@pytest.mark.parametrize("bad", ["", "../x", "a/b", ".hidden", "-x", "x" * 65, "sp ace"])
def test_invalid_names(store, bad):
    with pytest.raises(WorkspaceError):
        store.create(bad)


def test_duplicate_name(store):
    store.create("a")
    with pytest.raises(WorkspaceError):
        store.create("a")


def test_failed_create_leaves_nothing_behind(store, monkeypatch):
    import agentsnap.workspace as wsmod

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(wsmod, "create_snapshot", boom)
    with pytest.raises(OSError):
        store.create("a")
    assert not (store.ws_root / "a").exists()


def test_find_repo_from_inside_workspace(store, repo):
    ws = store.create("a")
    assert find_repo(ws.tree / "src") == repo
    assert find_repo(repo / "src") == repo


def test_find_repo_falls_back_to_git_then_cwd(tmp_path):
    (tmp_path / "proj/.git").mkdir(parents=True)
    (tmp_path / "proj/sub").mkdir()
    assert find_repo(tmp_path / "proj/sub") == (tmp_path / "proj").resolve()
    (tmp_path / "loose").mkdir()
    assert find_repo(tmp_path / "loose") == (tmp_path / "loose").resolve()


def test_workspace_for_path(store, repo):
    ws = store.create("a")
    hit = store.workspace_for_path(ws.tree / "src" / "app.py")
    assert hit is not None and hit[0].name == "a" and hit[1] == "src/app.py"
    assert store.workspace_for_path(repo / "src" / "app.py") is None
    assert store.workspace_for_path(ws.base_dir / "src" / "app.py") is None


def test_refresh_keeps_old_attempt(store, repo):
    ws = store.create("a")
    write(ws.tree, "src/util.py", "attempt one\n")
    ws.reads.record(["README.md"])
    write(repo, "README.md", "# updated upstream\n")
    ws = store.refresh("a")
    assert ws.attempts == 1
    assert read(ws.tree, "README.md") == "# updated upstream\n"
    assert read(ws.tree, "src/util.py") == read(repo, "src/util.py")
    assert read(ws.dir, "attempts/1/src/util.py") == "attempt one\n"
    assert not ws.changes()
    assert ws.reads.paths() == frozenset()
    assert store.get("a").attempts == 1


def test_config_extra_ignore(repo):
    write(repo, ".agentsnap/config.json", '{"extra_ignore": ["generated"]}')
    write(repo, "generated/out.txt", "x\n")
    store = Store(repo)
    ws = store.create("a")
    write(ws.tree, "generated/out.txt", "y\n")
    assert not ws.changes()
