<!-- spec-format: v1 -->
# SE3 Framework — Base Specification

## Purpose
项目基础约定。此 spec 由 `se3 init` 生成，在所有 `se3 run` 流程中自动加载。

## Requirements

### Requirement: Project Identity
- 项目名称: SE3 Framework
- 简述: SE 3.0 规范驱动开发框架 —— 纯 CLI 流程引擎，通过 spec 驱动 AI agent 的软件工程工作流
- 主要语言/框架: Python 3.8+, Typer (CLI), PyYAML, Rich

### Requirement: Directory Structure
- `src/se3/` — 框架源码（pip 可安装包）
  - `cli.py` — CLI 入口，注册所有命令
  - `commands/` — 各 CLI 子命令实现
  - `engine/` — 流程引擎核心（state machine, steps, context builder, LLM caller）
  - `templates/` — 项目初始化模板（base spec 等）
- `se3/` — SE3 运行时目录（gitignored，除 specs/）
  - `specs/` — 项目规范（已提交到 git）
  - `state/` — 流程引擎状态
  - `cache/` — 缓存索引
  - `logs/` — 执行日志
  - `calls/` — 人工调用队列
  - `collab/` — 多智能体协作状态
- `.claude/` — 开发依赖的框架规范（只读，不可修改）
- `tools/` — 工具实现
- `tests/` — pytest 测试
- `scripts/` — 辅助脚本

### Requirement: Coding Conventions
- Python 风格遵循标准 PEP 8
- CLI 命令使用 Typer 注册，复杂命令用 sub-typer（`add_typer`），带位置参数的简单命令用 `@app.command`
- 日志使用 `logging` 模块，每个模块 `logger = logging.getLogger(__name__)`
- 类型注解使用 `from __future__ import annotations` 风格
- 测试文件放在 `tests/` 目录，命名 `test_*.py`，使用 pytest

### Requirement: Key Constraints
- **自举约束**: 本项目是自举项目 —— 同时生成新规范和使用已发布规范开发。生成新规范时，不得更改 `.claude/` 中的已发布规范
- **spec 只读**: `se3/specs/` 中的 spec 在实现过程中不应被随意修改，修改需通过 spec guardrails 检查
- **LLM 子进程**: 流程引擎的某些步骤（如 analyze, plan）通过 LLM 子进程执行，这些子进程无法访问 CLAUDE.md，只能通过 spec 获取项目约定

### Requirement: Workflow Conventions
- 使用 `se3 commit` 代替 `git commit`（强制测试通过、拦截敏感文件）
- commit 消息需包含上下文，便于下次会话恢复
- 运行测试后才可标记功能完成
- 主入口命令: `se3 run "task description"`
