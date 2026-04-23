# SE3 自举项目

## 项目性质

这是一个**自举项目（Bootstrapping Project）**：

- **目的**：生成新的 SE3 规范
- **现状**：同时使用已发布的 SE3 规范进行开发

## 重要约束

生成新规范时：**不得更改项目使用的已发布规范**

- 已发布的规范位于 `.claude/` 目录中
- 开发依赖的规范文件应保持不变

## SE3 命令入口

| Command | 用途 |
|---------|------|
| `se3 run` | 启动 SE3 流程引擎 |

## 目录结构

- `se3/` — SE3 运行时目录（gitignored）
  - `specs/` — 项目规范
  - `state/` — 流程引擎状态
  - `cache/` — 缓存索引
  - `logs/` — 执行日志
  - `calls/` — 人工调用队列
  - `collab/` — 多智能体协作状态
- `se3.yaml` — 项目配置（可选）
- `.claude/` = 开发依赖的框架规范（只读）

## 确认步骤配置（可选）

在 `se3.yaml` 中按步骤配置确认/审阅（per-step dict 模型）：仅列出的 step 会被确认，没有全局总开关。

```yaml
agents:
  primary:      { type: claude-code, cmd: claude,      priority: 10 }
  reviewer_bot: { type: claude-code, cmd: claude-opus }

llm_caller:
  defaults: [primary]              # LLM 审阅省略 reviewer 时回落到这条链

confirmation:
  steps:                           # 未列出的 step = 不确认
    plan:    { reviewer: human }                           # 走 MCP call 人工确认
    design:  { reviewer: reviewer_bot, max_iterations: 3 } # 指定 agent 做 LLM 审阅
    propose: {}                                            # 省略 reviewer → 回落 llm_caller.defaults
```

每个 step 配置字段：
- `reviewer`:
  - `"human"` — 创建 MCP call 文件，等待人工确认
  - agent name（在顶层 `agents` 中注册） — 用该 agent 做单 agent LLM 审阅
  - 省略或 `null` — 使用 `llm_caller.defaults` 的链做 LLM 审阅
- `max_iterations` — LLM 审阅的最大修改循环次数（仅对非 `human` reviewer 生效）

引用未注册的 agent name 将在启动阶段 fail-fast。
