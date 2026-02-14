## Why

当前设计中 `intentions.md` 要求人类预先写好意图文件等AI来读，这与 Human-as-MCP 的异步按需调用原则矛盾。人类的所有输入（包括最初的项目意图）都应该通过统一的 human call 机制进入系统，而不是通过预置文件。

启动协议也存在问题：固定读取7个文件的顺序不考虑项目阶段，浪费context。成熟项目只需要 progress.md + git log 就能定位状态。

## What Changes

- **移除 `intentions.md`**：项目意图通过 human call 获取，响应直接转化为 `demands.md` 的内容
- **重设计启动协议**：从"固定文件清单"改为"渐进式状态发现"（progressive context loading）
- **统一人类输入通道**：所有人类输入（意图、决策、信息）通过 human call 机制，不再有特殊文件
- **Human call 双模式**：同步模式（人在场，直接问）+ 异步模式（人不在，写文件）

## Capabilities

### New Capabilities

### Modified Capabilities
- `session-protocol`: 重设计启动流程为渐进式加载，移除 intentions.md 依赖
- `human-as-mcp`: 增加同步/异步双模式，定义意图获取作为首次 human call
- `se3-scaffold`: 项目结构中移除 intentions.md，更新文件列表

## Impact

- 移除 intentions.md 的概念和相关引用
- 重写 session startup protocol
- 重写 human-as-mcp 规范增加双模式
- 更新 output/CLAUDE.md 模板
- 更新 demands.md、README.md、best-practices.md
