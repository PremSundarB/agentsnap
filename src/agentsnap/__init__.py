"""agentsnap: run many coding agents safely on one repository.

Three parts, mirroring the project proposal:

* :mod:`agentsnap.snapshot`  - copy-on-write snapshots (APFS clone / reflink / copy)
* :mod:`agentsnap.conflicts` - detect overlapping and stale changes before merging
* :mod:`agentsnap.merge`     - all-or-nothing merges with a user-chosen policy

:mod:`agentsnap.cli` and :mod:`agentsnap.mcp_server` expose them to people and agents.
"""

__version__ = "0.1.0"

from .conflicts import Conflict, Report, check
from .merge import MergeResult, Policy, merge_workspace
from .tracking import ChangeSet
from .workspace import Store, Workspace, WorkspaceError, find_repo

__all__ = [
    "ChangeSet",
    "Conflict",
    "MergeResult",
    "Policy",
    "Report",
    "Store",
    "Workspace",
    "WorkspaceError",
    "__version__",
    "check",
    "find_repo",
    "merge_workspace",
]
