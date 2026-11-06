import os

from agentsnap.tracking import (
    ChangeSet,
    ReadLog,
    build_manifest,
    diff_manifests,
    diff_trees,
    file_sig,
    hash_path,
    same_content,
)

from .conftest import write


def test_manifest_skips_ignored_dirs(repo):
    m = build_manifest(repo)
    assert "src/app.py" in m
    assert "app_link.py" in m
    assert not any(p.startswith("node_modules/") for p in m)


def test_manifest_with_custom_ignore(repo):
    m = build_manifest(repo, ignore=())
    assert "node_modules/pkg/index.js" in m


def test_symlinked_directory_is_tracked_as_link(repo):
    os.symlink("src", repo / "srclink")
    m = build_manifest(repo)
    assert "srclink" in m
    assert not any(p.startswith("srclink/") for p in m)


def test_symlink_hash_depends_on_target_not_content(repo):
    before = hash_path(repo, "app_link.py")
    write(repo, "src/app.py", "different\n")
    assert hash_path(repo, "app_link.py") == before
    os.remove(repo / "app_link.py")
    os.symlink("src/util.py", repo / "app_link.py")
    assert hash_path(repo, "app_link.py") != before


def test_hash_path_missing_is_none(repo):
    assert hash_path(repo, "nope.txt") is None
    assert hash_path(repo, "src") is None  # directories are not files


def test_diff_manifests():
    base = {"a": "1", "b": "2", "c": "3"}
    cur = {"a": "1", "b": "X", "d": "4"}
    cs = diff_manifests(base, cur)
    assert cs.added == {"d"}
    assert cs.modified == {"b"}
    assert cs.deleted == {"c"}
    assert cs.paths == {"b", "c", "d"}
    assert cs.written == {"b", "d"}
    assert cs.kind_of("c") == "deleted" and cs.kind_of("a") is None
    assert len(cs) == 3 and bool(cs)
    assert cs.to_dict() == {"added": ["d"], "modified": ["b"], "deleted": ["c"]}


def test_empty_changeset_is_falsy():
    assert not ChangeSet()


def test_read_log(tmp_path):
    log = ReadLog(tmp_path / "reads.jsonl")
    assert log.paths() == frozenset()
    log.record(["a.py", "b.py"])
    log.record(["a.py"])
    with open(log.path, "a") as fh:
        fh.write('{"path": "torn')  # simulate a crash mid-write
    assert log.paths() == {"a.py", "b.py"}
    log.clear()
    assert log.paths() == frozenset()


def test_file_sig(repo):
    assert file_sig(repo / "README.md")[0] == "f"
    assert file_sig(repo / "app_link.py")[0] == "l"
    assert file_sig(repo / "src") is None
    assert file_sig(repo / "missing") is None


def test_same_content(repo, tmp_path):
    a, b = write(tmp_path, "a", "same\n"), write(tmp_path, "b", "same\n")
    os.utime(b, ns=(1, 1))  # different mtime forces a content comparison
    assert same_content(a, b)
    write(tmp_path, "b", "diff\n")
    assert not same_content(a, b)
    write(tmp_path, "c", "longer content\n")
    assert not same_content(a, tmp_path / "c")
    assert same_content(tmp_path / "x", tmp_path / "y")  # both missing
    assert not same_content(a, tmp_path / "y")
    assert not same_content(a, repo / "app_link.py")  # file vs symlink


def test_diff_trees_ignores_touch_without_change(repo, tmp_path):
    import shutil

    shutil.copytree(repo, tmp_path / "copy", symlinks=True)
    os.utime(tmp_path / "copy/README.md", ns=(5, 5))  # touched, same bytes
    write(tmp_path / "copy", "src/util.py", "changed\n")
    cs = diff_trees(repo, tmp_path / "copy")
    assert cs.modified == {"src/util.py"} and not cs.added and not cs.deleted


def test_same_size_edit_is_detected(repo, tmp_path):
    import shutil

    shutil.copytree(repo, tmp_path / "copy", symlinks=True)
    p = tmp_path / "copy/README.md"
    p.write_text("# DEMO\n")  # same length, new mtime
    assert diff_trees(repo, tmp_path / "copy").modified == {"README.md"}
