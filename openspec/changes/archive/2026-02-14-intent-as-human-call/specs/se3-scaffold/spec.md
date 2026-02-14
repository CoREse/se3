## MODIFIED Requirements

### Requirement: SE 3.0 Project Structure
系统SHALL定义标准的SE 3.0项目文件结构。

标准结构（移除 intentions.md）：
```
project/
├── demands.md             # 项目需求（通过human call获取，AI+人类共管）
├── progress.md            # 跨session进展记录
├── se3.config.yaml        # 框架配置（可选）
├── README.md              # 项目文档
├── human-calls/           # 人类调用队列（异步模式）
├── agent-comms/           # Agent间通信
├── openspec/              # SDD管理
│   ├── specs/
│   ├── changes/
│   └── archive/
└── .claude/
    └── CLAUDE.md          # SE 3.0框架配置
```

#### Scenario: 初始化项目结构
- **WHEN** 在一个目录中初始化SE 3.0
- **THEN** 创建上述标准文件结构，不包含 intentions.md

### Requirement: Configuration System
系统SHALL支持通过 `se3.config.yaml` 配置框架行为。

可配置项包括：
- `max_tasks_per_change`: 每个change的最大任务数（默认5）
- `human_call.timeout_days`: human-call的默认超时天数（默认7）
- `agent_team.roles`: 启用的agent角色列表
- `session.max_progress_entries`: progress中保留的最大session记录数（默认20）

#### Scenario: 使用默认配置
- **WHEN** 项目中没有se3.config.yaml文件
- **THEN** 框架使用内置默认值运行

### Requirement: Output Artifacts
系统SHALL产出以下可交付物：
1. CLAUDE.md模板文件
2. 配置文件模板
3. 使用文档和最佳实践指南

#### Scenario: 完整交付
- **WHEN** SE 3.0框架设计完成
- **THEN** 所有交付物齐备，用户可以直接在新项目中使用
