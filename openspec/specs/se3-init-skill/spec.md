# se3-init-skill Specification

## Purpose
TBD - created by archiving change se3-config-and-skills. Update Purpose after archive.
## Requirements
### Requirement: SE 3.0 Init Skill
系统SHALL提供一个Claude Code Skill用于在新项目中初始化SE 3.0框架。

Skill执行时SHALL：
1. 创建标准文件结构（human-calls/、agent-comms/）
2. 创建默认的se3.config.yaml
3. 创建intentions.md模板
4. 创建progress.md
5. 初始化openspec（如果尚未初始化）
6. 输出使用说明

#### Scenario: 在空项目中初始化
- **WHEN** 用户在一个空目录中执行SE 3.0 init skill
- **THEN** 创建完整的SE 3.0项目结构和所有模板文件

#### Scenario: 在已有项目中初始化
- **WHEN** 用户在已有代码的项目中执行SE 3.0 init skill
- **THEN** 仅创建缺失的SE 3.0文件，不覆盖已有文件

