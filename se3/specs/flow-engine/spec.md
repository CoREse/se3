# flow-engine Specification

## Purpose

定义 SE3 3.0 的核心流程引擎（Flow Engine）：一个程序驱动的状态机，通过统一的 `se3 run` 入口控制开发流程的 11 个步骤编排，在每个步骤内调用 LLM 处理需要"思考"的部分。

## Requirements

### Requirement: 统一入口 `se3 run`

`se3 run` SHALL 作为 SE3 3.0 的唯一流程入口，取代 `se3:start` / `se3:work` / `se3:done` 的手动串联。

**Interface:**
```bash
# 新任务
se3 run "实现用户登录功能"

# 恢复中断的任务
se3 run --resume

# 循环模式（自动寻找并执行任务）
se3 run --loop

# 指定任务类型
se3 run "修复内存泄漏" --type=bugfix
```

#### Scenario: 新任务启动
- **WHEN** 用户执行 `se3 run "实现用户登录功能"`
- **THEN** 流程引擎创建新的流程实例
- **AND** 从 `analyze` 步骤开始执行

#### Scenario: 恢复已有任务
- **WHEN** 用户执行 `se3 run` 且存在未完成的流程状态
- **THEN** 流程引擎提示恢复或新建
- **AND** 如果选择恢复，从中断点继续

#### Scenario: 循环模式
- **WHEN** 用户执行 `se3 run --loop`
- **THEN** 流程引擎在完成一个任务后自动寻找下一个任务
- **AND** 支持从 backlog、roadmap、TODO 中发现任务

### Requirement: 状态机驱动流程

流程引擎 SHALL 以 Python 有限状态机实现，每个状态对应一个流程步骤。步骤之间的转换由程序逻辑控制，而非 LLM 决定。

**核心原则：**
1. 步骤转换是程序化的（programmatic）
2. LLM 只处理步骤内部的工作（思考、生成、分析）
3. LLM 的输出不改变步骤转换逻辑

#### Scenario: 正常流程执行
- **WHEN** 用户执行 `se3 run` 并提供任务描述
- **THEN** 流程引擎从 `init` 状态开始
- **AND** 按程序定义的转换规则依次进入后续步骤
- **AND** 每个步骤内调用 LLM 处理该步骤的具体工作

#### Scenario: 步骤池动态选择
- **WHEN** 流程引擎完成 `analyze` 步骤
- **THEN** 根据分析结果从固定步骤池中选取后续需要的步骤
- **AND** 步骤池是预定义的有限集合，不由 LLM 凭空生成

### Requirement: 11 步流程池

流程引擎 SHALL 定义固定的 11 步骤池，所有流程步骤从此池中选取。

| 步骤 | 职责 | LLM 参与 | 输入 | 输出 |
|------|------|---------|------|------|
| `analyze` | 分析任务类型和范围 | 是 | task_description | task_type, scope, complexity, required_steps |
| `read_spec` | 读取相关 spec 文件 | 否（程序自动） | scope | relevant_specs, spec_content |
| `propose` | 生成变更提案 | 是 | spec_content, task_description | proposal, files_to_modify, files_to_create |
| `design` | 设计方案和架构决策 | 是 | proposal, spec_content | design_doc, decisions, components |
| `plan_tasks` | 分解为具体可执行任务 | 是 | design_doc | task_list |
| `implement` | 编写代码实现 | 是 | design_doc, task_list | implementation, files_changed |
| `test` | 运行测试验证 | 否（程序执行） | - | test_results, tests_passed |
| `verify_spec` | 检查实现与 spec 一致性 | 是 | implementation, spec_content | verification_result, issues |
| `update_spec` | 更新 spec 记录变更 | 是 | changes_made | updated_specs |
| `commit` | 提交变更 | 否（程序执行） | changes_made | commit_hash |
| `summarize` | 生成总结和 handoff | 是 | all_previous_outputs | summary, handoff_context |

**不同任务类型的步骤序列：**
- `feature`: analyze → read_spec → propose → design → plan_tasks → implement → test → verify_spec → update_spec → commit → summarize
- `bugfix`: analyze → read_spec → propose → plan_tasks → implement → test → verify_spec → update_spec → commit → summarize
- `review`: analyze → read_spec → verify_spec → summarize
- `small`: analyze → implement → test → commit → summarize
- `directive`: analyze → read_spec → plan_tasks → implement → test → verify_spec → commit → summarize

#### Scenario: Feature 任务完整流程
- **WHEN** 任务类型为 `feature`
- **THEN** 执行完整的 11 步流程

#### Scenario: Small 任务简化流程
- **WHEN** 任务类型为 `small`
- **THEN** 跳过 propose、design、plan_tasks 步骤

### Requirement: 步骤内 LLM 调用

流程引擎 SHALL 在每个步骤内通过 subprocess 调用 LLM（`claude -p`），传入步骤特定的 prompt 和自动收集的 context。

**LLM 调用机制：**
1. 构建步骤特定的 prompt
2. 自动收集相关上下文（specs、前序步骤输出、项目状态）
3. 调用 Claude CLI 获取响应
4. 解析响应（支持 JSON 和文本）
5. 存储输出到步骤状态

#### Scenario: 自动注入上下文
- **WHEN** 流程引擎进入某个步骤
- **THEN** 程序自动收集该步骤所需的上下文
- **AND** 将上下文注入 LLM 调用的 prompt 中

#### Scenario: LLM 调用失败
- **WHEN** 步骤内的 LLM 调用失败（超时、API 错误、输出无效）
- **THEN** 流程引擎执行重试策略（最多 3 次）
- **AND** 如果重试仍失败，暂停流程并通知用户

### Requirement: 状态持久化与恢复

流程引擎 SHALL 将运行状态持久化为 JSON 文件（`se3/state/engine.json`），支持任意步骤中断后精确恢复。

**持久化内容：**
- 流程实例元数据（flow_id, task_description, task_type, status）
- 当前步骤状态（current_step_id, current_step_index）
- 已选步骤序列（selected_steps）
- 所有步骤历史（step_history, steps）
- 每个步骤的输入/输出

**原子写入：**
- 先写入临时文件，再 rename 到目标路径
- 避免写入中途中断导致状态文件损坏

#### Scenario: 中断恢复
- **WHEN** 流程在某步骤执行中被中断（ctrl-c、进程终止、系统崩溃）
- **AND** 用户重新执行 `se3 run`
- **THEN** 流程引擎从 JSON 状态文件恢复到中断前的步骤
- **AND** 提示用户当前恢复的位置和上下文

#### Scenario: Ctrl+C 中断注入
- **WHEN** 用户在中断时输入额外指令
- **THEN** 将指令注入到当前步骤的 LLM prompt 中
- **AND** 重新执行当前步骤

### Requirement: 步骤间输入传递

流程引擎 SHALL 自动构建步骤输入，将前序步骤的输出传递给后续步骤。

**输入构建规则：**
- 所有步骤接收 `task_description` 和 `flow_id`
- `read_spec` 接收 analyze 的 `scope`
- `propose` 接收 `relevant_specs` 和 `spec_content`
- `design` 接收 `proposal`
- `plan_tasks` 接收 `design_doc`
- `implement` 接收 `design_doc` 和 `task_list`
- `verify_spec` 接收 `implementation`
- `commit` 接收 `changes_made`
- `summarize` 接收所有前序输出

#### Scenario: 步骤输入自动构建
- **WHEN** 流程转换到新步骤
- **THEN** 根据规则自动构建步骤输入
- **AND** 包含所有相关的前序输出

### Requirement: 错误处理和重试

流程引擎 SHALL 提供错误处理和重试机制。

**错误处理策略：**
- 步骤失败时自动重试（最多 3 次）
- 超过重试次数后询问用户：重试、跳过、中止
- 用户可以选择跳过失败步骤继续执行

#### Scenario: 步骤失败重试
- **WHEN** 某个步骤执行失败
- **THEN** 自动重试该步骤
- **AND** 达到最大重试次数后询问用户

#### Scenario: 跳过失败步骤
- **WHEN** 用户选择跳过失败步骤
- **THEN** 将步骤标记为完成
- **AND** 继续执行后续步骤

## Architecture

### 核心组件

```
┌─────────────────────────────────────────────────────────────┐
│                     se3 run (CLI)                           │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                  State Machine                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ create_flow │→ │  run_step   │→ │ transition_to_next  │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│         ↑                │                      │           │
│         └────────────────┴──────────────────────┘           │
└───────────────────────────┬─────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Step Handler │    │ Persistence  │    │ LLM Caller   │
│  (11 steps)  │    │(engine.json) │    │(claude -p)   │
└──────────────┘    └──────────────┘    └──────────────┘
```

### 数据模型

**FlowInstance:**
- flow_id: 唯一标识
- task_description: 任务描述
- task_type: 任务类型
- status: 流程状态 (INIT, RUNNING, PAUSED, COMPLETED, FAILED)
- state: 状态对象（当前步骤、步骤历史、已选步骤）

**Step:**
- step_id: 唯一标识
- step_type: 步骤类型（11 种之一）
- status: 步骤状态 (PENDING, RUNNING, COMPLETED, FAILED, RETRYING)
- inputs: 输入字典
- outputs: 输出字典
- retry_count: 重试次数

## CLI 命令

### se3 run

主入口命令，创建或恢复流程实例并执行。

```bash
se3 run [TASK_DESCRIPTION] [OPTIONS]

Options:
  --resume          恢复中断的流程
  --loop            循环模式
  --type TYPE       指定任务类型 (feature|bugfix|review|small|directive)
  --change NAME     关联到指定 change
```

### se3 status

显示当前项目状态，包括流程状态、git 状态、pending human calls 等。

```bash
se3 status [--format json]
```

## 状态文件

流程状态保存在 `se3/state/engine.json`：

```json
{
  "flow_id": "uuid",
  "task_description": "...",
  "task_type": "feature",
  "status": "RUNNING",
  "state": {
    "current_step_id": "...",
    "selected_steps": ["analyze", "read_spec", ...],
    "steps": {...}
  }
}
```
