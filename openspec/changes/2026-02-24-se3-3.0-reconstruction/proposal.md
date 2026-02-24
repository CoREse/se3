## Why

SE3 2.x 把开发流程写在 markdown 规范里，依赖 agent "读懂并遵守"。这导致三个根本问题：

1. **流程不可靠**：Agent 跳步骤、漏验证、走错分支。5 种 workflow 的步骤序列全靠 prompt 约束
2. **状态不可靠**：progress.md 膨胀到 155KB+，状态散落在多个 markdown 文件中，中断后无法精确恢复
3. **协作不稳定**：Loop/Collab 的分支管理、worktree 隔离、中断恢复反复出问题

核心洞察：**LLM 是不可靠的执行者，但是优秀的思考者**。应该让程序负责"做什么、什么顺序做"，让 LLM 负责"怎么想、怎么写"。

## What Changes

### 架构级变更

- **引入流程引擎**：Python 状态机驱动开发流程，取代 prompt 驱动
- **单入口 `se3 run`**：取代 `start/work/done` 手动串联
- **命令精简**：从 38+ 缩减到 ~8 个核心命令
- **状态持久化**：JSON 状态机 + 中断恢复，取代 markdown 状态文件

### 流程级变更

- **OpenSpec 程序化集成**：流程引擎自动读取相关 spec 注入 LLM
- **递归标准流程**：Problem → Manager → Sub-problems/Tasks 的可分裂模型
- **模型分工**：不同角色使用不同模型

### 移除

- `se3 collab` — 被并行执行机制取代
- `se3 loop` — 被 `se3 run` 循环模式取代
- `se3 start/work/done` — 被 `se3 run` 统一
- `se3 handoff/sync/verify/guardrails` — 内化到流程引擎步骤
- 5 种 workflow 硬分类 — 流程引擎动态决定步骤

## Capabilities

### New Capabilities

- `flow-engine`: 状态机驱动的流程引擎，程序控制步骤转换
- `parallel-execution`: 递归标准流程，Task 级和 Problem 级并行
- `model-dispatch`: 模型分工和调度
- `long-term-planning`: Roadmap + Backlog 两层长期规划
- `observability`: 结构化日志和实时状态查询

### Removed Capabilities

- `se3-collab`: 由 parallel-execution 取代
- `se3-loop`: 由 flow-engine 的循环模式取代
- `se3-workflows` (5种硬分类): 由 flow-engine 动态步骤取代

## Impact

这是一次**架构级重构**，影响范围：

- `tools/se3_tools/` — 大部分命令重写或移除
- `output/` — 新的模板和规范输出
- `.claude/commands/` — 命令规范更新
- `openspec/specs/` — 多个 spec 需要重写

## Implementation Strategy

分 4 个 Phase 实施，Phase 1 为当前优先：

1. **Phase 1**: 流程引擎 + 单入口 + 状态持久化 + 命令精简
2. **Phase 2**: Task 级并行 + 静态模型角色映射
3. **Phase 3**: 递归标准流程 + 多 Manager + Problem 级并行
4. **Phase 4**: 知人善任模块 + 动态模型选择
