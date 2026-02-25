# se3 sync

**DEPRECATED** — This command is deprecated in SE3 3.0 and will be removed in a future version.

Use `se3 run` which includes automatic spec synchronization during the update-spec step.

## Description

The `se3 sync` command was used in SE3 2.x to synchronize the `output/` directory with source files. It checked which specs needed updating based on modification times.

In SE3 3.0, this functionality is integrated into the flow engine's `update-spec` step.

## Migration Guide

### SE3 2.x (Deprecated)

```bash
# Preview what needs syncing
se3 sync

# Apply sync changes
se3 sync --apply
```

### SE3 3.0 (Recommended)

```bash
# Spec synchronization happens automatically during se3 run
se3 run "Update documentation"
```

## What Happens Now

When you use `se3 run`, the flow engine automatically:

1. **Tracks spec changes** — During development, tracks which specs were modified
2. **Updates specs** — The `update-spec` step synchronizes output with source files
3. **Includes in commit** — Spec changes are included in the automatic commit

## Why This Changed

In SE3 2.x, spec synchronization was a separate manual step that users had to remember. In SE3 3.0, the flow engine ensures specs are synchronized at the correct time in the workflow.

## Options (Deprecated)

```
--dry-run         Preview changes without applying (default)
--apply           Apply synchronization changes
--prune           Remove orphaned output files
--project-root    Project root directory (default: current directory)
```

## See Also

- `se3 run` — The unified entry point that includes spec updates
- `se3 update` — Update SE3 framework files (still available)

## Internal Note

The sync logic has been moved to:
- `tools/se3_tools/engine/steps/update_spec.py` — Flow engine step
- `tools/se3_tools/spec_index.py` — Spec tracking and indexing
