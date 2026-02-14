# SE 3.0 Best Practices

## 1. Human Calls

### When to use sync mode
- Human is present and can answer immediately
- Project direction (first bootstrap)
- Quick decisions, requirement clarification

### When to use async mode
- Offline operations (deploy, create accounts)
- Human has left for the day
- Question needs research time
- Cross-session pending requests

### Writing good human calls
- Provide full context (why is input needed?)
- For decisions: list options with trade-off analysis
- State which tasks are blocked

### When NOT to issue a human call
- Pure implementation details
- Already specified in openspec specs
- Answerable through docs or code search

## 2. Specs and Adaptive Formality

### Specs as source of truth
- OpenSpec specs are the single authoritative record of what the project should do
- In agent team mode, specs are **contracts between agents** — write them precisely enough that an agent with no other context can implement from them
- Use `openspec list --specs` for a full requirements overview

### Match process to scope
- **Don't over-formalize**: a bug fix doesn't need a proposal, specs, design, and tasks
- **Don't under-formalize**: a new capability that sub-agents will implement needs detailed specs with scenarios
- Rule of thumb: if you can describe the change fully in a commit message, skip the openspec change

### When to write a design doc
- Cross-cutting changes affecting multiple modules
- Architecture decisions where sub-agents need to make consistent choices
- New external dependencies or significant data model changes
- Skip for everything else

### Writing good specs (agent contracts)
- Each requirement must be implementable by an agent reading only the spec
- Scenarios (WHEN/THEN) serve as acceptance criteria — a reviewer agent uses them to verify
- Avoid vague language: "should handle errors gracefully" → "SHALL return HTTP 400 with error detail when input validation fails"

## 3. Commits

- Commit when a **meaningful unit of work** is complete
- NOT on a timer, NOT tied to /new, NOT after every task group
- A good commit represents a coherent change that makes sense on its own
- Message must include context for the next session

```
[change-name] Completed XYZ

Status: 3/5 tasks done
Note: edge case in module Y needs attention
Next: finish remaining tasks, focus on error handling
```

## 4. Context Management

- Clear context (/new) when **saturated** or when **switching to a different task domain**
- Do NOT clear mechanically — if the next task benefits from current context, continue
- Before clearing, ensure you've committed if there's meaningful work

## 5. Session Management

### Progressive startup
- Read only `progress.md` latest entry + `git log`
- Don't pre-read specs — load them when the task needs them

### Scope control
- 1-2 openspec changes per session
- Split if scope grows too large

### Effective progress records
- Record **outcomes** not process
- **Open issues** matter more than completed items
- **Next steps** must be specific and actionable

## 6. Agent Team

### When to use multi-agent
- Multiple independent openspec changes can run in parallel
- Otherwise: single agent is simpler and sufficient

### Task tool usage
- **Architect prompt**: "Design the spec for change X. Requirements must be detailed enough for another agent to implement."
- **Implementer prompt**: "Implement tasks 1-3 of change X. Read `openspec/specs/` for requirements. Do not deviate from the spec."
- **Reviewer prompt**: "Verify change X. Read the spec, read the implementation, report gaps."
- Each sub-agent gets a separate change — natural file isolation

### Specs as the agent interface
- Without good specs, the parent must stuff everything into the Task prompt (limited context)
- With good specs, the prompt can be short: "implement change X per spec" — the sub-agent reads the spec file itself
- This is the core reason SDD works well with agent teams

## 7. Common Issues

### Context exhaustion
- **Prevent**: Scope control + progressive loading
- **Handle**: Commit + update progress.md before context runs out

### Drift from spec
- `openspec verify` after completing a change
- Create a corrective change — don't patch the current one

### progress.md grows large
- Archive old entries to `docs/progress-archive/`
- Keep ~20 most recent records
