# long-term-planning Specification

## Purpose

定义 SE3 3.0 的长期规划机制：Roadmap + Backlog 两层结构，管理未实现的远期想法。

## Requirements

### Requirement: Roadmap 方向管理

项目 SHALL 维护 `roadmap.md` 文件，记录阶段（Phase）和方向级规划。

#### Scenario: Roadmap 作为粗粒度优先级
- **WHEN** 需要判断"接下来做什么"
- **THEN** 参考 roadmap.md 中当前 Phase 的内容
- **AND** Phase 编号本身就是优先级（Phase 1 > Phase 2 > ...）
- **AND** 不需要额外的优先级数字系统

#### Scenario: Phase 完成后更新
- **WHEN** 一个 Phase 的所有工作完成
- **THEN** 流程引擎提示"Phase N+1 有 M 个待办，是否开始规划？"

### Requirement: Backlog 想法保鲜

项目 SHALL 维护 `openspec/backlog/` 目录，每个远期想法一个 markdown 文件。

#### Scenario: 创建 Backlog 项
- **WHEN** 产生一个当前不实现但未来需要的想法
- **THEN** 在 `openspec/backlog/` 创建文件，包含：
  - Phase 归属
  - 依赖关系（Depends-on）
  - 状态（idea / planned / implementing / done）
  - 动机
  - 核心想法
  - **当时的思考上下文**（最容易丢失的信息）
  - 约束和开放问题

#### Scenario: Backlog 与 Change 的衔接
- **WHEN** 决定开始实现某个 Backlog 项
- **THEN** 从 Backlog 文件的内容出发，创建正式的 OpenSpec Change
- **AND** Change 中记录 `Implements: backlog/{item-name}`
- **AND** Backlog 项状态更新为 `implementing`

#### Scenario: Backlog 项过期
- **WHEN** 项目演进导致某个 Backlog 项不再需要
- **THEN** 标记状态为 `obsolete` 并记录原因
- **AND** 不删除文件（保留决策历史）

### Requirement: 不引入优先级系统

项目 SHALL NOT 引入细粒度优先级系统（如 P0/P1/P2 数字优先级）。

#### Scenario: 优先级通过 Phase 和依赖表达
- **WHEN** 需要排序工作优先级
- **THEN** 使用 Phase 归属作为粗粒度排序
- **AND** 使用 Depends-on 关系确定执行顺序
- **AND** 同 Phase 内的细粒度排序在执行时由 Manager 判断
