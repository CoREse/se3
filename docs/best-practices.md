# SE 3.0 Best Practices

## 1. Human Calls

### When to use sync mode
- Human is present and can answer immediately
- Project intent (first bootstrap)
- Quick decisions (A or B?)
- Requirement confirmation

### When to use async mode
- Human must perform offline operations (deploy, create accounts)
- Human explicitly left ("I'm done for today")
- Question needs research before answering
- Cross-session pending requests

### Writing good human calls
- Provide full context (why is human input needed?)
- For decisions: list all options with trade-off analysis
- Set correct priority
- State which tasks are blocked by this call

### When NOT to issue a human call
- Pure implementation details (agent decides)
- Already specified in demands.md
- Answerable through docs or search

## 2. Managing demands.md

- Single source of project requirements
- Initial content comes from the first human call
- Additive only — remove entries only if explicitly deprecated
- Use numbered hierarchy (D1, D1.1) for tracking
- Each requirement should be verifiable (you can tell if it's done)

## 3. Session Management

### Progressive startup
- Read only `progress.md` latest entry + `git log` to start
- Don't pre-read all files — wastes context window
- Load specs/demands only when the current task needs them

### Scope control
- Focus each session on 1-2 openspec changes
- If scope grows too large, split into more changes
- Don't try to finish the whole project in one session

### Effective progress records
- Record **outcomes**, not process ("Implemented X" not "Modified a.js")
- **Open issues** are more important than completed items
- **Next steps** must be specific and actionable

### Commit messages
```
[change-name] Completed XYZ

Status: 3/5 tasks done
Note: edge case in module Y needs attention
Next: finish remaining tasks, focus on error handling
```

## 4. Agent Team

### When to use multi-agent
- Multiple independent openspec changes can run in parallel
- Large project with clear separation of concerns
- Otherwise: single agent is simpler and sufficient

### Task tool usage
- Parent assigns each sub-agent a specific change
- Include role in the prompt: "As implementer, execute tasks..."
- Sub-agents return results directly — no file coordination needed

### Avoiding conflicts
- Each change should touch a disjoint set of files
- If two changes must touch the same file, sequence them instead of parallelizing

## 5. Common Issues

### Context window exhaustion
- **Prevent**: Scope control + progressive loading
- **Handle**: Prioritize shutdown protocol (commit + update progress.md)
- **Recover**: Next session picks up from progress.md

### Implementation drifts from spec
- Run `openspec verify` after completing a change
- Create a new corrective change — don't patch the current one

### progress.md grows too large
- Archive old entries to `docs/progress-archive/`
- Keep only the latest ~20 session records
