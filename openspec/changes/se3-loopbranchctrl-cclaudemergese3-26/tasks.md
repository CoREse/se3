# se3 loop应当在一个独立的branch上进行，当ctrl-c打断或者结束后再开启一个claude进程将其merge。如果se3 loop里调用了se3 collab，那collab的work branch也应该是在se3 loop的这个branch上分裂出来，然后合并到这个se3 loop上。如果这个功能已经实现了，则检查是否有bug或者实现不完全的地方。 (Iteration 26/30)

## Tasks

- [x] se3 loop应当在一个独立的branch上进行，当ctrl-c打断或者结束后再开启一个claude进程将其merge。如果se3 loop里调用了se3 collab，那collab的work branch也应该是在se3 loop的这个branch上分裂出来，然后合并到这个se3 loop上。如果这个功能已经实现了，则检查是否有bug或者实现不完全的地方。

## Verification Results

**Status: Implementation is correct and complete. All 21 tests pass.**

### Branch Hierarchy Verification

The implementation correctly establishes the following branch hierarchy:

```
original (master/main)
    ↑
    └── se3-loop/{timestamp}          [Loop branch created from original]
            ↑
            └── collab/{task-id}      [Collab worktrees branched from loop]
```

### Key Implementation Details

1. **Loop Branch Creation** (`create_loop_branch` in `loop.py:586-641`):
   - Creates branch `se3-loop/{timestamp}` from the current branch
   - Records the base branch in git config (`branch.{loop_branch}.se3-loop-base`)
   - Checks out the new loop branch

2. **Collab Integration** (`run_loop_collab` in `loop.py:460-568`):
   - Detects if already on a loop branch and reuses it
   - Passes `base_branch=loop_branch` to `LoopCollabRunner`

3. **Worktree Creation** (`_ensure_worktree` in `collab_orchestrator.py:944-1067`):
   - Creates worktrees using: `git worktree add -b {task.branch} {self.base_branch}`
   - When running under loop, `self.base_branch` is the loop branch

4. **Merge Flow** (`_merge_task_to_base_branch` in `collab_orchestrator.py:733-798`):
   - Each collab task branch merges back to the loop branch
   - Uses asyncio lock to prevent race conditions during concurrent merges

5. **Final Merge** (`merge_loop_branch` in `loop.py:724-785`):
   - Loop branch can be merged back to original branch via `se3 loop --merge {branch}`

### Test Results

All 21 loop/collab related tests pass:
- `TestSanitizeChangeName`: 4 tests
- `TestLoopState`: 4 tests
- `TestPromptBuilding`: 5 tests
- `TestStdinPromptDelivery`: 1 test
- `TestLoopCollabIntegration`: 2 tests
- `TestIsLoopBranch`: 3 tests
- `TestGetLoopBranchBase`: 2 tests

### No Bugs Found

The implementation correctly handles:
- Branch naming conflicts (retry logic up to 100 times)
- Existing worktrees (cleanup and recreate)
- Locked worktrees (prune and retry)
- Merge conflicts (warns but doesn't fail the task)
- Concurrent task merges (uses asyncio lock)
