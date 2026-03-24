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
| `/se3:run` | 启动 SE3 流程引擎 |

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

在 `se3.yaml` 中配置确认/审阅步骤：

```yaml
confirmation:
  enabled: true                    # 启用确认步骤
  steps: ["propose", "design"]     # 在哪些步骤后插入确认
  reviewer: "human"                # 审阅者：human 或 llm
  llm_reviewer:
    model: null                    # LLM 模型（null=默认）
    max_iterations: 3              # 最大审阅-修改循环次数
```

审阅模式：
- `human` — 创建 MCP call 文件，等待人工确认
- `llm` — 自动 LLM 审阅，根据配置可能循环修改
