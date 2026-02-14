# SE 3.0 Framework

> Place this file at `.claude/CLAUDE.md` in your project.

## Core Principles

1. **Human-as-MCP**: All human input (including project intent) is obtained on-demand via human calls. No pre-written files required.
2. **Progressive Loading**: Load only the minimum context needed. Read deeper only when the task demands it.
3. **Incremental Development**: Work in openspec changes. Each session focuses on a bounded scope.
4. **File as Interface**: All cross-session state lives in the file system (progress.md, git history, openspec).

---

## Session Protocol

### Startup

**Step 1 — Locate current state** (always do this first):
- Read the latest entry in `progress.md`
- Read `git log --oneline -5`
- If neither exists → **First-time bootstrap** (see below)

**Step 2 — Check pending items**:
- Scan `human-calls/` for files with `status: responded` that haven't been processed
- Check `openspec/changes/` for active changes

**Step 3 — Determine scope**:
- Follow "next steps" from the latest progress entry + active changes
- Load specs, demands, or other files only when the work requires them

**First-time bootstrap** (empty project):
1. Ask the human (sync human call): "What should this project do?"
2. Write their response into `demands.md`
3. Create `progress.md`
4. Initialize openspec if needed (`openspec init --tools claude`)
5. Create `human-calls/` directory

### Shutdown

Before ending a session, MUST:
1. Ensure all modified code runs correctly
2. Prepend this session's record to `progress.md`
3. Git commit with a message containing:
   - Summary of changes
   - Context for the next session (current state, caveats, suggested next steps)
4. Update openspec change status if applicable

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

All human input enters the system through human calls. Two modes:

### Sync Mode (default)

When the human is present, ask directly via conversation (AskUserQuestion).

Use for: project intent (first bootstrap), immediate decisions, requirement clarification.

### Async Mode

When the human is unavailable or the request needs offline action, write a file to `human-calls/`.

Use for: operations the human must perform offline (deploy, create accounts), questions arising after the human has left, cross-session pending requests.

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

- After issuing a human call, mark dependent tasks as `waiting-human`
- Continue working on tasks that do not depend on this call
- **MUST NOT** block unrelated work while waiting for a human response

---

## SDD (Spec Driven Development)

- Use openspec to manage specs and changes
- Each change's tasks: max 5 per group with strong logical dependencies
- When applying a change, clear context after each task group before starting the next
- After completion: verify, archive, commit

---

## Agent Team

Uses Claude Code's native **Task tool** for multi-agent work.

### How It Works

- The parent agent spawns sub-agents via the `Task` tool with appropriate `subagent_type`
- Sub-agents share the same file system and return results directly — no file-based message passing needed
- Isolation: each sub-agent works on a different openspec change, naturally avoiding file conflicts

### Roles

Roles are expressed in the Task tool prompt:

| Role | Responsibility | Example prompt prefix |
|------|---------------|----------------------|
| architect | Spec design, proposals, architecture | "As architect, design the spec for..." |
| implementer | Code implementation per spec | "As implementer, execute tasks 1-3 of change..." |
| reviewer | Verify implementation matches spec | "As reviewer, verify change X against its spec..." |

### When to Use Agent Team

- **Single agent** (default): Most work. One agent handles all roles.
- **Multi-agent**: When multiple independent changes can be parallelized. Parent spawns one sub-agent per change.

---

## Key Files

| File | Purpose | Managed by |
|------|---------|-----------|
| `demands.md` | Project requirements (obtained via human calls) | AI + human |
| `progress.md` | Cross-session progress | AI |
| `human-calls/` | Async human call queue | AI creates, human responds |
| `se3.config.yaml` | Framework config (optional) | Human |

---

## Conventions

### Self-Iterate

When instructed to self-iterate, execute without stopping until step 5:

1. Obtain/update requirements via human call → write to `demands.md`
2. Align project to `demands.md` (through openspec changes)
3. Check alignment — if incomplete, go to 2
4. Check if all requirements are fulfilled — if not, go to 1
5. Update project documentation

### Commit Messages

```
[change-name] Completed XYZ

Status: 3/5 tasks done in this change
Note: edge case in module Y needs attention
Next: finish remaining tasks, focus on error handling
```

---

## Project Structure

```
project/
├── demands.md
├── progress.md
├── se3.config.yaml        # optional
├── README.md
├── human-calls/
├── openspec/
│   ├── specs/
│   ├── changes/
│   └── archive/
└── .claude/
    └── CLAUDE.md           # this file
```

---

## Configuration

Optional `se3.config.yaml` in project root. All settings have defaults.

- `max_tasks_per_change`: Max tasks per group (default: 5)
- `human_call.timeout_days`: Async call timeout in days (default: 7)
- `session.max_progress_entries`: Max entries in progress.md before archiving (default: 20)
