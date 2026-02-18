---
name: "SE3: Done"
description: End session — tests, commit, handoff
---

**Usage**: `/se3:done`

End the current SE3 work session with proper shutdown protocol. Ensures all changes are committed and session state is persisted.

**Steps**

1. **Run `se3 done --format json`** to compute shutdown actions
   ```bash
   se3 done --format json
   ```
   Parse the JSON output to get:
   - `uncommitted_changes`: Count and list of uncommitted files
   - `active_changes`: List of incomplete changes with remaining work
   - `test_command`: Detected test command (if any)
   - `actions`: Array of actions to execute

2. **Execute each action in the `actions` array** in order:

   - `run_tests`: Run the test suite
     - If tests FAIL → STOP, report failure, fix before proceeding
     - If tests PASS → continue

   - `commit`: Run `se3 commit` to commit uncommitted changes
     - This automatically runs tests, blocks sensitive files, generates message
     - If commit fails → diagnose and report

   - `update_change_status`: Note remaining work in the change directory
     - Document how many tasks remain for next session

   - `create_human_call`: (Collab mode only) Create human-call for orchestrator

   - `handoff`: Run `se3 handoff`
     - Generates session summary in `progress.md`
     - Transfers control to human

3. **Report handoff summary**:
   - Branch and last commit
   - Whether all changes were committed
   - Session summary from `progress.md`
   - Any incomplete changes with notes for next session

**Shutdown Protocol**

```
1. Run tests → MUST PASS
2. Commit changes → via `se3 commit`
3. Update change status → Document remaining work
4. Handoff → `se3 handoff` generates session summary
```

**Guardrails**

- **NEVER** skip tests if there are uncommitted changes
- **NEVER** commit if tests fail
- **ALWAYS** run `se3 handoff` at the end — it generates the session record
- If there are incomplete changes, document what remains before handing off
- Collab mode: Create human-call instead of direct handoff

**When to Use**

- End of session (before `/new` or closing Claude)
- Before switching to a substantially different task
- When handing control back to human for review/decision
- After completing a coherent unit of work
