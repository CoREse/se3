## Context

SE3 2.x 经历了 23 个小版本迭代，从最初的 session protocol 发展到包含 loop、collab、guardrails 等功能的复杂框架。但核心架构始终是 "prompt 指导 agent 遵守流程"，这导致可靠性无法根本提升。3.0 需要在保留 2.x 积累的领域知识的同时，重建执行架构。

## Goals

- 流程执行可靠性从"依赖 agent 自觉"提升到"程序保证"
- 任意步骤中断后可精确恢复
- 减少人工干预（不再需要手动串联 start/work/done）
- 支持并行任务执行
- 支持不同模型担任不同角色

## Non-Goals

- 不重写 OpenSpec 规范系统本身（保持 spec-driven 方法论）
- 不构建通用工作流引擎（只服务于 SE3 开发流程）
- 不在 Phase 1 实现并行执行（先做稳单线程流程）
- 不构建 Web UI 或可视化界面

## Decisions

### D1: 流程引擎实现方式 — Python 状态机

**Decision**: 用 Python 实现有限状态机，每个状态对应一个流程步骤，转换由程序逻辑控制。

**Rationale**:
- 状态机是最简单且足够的模型，开发流程的步骤是有限且确定的
- Python 与现有 se3_tools 代码库一致
- 状态可序列化为 JSON，天然支持持久化和恢复

**Alternative considered**:
- 用 DAG（有向无环图）：更灵活但对当前需求过度设计
- 用现成的 workflow engine（如 Prefect、Airflow）：依赖太重，且这些工具面向数据流而非开发流程

### D2: 步骤内 LLM 调用方式 — subprocess 调 claude

**Decision**: 每个步骤内通过 `claude -p` subprocess 调用 LLM，传入步骤特定的 prompt 和 context。

**Rationale**:
- 与 2.x 的 claude_runner.py 一致，已验证可行
- 每次调用独立 context window，避免上下文污染
- 可在调用时指定不同模型（为模型分工做准备）

**Alternative considered**:
- 直接调 Anthropic API：更灵活但失去 Claude Code 的工具链（file edit、bash 等）
- 在单个 Claude Code session 内通过 Task tool：受限于当前 session 的 context

### D3: 动态步骤 vs 固定 Workflow — 动态步骤

**Decision**: 取消 5 种硬编码 workflow（bugfix/feature/review/directive/small），改为流程引擎根据输入动态选择步骤序列。

**Rationale**:
- 2.x 的分类经常不准确（一个 "bugfix" 可能需要 "feature" 的设计步骤）
- 动态选择允许流程引擎根据实际情况调整，如发现 bug 背后是设计缺陷时自动加设计步骤
- LLM 在分析步骤给出判断，程序决定激活哪些后续步骤

**Risk**:
- 动态选择可能导致步骤序列不可预测。缓解：定义一组固定的可选步骤池，动态选择是从池中选取，而非凭空生成

### D4: 状态存储格式 — JSON

**Decision**: 流程引擎状态用 JSON 文件存储（`se3/state/engine.json`），markdown 仅作为人类可读导出。

**Rationale**:
- JSON 可精确解析，不会丢失结构信息
- 支持原子更新（写临时文件 + rename）
- 与 `.se3-state.json` 的既有模式一致

### D5: 长期规划架构 — Roadmap + Backlog 两层

**Decision**: 用 `roadmap.md`（方向级）+ `openspec/backlog/`（想法级）两层结构管理长期规划。不引入优先级系统。

**Rationale**:
- Phase 本身就是粗粒度优先级，同 Phase 内等执行时再排序
- 依赖关系（Depends-on）比优先级数字更有确定性和实用性
- 优先级系统的维护成本容易超过收益
- Backlog 文件的核心是保留"当时的思考上下文"，这是最容易丢失的信息

### D6: 自举策略 — 独立目录渐进迁移

**Decision**: 在 `tools/se3_tools/` 中新建 `engine/` 模块开发 3.0 核心，保持 2.x 命令可运行，通过 `se3 run` 新入口启用 3.0 流程。

**Rationale**:
- 避免大爆炸式重写，降低风险
- 可以逐步把 2.x 的功能迁移到流程引擎步骤中
- 2.x 命令在迁移完成前继续作为 fallback

### D7: 项目级 SE3 规范安装不再需要 — 流程在程序中

**Decision**: 3.0 中，`se3 init` 不再将 SE3 规范安装到 `.claude/commands/` 目录。项目只需初始化 `openspec/` 目录和 `se3.config.yaml`。

**Rationale**:
- 2.x 的规范安装是因为流程规则在 markdown prompt 中，需要 agent 读取 `.claude/` 目录里的文件来理解工作流程
- 3.0 的流程完全由程序（状态机 + 步骤处理器）控制，LLM 只在步骤内被调用执行具体思考任务
- LLM 不再需要"理解 SE3 的工作流程"，只需要执行被分配的具体任务（分析代码、写设计等）
- 消除了"规范版本不一致"的问题（之前 `.claude/` 中的规范可能与 `tools/` 中的代码不同步）
- 减少了 `se3 init` / `se3 update` 的复杂度

**Impact**:
- `se3 init`：只创建 `openspec/` 目录结构和 `se3.config.yaml`
- `se3 update`：不再需要同步 `.claude/` 目录（本自举项目除外，2.x 开发阶段仍需要）
- 项目的 `.claude/CLAUDE.md` 中不再需要 SE3 框架说明，只需要项目自身的说明

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| 流程引擎本身的复杂度 | 引入新的维护负担 | 保持状态机简单，不做通用化 |
| 动态步骤导致行为不可预测 | 用户困惑 | 固定步骤池 + 日志 + status 命令 |
| 自举期间 2.x/3.0 共存 | 代码混乱 | 清晰的模块边界（engine/ 目录） |
| subprocess 调 LLM 的延迟 | 流程变慢 | 并行化（Phase 2）+ 小模型做快速判断 |

## Open Questions

1. 流程引擎的步骤池具体包含哪些步骤？需要从 2.x 的 5 种 workflow 中提炼
2. 动态步骤选择的 LLM prompt 如何设计，才能可靠地选出正确步骤？
3. `se3 run` 的循环模式（取代 loop）具体如何设计？是内置还是外部包装？
4. 人类输入的方式在 3.0 中如何统一？（MCP call / prompt 文件 / 交互式）
