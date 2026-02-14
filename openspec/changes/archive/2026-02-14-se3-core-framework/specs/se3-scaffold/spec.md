## ADDED Requirements

### Requirement: CLAUDE.md Template System
系统SHALL产出一套可复用的CLAUDE.md模板，作为SE 3.0框架在Claude Code上的主要实现载体。

模板MUST包含：
- 标准流程定义（启动流程、执行流程、结束流程）
- 特别文件规定（intentions.md、demands.md、progress.md等）
- 约定行为定义（自行迭代、change管理等）
- Human-as-MCP调用规范
- Agent Team协作规范

#### Scenario: 新项目采用SE 3.0
- **WHEN** 用户在新项目中初始化SE 3.0框架
- **THEN** 生成完整的CLAUDE.md模板和配套文件结构

### Requirement: SE 3.0 Project Structure
系统SHALL定义标准的SE 3.0项目文件结构。

标准结构：
```
project/
├── intentions.md          # 项目意图（人类编写）
├── demands.md             # 具体需求（AI+人类共管）
├── progress.md            # 跨session进展记录
├── human-calls/           # 人类调用队列
├── agent-comms/           # Agent间通信
├── openspec/              # SDD管理
│   ├── specs/             # 功能规范
│   ├── changes/           # 变更管理
│   └── archive/           # 已归档变更
├── .claude/
│   └── CLAUDE.md          # SE 3.0框架配置
└── README.md              # 项目文档
```

#### Scenario: 初始化项目结构
- **WHEN** 在一个目录中初始化SE 3.0
- **THEN** 创建上述标准文件结构，包含合理的默认内容

### Requirement: Configuration System
系统SHALL支持通过配置文件调整框架行为。

可配置项包括：
- 每个change的最大任务数（默认5）
- progress.md的记录格式
- human-call的超时时间
- agent角色定义和权限

#### Scenario: 自定义配置
- **WHEN** 用户在CLAUDE.md或se3.config中修改配置
- **THEN** 框架按照新配置运行

### Requirement: Output Artifacts
系统SHALL产出以下可交付物：
1. CLAUDE.md模板文件
2. 配套Skills定义（如果适用）
3. 初始化脚本或命令
4. 使用文档和最佳实践指南

#### Scenario: 完整交付
- **WHEN** SE 3.0框架设计完成
- **THEN** 所有交付物齐备，用户可以直接在新项目中使用
