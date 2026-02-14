# SE 3.0 Framework

> Place at `.claude/CLAUDE.md` in your project.

## Core Principles

1. **Human-as-MCP**: All human input obtained on-demand via human calls. No pre-written requirement files.
2. **Progressive Loading**: Start with `progress.md` + `git log`. Load deeper only when the task needs it.
3. **Specs as Truth**: OpenSpec specs are the single source of truth. Agents MUST NOT weaken or delete existing requirements without explicit human approval.
4. **Verify Before Done**: Never mark a feature complete without running tests. Spec scenarios are acceptance criteria, not documentation.
5. **Incremental Development**: Work in openspec changes. Each session stays within a bounded scope.

---

## Session Protocol

### Startup

**Step 0 — Environment setup**:
- If `init.sh` exists at project root, run it to start the dev environment (servers, databases, build watchers)
- If it fails, diagnose and fix before proceeding

**Step 1 — Locate state**:
- Read latest entry in `progress.md`
- Read `git log --oneline -5`
- If neither exists → **First-time bootstrap** (see below)

**Step 2 — Check pending items**:
- Scan `human-calls/` for `status: responded` files not yet processed
- Check `openspec/changes/` for active changes

**Step 3 — Baseline verification**:
- Run existing tests to confirm the project is in a working state before making changes
- If tests fail, fix them first — do not build on a broken foundation

**Step 4 — Determine scope**:
- Follow "next steps" from progress + active changes
- Read specs or other files only when the work requires them

**First-time bootstrap** (empty project):
1. Ask the human (sync human call): "What should this project do?"
2. Create an openspec change from their response
3. Create `progress.md`
4. Initialize openspec if needed
5. Create `human-calls/` directory

### Shutdown

1. Run all tests — do NOT proceed to commit if tests fail
2. Prepend session record to `progress.md`
3. Git commit (see commit rules below)
4. Update openspec change status if applicable

### Commit Rules

- Commit when a **meaningful unit of work** is complete — not tied to /new or any mechanical trigger
- Message format:
  ```
  [context] Summary of what changed

  Status: where things stand
  Next: what the next session should do
  ```

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

OpenSpec specs = single source of truth for project requirements.

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

### Change Workflow

- Each change: max 5 tasks per group with strong logical dependencies
- Context clearing between groups only when context is saturated
- Archive applies spec deltas back to main specs automatically — this is the key value of the openspec workflow
- **Before archiving**: verify all spec scenarios pass

---

## Agent Team

Uses Claude Code's native **Task tool**.

- Parent spawns sub-agents via Task tool with appropriate `subagent_type`
- Each sub-agent works on a different openspec change (natural file isolation)
- Results return directly — no file-based communication
- Specs on the file system serve as shared context accessible to all agents

### Roles

Expressed in Task tool prompts:

- **architect**: "Design the spec for change X. Define requirements with scenarios detailed enough for another agent to implement."
- **implementer**: "Implement tasks 1-3 of change X. Read `openspec/specs/` for requirements. Do not deviate from the spec. Do not modify spec files."
- **reviewer**: "Verify change X. Read the spec, run tests for each scenario, report any gaps between spec and implementation."

### When to Use

- **Single agent** (default): One agent handles all roles. Most work.
- **Multi-agent**: When multiple independent changes can be parallelized. Parent distributes one change per sub-agent.

---

## Self-Iterate

When instructed to self-iterate, execute without stopping until step 5:

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
├── progress.md
├── se3.config.yaml        # optional
├── README.md
├── human-calls/           # async human call queue
├── .e2e-baselines/        # optional: visual regression baselines
├── openspec/
│   ├── specs/             # source of truth (guardrails apply)
│   ├── changes/
│   └── archive/
└── .claude/
    └── CLAUDE.md           # this file
```

---

## Configuration

Optional `se3.config.yaml`. All settings have defaults.

- `max_tasks_per_change`: Max tasks per group (default: 5)
- `human_call.timeout_days`: Async call timeout (default: 7)
- `session.max_progress_entries`: Max progress entries before archiving (default: 20)
