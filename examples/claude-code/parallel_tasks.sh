#!/usr/bin/env bash
# Run several headless Claude Code agents on one repo, each in its own
# agentsnap workspace, then check for conflicts and merge.
#
#   cd your-repo
#   path/to/parallel_tasks.sh "fix-typos:Fix spelling mistakes in the docs" \
#                             "add-tests:Add unit tests for src/util.py"
#
# Each argument is NAME:PROMPT. Requires `claude` and `agentsnap` on PATH.
set -euo pipefail

[ $# -gt 0 ] || { sed -n '2,10p' "$0"; exit 2; }

pids=()
names=()
for spec in "$@"; do
  name=${spec%%:*}
  prompt=${spec#*:}
  agentsnap new -q "$name" >/dev/null
  names+=("$name")
  # Each agent runs with its working directory set to its own snapshot.
  agentsnap run "$name" -- claude -p "$prompt" --permission-mode acceptEdits \
    > ".agentsnap/workspaces/$name/agent.log" 2>&1 &
  pids+=($!)
  echo "started $name (pid $!)"
done

for pid in "${pids[@]}"; do wait "$pid" || true; done
echo
agentsnap list
echo
agentsnap conflicts || true
echo
# Merge in the order given; fall back to a line-level merge on overlaps.
agentsnap merge --policy merge3 "${names[@]}" || {
  echo
  echo "Some workspaces did not merge. Inspect with 'agentsnap diff NAME', then either"
  echo "'agentsnap merge --policy rerun NAME' and re-run that agent, or merge by hand."
}
