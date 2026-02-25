# se3 verify

**DEPRECATED** — This command is deprecated in SE3 3.0 and will be removed in a future version.

Use `se3 run` which includes automatic verification during the verify-spec step.

## Description

The `se3 verify` command was used in SE3 2.x to verify that implementation matches change specifications. It extracted scenarios from specs and searched for verification markers in the codebase.

In SE3 3.0, this functionality is integrated into the flow engine's `verify-spec` step.

## Migration Guide

### SE3 2.x (Deprecated)

```bash
# Verify a specific change
se3 verify my-change-name

# Verify all specs
se3 verify

# Output as JSON
se3 verify --format json
```

### SE3 3.0 (Recommended)

```bash
# Verification happens automatically during se3 run
se3 run "Implement feature"

# Or use the OPSX skill for manual verification
/opsx:verify my-change-name
```

## What Happens Now

When you use `se3 run`, the flow engine automatically:

1. **Tracks scenarios** — During development, tracks which spec scenarios need verification
2. **Verifies implementation** — The `verify-spec` step checks that code contains verification markers
3. **Reports coverage** — Shows which scenarios are covered and which are not

## Scenario Coverage

In SE3 2.x, you marked scenarios as covered with verification markers in code:

```python
# Verify: change-verifier/Extract scenarios from change
# Verify: change-verifier/Find test marker
```

This still works in SE3 3.0. The flow engine searches for these markers during the verify-spec step.

## Why This Changed

In SE3 2.x, verification was a separate manual step. In SE3 3.0, the flow engine ensures verification happens at the correct time in the workflow — after implementation but before commit.

## Options (Deprecated)

```
--format          Output format: text or json (default: text)
--project-root    Project root directory (default: current directory)
```

## See Also

- `se3 run` — The unified entry point that includes verification
- `/opsx:verify` — OPSX skill for manual verification

## Internal Note

The verify logic has been moved to:
- `tools/se3_tools/engine/steps/verify_spec.py` — Flow engine step
- `tools/se3_tools/utils.py` — Scenario extraction and marker finding
