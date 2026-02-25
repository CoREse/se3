<!-- Generated on 2026-02-25 -->
<!-- SE3 Version: 3.1.1 -->
<!-- Checksum: 499f90fa5974cce35a9f29daf8326b877dc6267fadbc056e28d7e9a815a6853e -->

<!--
  SE 3.0 Framework Reference File
  ===============================
  This file is installed by `se3 init` and serves as the official framework specification.

  Generated File: DO NOT MODIFY DIRECTLY
  Version: {{SE3_VERSION}}
  Checksum: {{CHECKSUM}}
-->

# {{PROJECT_NAME}}

> **Note**: SE3 3.0 采用"流程引擎"架构。状态机驱动流程，步骤转换由程序保证，而非依赖 agent 自觉。

## SE3 Command 入口

### 推荐：统一入口 (3.0+)

| Command | 用途 |
|---------|------|
| `se3 run "任务描述"` | 启动新流程 |
| `se3 run --resume` | 恢复中断的流程 |
| `se3 run --loop` | 循环模式（自动执行多个任务） |
| `se3 dashboard` | 查看项目状态概览 |
| `se3 status --log` | 查看执行日志 |

### 兼容：传统入口 (2.x - 已弃用)

| Command | 用途 | 状态 |
|---------|------|------|
| `/se3:start` | 开始会话 | ⚠️ 弃用，使用 `se3 run` |
| `/se3:work <描述>` | 开始/继续工作 | ⚠️ 弃用，使用 `se3 run` |
| `/se3:done` | 结束会话 | ⚠️ 弃用，使用 `se3 run` |

## Flow Engine 架构 (3.0)

SE3 3.0 使用状态机驱动的流程引擎：

- **状态持久化**：流程状态保存为 JSON，支持任意步骤中断后精确恢复
- **程序控制**：步骤转换由程序逻辑控制，不依赖 LLM 决策
- **自动上下文**：每个步骤自动收集所需上下文（spec、代码状态、前序输出）
- **步骤池**：固定步骤池（analyze, propose, design, implement, test, commit 等），动态选择

流程状态保存在 `.se3/state/engine.json`。

## Git Commit

使用 `se3 commit` 代替 `git commit`：

```bash
se3 commit -m "描述" -f "file1.py file2.py"
```

或使用流程引擎自动提交（`se3 run` 包含 commit 步骤）。