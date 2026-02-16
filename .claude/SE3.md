<!-- Generated on 2026-02-16 -->
<!-- SE3 Version: 1.5.1 -->
<!-- Checksum: 5a46e76693095121db208ff5d30dace1ae7ca0aeb86d867d9ee3f0daeff0cf77 -->

<!--
  SE 3.0 Framework Reference File
  ===============================
  This file is installed by `se3 init` and serves as the official framework specification.
  It is a read-only reference for agents working on SE 3.0 projects.

  Generated File: DO NOT MODIFY DIRECTLY
  Version: {{SE3_VERSION}}
  Checksum: {{CHECKSUM}}

  For more information, visit: https://github.com/CoREse/se3
-->

# SE 3.0 Framework

> Place at `.claude/CLAUDE.md` in your project.

## Core Principles

1. **Human-as-MCP**: All human input obtained on-demand via human calls. No pre-written requirement files.
2. **Progressive Loading**: Start with `progress.md` + `git log`. Load deeper only when the task needs it.
3. **Specs as Truth**: OpenSpec specs are the source of truth for **requirements**. Agents MUST NOT weaken or delete existing requirements without explicit human approval.
4. **Verify Before Done**: Never mark a feature complete without running tests. Spec scenarios are acceptance criteria, not documentation.
5. **Tool-Assisted Enforcement**: Use CLI tools (`se3 lint`, `se3 verify`, `se3 status`) to validate specs, verify coverage, and diagnose issues. Tools make rules enforceable, not just documented.
6. **Incremental Development**: Work in openspec changes. Each session stays within a bounded scope.

---

## Input Classification & Stage Routing

**Principle**: ALL human input related to the project SHALL be processed through SE3 stage routing. No input is handled outside the framework.

### Input Classifier

For every human message, classify its intent:

| Intent Type | Description | Stage Entry |
|-------------|-------------|-------------|
| `directive` | Explicit self-iterate, "implement X", "start feature Y" | Full SDD workflow |
| `bug-report` | Error description, stack trace, broken behavior | Bug fix workflow |
| `feature-request` | New capability, enhancement idea | Feature proposal workflow |
| `question` | How does X work? Why Y? | Knowledge query |
| `review` | "Check this", "What do you think", "Is this correct" | Review workflow |
| `clarification` | Follow-up on previous topic | Resume/continue workflow |
| `meta` | About the project/process itself | Meta workflow |
| `off-topic` | Not related to project | Answer without modifying project files |

### Stage Decision Matrix

```
Input + Current State → Stage Decision

IF intent == bug-report:
  IF status.md has active_change AND active_change relates to bug:
    → Continue active change (add bug fix task)
  ELSE:
    → Create new change: "bugfix/{description}"
    → Stage: Analyze → Fix → Verify

IF intent == feature-request:
  IF complexity == small AND no spec change needed:
    → Direct implementation (Small workflow)
  ELSE:
    → Create new change: "feature/{description}"
    → Stage: Proposal → Specs → Design → Tasks → Code → Verify

IF intent == question:
  IF answer requires code investigation:
    → Quick exploration (no change created)
  ELSE:
    → Direct answer from existing knowledge

IF intent == review:
  → Review workflow: Check → Report → Optional fix

IF intent == clarification:
  → Continue previous context
  OR if new context: treat as new input
```

### Routing Execution

```python
# Pseudocode for every input
intent = classify_input(user_message)
route = determine_stage(intent, current_status)
execute_stage(route, user_message)
```

**MUST NOT**: Handle input outside of SE3 workflow
**MUST**: Create appropriate change record for any code modification
**MUST**: Update status.md to reflect current stage

---

## Session Protocol

### Startup

**Step 0 — Environment setup**:
- If `init.sh` exists at project root, run it to start the dev environment (servers, databases, build watchers)
- If it fails, diagnose and fix before proceeding

**Step 1 — OpenSpec check**:
- If `openspec` command is not found → ask the human to install it (sync human call). Do not proceed with spec-related work until resolved.
- If `openspec` is available but `openspec/` directory does not exist → run `openspec init` to initialize
- This ensures agents always have access to spec templates and format guidance via `openspec instructions`

**Step 2 — Read status**:
- Read `status.md` for current session state (runtime dashboard)
- If status shows `blocked` or `error`, diagnose and resolve first
- If status shows `waiting-human`, check `human-calls/` for response

**Step 3 — Load context**:
- Read latest entry in `progress.md` for cross-session history
- Read `git log --oneline -5`
- If neither exists → **First-time bootstrap** (see below)

**Step 4 — Check pending items**:
- Scan `human-calls/` for `status: responded` files not yet processed
- Check `openspec/changes/` for active changes (should match `status.md` Active Change)

**Step 5 — Baseline verification**:
- Run existing tests to confirm the project is in a working state before making changes
- If tests fail, fix them first — do not build on a broken foundation

**Step 6 — Classify input & Route to stage**:
- **ALWAYS** classify the current user message using Input Classifier
- If classified as `bug-report`, `feature-request`, `review`: Route to appropriate stage
- If classified as `directive`: Follow explicit instruction
- If classified as `question`: Answer directly (may explore code if needed)
- If classified as `off-topic`: Answer conversationally, do not modify project files
- Update `status.md` to reflect current Stage before proceeding

**Step 7 — Execute stage workflow**:
- Follow the determined stage's protocol
- Read specs or other files only when the work requires them

**First-time bootstrap** (empty project):
1. Ask the human (sync human call): "What should this project do?"
2. Create an openspec change from their response
3. Create `progress.md`
4. Create `human-calls/` directory

### Shutdown

1. Run all tests — do NOT proceed to commit if tests fail
2. **Update `status.md`**: Set Status to `ready`, clear Blockers, update Next Steps
3. Prepend session record to `progress.md`
4. Git commit
5. Update openspec change status if applicable

### Commit Cadence

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

### Context Clearing (/new)

- Clear when context **approaches saturation** or when switching to a **substantially different task**
- Do NOT clear mechanically after every task group — continue if there is budget and the next task benefits from current context

### Progress File Format

```markdown
## YYYY-MM-DD Session N

### Done
- [completed items]

### Changes
- `change-name`: status

### Open Issues
- [unresolved problems]

### Next Steps
- [specific actionable suggestions]
```

---

## Verification Protocol

### The Rule

**Never mark a feature or change as complete without running tests that prove it works.**

Without this rule, agents will over-report completion. This is the single most common failure mode in long-running agent systems.

### How to Verify

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

### When to Run Tests

- **Startup**: Run existing tests to establish a baseline before making changes
- **After implementation**: Run tests for the specific change
- **Before commit**: Run the full test suite — do not commit if tests fail
- **Before archiving a change**: Verify all spec scenarios pass

---

## Spec Guardrails

### What Agents MUST NOT Do

- **MUST NOT delete** an existing spec requirement without explicit human approval (via human call)
- **MUST NOT weaken** a requirement (e.g., changing "SHALL validate all inputs" to "SHOULD validate inputs")
- **MUST NOT modify** the description or scenarios of a requirement they are implementing — the implementer does not get to change the spec they're building against

### What Agents CAN Do

- **ADD** new requirements
- **MODIFY** requirements they are not currently implementing (with a change proposal)
- **Mark requirements as deprecated** with a human-approved reason and migration path

### Enforcement

After archiving a change, review the git diff of `openspec/specs/` to confirm no requirements were inappropriately weakened or removed. If spec drift is detected, revert and investigate.

---

## Human-as-MCP

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

---

## SDD (Spec Driven Development)

OpenSpec specs = source of truth for **requirements**.

### Adaptive Formality

Not every change needs full ceremony. Match the process to the scope:

**Large** (new capability, agent team, cross-cutting):
- Full openspec change: proposal → specs → design → tasks → code → **verify**
- Specs are detailed with scenarios — they serve as **contracts between agents**
- Design doc captures architecture decisions that sub-agents need

**Medium** (single agent, moderate scope):
- Openspec change with: proposal (brief) → specs (if requirements change) → tasks → **verify**
- Skip design unless there are real architecture decisions
- Proposal can be 2-3 sentences

**Small** (bug fix, tweak, simple addition):
- No openspec change needed
- Edit code directly, update the relevant spec file if behavior changed, **run tests**, commit

### Specs as Agent Contracts

In agent team mode, specs are the interface between agents:
- Parent (architect) writes the spec defining **what** to build
- Sub-agent (implementer) reads the spec and implements against it — **MUST NOT modify the spec**
- Sub-agent (reviewer) verifies the implementation matches the spec by testing each scenario
- The spec must be precise enough that an agent with no other context can implement from it

### Input-Driven Workflows

#### Bug Fix Workflow (triggered by bug-report)

```
1. ANALYZE (current session)
   - Reproduce the bug
   - Identify root cause
   - Determine affected components
   - Update status.md: Stage = analyzing

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
   - Update status.md: Stage = ready
```

#### Feature Request Workflow (triggered by feature-request)

```
1. CLARIFY (human call if needed)
   - Understand the request
   - Ask clarifying questions
   - Determine scope and priority
   - Update status.md: Stage = clarifying

2. PROPOSE (openspec/change/)
   - Create openspec/change/feature-{id}/
   - Write proposal.md: what, why, acceptance criteria
   - Get human approval (if significant)
   - Update status.md: Stage = proposing

3. SPEC (if requirements change)
   - Write/update specs in openspec/specs/
   - Define scenarios (WHEN/THEN)
   - Run se3 lint to validate
   - Update status.md: Stage = spec-writing

4. DESIGN (if needed)
   - Write design.md for complex changes
   - Update status.md: Stage = designing

5. IMPLEMENT
   - Break into tasks (max 5 per group)
   - Implement incrementally
   - Run tests continuously
   - Update status.md: Stage = implementing

6. VERIFY
   - Run all tests
   - Verify each spec scenario
   - Archive change
   - Update status.md: Stage = ready
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

### Change Workflow

- Each change: max 5 tasks per group with strong logical dependencies
- Context clearing between groups only when context is saturated
- Archive applies spec deltas back to main specs automatically — this is the key value of the openspec workflow
- **Before archiving**: verify all spec scenarios pass

---

## Agent Team

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

**Worker Task JSON**:
```json
{
  "id": "task-001",
  "status": "pending",
  "title": "Implement auth",
  "branch": "collab/auth",
  "worktree": ".worktrees/auth",
  "prompt": "Implement user authentication...",
  "base_branch": "master",
  "spec_refs": ["auth-spec.md"],
  "dependencies": [],
  "health": {"timeout_minutes": 60, "attempts": 0, "max_attempts": 3}
}
```

**Manager Decision JSON**:
```json
{
  "action": "plan|merge|reject|retry|split|escalate|complete",
  "tasks": [...],
  "target_task": "task-id",
  "merge_branch": "branch-name",
  "retry_prompt": "...",
  "reason": "...",
  "summary": "..."
}
```

---

## Self-Iterate

Self-iterate is the **directive** input type handler — when explicitly instructed to continue development.

When the input classifier detects a `directive` intent (e.g., "self-iterate", "implement X", "continue"), execute without stopping until step 5:

1. Obtain direction via human call → create openspec change
2. Implement the change (proposal → specs → design → tasks → code)
3. **Verify** implementation against specs (run tests, check each scenario), archive the change
4. Check if specs fully cover project goals — if gaps, go to 1
5. Update project documentation

---

## Project Structure

```
project/
├── init.sh                # optional: environment setup script
├── status.md              # runtime dashboard (current session state)
├── progress.md            # cross-session history
├── se3.config.yaml        # optional
├── README.md
├── human-calls/           # async human call queue
├── .e2e-baselines/        # optional: visual regression baselines
├── openspec/
│   ├── specs/             # source of truth for requirements (guardrails apply)
│   └── changes/
│       └── archive/
└── .claude/
    └── CLAUDE.md           # this file
```

---

## Configuration

Optional `se3.config.yaml`. All settings have defaults.

- `max_tasks_per_change`: Max tasks per group (default: 5)
- `human_call.timeout_days`: Async call timeout (default: 7)
- `session.max_progress_entries`: Max progress entries before archiving (default: 20)

---

## Versioning

SE3 follows [Semantic Versioning 2.0.0](https://semver.org/) with version format `MAJOR.MINOR.PATCH`:

- **MAJOR** (e.g. 1.0.0 → 2.0.0): Breaking changes. Existing projects need manual review before upgrading.
  - Example: Removing or renaming core concepts, changing session protocol flow
- **MINOR** (e.g. 1.0.0 → 1.1.0): Backward-compatible additions. Safe to `se3 update`.
  - Example: New CLI commands, new optional specs, new framework capabilities
- **PATCH** (e.g. 1.1.0 → 1.1.1): Backward-compatible bug fixes. Behavior unchanged.
  - Example: CLI bug fixes, documentation corrections, rule clarifications

The version is embedded in `.claude/SE3.md` metadata:
```
<!-- SE3 Version: 1.1.0 -->
```

**Upgrade path**: `se3 update --se3-version X.Y.Z` reads `output/SE3.md.template`, stamps it with version metadata, and writes to `.claude/SE3.md`.

**Self-hosted projects** (developing SE3 itself): MUST NOT edit `.claude/SE3.md` directly. All changes go through `output/SE3.md.template` → `se3 update`.

### Version Management Rules

**Single Source of Truth**: `tools/se3_tools/__init__.py:SE3_FRAMEWORK_VERSION`

**When to bump version**:
| Change Type | Version Bump | Example |
|-------------|--------------|---------|
| Fix typo, docs correction | PATCH | 1.0.0 → 1.0.1 |
| New feature, new CLI command | MINOR | 1.0.0 → 1.1.0 |
| Breaking change, rename concept | MAJOR | 1.0.0 → 2.0.0 |

**Mandatory version update checklist**:
1. Update `SE3_FRAMEWORK_VERSION` in `tools/se3_tools/__init__.py`
2. Add entry to `README.md` Version History
3. Update `output/SE3.md.template` version comment
4. Run `se3 update` to regenerate `.claude/SE3.md`

**Enforcement**: `se3 commit` checks for framework file changes and warns if version not updated.
