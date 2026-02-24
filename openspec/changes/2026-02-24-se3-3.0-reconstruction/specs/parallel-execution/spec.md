# parallel-execution Specification

## Purpose

定义 SE3 3.0 的并行执行模型：递归标准流程和两种协作模式（Task 级并行、Problem 级并行）。

> **Phase**: 2-3（Phase 1 不实现，此 spec 为前瞻设计）

## Requirements

### Requirement: 递归标准流程

标准流程 SHALL 支持递归分裂：一个标准流程可以产生子标准流程，每个子流程是完整的标准流程。

#### Scenario: 单层 Task 并行
- **WHEN** Manager 在 `plan-tasks` 步骤发现多个任务互不依赖
- **THEN** 为每个任务创建独立的 git branch（或 worktree）
- **AND** 并行执行这些任务
- **AND** 所有任务完成后 merge 回 Manager 的 branch

#### Scenario: Problem 级分裂
- **WHEN** Manager 在 `analyze` 步骤判断当前问题应分解为多个独立子问题
- **THEN** 为每个子问题创建独立 branch
- **AND** 每个子问题启动独立的标准流程（含独立 Manager）
- **AND** 所有子流程完成后 merge 回原 branch

#### Scenario: 递归深度限制
- **WHEN** 标准流程分裂的深度达到用户指定的最大层数
- **THEN** 不再允许进一步分裂
- **AND** 剩余工作在当前层级串行执行

### Requirement: Task 是最小执行单元

Task SHALL 是执行的最小单元，没有 Manager，不可再分。

#### Scenario: Task 执行
- **WHEN** 一个 Task 被分配给 worker agent
- **THEN** worker 在独立 branch/worktree 中执行
- **AND** worker 不可创建子任务或子流程
- **AND** 完成后 commit 并退出

### Requirement: Branch 隔离

所有并行执行 SHALL 通过 git branch 或 worktree 实现隔离。

#### Scenario: 并行任务的 branch 管理
- **WHEN** Manager 启动并行任务
- **THEN** 每个任务从 Manager 的当前 branch 创建子 branch
- **AND** 任务完成后 merge 回 Manager branch
- **AND** merge 冲突由 Manager（LLM）解决
