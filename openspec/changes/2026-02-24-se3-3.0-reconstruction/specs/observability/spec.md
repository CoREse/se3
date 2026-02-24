# observability Specification

## Purpose

定义 SE3 3.0 的可观测性机制：结构化日志、实时状态查询、项目 Dashboard。

## Requirements

### Requirement: 结构化日志

流程引擎 SHALL 为每个步骤记录结构化日志（JSON 格式）。

#### Scenario: 步骤执行日志
- **WHEN** 流程引擎完成一个步骤
- **THEN** 记录日志包含：
  - 步骤名称
  - 开始/结束时间
  - 使用的模型
  - Token 消耗（input/output）
  - 成功/失败状态
  - 输入摘要和输出摘要

#### Scenario: 日志查询
- **WHEN** 用户需要了解流程执行详情
- **THEN** 可通过 `se3 status --log` 查看结构化日志
- **AND** 支持按步骤、时间、模型过滤

### Requirement: 实时状态查询

`se3 status` SHALL 显示流程引擎的实时状态。

#### Scenario: 查看当前进度
- **WHEN** 用户执行 `se3 status`
- **THEN** 显示：当前步骤、已完成步骤、下一步骤、已用时间、token 消耗
- **AND** 如果有并行任务，显示每个任务的状态

### Requirement: 项目 Dashboard

`se3 dashboard` SHALL 生成人类可读的项目状态概览。

#### Scenario: Dashboard 内容
- **WHEN** 用户执行 `se3 dashboard`
- **THEN** 显示：
  - 当前 Phase 和进度
  - 活跃的 OpenSpec Changes
  - 最近的提交和变更
  - Backlog 中本 Phase 的待办项
  - 健康状态（lint/test 结果摘要）

### Requirement: AI Context 导出

流程引擎 SHALL 维护结构化的 `context.json`，供 LLM 调用时快速理解项目状态。

#### Scenario: Context 自动更新
- **WHEN** 流程引擎完成一个步骤
- **THEN** 更新 `se3/state/context.json`
- **AND** 包含：当前 Phase、活跃 change、最近提交、相关 spec 索引
- **AND** 后续步骤的 LLM 调用自动注入此 context
