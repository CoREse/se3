# Task CLI Functionality Specification

## Purpose
定义 Task CLI 的核心功能需求。

## Requirements

### Requirement: Task Management

用户 SHALL 能够通过命令行管理任务。

#### Scenario: Add a task
- **GIVEN** 用户想要添加新任务
- **WHEN** 执行 `task add "Task title"`
- **THEN** 任务被保存到 tasks.json
- **AND** 显示确认消息

#### Scenario: List tasks
- **GIVEN** 存在已保存的任务
- **WHEN** 执行 `task list`
- **THEN** 以表格形式显示所有任务
- **AND** 包含 ID、标题、优先级、截止日期和状态

#### Scenario: Mark task as done
- **GIVEN** 存在一个未完成的任务
- **WHEN** 执行 `task done <id>`
- **THEN** 该任务被标记为已完成
- **AND** 显示确认消息

#### Scenario: Delete a task
- **GIVEN** 存在一个任务
- **WHEN** 执行 `task delete <id>`
- **THEN** 该任务被删除
- **AND** 剩余任务 ID 被重新排序
- **AND** 显示确认消息

### Requirement: Task Properties

每个任务 SHALL 包含以下属性：
- `id`: 唯一标识符（整数，自动分配）
- `title`: 任务标题（字符串，必填）
- `priority`: 优先级（low/medium/high，默认 medium）
- `due`: 截止日期（可选，格式 YYYY-MM-DD）
- `done`: 完成状态（布尔值，默认 false）

#### Scenario: Add task with priority
- **GIVEN** 用户想要添加高优先级任务
- **WHEN** 执行 `task add "Urgent task" -p high`
- **THEN** 任务被保存，优先级设为 high

#### Scenario: Add task with due date
- **GIVEN** 用户想要添加带截止日期的任务
- **WHEN** 执行 `task add "Task with deadline" -d 2024-12-31`
- **THEN** 任务被保存，截止日期设为 2024-12-31

### Requirement: Data Persistence

任务数据 SHALL 持久化存储在 JSON 文件中。

#### Scenario: Tasks persist across sessions
- **GIVEN** 用户已添加任务
- **WHEN** 重新运行程序
- **THEN** 之前添加的任务仍然可见

#### Scenario: Custom tasks file location
- **GIVEN** 设置了 TASKS_FILE 环境变量
- **WHEN** 执行任何任务操作
- **THEN** 使用指定路径存储任务数据
