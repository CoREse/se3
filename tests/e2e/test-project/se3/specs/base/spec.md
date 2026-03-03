# Task CLI — Base Specification

## Purpose
项目基础约定。此 spec 由 `se3 init` 生成，在所有 `se3 run` 流程中自动加载。

## Requirements

### Requirement: Project Identity
- 项目名称: Task CLI
- 简述: 一个简单的命令行任务管理工具，用于测试 SE3 工作流
- 主要语言/框架: Python 3.8+, Click, Rich

### Requirement: Directory Structure
- `src/task_cli/` — 源码目录
  - `__init__.py` — 包初始化
  - `cli.py` — CLI 主模块
- `tests/` — 测试目录
  - `test_cli.py` — CLI 测试
- `pyproject.toml` — 项目配置和依赖
- `README.md` — 项目文档
- `se3/` — SE3 运行时目录
  - `specs/` — 项目规范

### Requirement: Coding Conventions
- Python 风格遵循 PEP 8
- 使用 Black 进行代码格式化 (line-length: 88)
- 使用 Ruff 进行代码检查
- 类型注解使用 `from __future__ import annotations` 风格
- 测试使用 pytest，文件名 `test_*.py`

### Requirement: Key Constraints
- 保持简单，作为 SE3 测试项目
- 所有功能必须有对应的测试
- 版本管理使用 SemVer 2.0.0

### Requirement: Workflow Conventions
- 使用 `se3 commit` 代替 `git commit`
- commit 消息需包含上下文
- 运行测试后才可标记功能完成
- 主入口命令: `se3 run "task description"`

### Requirement: Version Management

项目 SHALL 使用语义化版本控制（Semantic Versioning 2.0.0）作为版本管理标准。

**版本号文件（单一真相源）:**
- Python 项目: `pyproject.toml` 中的 `project.version` 字段

**版本格式:**
遵循 SemVer 2.0.0: `MAJOR.MINOR.PATCH[-prerelease][+build]`
- MAJOR: 不兼容的 API 修改
- MINOR: 向下兼容的功能添加
- PATCH: 向下兼容的问题修复

**版本更新规则:**
- `feature` 任务 → bump minor 版本 (X.Y+1.0)
- `bugfix` 任务 → bump patch 版本 (X.Y.Z+1)
- `breaking` 变更 → bump major 版本 (X+1.0.0)

**文档更新:**
- README.md: 显示当前版本徽章/头部
- VERSIONS.md: 维护版本历史变更日志

#### Scenario: 版本自动更新
- **GIVEN** 当前版本为 1.2.3
- **WHEN** 完成 feature 任务并执行 commit 步骤
- **THEN** 版本自动更新为 1.3.0
- **AND** README.md 和 VERSIONS.md 同步更新
- **AND** 所有变更一起提交

#### Scenario: 手动版本控制
- **GIVEN** 在 `se3.yaml` 中设置 `version.enabled: false`
- **WHEN** 执行 commit 步骤
- **THEN** 不自动 bump 版本
- **AND** 需要手动更新版本号
