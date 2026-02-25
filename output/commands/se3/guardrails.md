# se3 guardrails

**DEPRECATED** — This command is deprecated in SE3 3.0 and will be removed in a future version.

Use `se3 run` which includes built-in guardrails checks during the analyze and verify-spec steps.

## Description

Guardrails were checks that ran before starting work in SE3 2.x. In SE3 3.0, these checks are integrated into the flow engine and run automatically at appropriate steps.

## Migration Guide

### SE3 2.x (Deprecated)

```bash
# Check if safe to start work
se3 guardrails

# Start work if guardrails pass
se3 work "Implement feature"
```

### SE3 3.0 (Recommended)

```bash
# Guardrails checks run automatically during se3 run
se3 run "Implement feature"
```

## What Happens Now

The flow engine automatically performs these guardrails checks:

1. **During `analyze` step**: Validates repository state, checks for uncommitted changes, verifies project structure
2. **During `verify-spec` step**: Ensures implementation matches specifications
3. **During `commit` step**: Validates all changes before final commit

## Why This Changed

In SE3 2.x, guardrails were a separate manual check that users could forget to run. In SE3 3.0, the flow engine ensures these checks run automatically at the correct time in the workflow.

## See Also

- `se3 run` — The unified entry point that includes guardrails
- `se3 status` — Check project status including any issues
