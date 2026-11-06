import io
import json
import sys

import pytest

from agentsnap.cli import main

from .conftest import read, write


@pytest.fixture
def cli(repo, capsys):
    def run(*args, stdin=None):
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            rc = main(["-C", str(repo), *args])
        finally:
            sys.stdin = sys.__stdin__
        out = capsys.readouterr()
        return rc, out.out, out.err

    return run


def test_new_list_status_diff(cli, repo):
    rc, out, _ = cli("new", "a", "b")
    assert rc == 0 and "created a" in out and "created b" in out
    rc, out, _ = cli("path", "a")
    tree = out.strip()
    write(repo / tree, "README.md", "changed\n")

    rc, out, _ = cli("list")
    assert "a" in out and "+0 ~1 -0" in out
    rc, out, _ = cli("status", "a")
    assert "M README.md" in out
    rc, out, _ = cli("diff", "a")
    assert "+changed" in out
    rc, out, _ = cli("list", "--json")
    data = json.loads(out)
    assert [w["name"] for w in data] == ["a", "b"]
    assert data[0]["changes"]["modified"] == ["README.md"]


def test_new_quiet_and_json(cli):
    rc, out, _ = cli("new", "-q", "a")
    assert out.strip().endswith("workspaces/a/tree")
    rc, out, _ = cli("new", "--json", "b")
    assert json.loads(out)["elapsed_ms"] >= 0


def test_empty_list(cli):
    rc, out, _ = cli("list")
    assert "no workspaces" in out


def test_conflicts_and_merge_exit_codes(cli, store):
    cli("new", "a", "b")
    write(store.get("a").tree, "README.md", "a\n")
    write(store.get("b").tree, "README.md", "b\n")

    rc, out, _ = cli("conflicts")
    assert rc == 1 and "write-write" in out
    rc, out, _ = cli("conflicts", "--json")
    assert json.loads(out)["errors"] == 1

    rc, out, _ = cli("merge", "--dry-run", "a")
    assert rc == 0 and "would merge 1 file" in out
    rc, out, _ = cli("merge", "a", "b")
    assert rc == 1
    assert "a: merged 1 file" in out and "b: NOT merged" in out and "--policy merge3" in out

    rc, out, _ = cli("merge", "--policy", "rerun", "b")
    assert rc == 1 and "re-snapshotted" in out
    rc, out, _ = cli("merge", "b")
    assert rc == 0 and "nothing to merge" in out
    rc, out, _ = cli("conflicts")
    assert rc == 0 and "no conflicts" in out


def test_merge_json(cli, store):
    cli("new", "a")
    write(store.get("a").tree, "x.txt", "x\n")
    rc, out, _ = cli("merge", "--json", "a")
    assert json.loads(out)["applied"] == {"x.txt": "added"}


def test_refresh_and_drop(cli, store):
    cli("new", "a")
    write(store.get("a").tree, "x.txt", "x\n")
    rc, out, _ = cli("drop", "a")
    assert rc == 1
    rc, out, _ = cli("refresh", "a")
    assert rc == 0 and "re-snapshotted" in out
    rc, out, _ = cli("drop", "a")
    assert rc == 0 and "dropped a" in out


def test_unknown_workspace_is_usage_error(cli):
    rc, _, err = cli("status", "ghost")
    assert rc == 2 and "no workspace named 'ghost'" in err


def test_run_inside_workspace(cli, store):
    cli("new", "a")
    rc, _, _ = cli(
        "run",
        "a",
        "--",
        sys.executable,
        "-c",
        "import os; open('from_agent.txt','w').write(os.environ['AGENTSNAP_WORKSPACE'])",
    )
    assert rc == 0
    assert read(store.get("a").tree, "from_agent.txt") == "a"
    rc, _, err = cli("run", "a")
    assert rc == 2


def test_record_read_from_claude_code_hook(cli, store):
    cli("new", "a")
    ws = store.get("a")
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "cwd": str(ws.tree),
        "tool_input": {"file_path": "src/app.py"},
    }
    rc, _, _ = cli("record-read", stdin=json.dumps(payload))
    assert rc == 0
    assert ws.reads.paths() == {"src/app.py"}
    # paths outside any workspace, and garbage input, are ignored silently
    rc, _, _ = cli("record-read", stdin=json.dumps({"tool_input": {"file_path": "/etc/hosts"}}))
    assert rc == 0
    rc, _, _ = cli("record-read", stdin="not json")
    assert rc == 0


def test_record_read_explicit(cli, store):
    cli("new", "a")
    ws = store.get("a")
    rc, out, _ = cli("record-read", "-v", "-w", "a", str(ws.tree / "README.md"), "/etc/hosts")
    assert "recorded 1 read" in out
    assert ws.reads.paths() == {"README.md"}


def test_python_m_entrypoint(repo):
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "agentsnap", "-C", str(repo), "list"], capture_output=True, text=True
    )
    assert out.returncode == 0 and "no workspaces" in out.stdout
