# incremental-dev-flow Specification

## Purpose
TBD - created by archiving change se3-core-framework. Update Purpose after archive.
## Requirements
### Requirement: Change-Based Incremental Development
系统SHALL以openspec change作为增量开发的基本单元，每个change代表一组逻辑相关的变更。

#### Scenario: 创建新的增量开发单元
- **WHEN** 需要对项目进行一组相关变更
- **THEN** 通过openspec创建一个新change，change内最多包含5个有强逻辑关系的任务

#### Scenario: 跨session的change管理
- **WHEN** 一个change的任务无法在单个session内完成
- **THEN** 在session结束时记录change的完成状态，下一个session继续推进

### Requirement: Git Checkpoint Mechanism
每完成一个有意义的工作单元后MUST进行git commit作为checkpoint。

commit message格式SHALL包含：
- 修改的大致内容摘要
- 对下一个session有帮助的上下文信息（如当前状态、注意事项、建议的下一步）

#### Scenario: Feature完成后checkpoint
- **WHEN** agent完成一个feature或change中的一组任务
- **THEN** 进行git commit，message包含内容摘要和上下文信息

#### Scenario: 通过checkpoint回滚
- **WHEN** 新的实现引入了问题
- **THEN** 可以通过git history定位并回滚到之前的正确状态

### Requirement: Demands-Spec Alignment Loop
系统SHALL维护demands.md与openspec specs之间的对齐关系。

#### Scenario: 新需求产生
- **WHEN** intentions.md更新导致新的需求出现
- **THEN** demands.md被更新以反映新需求，并通过openspec change将其实现到spec和代码中

#### Scenario: 对齐检查
- **WHEN** 一轮change完成后
- **THEN** 检查demands.md中的所有需求是否都已在spec和代码中实现

