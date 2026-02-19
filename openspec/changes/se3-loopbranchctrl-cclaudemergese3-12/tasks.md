# se3 loop应当在一个独立的branch上进行，当ctrl-c打断或者结束后再开启一个claude进程将其merge。如果se3 loop里调用了se3 collab，那collab的work branch也应该是在se3 loop的这个branch上分裂出来，然后合并到这个se3 loop上。如果这个功能已经实现了，则检查是否有bug或者实现不完全的地方。 (Iteration 12/30)

## Tasks

- [x] 检查 se3 loop 分支隔离功能的当前实现
- [x] 识别并修复 collab 分支合并问题
- [x] 改进 base branch fallback 逻辑

## 发现的问题与修复

### 问题 1: Collab 分支不会自动合并回 loop branch (已修复)

**问题描述**: 当 collab 任务在 worktree 中完成时，更改被提交到 `collab/{task_id}` 分支，但这些更改不会自动合并回 loop branch。这意味着 loop branch 实际上并没有包含 collab 任务的更改。

**修复方案**: 在 `collab_orchestrator.py` 的 `_run_worker` 方法中，当任务成功完成时，调用新的 `_merge_task_to_base_branch` 方法将 `collab/{task_id}` 分支合并回 `base_branch`（loop branch）。

**修改文件**:
- `tools/se3_tools/collab_orchestrator.py`: 添加 `_merge_task_to_base_branch` 方法，在任务完成后自动合并分支

### 问题 2: Base branch fallback 不正确 (已改进)

**问题描述**: 在 `cli.py` 中，当无法从 git config 获取 base branch 时，会回退到当前分支，但如果用户已经切换了分支，这可能不正确。

**修复方案**: 添加 `infer_loop_branch_base` 函数，通过检查 git 历史来推断最可能的 base branch（如 master、main、develop 等），而不是简单地使用当前分支。

**修改文件**:
- `tools/se3_tools/commands/loop.py`: 添加 `infer_loop_branch_base` 函数
- `tools/se3_tools/cli.py`: 更新导入和逻辑，先尝试推断 base branch，再回退到当前分支

## 分支层次结构

正确的分支层次结构是:
```
original (master/main) <- se3-loop/{timestamp} <- collab/{task_id}
```

当 collab 任务完成时:
1. collab/{task_id} 分支合并到 se3-loop/{timestamp}
2. 当 loop 完成时，se3-loop/{timestamp} 合并回 original (使用 `se3 loop --merge`)
