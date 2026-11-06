import asyncio
import json

import pytest

pytest.importorskip("mcp")

from agentsnap.mcp_server import build_server  # noqa: E402

from .conftest import read, write  # noqa: E402


class ToolFailed(Exception):
    pass


def call(server, tool, /, **args):
    try:
        result = asyncio.run(server.call_tool(tool, args))
    except Exception as exc:  # SDK-specific error types
        raise ToolFailed(str(exc)) from exc
    # SDK 2.x returns CallToolResult; 1.x returns content (or (content, structured)).
    if hasattr(result, "content"):
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            raise ToolFailed(result.content[0].text)
        content = result.content
    else:
        content = result[0] if isinstance(result, tuple) else result
    text = "".join(c.text for c in content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def tool_names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def test_tools_listed(repo):
    assert {
        "create_workspace",
        "list_workspaces",
        "workspace_status",
        "workspace_diff",
        "read_file",
        "write_file",
        "record_reads",
        "check_conflicts",
        "merge_workspace",
        "refresh_workspace",
        "drop_workspace",
    } <= tool_names(build_server(repo))


def test_full_agent_flow(repo):
    s = build_server(repo)
    info = call(s, "create_workspace", name="fix")
    assert info["name"] == "fix" and info["path"].endswith("fix/tree")

    assert call(s, "read_file", workspace="fix", path="src/util.py").startswith("def add")
    call(s, "write_file", workspace="fix", path="src/util.py", content="def add(a, b):\n    return b + a\n")
    call(s, "record_reads", workspace="fix", paths=["README.md"])

    status = call(s, "workspace_status", name="fix")
    assert status["changes"]["modified"] == ["src/util.py"]
    assert status["reads"] == ["README.md", "src/util.py"]
    assert "+    return b + a" in call(s, "workspace_diff", name="fix")
    listed = call(s, "list_workspaces")["workspaces"]
    assert listed[0]["name"] == "fix" and listed[0]["modified"] == 1

    assert call(s, "check_conflicts")["errors"] == 0
    res = call(s, "merge_workspace", name="fix")
    assert res["merged"] and res["applied"] == {"src/util.py": "modified"}
    assert read(repo, "src/util.py").endswith("b + a\n")
    assert call(s, "drop_workspace", name="fix") == "dropped fix"


def test_conflict_flow_with_rerun(repo):
    s = build_server(repo)
    call(s, "create_workspace", name="a")
    call(s, "create_workspace", name="b")
    call(s, "write_file", workspace="a", path="README.md", content="a\n")
    call(s, "write_file", workspace="b", path="README.md", content="b\n")
    assert call(s, "check_conflicts")["errors"] == 1
    assert call(s, "merge_workspace", name="a")["merged"]
    res = call(s, "merge_workspace", name="b", policy="rerun")
    assert not res["merged"] and res["refreshed"]
    assert call(s, "read_file", workspace="b", path="README.md") == "a\n"


def test_path_traversal_refused(repo):
    s = build_server(repo)
    call(s, "create_workspace", name="a")
    write(repo, "secret.txt", "nope\n")
    for bad in ["../../../../secret.txt", "/etc/hosts"]:
        with pytest.raises(ToolFailed, match="outside workspace"):
            call(s, "read_file", workspace="a", path=bad)
        with pytest.raises(ToolFailed, match="outside workspace"):
            call(s, "write_file", workspace="a", path=bad, content="x")
    assert read(repo, "secret.txt") == "nope\n"


def test_drop_refuses_unmerged(repo):
    s = build_server(repo)
    call(s, "create_workspace", name="a")
    call(s, "write_file", workspace="a", path="x.txt", content="x")
    with pytest.raises(ToolFailed, match="unmerged changes"):
        call(s, "drop_workspace", name="a")
    assert call(s, "drop_workspace", name="a", force=True) == "dropped a"


def test_errors_are_readable(repo):
    s = build_server(repo)
    with pytest.raises(ToolFailed, match="invalid workspace name"):
        call(s, "create_workspace", name="../evil")
    with pytest.raises(ToolFailed, match="no workspace named"):
        call(s, "workspace_status", name="ghost")
