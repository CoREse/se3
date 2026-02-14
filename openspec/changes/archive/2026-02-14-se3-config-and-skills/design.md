## Context

SE 3.0核心框架（CLAUDE.md模板、协议定义、文件格式）已完成。本change补充配置系统和初始化工具。

## Goals / Non-Goals

**Goals:**
- 提供YAML格式的配置文件，允许自定义框架行为
- 提供一键初始化Skill，降低使用门槛

**Non-Goals:**
- 不构建配置文件的运行时解析器（框架基于CLAUDE.md指令，配置主要供agent参考）
- 不构建复杂的配置继承机制

## Decisions

### D1: YAML作为配置格式

**决定**: 使用YAML而非JSON或TOML。

**理由**: YAML对人类和AI都友好，支持注释，与openspec的YAML配置保持一致。

### D2: Skill采用SKILL.md格式

**决定**: 使用Claude Code原生的SKILL.md格式。

**理由**: 这是Claude Code的标准Skill定义方式，用户可以通过slash command调用。
