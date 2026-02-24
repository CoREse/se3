# SE3 3.0 Reconstruction — Tasks

> 按 Phase 组织。Phase 1 为当前优先实施，Phase 2-4 为远期规划。

## Phase 1: 流程引擎（核心基础）

### 1. 流程引擎核心

- [x] 1.1 设计状态机数据结构（状态、转换、步骤池定义）
- [x] 1.2 实现 `engine/state_machine.py` — 状态机核心（状态转换、步骤执行框架）
- [x] 1.3 实现 `engine/persistence.py` — JSON 状态持久化（原子写入、恢复逻辑）
- [x] 1.4 实现 `engine/llm_caller.py` — 步骤内 LLM 调用封装（subprocess claude -p、重试、降级）
- [x] 1.5 实现 `engine/context_builder.py` — 自动上下文收集（读取相关 spec、前序输出、项目状态）
- [x] 1.6 为流程引擎编写单元测试

### 2. 步骤实现

- [x] 2.1 实现 `analyze` 步骤 — 分析输入、判断任务类型和范围、选择后续步骤
- [x] 2.2 实现 `read-spec` 步骤 — 程序化读取相关 OpenSpec 规范
- [x] 2.3 实现 `propose` 步骤 — 生成变更提案（调 LLM）
- [x] 2.4 实现 `design` 步骤 — 设计方案和架构决策（调 LLM）
- [x] 2.5 实现 `plan-tasks` 步骤 — 分解为具体任务（调 LLM）
- [x] 2.6 实现 `implement` 步骤 — 编写代码（调 LLM，最核心步骤）
- [x] 2.7 实现 `test` 步骤 — 运行测试（程序执行，非 LLM）
- [x] 2.8 实现 `verify-spec` 步骤 — 检查实现与 spec 一致性（调 LLM）
- [x] 2.9 实现 `update-spec` 步骤 — 更新 spec 记录变更（调 LLM）
- [x] 2.10 实现 `commit` 步骤 — 提交变更（程序执行，复用 se3 commit）
- [x] 2.11 实现 `summarize` 步骤 — 生成总结和 handoff（调 LLM）

**Phase 2 完成** — 流程引擎核心和全部步骤已实现

### 3. 单入口 `se3 run`

- [x] 3.1 实现 `commands/run.py` — 新建流程 / 恢复中断流程 / 循环模式
- [x] 3.2 实现中断恢复交互（提示用户选择继续或重新开始）
- [x] 3.3 实现 `--loop` 循环模式（自动寻找下一任务）

### 4. 状态与上下文

- [x] 4.1 设计 `se3/state/engine.json` 的 schema
- [x] 4.2 设计 `se3/state/context.json` 的 schema（AI context 导出）
- [x] 4.3 实现 context.json 的自动更新逻辑

### 5. 命令精简

- [x] 5.1 标记需要移除的命令，添加 deprecation 提示
- [x] 5.2 将 `handoff/sync/verify/guardrails` 的功能迁移到流程引擎步骤中
- [x] 5.3 更新 `se3 status` 以显示流程引擎状态
- [x] 5.4 实现 `se3 dashboard`（项目状态概览）

### 6. OpenSpec 程序化集成

- [x] 6.1 实现 spec 索引构建（扫描 openspec/specs/ 建立 capability → spec 映射）
- [x] 6.2 实现按任务描述自动匹配相关 spec
- [x] 6.3 实现 change 规模评估（自动决定需要哪些 artifact）

### 7. 长期规划基础设施

- [x] 7.1 创建 `roadmap.md`（初始版本，含 Phase 1-4 规划）
- [x] 7.2 创建 `openspec/backlog/` 目录结构
- [x] 7.3 将当前讨论中的远期想法（知人善任、递归流程等）写入 backlog 文件
- [x] 7.4 在流程引擎中集成 backlog 扫描（启动时加载当前 Phase 的待办）

### 8. 可观测性基础

- [x] 8.1 实现结构化日志模块（JSON 格式，每步记录输入/输出/耗时/模型/token）
- [x] 8.2 `se3 status --log` 支持查看执行日志

### 9. 测试与验证

- [x] 9.1 为状态机核心编写单元测试（状态转换、持久化、恢复）
- [x] 9.2 为步骤实现编写集成测试（mock LLM 调用）
- [x] 9.3 端到端测试：完整流程执行一个小任务
- [x] 9.4 中断恢复测试：模拟各种中断场景

### 10. 自举迁移

- [x] 10.1 在 `tools/se3_tools/` 中创建 `engine/` 模块目录
- [x] 10.2 保持 2.x 命令可运行作为 fallback
- [x] 10.3 更新 output/ 模板以反映 3.0 架构
- [x] 10.4 更新 .claude/ 发布规范

---

## Phase 2: 并行执行（远期）

- [ ] P2.1 实现 Task 级并行（worktree 隔离、并行 claude -p 调用、merge）
- [ ] P2.2 实现静态模型角色映射（config 中定义 manager/worker/reviewer → 模型）
- [ ] P2.3 改造 loop 模式（独立 branch、task 从 loop branch 分裂）
- [ ] P2.4 并行任务的 merge 冲突处理（LLM 辅助解决）

## Phase 3: 自治协作（远期）

- [ ] P3.1 实现递归标准流程（Problem 分裂为 Sub-problems）
- [ ] P3.2 实现多 Manager 协调（独立 branch、独立 context）
- [ ] P3.3 实现递归深度限制
- [ ] P3.4 Problem 级并行的 branch 管理和 merge 策略

## Phase 4: 智能调度（远期）

- [ ] P4.1 实现模型能力评估模块（执行结果评分、能力画像）
- [ ] P4.2 实现动态模型选择（基于能力评分自动选模型）
- [ ] P4.3 实现"全力以赴"模式
- [ ] P4.4 实现"给年轻人一个机会"探索模式
- [ ] P4.5 实现能力评估数据持久化和冷启动策略
