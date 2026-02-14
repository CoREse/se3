# se3-scaffold Specification

## Purpose
TBD - created by archiving change se3-core-framework. Update Purpose after archive.
## Requirements
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
The system SHALL define the standard SE 3.0 project file structure.

Standard structure (demands.md removed):
```
project/
├── progress.md            # Cross-session progress tracking
├── se3.config.yaml        # Framework configuration (optional)
├── README.md              # Project documentation
├── human-calls/           # Async human call queue
├── openspec/
│   ├── specs/             # Source of truth for requirements
│   ├── changes/
│   └── archive/
└── .claude/
    └── CLAUDE.md          # SE 3.0 framework (project-level)
```

OpenSpec specs serve as the single source of truth for project requirements. No separate demands/requirements file is needed.

#### Scenario: Project initialization
- **WHEN** SE 3.0 is initialized in a directory
- **THEN** the standard file structure is created without demands.md

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
The system SHALL produce the following deliverables:
1. Project-level CLAUDE.md template (English)
2. Global CLAUDE.md template for ~/.claude/CLAUDE.md (English)
3. Configuration file template
4. Documentation and best practices guide

#### Scenario: Complete delivery
- **WHEN** SE 3.0 framework design is complete
- **THEN** all deliverables are available for direct use in new projects

### Requirement: Self-Iterate Flow
The system SHALL define a self-iterate behavior that drives the project from human intent to working implementation.

Flow:
1. Obtain direction via human call → create openspec change (proposal captures the intent)
2. Implement the change (specs → design → tasks → code)
3. Verify implementation against specs
4. Check if specs fully cover the project goals — if gaps exist, go to 1
5. Update project documentation

#### Scenario: Self-iterate execution
- **WHEN** agent is instructed to self-iterate
- **THEN** agent executes the flow without stopping until step 5, using human calls only when blocked

