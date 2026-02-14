# SE 3.0 - Software Engineering 3.0 Framework

一套基于Claude Code的AI-first软件开发框架，探索AI agent主导的长程开发范式。

## 简介

SE 3.0将Anthropic的长程Agent最佳实践与Spec Driven Development (SDD)有机结合，在Claude Code平台上实现了一套完整自洽的开发体系。核心理念：

- **跨Session知识传递**：通过结构化文件（progress.md、git history、openspec specs）实现agent间的记忆延续
- **增量式开发**：以openspec change为单位的增量开发，每个session聚焦有限范围
- **Human-as-MCP**：将人类交互建模为异步调用，不阻塞其他任务的执行
- **Agent Team**：支持多agent并行工作，通过文件系统协调

## 快速开始

### 1. 在新项目中使用SE 3.0

```bash
# 初始化项目
mkdir my-project && cd my-project
git init

# 初始化openspec
openspec init --tools claude

# 创建SE 3.0文件结构
mkdir -p human-calls agent-comms

# 复制SE 3.0 CLAUDE.md模板
cp path/to/se3.0/output/CLAUDE.md .claude/CLAUDE.md

# 创建意图文件
echo "# 意图\n\n[在此描述项目意图]" > intentions.md
```

### 2. 开始开发

在Claude Code中输入 `自行迭代`，agent将自动：
1. 读取intentions.md，生成demands.md
2. 通过openspec change逐步实现需求
3. 维护progress.md记录进展
4. 需要人类介入时创建human-call请求

### 3. 响应Human Call

检查 `human-calls/` 目录下的请求文件，在 `## Response` 部分填写响应。下一个session将自动处理。

## 项目结构

```
project/
├── intentions.md          # 项目意图（人类编写）
├── demands.md             # 具体需求（AI+人类共管）
├── progress.md            # 跨session进展记录
├── README.md              # 项目文档
├── human-calls/           # 人类调用队列
├── agent-comms/           # Agent间通信
├── openspec/              # SDD管理
│   ├── specs/             # 功能规范
│   ├── changes/           # 变更管理
│   └── archive/           # 已归档变更
└── .claude/
    └── CLAUDE.md          # SE 3.0框架配置
```

## 核心概念

### Session Protocol

每个Claude Code session遵循严格的生命周期：
- **启动**：读取所有上下文文件，确定工作范围
- **执行**：聚焦有限范围的任务，增量推进
- **结束**：确保代码可合并，更新progress.md，git commit

### Spec Driven Development

以openspec为基础的SDD流程：
- `intentions.md` → `demands.md` → `openspec specs` → `openspec changes` → 代码实现
- 每个change最多5个任务，粒度适合单个context window

### Human-as-MCP

将人类交互建模为异步MCP调用：
- 三种调用类型：decision（决策）、action（操作）、information（信息）
- 通过文件系统传递，非阻塞执行
- 跨session持久化

### Agent Team

多agent协作通过文件系统协调：
- Change级别隔离避免冲突
- 角色分化：architect、implementer、reviewer
- 通过agent-comms/进行通信

## 配置

SE 3.0支持通过 `se3.config.yaml` 自定义框架行为，所有配置项都有默认值。详见 `output/se3.config.yaml`。

## 核心文件

| 文件 | 说明 |
|------|------|
| `output/CLAUDE.md` | SE 3.0框架的CLAUDE.md模板，可直接复制到新项目使用 |
| `output/se3.config.yaml` | SE 3.0配置文件模板 |
| `output/skills/se3-init/SKILL.md` | SE 3.0初始化Skill |
| `intentions.md` | 本项目（SE 3.0框架本身）的意图 |
| `demands.md` | 本项目的详细需求 |
| `docs/best-practices.md` | 最佳实践指南 |

## 灵感来源

- [Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) - Anthropic
- [OpenSpec](https://github.com/Fission-AI/OpenSpec) - Spec Driven Development工具

## 版本

- v1.0 - 2026-02-14 - 初始版本，包含核心框架设计和CLAUDE.md模板
