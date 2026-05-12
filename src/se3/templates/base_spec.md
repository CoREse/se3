# {project_name} — Base Specification

## Purpose
项目基础约定。此 spec 由 `se3 init` 生成，在所有 `se3 run` 流程中自动加载。

## Requirements

### Requirement: Project Identity
- 项目名称: {project_name}
- 简述: {project_description}
- 主要语言/框架: {languages_and_frameworks}

### Requirement: Directory Structure
- {directory_structure}

### Requirement: Coding Conventions
- {coding_conventions}

### Requirement: Key Constraints
- {key_constraints}

### Requirement: Workflow Conventions
- {workflow_conventions}

### Requirement: Version Management

项目 SHALL 使用语义化版本控制（Semantic Versioning 2.0.0）作为版本管理标准。

**版本号文件（单一真相源）:**
- Python 项目: `pyproject.toml` 中的 `project.version` 字段
- Node.js 项目: `package.json` 中的 `version` 字段
- 其他项目: 在 `se3.yaml` 中显式指定 `version.file_path`

**版本格式:**
遵循 SemVer 2.0.0: `MAJOR.MINOR.PATCH[-prerelease][+build]`
- MAJOR: 不兼容的 API 修改
- MINOR: 向下兼容的功能添加
- PATCH: 向下兼容的问题修复

**版本决策模型:**
- `version_analyze` 步骤的 `suggested_version` 字段是新版本号的唯一权威来源
  （由 LLM 基于实际变更内容、SemVer 2.0.0 默认规则以及可选的项目级规则文件推导）
- 可选自定义规则: 在 `se3/version-rules.md` 写入自然语言规则，
  `version_analyze` 会将其注入 LLM prompt 作为决策依据；文件不存在时回落到默认 SemVer 2.0.0 规则
- `commit` 步骤直接采用 `suggested_version` 写入版本文件；若该字段缺失或步骤失败，
  流程报错中断并提示人工介入（不再有静默 patch bump 兜底）

**文档更新:**
- README.md: 显示当前版本徽章/头部
- VERSIONS.md: 维护版本历史变更日志

**配置（se3.yaml）:**
```yaml
version:
  enabled: true
  file_path: null  # 自动检测
  include_in_commit_message: true
```

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
