# model-dispatch Specification

## Purpose

定义 SE3 3.0 的模型分工和调度机制：不同角色使用不同模型，后期支持基于能力评估的动态调度。

## Requirements

### Requirement: 静态角色映射（Phase 2）

流程引擎 SHALL 支持在配置文件中定义角色到模型的映射，不同流程步骤使用不同模型。

#### Scenario: 按角色调用模型
- **WHEN** 流程引擎需要在 `analyze` 步骤调用 LLM
- **AND** 配置中 manager 角色映射到 opus
- **THEN** 使用 opus 模型执行该步骤
- **WHEN** 流程引擎需要在 `implement` 步骤调用 LLM
- **AND** 配置中 worker 角色映射到 sonnet
- **THEN** 使用 sonnet 模型执行该步骤

#### Scenario: 配置格式
- **WHEN** 用户在 `se3.config.yaml` 中配置模型映射
- **THEN** 格式为：
  ```yaml
  models:
    manager: opus
    worker: sonnet
    reviewer: haiku
  ```
- **AND** 未配置的角色使用默认模型

### Requirement: 知人善任模块（Phase 4）

流程引擎 SHOULD 支持基于历史执行结果评估模型能力，动态选择最佳模型。

> 此 requirement 为远期规划，Phase 4 实现。

#### Scenario: 模型能力评估
- **WHEN** 一个步骤由某模型执行完成
- **THEN** Manager 评估执行结果的质量
- **AND** 更新该模型在该类型任务上的能力评分

#### Scenario: 全力以赴模式
- **WHEN** 用户指定"全力以赴"模式
- **THEN** 每个步骤使用该类型任务评分最高的模型

#### Scenario: 给年轻人一个机会模式
- **WHEN** 用户指定"探索"模式
- **THEN** 以一定概率选择非最优模型执行
- **AND** 用于探索模型能力边界、发现新的最优选择
