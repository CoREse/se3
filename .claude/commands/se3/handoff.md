# se3 handoff

**DEPRECATED** — This command is deprecated in SE3 3.0 and will be removed in a future version.

Use `se3 run` which includes automatic handoff during the summarize step.

## Description

The `se3 handoff` command was used in SE3 2.x to enforce the "commit before handoff" rule. It committed changes and generated a session summary.

In SE3 3.0, this functionality is integrated into the flow engine's `summarize` and `commit` steps.

## Migration Guide

### SE3 2.x (Deprecated)

```bash
# Do work
se3 work "Implement feature"

# Commit and handoff
se3 handoff "Completed feature implementation"
```

### SE3 3.0 (Recommended)

```bash
# Single command handles the entire flow including commit and summary
se3 run "Implement feature"
```

## What Happens Now

When you use `se3 run`, the flow engine automatically:

1. **Commits changes** — The `commit` step runs `se3 commit` automatically
2. **Generates summary** — The `summarize` step creates a session summary
3. **Updates progress** — Automatically updates progress tracking

## Why This Changed

In SE3 2.x, users had to manually remember to run `se3 handoff` after completing work. In SE3 3.0, the flow engine ensures proper handoff happens automatically as part of completing a flow.

## See Also

- `se3 run` — The unified entry point that includes commit and summarize
- `se3 commit` — Direct commit command (still available)
- `se3 done` — Deprecated, use `se3 run` instead

## Options

```
--skip-commit     Skip automatic commit (use with caution)
--dry-run         Preview what would happen without executing
--project-root    Project root directory (default: current directory)
```
