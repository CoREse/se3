# flow-engine Specification

## Purpose

定义 SE3 3.0 的核心流程引擎：一个程序驱动的状态机，控制开发流程的步骤编排，在每个步骤内调用 LLM 处理需要"思考"的部分。

## Requirements

### Requirement: 状态机驱动流程

流程引擎 SHALL 以 Python 有限状态机实现，每个状态对应一个流程步骤。步骤之间的转换由程序逻辑控制，而非 LLM 决定。

#### Scenario: 正常流程执行
- **WHEN** 用户执行 `se3 run` 并提供任务描述
- **THEN** 流程引擎从 `init` 状态开始
- **AND** 按程序定义的转换规则依次进入后续步骤
- **AND** 每个步骤内调用 LLM 处理该步骤的具体工作
- **AND** LLM 的输出不改变步骤转换逻辑（只影响步骤内容）

#### Scenario: 步骤池动态选择
- **WHEN** 流程引擎完成 `analyze` 步骤
- **THEN** 根据分析结果从固定步骤池中选取后续需要的步骤
- **AND** 步骤池是预定义的有限集合，不由 LLM 凭空生成

### Requirement: 步骤内 LLM 调用

流程引擎 SHALL 在每个步骤内通过 subprocess 调用 LLM（`claude -p`），传入步骤特定的 prompt 和自动收集的 context。

#### Scenario: 自动注入上下文
- **WHEN** 流程引擎进入某个步骤
- **THEN** 程序自动收集该步骤所需的上下文（相关 spec、当前代码状态、前序步骤输出）
- **AND** 将上下文注入 LLM 调用的 prompt 中
- **AND** LLM 不需要自行查找上下文

#### Scenario: LLM 调用失败
- **WHEN** 步骤内的 LLM 调用失败（超时、API 错误、输出无效）
- **THEN** 流程引擎执行重试策略（最多 N 次）
- **AND** 如果重试仍失败，尝试降级到备选模型
- **AND** 如果降级也失败，暂停流程并通知用户

### Requirement: 状态持久化与恢复

流程引擎 SHALL 将运行状态持久化为 JSON 文件，支持任意步骤中断后精确恢复。

#### Scenario: 中断恢复
- **WHEN** 流程在某步骤执行中被中断（ctrl-c、进程终止、系统崩溃）
- **AND** 用户重新执行 `se3 run`
- **THEN** 流程引擎从 JSON 状态文件恢复到中断前的步骤
- **AND** 提示用户当前恢复的位置和上下文
- **AND** 用户可以选择继续或重新开始

#### Scenario: 状态原子更新
- **WHEN** 流程引擎更新状态
- **THEN** 先写入临时文件，再 rename 到目标路径
- **AND** 避免写入中途中断导致状态文件损坏

### Requirement: 单入口 `se3 run`

`se3 run` SHALL 作为 SE3 3.0 的唯一流程入口，取代 `se3 start` / `se3 work` / `se3 done` 的手动串联。

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
- **AND** 创建独立 branch 隔离 loop 的所有变更
- **AND** loop 结束或中断后 merge 回原 branch

### Requirement: 步骤池定义

流程引擎 SHALL 定义一个固定的步骤池，所有流程步骤从此池中选取。

步骤池（初始版本）：

| 步骤 | 职责 | LLM 参与 |
|------|------|---------|
| `analyze` | 分析输入，判断任务类型和范围 | 是 |
| `read-spec` | 读取相关 OpenSpec 规范 | 否（程序自动） |
| `propose` | 生成变更提案 | 是 |
| `design` | 设计方案和架构决策 | 是 |
| `plan-tasks` | 分解为具体可执行任务 | 是 |
| `implement` | 编写代码实现 | 是 |
| `test` | 运行测试验证 | 否（程序执行） |
| `verify-spec` | 检查实现与 spec 一致性 | 是 |
| `update-spec` | 更新 spec 记录变更 | 是 |
| `commit` | 提交变更 | 否（程序执行） |
| `summarize` | 生成总结和 handoff | 是 |
