# Proposal: Fix Collab Mode Detection

## Problem

`is_in_collab_mode()` function in both `done.py` and `handoff.py` incorrectly
detects collab mode when:
1. `SE3_AGENT_ROLE` environment variable is NOT set (current process is NOT a collab agent)
2. BUT `.collab/config.json` exists with `status: "active"` (orchestrator is running)

This causes interactive Claude Code sessions to incorrectly act as collab work agents,
creating human-calls for orchestrator instead of running normal shutdown protocol.

## Root Cause

The function checks `.collab/config.json` existence as a fallback to `SE3_AGENT_ROLE`.
But `.collab/config.json` existence only means "a collab session exists in this project",
not "the current process is a collab work agent".

## Solution

Remove the `.collab/config.json` check. Only rely on `SE3_AGENT_ROLE` environment variable.

Files changed:
- `tools/se3_tools/commands/done.py`
- `tools/se3_tools/commands/handoff.py`

## Impact

Interactive Claude Code sessions will correctly use standard shutdown protocol
even when a collab orchestrator is running in the same project.
