<!-- spec-format: v1 -->
# SE3 Framework — Base Specification

## Purpose
项目基础约定。此 spec 由 `se3 init` 生成，在所有 `se3 run` 流程中自动加载。

## Requirements

### Requirement: Project Identity
- 项目名称: SE3 Framework
- 简述: SE 3.0 规范驱动开发框架 —— 纯 CLI 流程引擎，通过 spec 驱动 AI agent 的软件工程工作流
- 主要语言/框架: Python 3.8+, Typer (CLI), PyYAML, Rich, prompt-toolkit

### Requirement: Directory Structure
- `src/se3/` — 框架源码（pip 可安装包）
  - `cli.py` — CLI 入口，注册所有命令
  - `commands/` — 各 CLI 子命令实现
  - `engine/` — 流程引擎核心（state machine, steps, context builder, LLM caller）
    - `engine/sync_engine.py` — `se3 sync` 单轮无状态引擎（spec ↔ code drift 分析与回写）
    - `engine/sync_loop.py` — `se3 sync` 跨轮编排（收敛检测、振荡守卫、连续基础设施失败计数、checkpoint 写入与 `--resume` 续跑）
    - `engine/sync_analyzer.py` — sync 阶段的 spec/code 对比与 LLM 响应解析；JSON 解析失败时记录 `failed_analysis_reason` 而不再合成伪 CONFLICT diff
    - `engine/sync_discovery.py` — sync 首轮在缺失 spec 时通过 LLM 生成新 spec；新建 spec 必须通过 `spec_validator.validate_spec_structure(...)` 才落盘
    - `engine/sync_interaction.py` — sync 流程的交互式提示（高风险删除审批、配额耗尽暂停等）
    - `engine/sync_checkpoint.py` — sync 续跑用的轻量持久化模块（schema v1 dataclass，原子写入 `se3/state/sync_checkpoint.json`，按 SHA-256 重算 in-sync spec）
    - `engine/spec_validator.py` — 纯函数式 spec 结构校验器，强制 spec-format v1 契约（v1 marker、`# <name> Specification` 标题、`## Purpose`、至少一个 `### Requirement:`、首行非叙述句）；供 sync_discovery、sync_engine、`se3 sync --validate-only` 三处复用
  - `templates/` — 项目初始化模板（base spec 等）
  - `agent_runner.py` — Agent runner 抽象基类（`AgentRunner` ABC、`RunResult`、`InfraErrorType` 枚举），定义统一的 agent 执行接口，便于未来扩展多种 runner 类型（API-based、其他 CLI 等）
  - `claude_runner.py` — Claude Code CLI 单命令适配器（`ClaudeCodeRunner`），实现 `AgentRunner` 接口，负责通过子进程调用 Claude Code CLI；agent 选择/轮换由 `LLMCaller` 处理；保留 `ClaudeRunner` 别名以向后兼容
  - `config.py` — SE3 配置管理（`se3.yaml`、`se3.local.yaml` 加载，git worktree 主仓库根目录解析，Claude 子进程配置加载）
  - `utils.py` — 共享工具函数（如 `discover_specs`、specs 目录解析等，支持 `se3/specs/` 优先、`specs/` 回退、`openspec/specs/` 遗留路径）
  - `core/` — 核心工具子包
    - `utils.py` — 跨框架共享的核心工具函数（如 `truncate_preview` 等格式化辅助函数）
- `se3/` — SE3 运行时目录（gitignored，除 specs/）
  - `specs/` — 项目规范（已提交到 git）
  - `state/` — 流程引擎状态
    - `state/sync_checkpoint.json` — `se3 sync` 中断时的续跑 checkpoint（gitignored；正常收敛 / `--resume` 成功完成时被清除）
  - `cache/` — 缓存索引
  - `logs/` — 执行日志
  - `calls/` — 人工调用队列
  - `collab/` — 多智能体协作状态
- `.claude/` — 开发依赖的框架规范（只读，不可修改）
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

### Requirement: Agent Runner Abstraction
- Agent 执行通过 `AgentRunner` 抽象基类统一接口（`src/se3/agent_runner.py`），所有具体 runner 必须实现该接口
- `InfraErrorType` 枚举定义需要 agent 轮换的基础设施错误类型: `NONE`、`USAGE_LIMIT`、`TIMEOUT`、`HANG`
- `RunResult` dataclass 表示 runner 执行结果，供上层流程引擎判断成败与轮换决策
- 当前唯一具体实现是 `ClaudeCodeRunner`（`src/se3/claude_runner.py`），封装单次 Claude Code CLI 命令调用；agent 命令列表的遍历/轮换逻辑已上移至 `LLMCaller`
- Linux 平台下使用 `psutil` 进行子进程资源监控，用于检测 hang 等异常状态
- 该抽象允许未来加入新的 runner 类型（基于 API、其他 CLI 工具等）而无需改动上层调用方

### Requirement: Configuration Management
- 项目配置通过 `src/se3/config.py` 统一加载，支持两个配置文件：
  - `se3.yaml` — 主配置（已提交到 git）
  - `se3.local.yaml` — 本地覆盖配置（gitignored）
- 配置加载支持 git worktree 检测：通过比较 `--git-common-dir` 与 `--git-dir`，自动从 worktree 解析回主仓库工作树根目录，以读取主仓库的配置
- Claude 子进程配置通过 `load_claude_subprocess_config` 加载，Claude 命令列表通过 `load_claude_commands` 加载，供 `ClaudeCodeRunner` / `LLMCaller` 使用

### Requirement: Shared Utilities
- `src/se3/utils.py` 提供跨命令的共享工具函数，包括 spec 发现（`discover_specs`）等
- Specs 目录解析顺序：`se3/specs/` 优先 → `specs/` 回退 → `openspec/specs/` 遗留路径，便于从旧版迁移
- `src/se3/core/` 子包用于存放更底层的核心工具，与项目业务逻辑解耦：
  - `truncate_preview(text, max_length, ellipsis_str)` — 一致的预览截断与省略号格式化，用于控制台输出

### Requirement: Runtime Dependencies
- 项目运行时依赖在 `pyproject.toml` 的 `[project].dependencies` 中声明，包括：
  - `typer>=0.9.0` — CLI 框架
  - `pyyaml>=6.0` — YAML 配置解析（`se3.yaml`、`se3.local.yaml` 等）
  - `rich>=13.0.0` — 终端富文本渲染（日志、表格、状态显示等）
  - `prompt-toolkit>=3.0.0` — 交互式命令行输入支持（用于交互式 prompt、人工调用队列等需要终端输入的场景）
- 新增运行时依赖时必须同步更新 `pyproject.toml`，且在该 spec 中登记，便于子进程 / 子智能体了解可用库