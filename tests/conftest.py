from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agentsnap.workspace import Store

SAMPLE = {
    "README.md": "# demo\n",
    "src/app.py": (
        "import os\n"
        "\n"
        "\n"
        "def greet(name):\n"
        "    return f'hello {name}'\n"
        "\n"
        "\n"
        "def farewell(name):\n"
        "    return f'bye {name}'\n"
        "\n"
        "\n"
        "class Counter:\n"
        "    def __init__(self):\n"
        "        self.n = 0\n"
        "\n"
        "    def inc(self):\n"
        "        self.n += 1\n"
        "        return self.n\n"
    ),
    "src/util.py": "def add(a, b):\n    return a + b\n",
    "docs/guide.txt": "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\n",
    "node_modules/pkg/index.js": "module.exports = 1;\n",
}


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def contents(root: Path, ignore=None) -> dict[str, str]:
    """Path -> content hash for every tracked file under root."""
    from agentsnap.tracking import DEFAULT_IGNORES, build_manifest, hash_path

    return {
        p: hash_path(root, p) for p in build_manifest(root, DEFAULT_IGNORES if ignore is None else ignore)
    }


def read(root: Path, rel: str) -> str:
    return (root / rel).read_text()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for rel, text in SAMPLE.items():
        write(root, rel, text)
    os.chmod(root / "src/util.py", 0o755)
    os.symlink("src/app.py", root / "app_link.py")
    return root


@pytest.fixture
def git_repo(repo: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), *a], check=True, capture_output=True, env={**os.environ, **env}
    )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("config", "commit.gpgsign", "false")
    run("add", "-A")
    run("commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def store(repo: Path) -> Store:
    return Store(repo)
