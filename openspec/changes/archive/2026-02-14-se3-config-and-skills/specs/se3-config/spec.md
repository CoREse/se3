## ADDED Requirements

### Requirement: Configuration File Format
系统SHALL支持通过 `se3.config.yaml` 文件配置框架行为。

配置文件位于项目根目录，使用YAML格式。所有配置项MUST有合理的默认值。

可配置项包括：
- `max_tasks_per_change`: 每个change的最大任务数（默认5）
- `progress_format`: progress.md的记录格式
- `human_call_timeout_days`: human-call的默认超时天数（默认7）
- `agent_roles`: 启用的agent角色列表
- `auto_archive`: change完成后是否自动归档（默认false）

#### Scenario: 使用默认配置
- **WHEN** 项目中没有se3.config.yaml文件
- **THEN** 框架使用内置默认值运行

#### Scenario: 自定义配置
- **WHEN** 项目中存在se3.config.yaml且指定了max_tasks_per_change为3
- **THEN** 框架在创建change时限制每组最多3个任务
