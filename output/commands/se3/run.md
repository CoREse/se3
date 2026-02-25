# se3 run

**SE3 3.0** - Unified entry point for the flow engine.

## Overview

`se3 run` is the primary command for SE3 3.0. It replaces the manual `start`/`work`/`done` workflow with a state machine-driven process that:

- Automatically manages workflow steps
- Persists state for interrupt/resume
- Handles context gathering automatically
- Runs steps programmatically

## Usage

### Start a New Flow

```bash
se3 run "Implement user authentication"
se3 run "Fix login bug" --type=bugfix
se3 run "Add tests" --type=small
```

### Resume an Interrupted Flow

```bash
se3 run --resume
se3 run --resume --flow-id=abc123
```

### Loop Mode (Continuous Execution)

```bash
se3 run --loop
se3 run --loop "Initial task"
```

### Options

| Option | Description |
|--------|-------------|
| `--resume, -r` | Resume interrupted flow |
| `--loop, -l` | Loop mode (continuous task execution) |
| `--type, -t` | Task type: feature, bugfix, refactor, small (default: feature) |
| `--change, -c` | Change name for this task |
| `--flow-id` | Specific flow ID to resume |

## Flow Steps

The flow engine executes steps from a fixed step pool:

1. **analyze** - Analyze task and determine required steps
2. **read-spec** - Read relevant OpenSpec specifications
3. **propose** - Generate change proposal (if needed)
4. **design** - Design solution (for medium/large changes)
5. **plan-tasks** - Break down into specific tasks
6. **implement** - Write code
7. **test** - Run tests
8. **verify-spec** - Verify implementation matches spec
9. **update-spec** - Update spec to reflect changes
10. **commit** - Commit changes
11. **summarize** - Generate summary

Steps are selected dynamically based on task analysis.

## State Persistence

Flow state is saved to `.se3/state/flow_*.json` after each step. This enables:

- Recovery from crashes or interruptions
- Resume with `se3 run --resume`
- Review of flow history

## Examples

```bash
# Start a feature
se3 run "Add payment integration"

# Quick bug fix
se3 run "Fix null pointer" --type=bugfix --type=small

# Resume after interruption
se3 run --resume

# Continuous development mode
se3 run --loop "Process backlog items"
```

## Migration from 2.x

| 2.x Command | 3.0 Equivalent |
|-------------|----------------|
| `se3 start` | `se3 run` (automatic) |
| `se3 work` | `se3 run` or `se3 run --resume` |
| `se3 done` | `se3 run` (includes commit/summarize) |
| `se3 full-cycle` | `se3 run` |
| `se3 loop` | `se3 run --loop` |

Legacy commands still work but show deprecation warnings.
