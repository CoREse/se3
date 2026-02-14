# SE 3.0 开发框架 v2

> AI-first的长程软件开发框架。将本文件放置在项目的 `.claude/CLAUDE.md` 中使用。

## 核心原则

1. **Human-as-MCP**：人类的一切输入（包括项目意图）通过 human call 按需获取，不依赖预置文件
2. **渐进式加载**：只加载做决策所需的最小上下文，按需深入
3. **增量开发**：以 openspec change 为单位推进，每个 session 聚焦有限范围
4. **文件即接口**：所有跨 session、跨 agent 的通信通过文件系统

---

## Session Protocol

### 启动流程

**Step 1: 快速定位状态**
- 读取 `progress.md` 最近一条记录
- 读取 `git log --oneline -5`
- 若两者均不存在 → 进入**首次启动**（见下方）

**Step 2: 检查待处理事项**
- 扫描 `human-calls/` 中 `status: responded` 但未处理的请求
- 检查 `openspec/changes/` 中进行中的 change

**Step 3: 确定工作范围**
- 根据 progress 中"下一步建议" + 活跃 changes 确定本 session 目标
- 仅在工作需要时按需读取 specs、demands 等文件

**首次启动**（项目为空时）：
1. 通过 human call（同步模式）询问："这个项目要做什么？"
2. 将人类响应转化为 `demands.md` 初始内容
3. 创建 `progress.md`
4. 初始化 openspec（如未初始化）

### 结束流程

1. 确保代码可正常运行
2. 更新 `progress.md`（在顶部添加本 session 记录）
3. git commit（message 含内容摘要 + 下一 session 上下文）
4. 更新 openspec change 状态

### Progress 文件格式

```markdown
## YYYY-MM-DD Session N

### 工作内容
- [完成的工作项]

### 完成的Change
- `change-name`: 状态

### 遗留问题
- [未解决的问题]

### 下一步建议
- [具体可执行的建议]
```

---

## Human-as-MCP

所有人类输入通过 human call 获取。两种模式：

### 同步模式（默认）

人类在场时，直接通过对话交互（AskUserQuestion）。

适用场景：
- 项目意图获取（首次启动）
- 即时决策（方案选择、需求确认）
- 信息补充（业务逻辑、优先级）

### 异步模式

人类不在场、或请求需要离线处理时，在 `human-calls/` 下创建请求文件。

**何时使用异步模式：**
- 需要人类离线执行的操作（部署、账号申请、外部配置）
- 人类明确离场后产生的新问题
- 跨 session 的未决请求

**请求文件格式**（文件名：`{YYYYMMDD}-{HHmmss}-{简短描述}.md`）：

```markdown
---
type: decision | action | information
priority: high | medium | low
status: pending | responded
created: YYYY-MM-DD
---

# [请求标题]

## Context
[为什么需要人类介入]

## Request
[具体请求内容]

## Options（decision类型时提供）
- **A**: [选项A + 优劣]
- **B**: [选项B + 优劣]

---
## Response（由人类填写）

```

### 非阻塞原则

- 发起 human call 后，依赖此调用的任务标记为 waiting-human
- 继续执行其他不依赖此调用的任务
- **MUST NOT** 因等待人类响应而阻塞不相关任务

---

## SDD (Spec Driven Development)

- 使用 openspec 管理 specs 和 changes
- 每个 change 的 tasks 最多5个为一组
- apply change 时，每组任务完成后清除上下文再执行下一组
- 完成后验证、归档、commit

---

## Agent Team 协作

### 角色

| 角色 | 职责 |
|------|------|
| `architect` | spec 设计、change proposal、架构决策 |
| `implementer` | 按 spec 和 design 实现代码 |
| `reviewer` | 验证实现是否符合 spec |

### 协作原则

1. **Change 级别隔离**：每个 agent 负责不同的 change
2. **文件通信**：通过 `agent-comms/` 目录通信
3. **状态同步**：通过 openspec change 状态和 git commit 同步

### 通信文件格式

文件名：`{YYYYMMDD}-{HHmmss}-{from}-to-{to}.md`

```markdown
---
from: [发送者]
to: [接收者]
type: notification | request | handoff
status: unread | read | resolved
---

# [消息标题]

[消息内容]
```

---

## 特别文件

| 文件 | 用途 | 管理者 |
|------|------|--------|
| `demands.md` | 项目需求（通过 human call 获取） | AI + 人类共管 |
| `progress.md` | 跨 session 进展 | AI 管理 |
| `human-calls/` | 异步 human call 队列 | AI 创建，人类响应 |
| `agent-comms/` | Agent 间通信 | Agent 共管 |
| `se3.config.yaml` | 框架配置（可选） | 人类配置 |

---

## 约定行为

### 自行迭代

1. 通过 human call 获取/更新需求 → 写入 `demands.md`
2. 对齐项目到 `demands.md`（通过 openspec changes）
3. 检查对齐，未完成则跳转2
4. 检查需求是否完全实现，未完成则跳转1
5. 更新文档

---

## 项目结构

```
project/
├── demands.md             # 项目需求
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
    └── CLAUDE.md          # 本文件
```

---

## 配置

通过 `se3.config.yaml` 自定义（可选，所有项有默认值）：

- `max_tasks_per_change`: 每组最大任务数（默认5）
- `human_call.timeout_days`: 异步调用超时天数（默认7）
- `agent_team.roles`: 启用的角色
- `session.max_progress_entries`: progress 最大记录数（默认20）
