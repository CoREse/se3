## Why

SE 3.0核心框架已设计完成（CLAUDE.md模板、文件结构、协议定义），但缺少配置化能力和便捷的初始化工具。用户需要能够自定义框架行为，并能通过简单的命令快速在新项目中设置SE 3.0。

## What Changes

- 定义SE 3.0配置文件格式和配置项
- 创建SE 3.0初始化Skill，用于一键设置新项目
- 将本项目明确为SE 3.0的参考实现

## Capabilities

### New Capabilities
- `se3-config`: SE 3.0配置系统，支持项目级和全局级配置
- `se3-init-skill`: SE 3.0项目初始化Skill

### Modified Capabilities
- `se3-scaffold`: 增加配置系统集成和init skill引用

## Impact

- 新增se3.config.yaml配置文件规范
- 新增.claude/skills/se3-init/ Skill定义
- 修改output/CLAUDE.md模板以包含配置系统引用
