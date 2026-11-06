#!/usr/bin/env bash
# Walk through agentsnap with three scripted "agents" on a throwaway repo.
#
#   ./examples/demo.sh                              # uses `agentsnap` from PATH
#   AGENTSNAP=.venv/bin/agentsnap ./examples/demo.sh   # or a specific binary
set -euo pipefail

AGENTSNAP=${AGENTSNAP:-agentsnap}
DEMO=$(mktemp -d "${TMPDIR:-/tmp}/agentsnap-demo.XXXXXX")
trap 'rm -rf "$DEMO"' EXIT
step() { printf '\n\033[1m$ %s\033[0m\n' "$*"; }
as() { step "agentsnap $*"; "$AGENTSNAP" -C "$DEMO" "$@" || true; }

# A tiny project.
mkdir -p "$DEMO/src"
cat > "$DEMO/src/app.py" <<'PY'
def greet(name):
    return f"hello {name}"


def farewell(name):
    return f"bye {name}"
PY
printf '# Demo\n\nA demo project.\n' > "$DEMO/README.md"
git -C "$DEMO" init -q && git -C "$DEMO" add -A
git -C "$DEMO" -c user.email=demo@example.com -c user.name=demo commit -qm init

as new docs greeter closer
DOCS=$("$AGENTSNAP" -C "$DEMO" path docs)
GREETER=$("$AGENTSNAP" -C "$DEMO" path greeter)
CLOSER=$("$AGENTSNAP" -C "$DEMO" path closer)

step "# three agents work in parallel, each in its own snapshot"
printf '# Demo\n\nA demo project.\n\n## Usage\n\nRun it.\n' > "$DOCS/README.md"
sed -i.bak 's/hello/hi/' "$GREETER/src/app.py" && rm "$GREETER/src/app.py.bak"
sed -i.bak 's/bye/see you/' "$CLOSER/src/app.py" && rm "$CLOSER/src/app.py.bak"
printf 'def test_greet():\n    pass\n' > "$CLOSER/src/test_app.py"

as list
as conflicts
as merge docs greeter
as merge closer
as merge closer --policy merge3
step "cat src/app.py"
cat "$DEMO/src/app.py"
as conflicts
as drop docs greeter closer

step "# scene 2: two edits with no file overlap that still break each other"
as new renamer caller
RENAMER=$("$AGENTSNAP" -C "$DEMO" path renamer)
CALLER=$("$AGENTSNAP" -C "$DEMO" path caller)
sed -i.bak 's/def greet/def say_hi/' "$RENAMER/src/app.py" && rm "$RENAMER/src/app.py.bak"
printf 'from app import greet\n\nprint(greet("world"))\n' > "$CALLER/src/main.py"
CHECK="cd src && python3 -c 'import app; import os; os.path.exists(\"main.py\") and __import__(\"main\")'"
as conflicts
as merge renamer --check "$CHECK"
as merge caller --check "$CHECK"
step "# the caller's change was refused: the repo still works"
step "cat src/main.py"
cat "$DEMO/src/main.py" 2>/dev/null || echo "(not merged)"
as drop -f renamer caller
