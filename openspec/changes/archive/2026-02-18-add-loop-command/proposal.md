# Proposal: Add se3 loop Command

## Problem

No way to run multiple iterations of SE3 workflow automatically. Users need to
manually run se3 start/work/done for each task, which is tedious for repetitive
work like:
- Processing multiple items
- Adding multiple test cases
- Refactoring multiple modules

## Solution

Add `se3 loop` command that:
1. Runs SE3 workflow in a loop for specified iterations (default: 10)
2. Creates a new change for each iteration
3. Tracks progress via .se3-loop-state.json
4. Continues from where it left off if interrupted
5. Provides --reset flag to start over

## Usage

```bash
se3 loop "refactor module" --iterations 5
se3 loop "add test case" -n 20 --quick
se3 loop "process item" --reset
```

## Implementation

New files:
- `tools/se3_tools/commands/loop.py` - Core loop logic

Modified files:
- `tools/se3_tools/cli.py` - Register loop command
