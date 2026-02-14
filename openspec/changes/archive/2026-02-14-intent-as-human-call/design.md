## Context

v1设计中 intentions.md 违反了 Human-as-MCP 原则——它要求人类预先准备文件，是同步阻塞思维。本次重设计将所有人类输入统一到 human call 通道。

## Goals / Non-Goals

**Goals:**
- 移除 intentions.md，项目意图通过 human call 获取
- 启动协议改为渐进式加载
- Human call 支持同步/异步双模式

**Non-Goals:**
- 不改变 SDD 流程和 openspec 用法
- 不改变 Agent Team 协作机制
- 不改变 git checkpoint 机制

## Decisions

### D1: 意图获取 = 首次 human call

项目意图不再需要预置文件。当 agent 进入空项目时，通过 human call（同步模式）直接询问人类。响应直接写入 demands.md 作为初始需求。

这消除了 intentions.md 这个概念，同时也消除了 "intentions.md 和 demands.md 内容重叠" 的问题。

### D2: Human call 同步/异步双模式

- **同步**：人在场，用 AskUserQuestion 直接问。大多数场景用这个。
- **异步**：人不在或需要离线操作，写文件到 human-calls/。

选择哪种模式的判断标准：人当前是否在场 + 请求是否需要离线时间完成。

### D3: 渐进式启动

不再固定读取文件清单。改为：
1. 先读最小信息定位状态（progress + git log）
2. 工作中按需读取其他文件

这类似于人类上班——先看交接记录，需要时再查文档。
