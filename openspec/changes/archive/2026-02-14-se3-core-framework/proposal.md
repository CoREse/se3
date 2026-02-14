## Why

当前Claude Code的开发模式受限于单个context window，无法有效处理跨session的复杂项目。Anthropic的长程Agent研究表明，通过结构化的知识传递、增量开发和checkpoint机制，可以让AI agent像人类团队轮班一样高效协作。我们需要设计一套完整的SE 3.0框架，将这些理念与Spec Driven Development有机结合，在Claude Code平台上实现。

## What Changes

- 定义SE 3.0核心开发流程：session启动协议、增量开发边界、session结束规范
- 设计跨session知识传递机制：结合git history、progress tracking、openspec specs
- 建立SDD与长程Agent的集成方案：openspec change作为增量开发单元
- 设计Human-as-MCP异步调用机制：非阻塞的人类交互模型
- 设计Agent Team协作模型：多agent并行工作的任务分配与协调
- 确定系统实现载体：CLAUDE.md模板、Skills、配置文件的组合方案

## Capabilities

### New Capabilities
- `session-protocol`: 跨session知识传递与session生命周期管理（启动/执行/结束协议）
- `incremental-dev-flow`: 基于openspec change的增量开发流程与git checkpoint机制
- `human-as-mcp`: 人类调用的异步接口设计与非阻塞执行模型
- `agent-team`: 多Agent协作模型，包括角色分化和任务协调
- `se3-scaffold`: SE 3.0系统的实现载体（CLAUDE.md模板、Skills定义、配置体系）

### Modified Capabilities
<!-- 这是全新项目，没有现有capability需要修改 -->

## Impact

- 产出一套可复用的CLAUDE.md模板体系
- 产出配套的Claude Code Skills
- 产出human-call队列文件规范
- 产出agent-team协调机制
- 影响所有使用此框架的Claude Code项目的开发流程
