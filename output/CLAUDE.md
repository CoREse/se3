# SE 3.0 开发框架 v1

> 本文件是SE 3.0 (Software Engineering 3.0) 框架的Claude Code配置模板。
> 将本文件放置在项目的 `.claude/CLAUDE.md` 或全局 `~/.claude/CLAUDE.md` 中使用。

## 标准流程 [[**重要**]]

如无特殊约定，本session中的任何行为都需要遵循这一标准流程。

### Case Sensitive
- 本文档中提及的所有文件名和路径都是大小写敏感的。

### SDD (Spec Driven Development)
- 项目使用openspec进行Spec Driven Development。
- 会对项目的spec产生变更的修改都需要通过openspec change完成。
- 每个openspec change的tasks最多分为5个有强逻辑关系的任务为一组。
- apply change时，每执行完一组任务后清除上下文再执行下一组。
- 完成后验证实现，归档change，提交代码。

### 提交规范
- 每次修改完成后进行git commit。
- commit message包含：
  - 修改内容摘要
  - 对下一个session有帮助的上下文信息（当前状态、注意事项、建议的下一步）

### 文档
- 项目文档写在README.md中。
- 如有必要可在docs/目录下增加更多文档。

---

## 特别文件规定

| 文件 | 用途 | 管理者 |
|------|------|--------|
| `intentions.md` | 项目最根本意图 | 人类编写 |
| `demands.md` | 项目具体需求 | AI+人类共管，只增不减（除非与intentions冲突） |
| `progress.md` | 跨session累积进展 | AI管理，按时间倒序记录 |
| `human-calls/` | 人类调用请求队列 | AI创建请求，人类填写响应 |
| `agent-comms/` | Agent间通信目录 | Agent间共管 |
| `se3.config.yaml` | 框架配置文件 | 人类配置，AI读取 |

---

## Session Protocol

### 启动流程（每个session开始时MUST执行）

1. 阅读 `intentions.md` 了解项目意图
2. 阅读 `demands.md` 了解具体需求
3. 阅读 `openspec/` 下的specs和changes了解项目进展
4. 阅读git最近commit信息了解最新动态
5. 阅读 `progress.md` 了解跨session的累积进展
6. 检查 `human-calls/` 中是否有已响应但未处理的请求
7. 确定当前session的工作范围

### 结束流程（每个session结束前MUST执行）

1. 确保所有修改的代码可正常运行（无未解决的错误）
2. 更新 `progress.md`：在文件顶部添加本session的记录
3. 进行git commit（遵循提交规范）
4. 更新openspec相关状态（如有进行中的change）

### Progress文件格式

```markdown
## YYYY-MM-DD Session N

### 工作内容
- [完成的工作项]

### 完成的Change
- `change-name`: 状态

### 遗留问题
- [未解决的问题]

### 下一步建议
- [建议下一个session做什么]
```

---

## Human-as-MCP 调用规范

当遇到需要人类介入的事项时，使用以下机制进行非阻塞的人类调用。

### 调用流程

1. 在 `human-calls/` 目录下创建请求文件
2. 文件名格式：`{YYYYMMDD}-{HHmmss}-{简短描述}.md`
3. 标记依赖此调用的任务为 waiting-human
4. 继续执行其他不依赖此调用的任务
5. 在后续session中检查响应状态

### 请求文件格式

```markdown
---
id: hc-{序号}
type: decision | action | information
priority: high | medium | low
status: pending | responded | expired
created: YYYY-MM-DD
---

# [请求标题]

## Context
[为什么需要人类介入，提供足够的背景信息]

## Request
[具体请求内容]

## Options（decision类型时提供）
- **A**: [选项A描述，优劣分析]
- **B**: [选项B描述，优劣分析]

---

## Response（由人类填写）
<!-- 人类在此处填写响应 -->
```

### 调用类型

| 类型 | 场景 | 示例 |
|------|------|------|
| `decision` | 需要人类做出判断或选择 | 架构决策、技术选型、优先级排序 |
| `action` | 需要人类执行的操作 | 外部系统配置、账号申请、部署操作 |
| `information` | 需要人类提供的信息 | 业务逻辑确认、需求澄清、密钥提供 |

### 非阻塞原则
- **MUST NOT** 因等待人类响应而阻塞其他不相关任务
- 依赖人类响应的任务标记为 waiting-human 后暂停
- 其他任务继续正常推进

---

## Agent Team 协作规范

### 角色定义

| 角色 | 职责 | 权限 |
|------|------|------|
| `architect` | spec设计、change proposal、架构决策 | 创建/修改specs、proposal、design |
| `implementer` | 按spec和design实现代码 | 执行tasks、修改代码 |
| `reviewer` | 验证实现是否符合spec | 读取所有文件、创建验证报告 |

### 协作原则

1. **Change级别隔离**：每个agent负责不同的openspec change，避免文件冲突
2. **文件通信**：agent间通信通过 `agent-comms/` 目录下的文件进行
3. **状态同步**：通过openspec change状态和git commit同步进展

### 通信文件格式

文件存放在 `agent-comms/` 目录，文件名格式：`{YYYYMMDD}-{HHmmss}-{from}-to-{to}.md`

```markdown
---
from: [发送agent角色/ID]
to: [接收agent角色/ID]
type: notification | request | handoff
status: unread | read | resolved
---

# [消息标题]

[消息内容]
```

---

## 约定行为

### 自行迭代（不要停，不要问我，一直执行到第5步）

1. 根据 `intentions.md` 更新 `demands.md`
2. 对齐项目到 `demands.md`，中间可能涉及多次openspec change的建立和实现
3. 检查 `demands.md` 是否已完全对齐，如果没有，跳转到2
4. 检查 `intentions.md` 中的意图是否完全实现，如果没有，跳转到1
5. 为项目更新完整的文档

---

## SE 3.0 项目标准结构

```
project/
├── intentions.md          # 项目意图（人类编写）
├── demands.md             # 具体需求（AI+人类共管）
├── progress.md            # 跨session进展记录
├── se3.config.yaml        # 框架配置（可选）
├── README.md              # 项目文档
├── human-calls/           # 人类调用队列
├── agent-comms/           # Agent间通信
├── openspec/              # SDD管理
│   ├── specs/             # 功能规范
│   ├── changes/           # 变更管理
│   └── archive/           # 已归档变更
└── .claude/
    ├── CLAUDE.md          # 本文件
    └── skills/
        └── se3-init/      # SE 3.0初始化Skill
            └── SKILL.md
```

---

## 配置系统

框架行为可通过项目根目录的 `se3.config.yaml` 进行自定义。所有配置项都有默认值，配置文件是可选的。

主要配置项：
- `max_tasks_per_change`: 每个change的最大任务数（默认5）
- `human_call.timeout_days`: human-call超时天数（默认7）
- `agent_team.roles`: 启用的agent角色
- `session.max_progress_entries`: progress中保留的最大记录数（默认20）
