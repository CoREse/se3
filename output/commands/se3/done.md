---
name: "SE3: Done"
description: End session — tests, commit, handoff
deprecated: true
replacement: se3 run
---

> ⚠️ **DEPRECATED**: This command is deprecated in SE3 3.0. Use `se3 run` instead.
> This command will be removed in a future version.

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

**Session Guard (2.1+)**

`se3 done` checks if session is properly started before proceeding:
- If `.claude/.session.json` does not exist → returns error `SESSION_NOT_STARTED`
- If session status is not "active" → returns error `SESSION_NOT_ACTIVE`
- If session file is corrupted → returns error `SESSION_INVALID`

In all cases, the agent should run `se3 start` first.

**Project Structure (SE3 1.x)**

```
project/
├── init.sh                # optional: environment setup script
├── progress.md            # cross-session history (auto-maintained by SE3 tools)
├── se3.config.yaml        # optional
├── README.md
├── human-calls/           # async human call queue
├── .e2e-baselines/        # optional: visual regression baselines
├── openspec/
│   ├── specs/             # source of truth for requirements (guardrails apply)
│   └── changes/
│       └── archive/
└── .claude/
    ├── SE3.md             # framework reference
    └── commands/se3/      # workflow skills (start.md, work.md, done.md)
```

**Configuration (SE3 1.x)**

Optional `se3.config.yaml`. All settings have defaults.

- `max_tasks_per_change`: Max tasks per group (default: 5)
- `human_call.timeout_days`: Async call timeout (default: 7)
- `session.max_progress_entries`: Max progress entries before archiving (default: 20)

**Verification Protocol (SE3 1.x)**

**The Rule**: Never mark a feature or change as complete without running tests that prove it works.

Without this rule, agents will over-report completion. This is the single most common failure mode in long-running agent systems.

**How to Verify**:

1. **Spec scenarios = acceptance criteria**. Each WHEN/THEN scenario in a spec is a test case. Before marking a change complete, verify every scenario.

2. **Prefer automated tests**. Write tests for spec scenarios when possible. Run them. A passing test suite is the only reliable proof of completion.

3. **E2E testing for user-facing features**. Visual regression testing catches issues that unit tests miss.

   **With Puppeteer MCP** (recommended):
   - Navigate to the feature URL
   - Screenshot the critical UI state
   - Compare with baseline or verify key elements exist
   - Test user flows: click → wait → screenshot → assert

   **Visual verification checklist**:
   - Layout not broken (no overlapping elements)
   - Critical text visible and not truncated
   - Interactive elements clickable
   - No console errors during interaction

4. **Manual verification as fallback**. If no automated testing is feasible, manually exercise the feature and document the result.

**When to Run Tests**:
- **Startup**: Run existing tests to establish a baseline before making changes
- **After implementation**: Run tests for the specific change
- **Before commit**: Run the full test suite — do not commit if tests fail
- **Before archiving a change**: Verify all spec scenarios pass

**Human-as-MCP (SE3 1.x)**

All human input enters through human calls. Two modes:

### Sync Mode (default)

Human is present → ask directly (AskUserQuestion).

Use for: project direction, immediate decisions, requirement clarification.

### Async Mode

Human unavailable → write file to `human-calls/`.

Use for: offline operations (deploy, create accounts), cross-session pending requests.

**File format** (filename: `{YYYYMMDD}-{HHmmss}-{short-description}.md`):

```markdown
---
type: decision | action | information
priority: high | medium | low
status: pending | responded
created: YYYY-MM-DD
---

# [Title]

## Context
[Why human input is needed]

## Request
[What is being asked]

## Options (for decision type)
- **A**: [option + trade-offs]
- **B**: [option + trade-offs]

---
## Response (filled by human)
```

### Non-Blocking Rule

- Mark dependent tasks as `waiting-human`, continue other work
- **MUST NOT** block unrelated tasks

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
