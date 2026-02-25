---
name: "SE3: Full Cycle"
description: Run complete start-work-done workflow in one command for simple/quick tasks
deprecated: true
replacement: se3 run
---

> ⚠️ **DEPRECATED**: This command is deprecated in SE3 3.0. Use `se3 run` instead.
> This command will be removed in a future version.

**Usage**: `/se3:fc <description>` or `/se3:fc <description> --quick`

Run the complete SE3 workflow (start → work → implementation → done) in a single command.
Optimized for simple, quick tasks that can be completed in one session without complex planning.

**Arguments**

- `description` (required): Description of the work to do
- `--quick` or `-q` (optional): Quick mode — skip formal change creation, use 'small' workflow

**Steps**

1. **Run `se3 full-cycle --format json`** to set up the complete workflow
   ```bash
   se3 full-cycle "description of work" [--quick] --format json
   ```
   Parse the JSON output to get:
   - `phases.start`: Session initialization results
   - `phases.work`: Change creation details
   - `phases.implementation.actions`: Actions to execute
   - `phases.done`: Completion check results
   - `actions`: Complete action sequence

2. **Execute each action in the `actions` array**:

   - `implement`: Implement the requested change
   - `run_tests`: Run tests to verify implementation
   - `commit`: Commit changes via `se3 commit`
   - `handoff`: Complete session via `se3 handoff`

3. **Report completion summary**:
   - What was implemented
   - Test results
   - Commit status
   - Any remaining actions

**When to Use**

| Scenario | Command |
|----------|---------|
| Quick bug fix (< 30 min) | `/se3:fc "fix login bug" --quick` |
| Simple feature | `/se3:fc "add validation" --quick` |
| Documentation update | `/se3:fc "update README" --quick` |
| Complex feature (needs planning) | Use `/se3:start` → `/se3:work` separately |

**Session Guard (2.1+)**

`se3 full-cycle` checks if session is properly started before proceeding:
- If `.claude/.session.json` does not exist → runs `se3 start` automatically
- If session status is not "active" → prompts to run `se3 start`

**Guardrails**

- If start phase requires manual intervention (init.sh, openspec init), pause and ask user
- If tests fail during implementation, pause and fix before committing
- Never skip tests before commit
- For complex tasks requiring design/specs, recommend using separate `/se3:start` and `/se3:work` instead

**Workflow Comparison**

```
Full-Cycle (this skill):  start + work + implement + done  → One command, quick tasks
Standard Flow:            start → work → [plan/spec/design] → implement → done  → Complex tasks
```
