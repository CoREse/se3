# se3 loop应当在一个独立的branch上进行，当ctrl-c打断或者结束后再开启一个claude进程将其merge。如果se3 loop里调用了se3 collab，那collab的work branch也应该是在se3 loop的这个branch上分裂出来，然后合并到这个se3 loop上。如果这个功能已经实现了，则检查是否有bug或者实现不完全的地方。 (Iteration 21/30)

## Tasks

- [x] se3 loop应当在一个独立的branch上进行，当ctrl-c打断或者结束后再开启一个claude进程将其merge。如果se3 loop里调用了se3 collab，那collab的work branch也应该是在se3 loop的这个branch上分裂出来，然后合并到这个se3 loop上。如果这个功能已经实现了，则检查是否有bug或者实现不完全的地方。

## Bug Analysis and Fixes

### Bugs Found

1. **每次启动都创建新分支的问题**
   - `run_exclusive_loop` 和 `run_loop_collab` 在每次启动时都会创建新的 loop branch
   - 如果用户在中断后重新启动 loop，会创建新的分支而不是继续在原有分支上工作

2. **分支切换错误处理问题**
   - `create_loop_branch` 使用 `git checkout -b` 一步完成创建和切换
   - 如果切换失败，分支已创建但工作目录未切换，导致不一致状态

### Fixes Applied

1. **添加 `is_loop_branch` 函数** (`tools/se3_tools/commands/loop.py`)
   - 检测当前是否已在 loop branch 上
   - 如果是，则复用现有分支而不是创建新分支

2. **修复 `create_loop_branch` 函数** (`tools/se3_tools/commands/loop.py`)
   - 分离分支创建和切换为两个步骤
   - 添加切换失败时的清理逻辑（删除已创建分支）
   - 提供更好的错误处理

3. **修复 `run_exclusive_loop` 函数** (`tools/se3_tools/commands/loop.py`)
   - 启动时检测当前分支
   - 如果已在 loop branch 上，复用该分支
   - 否则创建新的 loop branch

4. **修复 `run_loop_collab` 函数** (`tools/se3_tools/commands/loop.py`)
   - 与 `run_exclusive_loop` 保持一致的分支检测逻辑
   - 复用现有 loop branch 如果已存在

### Branch Flow Verification

当前实现的分支流程是正确的：

```
original_branch (master/main)
    ↓
se3-loop/{timestamp} (loop branch created)
    ↓ (se3 collab within loop)
collab/{task_id} (task branches from loop branch)
    ↓ (after task completes)
se3-loop/{timestamp} (merged back)
    ↓ (after loop completes)
original_branch (user runs se3 loop --merge)
```

### Tests Added

- `TestIsLoopBranch`: 测试新的 `is_loop_branch` 函数
