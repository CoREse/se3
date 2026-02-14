# SE 3.0 - Software Engineering 3.0 Framework

一套基于Claude Code的AI-first软件开发框架，探索AI agent主导的长程开发范式。

## 简介

SE 3.0将Anthropic的长程Agent最佳实践与Spec Driven Development (SDD)有机结合，在Claude Code平台上实现了一套完整自洽的开发体系。

### 核心原则

- **Human-as-MCP**：人类的一切输入（包括项目意图）通过 human call 按需获取，不依赖预置文件。同步模式（人在场直接问）+ 异步模式（人不在写文件）。
- **渐进式加载**：session启动时只读最小上下文（progress + git log），按需加载其他文件。不浪费context window。
- **增量开发**：以 openspec change 为单位推进，每个session聚焦有限范围。
- **文件即接口**：所有跨session、跨agent的通信通过文件系统。

## 快速开始

### 1. 在新项目中使用

```bash
mkdir my-project && cd my-project
git init
openspec init --tools claude

# 创建SE 3.0文件结构
mkdir -p human-calls agent-comms

# 复制CLAUDE.md模板
cp path/to/se3.0/output/CLAUDE.md .claude/CLAUDE.md
```

### 2. 开始开发

在Claude Code中输入 `自行迭代`。agent会：
1. 发现项目为空，通过 human call 直接询问你"这个项目要做什么？"
2. 将你的回答转化为 `demands.md`
3. 通过 openspec changes 逐步实现需求
4. 需要你介入时发起 human call（同步直接问，异步写文件）

### 3. 响应异步 Human Call

检查 `human-calls/` 目录下的请求文件，在 `## Response` 部分填写响应。

## 项目结构

```
project/
├── demands.md             # 项目需求（通过human call获取）
├── progress.md            # 跨session进展
├── se3.config.yaml        # 配置（可选）
├── README.md              # 文档
├── human-calls/           # 异步human call队列
├── agent-comms/           # Agent通信
├── openspec/
│   ├── specs/
│   ├── changes/
│   └── archive/
└── .claude/
    └── CLAUDE.md          # SE 3.0框架
```

## 核心概念

### Session Protocol

渐进式启动：
1. 读 `progress.md` 最近记录 + `git log` → 定位状态
2. 扫描 `human-calls/` 已响应请求 → 处理待办
3. 确定工作范围 → 按需加载其他文件

首次启动时通过 human call 获取项目意图。

### Human-as-MCP

| 模式 | 条件 | 方式 |
|------|------|------|
| 同步 | 人在场 | 直接对话（AskUserQuestion） |
| 异步 | 人不在/需离线操作 | 写文件到 human-calls/ |

三种调用类型：decision（决策）、action（操作）、information（信息）

### Spec Driven Development

`demands.md` → `openspec specs` → `openspec changes` → 代码实现

### Agent Team

- Change级别隔离避免冲突
- 角色：architect / implementer / reviewer
- 通过 agent-comms/ 通信

## 核心文件

| 文件 | 说明 |
|------|------|
| `output/CLAUDE.md` | SE 3.0 CLAUDE.md模板 |
| `output/se3.config.yaml` | 配置文件模板 |
| `docs/best-practices.md` | 最佳实践指南 |

## 灵感来源

- [Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) - Anthropic
- [OpenSpec](https://github.com/Fission-AI/OpenSpec) - Spec Driven Development

## 版本

- v2.0 - 2026-02-14 - 移除 intentions.md，统一 Human-as-MCP，渐进式启动协议
- v1.0 - 2026-02-14 - 初始版本
