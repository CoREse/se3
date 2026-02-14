## Context

SE 3.0是一个探索性项目，目标是在Claude Code平台上建立一套完整自洽的AI-first开发体系。当前Claude Code的主要限制是：
1. 单个context window有限，无法在一个session中完成大型项目
2. 每个新session从零开始，没有之前session的记忆
3. 多agent协作没有原生的协调机制
4. 人类交互是同步阻塞的

Anthropic的长程Agent研究提供了解决跨session问题的实践框架，openspec提供了SDD的基础设施。本设计将两者结合。

## Goals / Non-Goals

**Goals:**
- 设计一套基于文件系统的、Claude Code原生的SE 3.0框架
- 框架以CLAUDE.md为主要载体，辅以Skills和标准文件结构
- 所有机制基于文件系统，不引入额外的运行时依赖
- 框架应可直接在任何新项目中使用（通过复制CLAUDE.md和初始化文件结构）

**Non-Goals:**
- 不构建独立的软件工具或CLI（依赖现有的openspec CLI）
- 不构建实时通信系统（一切通过文件系统）
- 不设计复杂的权限管理系统
- 不处理分布式agent（所有agent在同一文件系统上工作）

## Decisions

### D1: 以CLAUDE.md作为主要实现载体

**决定**: 核心框架逻辑编码在CLAUDE.md中，作为agent的行为指令。

**理由**: CLAUDE.md是Claude Code原生支持的指令机制，agent在每个session开始时自动加载。相比Skills（需要用户主动调用）或MCP Server（需要额外部署），CLAUDE.md是最无摩擦的方式。

**替代方案考量**:
- Skills: 适合可选的、用户主动触发的行为，但不适合必须遵循的基础流程
- MCP Server: 过于重量级，引入额外依赖
- 组合方案: CLAUDE.md定义核心流程 + Skills提供便捷操作（如快速初始化），这是最终选择

### D2: 文件系统作为唯一通信渠道

**决定**: 所有跨session、跨agent的通信通过文件系统进行。

**理由**:
- Claude Code的agent没有网络通信能力（除了工具调用）
- 文件系统是唯一可靠的持久化机制
- Git提供了天然的版本控制和冲突检测

### D3: Human-as-MCP基于文件队列

**决定**: human call通过在`human-calls/`目录下创建markdown文件实现。

**理由**:
- Markdown文件对人类友好，易于阅读和响应
- 文件名包含时间戳，自然排序
- 状态通过文件内容（YAML frontmatter）管理
- 不需要任何额外工具或界面

**文件格式设计**:
```markdown
---
id: hc-001
type: decision
priority: high
status: pending
created: 2026-02-14
---
# [请求标题]

## Context
[为什么需要人类介入]

## Request
[具体请求内容]

## Options (如果是decision类型)
- A: [选项A描述]
- B: [选项B描述]

---
## Response (由人类填写)
[人类的响应内容]
```

### D4: Agent Team通过Change隔离

**决定**: 多agent协作通过让每个agent负责不同的openspec change来实现隔离。

**理由**:
- openspec change天然是独立的工作单元
- 不同change通常影响不同的文件
- 通过git分支可以进一步隔离
- 简单有效，不需要复杂的锁机制

### D5: Progress.md作为跨Session记忆

**决定**: 使用单一的progress.md文件记录所有session的进展。

**理由**:
- 简单直观，一个文件包含所有历史
- 倒序排列，最新信息在最前面
- 与git commit messages互补：commit message记录"做了什么"，progress.md记录"到了哪里、下一步做什么"

## Risks / Trade-offs

- [文件冲突] 多agent同时写入同一文件 → 通过change级别隔离和git分支缓解
- [Progress文件膨胀] 长期项目progress.md可能很大 → 定期归档旧记录
- [Human Call超时] 人类长时间不响应 → 设置优先级和超时提醒机制
- [CLAUDE.md复杂度] 指令过多可能降低agent遵循度 → 分层设计，核心指令精简
- [探索性质] 作为探索项目，某些设计可能需要在实践中迭代调整 → 保持灵活性，避免过度规范

## Open Questions

- Q1: 是否需要设计一个SE 3.0初始化命令/脚本来自动创建项目结构？
- Q2: agent角色分化在实际操作中如何指定（通过命令行参数？环境变量？CLAUDE.md分文件？）
- Q3: 对于非常大的项目，是否需要将progress.md拆分为多个文件？
