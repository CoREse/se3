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

## 2. Specs as Source of Truth

- OpenSpec specs are the single authoritative record of what the project should do
- Human call results go directly into openspec change proposals — no intermediate file
- The proposal IS the demand. Specs formalize it. Archives preserve history.
- Use `openspec list --specs` for a full requirements overview

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
- Otherwise: single agent is simpler

### Task tool usage
- Include role in prompt: "As implementer, execute tasks..."
- Each sub-agent gets a separate change
- Results return directly — no file coordination

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
