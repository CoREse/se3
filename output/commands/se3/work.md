---
name: "SE3: Work"
description: Start or continue working on a change (feature, bugfix, review, directive)
---

**Usage**:
- `/se3:work` — List active changes or create new one
- `/se3:work <change-name>` — Continue working on specific change
- `/se3:work <description>` — Create and start working on new change

**Steps**

1. **Determine what to work on**:
   - If a change name was provided: `se3 work <name> --format json`
   - If a description was provided: Infer workflow type, then `se3 work --new <type>/<kebab-name> --format json`
   - If nothing provided: `se3 work --format json` to list active changes, then AskUserQuestion

2. **Parse JSON response** to get:
   - `change`: The change name
   - `workflow`: Workflow type (bugfix/feature/review/directive/small)
   - `current_step`: Current workflow step
   - `steps`: All steps with their status
   - `tasks`: List of tasks with done/not-done status
   - `progress`: Task completion statistics
   - `actions`: Array of actions to execute

3. **Execute the workflow loop**:

   For each action in `actions`:
   - `ask_user`: Ask clarifying questions using AskUserQuestion
   - `create_change`: Run `openspec new change <name>`
   - `write_proposal`: Create `proposal.md` in the change directory
   - `write_spec`: Create/update specs in `openspec/specs/` with WHEN/THEN scenarios
   - `write_design`: Create `design.md` (only if complexity warrants it)
   - `write_tasks`: Create `tasks.md` breaking work into max 5 tasks
   - `analyze_bug`: Reproduce, identify root cause, report findings
   - `inspect_code`: Read and review the code/files in question
   - `report_review`: Present findings categorized as critical/warning/suggestion
   - `implement_task`: Implement the specified task, then mark `- [x]` in tasks.md
   - `implement`: Direct code implementation (for small changes)
   - `run_tests`: Run the test suite, report results. If FAIL → pause and fix
   - `run_lint`: Run `se3 lint` to validate specs
   - `verify_scenarios`: Check that all spec scenarios pass
   - `archive_change`: Run `openspec archive <name>` when complete
   - `skip_step`: Skip a step (e.g., design for simple changes)
   - `advance_step`: Trigger workflow step advancement
   - `complete`: All steps done

4. **After completing actions**, re-run `se3 work <name> --format json` for updated state
   - Continue the loop until `complete` or blocked
   - If blocked: Report blocker and suggest next steps

5. **On completion**, show summary and suggest `/se3:done`

**Workflow Types**

| Type | Steps | When Used |
|------|-------|-----------|
| `bugfix` | analyze → fix → verify | Bug reports |
| `feature` | clarify → propose → spec → design → implement → verify | Feature requests |
| `review` | inspect → report → fix | Code review requests |
| `directive` | plan → implement → verify → check_coverage | "Implement X" commands |
| `small` | implement → verify | Simple changes, no openspec needed |

**Adaptive Formality**

The CLI automatically determines formality based on change contents:
- **Large**: Has proposal + specs + design
- **Medium**: Has proposal + specs (no design)
- **Small**: No proposal/specs, ≤3 tasks

**Spec Guardrails (SE3 1.x)**

What Agents MUST NOT Do:
- **MUST NOT delete** an existing spec requirement without explicit human approval (via human call)
- **MUST NOT weaken** a requirement (e.g., changing "SHALL validate all inputs" to "SHOULD validate inputs")
- **MUST NOT modify** the description or scenarios of a requirement they are implementing — the implementer does not get to change the spec they're building against

What Agents CAN Do:
- **ADD** new requirements
- **MODIFY** requirements they are not currently implementing (with a change proposal)
- **Mark requirements as deprecated** with a human-approved reason and migration path

**Enforcement**: After archiving a change, review the git diff of `openspec/specs/` to confirm no requirements were inappropriately weakened or removed. If spec drift is detected, revert and investigate.

**Input-Driven Workflows (SE3 1.x)**

#### Bug Fix Workflow (triggered by bug-report)

```
1. ANALYZE (current session)
   - Reproduce the bug
   - Identify root cause
   - Determine affected components
   - Analyze root cause

2. FIX (openspec/change or direct)
   IF complexity > small:
     - Create openspec/change/bugfix-{id}/
     - Write fix-spec.md: expected behavior, test cases
     - Implement fix
     - Run tests to verify
   ELSE:
     - Fix directly
     - Run tests

3. VERIFY
   - Confirm bug is resolved
   - Run regression tests
   - Update relevant specs if behavior changed
   - Archive change (if created)
   - Verify complete
```

#### Feature Request Workflow (triggered by feature-request)

```
1. CLARIFY (human call if needed)
   - Understand the request
   - Ask clarifying questions
   - Determine scope and priority
   - Clarify requirements

2. PROPOSE (openspec/change/)
   - Create openspec/change/feature-{id}/
   - Write proposal.md: what, why, acceptance criteria
   - Get human approval (if significant)
   - Create proposal

3. SPEC (if requirements change)
   - Write/update specs in openspec/specs/
   - Define scenarios (WHEN/THEN)
   - Run se3 lint to validate
   - Write specs

4. DESIGN (if needed)
   - Write design.md for complex changes
   - Design architecture

5. IMPLEMENT
   - Break into tasks (max 5 per group)
   - Implement incrementally
   - Run tests continuously
   - Implement changes

6. VERIFY
   - Run all tests
   - Verify each spec scenario
   - Archive change
   - Verify complete
```

#### Review Workflow (triggered by review)

```
1. INSPECT
   - Read the code/file in question
   - Check against specs
   - Identify issues

2. REPORT
   - Provide findings to human
   - Categorize: critical / warning / suggestion

3. FIX (optional, if requested)
   - IF fix approved: route to Bug Fix or Feature workflow
   - ELSE: end here
```

**Agent Team (SE3 1.x)**

### Mode 1: Task Tool (Default)

Uses Claude Code's native **Task tool**.

- Parent spawns sub-agents via Task tool with appropriate `subagent_type`
- Each sub-agent works on a different openspec change (natural file isolation)
- Results return directly — no file-based communication
- Specs on the file system serve as shared context accessible to all agents

**Roles** (expressed in Task tool prompts):

- **architect**: "Design the spec for change X. Define requirements with scenarios detailed enough for another agent to implement."
- **implementer**: "Implement tasks 1-3 of change X. Read `openspec/specs/` for requirements. Do not deviate from the spec. Do not modify spec files."
- **reviewer**: "Verify change X. Read the spec, run tests for each scenario, report any gaps between spec and implementation."

**When to Use**: Single agent (default) for most work. Multi-agent when multiple independent changes can be parallelized.

### Mode 2: Git Worktree Collaboration (se3 collab)

**Purpose**: Long-running multi-agent collaboration with full isolation and independent context windows.

**Architecture**:
- **Orchestrator** (bash): Manages task state, health checks, launches manager/worker processes
- **Manager** (`kclaude -p`): Analyzes state, creates tasks, reviews work, makes merge decisions
- **Worker** (`kclaude -p`): Implements tasks in isolated worktrees

**Directory Structure**:
```
.collab/
├── config.json           # session configuration
├── tasks/                # task definitions (task-*.json)
├── logs/                 # manager/worker logs
└── events/               # event queue

.worktrees/
└── {task-id}/           # per-task git worktrees
```

**Task State Machine**:
```
pending → in_progress → done/failed/timeout/blocked/escalated
```

**Launch Modes**:

1. **Daemon mode** (`--daemon`): Fully automatic, orchestrator manages everything
2. **Manual mode** (`--manual`): Generate task files, user launches manager/worker manually
3. **Direct mode** (default): Run orchestrator in foreground (for testing)

**Commands**:
```bash
se3 collab --daemon "Implement feature X"          # Start automatic collaboration
se3 collab --manual "Implement feature X"          # Generate plan, manual execution
se3 collab --launch-manager plan                   # Launch manager for event
se3 collab --launch-worker task-001                # Launch worker for task
se3 collab --status                                # Check session status
se3 collab --abort                                 # Stop and cleanup
```

**Session Guard (2.1+)**

`se3 work` checks if session is properly started before proceeding:
- If `.claude/.session.json` does not exist → returns error `SESSION_NOT_STARTED`
- If session status is not "active" → returns error `SESSION_NOT_ACTIVE`
- If session file is corrupted → returns error `SESSION_INVALID`

In all cases, the agent should run `se3 start` first.

**Guardrails**

- NEVER modify spec files you are implementing against
- Run tests after each implementation task
- Mark tasks complete (`- [x]`) immediately after finishing
- If a step is unclear, pause and AskUserQuestion
- If implementation reveals design issues, suggest updating artifacts
- Max 5 tasks per group — if more needed, context clear between groups
- Before archiving: verify all spec scenarios pass

**Commit Cadence (SE3 1.x)**

Commit during the session when a distinct, working unit of change is complete — do not accumulate unrelated changes into a single commit.

**All commits MUST go through `se3 commit`.** Do NOT use `git commit` directly. `se3 commit` enforces test verification, sensitive file blocking, and message conventions.

**When to commit mid-session:**
- After completing a coherent unit of work that passes tests
- Before starting a substantially different task that would muddy the commit message
- Before context clearing (/new) if there are completed changes to preserve

**Commit command:**
```bash
se3 commit -m "[context] Summary of what changed

Status: where things stand
Next: what the next session should do" -f "file1.py file2.py"
```

`se3 commit` will automatically:
1. Run tests — blocks commit if tests fail
2. Check for sensitive files — auto-unstages .env, credentials, keys
3. Stage specified files (or all tracked changes if no -f flag)
4. Execute the commit

**Commit Rules:**
- Commit when a **meaningful unit of work** is complete — not tied to /new or any mechanical trigger
- **Before handing control to human**: Always commit all changes. Humans may close the session at any time; uncommitted work will be lost. This is equivalent to committing before `/new` — session end requires persistence.
- Do not batch unrelated changes into one commit
- Commit messages must include context for the next session

**Context Clearing (/new) (SE3 1.x)**

- Clear when context **approaches saturation** or when switching to a **substantially different task**
- Do NOT clear mechanically after every task group — continue if there is budget and the next task benefits from current context
