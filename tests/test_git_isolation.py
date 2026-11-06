"""Git commands run by an agent inside its workspace must never touch the main repo."""

import os
import subprocess

import pytest

from agentsnap.merge import merge_workspace
from agentsnap.snapshot import probe_methods
from agentsnap.workspace import Store

from .conftest import read, write

ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=agent", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env=ENV,
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture(params=["native", "copy"])
def method(request, tmp_path):
    """'native' = best method here (CoW clone of .git if possible); 'copy' = shared-clone path."""
    if request.param == "native":
        return probe_methods(tmp_path)[0]
    return "copy"


def test_git_in_workspace_is_private(git_repo, method):
    write(git_repo, "README.md", "user's uncommitted work\n")
    store = Store(git_repo)
    ws = store.create("a", method=method)

    top = git(ws.tree, "rev-parse", "--show-toplevel").stdout.strip()
    assert os.path.realpath(top) == os.path.realpath(ws.tree)

    # The agent's git sees the agent's own changes...
    write(ws.tree, "src/util.py", "agent edit\n")
    status = git(ws.tree, "status", "--porcelain").stdout
    assert "src/util.py" in status

    # ...and destructive commands only affect the workspace.
    head_before = git(git_repo, "rev-parse", "HEAD").stdout
    git(ws.tree, "checkout", "--", ".")
    git(ws.tree, "reset", "--hard", "-q")
    write(ws.tree, "agent.txt", "x\n")
    git(ws.tree, "add", "-A")
    git(ws.tree, "commit", "-qm", "agent commit")
    assert read(git_repo, "README.md") == "user's uncommitted work\n"
    assert git(git_repo, "rev-parse", "HEAD").stdout == head_before
    assert "README.md" in git(git_repo, "status", "--porcelain").stdout


def test_private_git_is_not_merged_back(git_repo, method):
    store = Store(git_repo)
    ws = store.create("a", method=method)
    write(ws.tree, "new.py", "x = 1\n")
    git(ws.tree, "add", "-A")
    git(ws.tree, "commit", "-qm", "agent commit")
    res = merge_workspace(store, "a")
    assert res.merged and res.applied == {"new.py": "added"}
    assert git(git_repo, "log", "--oneline").stdout.count("\n") == 1  # only "initial"


def test_refresh_keeps_git_private(git_repo, method):
    store = Store(git_repo)
    store.create("a", method=method)
    ws = store.refresh("a")
    top = git(ws.tree, "rev-parse", "--show-toplevel").stdout.strip()
    assert os.path.realpath(top) == os.path.realpath(ws.tree)


def test_workspace_git_none_blocks_git(git_repo):
    write(git_repo, ".agentsnap/config.json", '{"workspace_git": "none"}')
    store = Store(git_repo)
    ws = store.create("a")
    proc = git(ws.tree, "status", check=False)
    assert proc.returncode != 0
    assert not ws.changes()  # the blocker file is not a change


def test_non_git_repo_has_no_git(store):
    ws = store.create("a")
    assert not (ws.tree / ".git").exists()
