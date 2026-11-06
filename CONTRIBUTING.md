# Contributing to agentsnap

Thanks for helping. Issues, bug reports, docs fixes and code are all welcome.

## Set up

```bash
git clone https://github.com/PremSundarB/agentsnap && cd agentsnap
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,mcp]'
pytest                 # about 100 tests, a few seconds
./examples/demo.sh     # end-to-end walkthrough
```

## Before opening a pull request

```bash
ruff format .
ruff check .
pytest
```

CI runs the same checks on Linux and macOS with Python 3.10 and 3.13.

- Keep pull requests small and focused. One feature or fix per PR.
- Add tests for new behaviour. Bug fixes should come with a test that fails
  without the fix.
- If you change behaviour, update the README or `docs/design.md`.
- Add a line to `CHANGELOG.md` under "Unreleased".

## Where things live

| area | module | owner |
|---|---|---|
| snapshots | `src/agentsnap/snapshot.py` | Bhushan |
| merge engine | `src/agentsnap/merge.py`, `journal.py` | Bhushan |
| read/write tracking | `src/agentsnap/tracking.py` | Arvind |
| conflict detection | `src/agentsnap/conflicts.py`, `symbols.py` | Arvind |
| CLI | `src/agentsnap/cli.py` | Prem |
| MCP server | `src/agentsnap/mcp_server.py` | Prem |

Owners review changes in their area, but anyone can work anywhere.
`docs/design.md` explains the concurrency model. Please read it before
changing `merge.py` or `conflicts.py`.

## Principles

- **Never lose work.** A merge either applies completely or not at all, and
  never overwrites a change it has not seen.
- **No required dependencies** for the core and CLI. The MCP SDK is optional.
- **Works with any agent.** Do not depend on one vendor's features.

## Reporting bugs

Use the bug report template. Include your OS and filesystem (APFS, ext4,
btrfs...), since snapshot behaviour depends on them.
