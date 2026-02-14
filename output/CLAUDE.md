# SE 3.0 Framework

> Place at `.claude/CLAUDE.md` in your project.

## Core Principles

1. **Human-as-MCP**: All human input obtained on-demand via human calls. No pre-written requirement files.
2. **Progressive Loading**: Start with `progress.md` + `git log`. Load deeper only when the task needs it.
3. **Specs as Truth**: OpenSpec specs are the single source of truth for what the project should do.
4. **Incremental Development**: Work in openspec changes. Each session stays within a bounded scope.

---

## Session Protocol

### Startup

**Step 1 — Locate state**:
- Read latest entry in `progress.md`
- Read `git log --oneline -5`
- If neither exists → **First-time bootstrap** (see below)

**Step 2 — Check pending items**:
- Scan `human-calls/` for `status: responded` files not yet processed
- Check `openspec/changes/` for active changes

**Step 3 — Determine scope**:
- Follow "next steps" from progress + active changes
- Read specs or other files only when the work requires them

**First-time bootstrap** (empty project):
1. Ask the human (sync human call): "What should this project do?"
2. Create an openspec change from their response — the proposal captures the intent, specs formalize it
3. Create `progress.md`
4. Initialize openspec if needed
5. Create `human-calls/` directory

### Shutdown

1. Ensure modified code runs correctly
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

- OpenSpec specs = single source of truth for project requirements
- Human call results drive openspec changes directly (proposal = the demand, specs = formalization)
- Each change: max 5 tasks per group with strong logical dependencies
- Context clearing between groups only when context is saturated

---

## Agent Team

Uses Claude Code's native **Task tool**.

- Parent spawns sub-agents via Task tool with appropriate `subagent_type`
- Each sub-agent works on a different openspec change (natural file isolation)
- Results return directly — no file-based communication
- Roles expressed in prompts: architect / implementer / reviewer
- Default: single agent. Multi-agent only when independent changes can be parallelized.

---

## Self-Iterate

When instructed to self-iterate, execute without stopping until step 5:

1. Obtain direction via human call → create openspec change
2. Implement the change (proposal → specs → design → tasks → code)
3. Verify implementation against specs, archive the change
4. Check if specs fully cover project goals — if gaps, go to 1
5. Update project documentation

---

## Project Structure

```
project/
├── progress.md
├── se3.config.yaml        # optional
├── README.md
├── human-calls/
├── openspec/
│   ├── specs/             # source of truth
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
