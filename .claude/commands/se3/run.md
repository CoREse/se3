# se3 run

**SE3 3.0** - Unified entry point for the flow engine.

## Overview

`se3 run` is the primary command for SE3 3.0. It replaces the manual `start`/`work`/`done` workflow with a state machine-driven process that:

- Automatically manages workflow steps
- Persists state for interrupt/resume
- Handles context gathering automatically
- Runs steps programmatically
- **NEW:** Supports confirmation/review steps with human or LLM reviewers

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
| `--reviewer` | Override confirmation reviewer: `human` or `llm` |

## Flow Steps

The flow engine executes steps from a fixed step pool:

1. **analyze** - Analyze task and determine required steps
2. **read-spec** - Read relevant OpenSpec specifications
3. **propose** - Generate change proposal (if needed)
4. **confirm** - Review proposal (configurable)
5. **design** - Design solution (for medium/large changes)
6. **confirm** - Review design (configurable)
7. **plan-tasks** - Break down into specific tasks
8. **implement** - Write code
9. **test** - Run tests
10. **verify-spec** - Verify implementation matches spec
11. **update-spec** - Update spec to reflect changes
12. **commit** - Commit changes
13. **summarize** - Generate summary

Steps are selected dynamically based on task analysis. Confirmation steps are inserted based on configuration.

## Confirmation (Review) Steps

SE3 3.0 supports optional confirmation steps after `propose`, `design`, and `plan_tasks`. These steps allow human or LLM reviewers to verify outputs before proceeding.

### Configuration

Add to your `se3.yaml`:

```yaml
confirmation:
  enabled: true                    # Enable confirmation steps
  steps: ["propose", "design"]     # Steps to confirm (default)
  reviewer: "human"                # Default: "human" or "llm"
  llm_reviewer:
    model: null                    # Model to use (null = default)
    max_iterations: 3              # Max review-modify cycles
```

### Reviewer Types

| Type | Description |
|------|-------------|
| `human` | Creates MCP call file; waits for human input via file edit or CLI |
| `llm` | Uses another LLM call to review; automatically approves or requests changes |

### Human Review Workflow

When `reviewer: human`:

1. Step output is displayed
2. MCP call file created in `se3/calls/`
3. Agent waits for response via:
   - **File edit:** Modify the Response section in the call file
   - **CLI input:** Interactive commands (y/n/r:feedback)
4. Three possible outcomes:
   - ✅ **Approve** - Continue to next step
   - 🔄 **Request Changes** - Return to previous step with feedback
   - ❌ **Abort** - Stop the workflow

### LLM Review Workflow

When `reviewer: llm`:

1. Step output is sent to LLM for review
2. LLM evaluates: completeness, spec compliance, maintainability
3. Automatic decision:
   - **Approved** - Continue
   - **Changes Requested** - Return to previous step (up to max_iterations)
   - **Max Iterations** - Mark flow as failed

### CLI Commands for Human Review

When waiting for confirmation in interactive mode, simply type:

| Input | Action |
|-------|--------|
| `y`, `yes`, `approve` | ✅ Approve and continue |
| `n`, `no`, `abort` | ❌ Abort the workflow |
| **Anything else** | 🔄 Request changes (your input becomes the feedback) |

**Simple rule:** Type `y` to pass, `n` to stop, or just type your feedback directly to request changes.

Examples:
```
> y                    # Approve
> yes                  # Approve
> n                    # Abort
> Please add error handling for the edge case  # Request changes with this feedback
> The design should use a factory pattern instead  # Request changes
> Missing unit tests   # Request changes
```

### Review Loop

If changes are requested, the flow returns to the original step with feedback:

```
propose → confirm (needs changes) → propose (with feedback) → confirm → design → confirm → ...
```

Each step tracks its review iteration count to prevent infinite loops.

## State Persistence

Flow state is saved to `.se3/state/engine.json` after each step. This enables:

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
