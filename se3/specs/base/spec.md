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
    - `engine/sync_engine.py` — `se3 sync` 单轮无状态引擎（spec ↔ code drift 分析与回写）；`run_once` 接受每轮的 analyze_specs 子集与 do_discovery 标志，聚合本轮每个 spec analyze 所 touch 的文件到累计 deps,并暴露 discovery 本轮新发现子系统数与过时 spec 候选
    - `engine/sync_loop.py` — `se3 sync` 跨轮编排（收敛检测、振荡守卫、连续基础设施失败计数、checkpoint 写入与 `--resume` 续跑）；轮循环前依据 sync_state 做第 1 级全局快门与第 2 级 per-spec 闸门跳过,轮循环内做第 3 级 per-spec 提前退出(跟踪每个 spec 连续 0 drift 轮数),收敛后执行过时 spec 删除并写入 sync_state
    - `engine/sync_analyzer.py` — sync 阶段的 spec/code 对比与 LLM 响应解析；解析前剥离 markdown fence 再 `json.loads`；JSON 解析失败时记录 `failed_analysis_reason` 而不再合成伪 CONFLICT diff；消费 llm_caller 暴露的本次调用 touched-files 供 per-spec 依赖跟踪
    - `engine/sync_discovery.py` — sync 内通过 LLM 发现缺失 spec 并生成新 spec(每轮可被调用、跑到自身收敛)；新建 spec 必须通过 `spec_validator.validate_spec_structure(...)` 才落盘；并提供过时 spec 删除入口 `delete_obsolete_specs`(收敛后删除 `se3/specs/<name>/` 目录,默认直删,可选逐个人工确认)
    - `engine/sync_interaction.py` — sync 流程的交互式提示（高风险删除审批、配额耗尽暂停等）
    - `engine/sync_checkpoint.py` — sync 续跑用的轻量持久化模块（schema v1 dataclass，原子写入 `se3/state/sync_checkpoint.json`，按 SHA-256 重算 in-sync spec）
    - `engine/sync_state.py` — sync 持久化增量优化的缓存模块：`SyncState` dataclass（`state_version` / `converged_at` / `code_fingerprint` / `discovery_converged` / `spec_deps` / `obsolete_specs`），原子写入 `se3/state/sync_state.json`，JSON 损坏 / 版本不匹配 / 文件缺失一律返回无缓存；提供 `compute_code_fingerprint`（git ls-files blob sha 排序整体 hash + untracked 未忽略文件内容 hash，排除 `se3/`）、文件内容 hash、文件增/删/重命名检测;记录『上次成功收敛』快照,与 `sync_checkpoint.py`（中断恢复临时态）语义、生命周期完全不同
    - `engine/spec_validator.py` — 纯函数式 spec 结构校验器，强制 spec-format v1 契约（v1 marker、`# <name> Specification` 标题、`## Purpose`、至少一个 `### Requirement:`、首行非叙述句）；供 sync_discovery、sync_engine、`se3 sync --validate-only` 三处复用
    - `engine/state_machine.py` — `se3 run` 流程引擎的有限状态机核心，驱动 step 间转移与上下文流转
    - `engine/steps/` — `se3 run` 各步骤（analyze / plan / implement / verify 等）的具体实现包
    - `engine/context_builder.py` — 为 LLM 子进程构建上下文（注入 spec、历史摘要、当前任务等）
    - `engine/llm_caller.py` — LLM 调用统一入口，承担 agent 命令列表的遍历/轮换、配额耗尽与超时检测、调用结果包装等职责
    - `engine/chat_history.py` — 多轮对话历史管理与持久化
    - `engine/dag_scheduler.py` — 基于 DAG 的任务调度器，用于解析步骤/任务间依赖并按拓扑序执行
    - `engine/loop_controller.py` — 流程循环控制（迭代上限、退出条件、相同状态防抖等）
    - `engine/issue_discovery.py` — issue discovery 流程实现，扫描代码/spec 并产出待办 issue
    - `engine/issue_manager.py` — `se3/issues/` 下 issue 记录的读写、状态管理与去重
    - `engine/docs_updater.py` — 文档自动更新逻辑（如 README、spec 摘要等同步）
    - `engine/version_bumper.py` — 根据 commit 类型（bugfix/feature）和上次版本基线计算并提升版本号
    - `engine/version_script_interface.py` — 版本脚本接口适配层，提供项目内 version 管理脚本的发现（探测 `scripts/version.py` 等候选路径）、按需生成（基于 `templates/version_script.py.tmpl` 落盘）以及统一调用入口（`get` / `bump` / `set` 子命令的子进程封装），供 `engine/version_bumper.py` 在不直接耦合具体脚本路径的前提下完成版本读取与提升
    - `engine/worktree.py` — 引擎侧 git worktree 操作封装，配合 `se3/worktrees/` 目录使用
    - `engine/merge/` — 多 worktree / 多分支合并流程相关实现（具体子模块见 "Engine Merge Submodules" Requirement）
  - `templates/` — 项目初始化模板（base spec、README、版本管理脚本、版本历史等）
    - `base_spec.md` — 新项目 base spec 的初始模板，由 `se3 init` 写入 `se3/specs/base/spec.md`
    - `readme_md.md` — 新项目 README 模板，含 `{project_name}` / `{project_description}` / `{project_overview}` 等可格式化占位
    - `versions_md.md` — 新项目版本历史文档模板（`VERSIONS.md`），含 `{project_name}` / `{date}` 占位
    - `version_script.py.tmpl` — 项目版本管理脚本模板，提供 `get` / `bump` / `set` 三个子命令的统一接口；输出契约为成功时 stdout 输出版本字符串并 exit 0，失败时 stderr 输出错误并 exit 1
  - `agent_runner.py` — Agent runner 抽象基类（`AgentRunner` ABC、`RunResult`、`InfraErrorType` 枚举），定义统一的 agent 执行接口，便于未来扩展多种 runner 类型（API-based、其他 CLI 等）
  - `claude_runner.py` — Claude Code CLI 单命令适配器（`ClaudeCodeRunner`），实现 `AgentRunner` 接口，负责通过子进程调用 Claude Code CLI；agent 选择/轮换由 `LLMCaller` 处理；保留 `ClaudeRunner` 别名以向后兼容
  - `config.py` — SE3 配置管理（`se3.yaml`、`se3.local.yaml` 加载，git worktree 主仓库根目录解析，Claude 子进程配置加载）
  - `utils.py` — 共享工具函数（如 `discover_specs`、specs 目录解析等，支持 `se3/specs/` 优先、`specs/` 回退、`openspec/specs/` 遗留路径）
  - `core/` — 核心工具子包
    - `utils.py` — 跨框架共享的核心工具函数（如 `truncate_preview` 等格式化辅助函数）
  - `daemon/` — 常驻控制面 daemon 包（`se3 daemon` 子命令的实现），与核心同包同 wheel、版本同步演进；负责发现/监管本机 `se3 run` 流程、代为 spawn 新流程、聚合 `se3/state|logs|calls|issues` 状态，并维持一条到中心服务器的出站连接（具体子模块见 "Daemon Modules" Requirement）
  - `server/` — 中心服务器后端 + 自带网页前端包；不是核心 `se3` 的子命令，通过独立 console_scripts 入口 `se3-server` 启动；web 重依赖（FastAPI/uvicorn/websockets）经 `pyproject.toml` optional-dependencies（`se3[server]`）隔离，未安装时核心 CLI 不受影响（具体子模块见 "Server Modules" Requirement）
- `se3/` — SE3 运行时目录（gitignored，除 specs/）
  - `specs/` — 项目规范（已提交到 git）
  - `state/` — 流程引擎状态（gitignored），存放多种运行时持久化产物：
    - `state/sync_checkpoint.json` — `se3 sync` 中断时的续跑 checkpoint（正常收敛 / `--resume` 成功完成时被清除）
    - `state/sync_state.json` — `se3 sync` 的持久化增量缓存（『上次成功收敛』快照），记录全局内容指纹、discovery 是否收敛、每个 spec 的 spec_hash 与依赖文件集、过时 spec 候选集；仅在 sync 真正收敛且无未解决失败分析时写入；与 `sync_checkpoint.json` 不同——后者是中断恢复临时态、收敛即清除，前者是跨调用长期存在的构建缓存；被 `/se3/*` 的 `.gitignore` 规则忽略（必需属性：被 git 跟踪会导致跨机器携带陈旧缓存而误判 in-sync）
    - `state/engine.json` — `se3 run` 状态机的持久化状态（当前 step、上下文、迭代计数等），用于支持流程中断后通过 `--resume` 续跑
    - `state/known_test_failures.json` — 已知/允许失败的测试清单，供流程引擎在 verify 阶段区分新引入回归与历史既存失败
    - `state/merge.lock` — `se3 merge` 进程级互斥锁文件（基于 `fcntl.flock` + PID stale 检测），防止并发 merge
    - `state/summary-*.md` / `state/summary-*.json` — 各流程阶段产出的摘要快照（markdown 给人读、JSON 给下游程序消费）
    - `state/archive/` — 已完成或被替换的旧 state 文件的归档子目录，用于排查历史流程
  - `cache/` — 缓存索引
  - `logs/` — 执行日志
  - `calls/` — 人工调用队列
  - `collab/` — 多智能体协作状态
  - `history/` — 历史会话/流程归档（gitignored），保留过往 `se3 run` / `se3 sync` 等流程的归档产物
  - `issues/` — issue discovery 流程产出的 issue 记录（gitignored），由 issue-discovery 相关命令读写
  - `tmp/` — 临时文件目录（gitignored），用于流程中间产物（如 LLM 子进程的 prompt / 响应快照等）
  - `worktrees/` — git worktree 工作区目录（gitignored），由 worktree-management 相关流程创建与维护，用于隔离并行任务
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

### Requirement: CLI Command Modules
`src/se3/commands/` 下每个模块对应一个或一组 `se3` 子命令，由 `src/se3/cli.py` 注册到 Typer app。新增命令必须在此 spec 中登记，避免 LLM 子进程在缺少 CLAUDE.md 时无法发现命令边界。

- `run.py` — `se3 run`，SE3 3.0 流程引擎的统一入口；替代旧的 start/work/done 三命令，由状态机驱动；支持新建流程、`--resume` 续跑、`--loop` 循环模式、`--type=<bugfix|feature>` 任务类型标注
- `sync.py` — `se3 sync`，驱动 `SyncLoop` 进行 code → spec 单向同步直至收敛；支持 `--once`、`--max-rounds`、`--stable-rounds`、`--interactive`、`--show-diff`、`--resume`、`--validate-only` 等开关；只写 spec，不修改代码；同时提供 `process_call_response` 入口供 `se3 sync-respond` 处理高风险删除审批的人工响应文件
- `cli.py::sync_respond_cmd` — `se3 sync-respond <call-file-path>`，处理 `se3 sync` 流程在交互式高影响删除审批（high-impact deletion call）等场景下写出的人工响应 call 文件；委托给 `commands/sync.py::process_call_response` 解析响应并推动 sync 流程继续
- `init_cmd.py` — `se3 init`，初始化新 SE3 项目；落地 `se3.yaml`、base spec、README、`VERSIONS.md`、版本管理脚本等模板（由 `cli.py` 通过 `@app.command(name="init")` 装饰器注册）
- `history_cmd.py` — `se3 history`，会话/流程历史的查看与管理；子命令包括 `list`、`show <flow_id>`、`restore <flow_id>`、`archived`
- `issue_cmd.py` — `se3 issue`，issue discovery 产出的 issue 记录读写；子命令包括 `list`（默认仅 open，支持 `--all`）、`show <id>`、`create`（交互式）、`reset <id>`（将 in-progress 重置为 open）
- `merge_cmd.py` — `se3 merge <branch> [...]`，按序合并分支到当前分支；支持 `--strategy=default|strict|fast`、`--delete-merged`
- `merge_respond.py` — `se3 merge-respond <call-file-path>`，处理 merge 流程中通过 MCP call 队列写出的人工/外部响应文件
- `salvage_cmd.py` — `se3 salvage`，从异常终止的会话中尽力抢救工作；容错地读取 session state、评估 git diff、提交既有改动、为未完成工作创建 issue 并归档会话，每步独立容错
- `cli.py::guardrails_cmd` — `se3 guardrails`，对 `se3/specs/` 下的 spec 文件执行 SE3 Spec Guardrails 检查（spec-format v1 契约、结构性约束等），用于在 commit / sync / CI 等阶段拦截不合规的 spec 改动；该命令直接在 `src/se3/cli.py` 中通过 `@app.command(name="guardrails")` 注册，没有独立的 `commands/guardrails_cmd.py` 模块文件
- `cli.py::daemon_app` — `se3 daemon`，常驻控制面 daemon 的管理子命令组（`start` / `stop` / `status`），通过 `add_typer` 注册到 `cli.py` 的 sub-typer。`start` 默认以 detached 后台进程启动 daemon（`--foreground` 不脱离终端，`--server-url` 指定中心服务器地址）；`stop` 停止运行中的 daemon；`status` 报告 daemon 运行状态与已跟踪流程（`--json` 输出 JSON）。该命令组直接在 `src/se3/cli.py` 中定义，daemon 实现位于 `src/se3/daemon/` 包；对 `daemon` 包的 import 一律延迟到命令体内，使核心 CLI 启动不受影响
- `merge/` — merge 相关支撑模块子包，供 `merge_cmd.py` 与 `engine/merge/` 使用：
  - `failure_reason.py` — `FailureReason` IntEnum，替代旧 orchestrator 中散落的 ~60 个 failure_reason 字符串字面量，便于 downstream 类型化分派
  - `result_model.py` — merge orchestrator 的类型化结果模型，按 per-branch 结果（success / failure / skipped）拆分，区分 `newly_merged` / `already_merged_branches` / `with_warnings`
  - `postcondition.py` — merge 成功路径的后置断言（ancestry、merge commit、version bumped 三独立条件）
  - `merge_lock.py` — 基于 `fcntl.flock(LOCK_EX | LOCK_NB)` 的进程级 merge 锁，防止并发 `se3 merge`；非阻塞 + 基于 PID 的 stale 检测
  - `llm_trace.py` — `se3 merge` 期间每次 LLM 调用的 JSONL trace 日志，append-only + 调用后 fsync，输出至 `se3/logs/llm/merge_<timestamp>_<seq>.jsonl`
  - `secret_redact.py` — 写入 merge 日志/trace 前对常见密钥模式（`sk-...`、`ak-...`、`Bearer <token>`、`ghp_...`、`pypi-...`、`npm_...`、TOML/JSON/YAML password 字段等）做脱敏

### Requirement: Agent Runner Abstraction
- Agent 执行通过 `AgentRunner` 抽象基类统一接口（`src/se3/agent_runner.py`），所有具体 runner 必须实现该接口
- `InfraErrorType` 枚举定义需要 agent 轮换的基础设施错误类型: `NONE`、`USAGE_LIMIT`、`TIMEOUT`、`HANG`
- `RunResult` dataclass 表示 runner 执行结果，供上层流程引擎判断成败与轮换决策
- 当前唯一具体实现是 `ClaudeCodeRunner`（`src/se3/claude_runner.py`），封装单次 Claude Code CLI 命令调用；agent 命令列表的遍历/轮换逻辑已上移至 `LLMCaller`
- Linux 平台下使用 `psutil` 进行子进程资源监控，用于检测 hang 等异常状态
- 该抽象允许未来加入新的 runner 类型（基于 API、其他 CLI 工具等）而无需改动上层调用方

### Requirement: Engine Module Extensions
`src/se3/engine/` 下除 Directory Structure 中已登记的核心模块外，还包含一组辅助/共享模块，新增引擎模块必须在此 spec 中登记，避免子智能体在缺少 CLAUDE.md 时无法发现可复用的模块边界。

- **Spec 处理与索引**
  - `spec_format.py` — Spec format v1 解析器与校验器，提供 `parse_spec()`（将 spec markdown 拆分为共享 header + `Requirement` 列表的 `ParsedSpec`）与 `validate()`（v1 规则校验），并定义 `SPEC_FORMAT_VERSION_MARKER` 等共享常量
  - `spec_index.py` — 基于 `<spec>::<requirement>` 的 item 级 spec 索引，跟踪 mtime / size / sha256 前缀以支持增量失效；索引文件 `se3/cache/spec-index.json`（gitignored）
  - `spec_loader.py` — Spec 加载入口 `load_for_step()`，为下游 step 组装 spec 文本，支持 `items`（base 全文 + 各 spec header + 选中 item + 1 跳引用）与 `full_spec`（base 全文 + 各 spec 全文）两种模式
- **LLM 输出结构化**
  - `json_extractor.py` — 两阶段 JSON 抽取：当主调用输出非 JSON / 残缺 JSON 时，用第二次 LLM 调用将内容再表达为合法 JSON，避免污染主 prompt
  - `json_modes.py` — 定义三种 LLM JSON 输出策略：`STRICT`（require_json，失败重试）、`EXTRACT`（json_extract，失败回退到 LLM 抽取）、`TWO_PHASE`（two_phase_json，自然生成 + LLM 抽取）
  - `schema.py` — SE3 状态文件的 JSON Schema / TypedDict 定义（`engine.json`、`context.json` 等），提供结构文档与校验能力
- **状态、模型与持久化**
  - `models.py` — 流程引擎状态机核心数据模型：`Step`、`State`、`Transition`、`FlowInstance` 及 `StepStatus` / `FlowStatus` 等枚举；`State.fix_history` 等字段按滑动窗口控制内存与 `engine.json` 体积
  - `persistence.py` — `engine.json` 等流程状态的 JSON 序列化/反序列化与原子写入，避免中断导致状态文件损坏
  - `context.py` — `Context` 类，对 step 执行与 UI 展示提供只读上下文，封装 workflow state
  - `project_context.py` — 项目级上下文采集器（git status、flow 历史、backlog、specs 等），供 `PROJECT_SUMMARY` step 与 `se3 summary` CLI 消费
- **Prompt / 重试 / 历史**
  - `retry_context.py` — 重试上下文格式化的共享常量（`RETRY_HISTORY_MARKER` / `RETRY_HISTORY_SEPARATOR` 等），生产者 `chat_history.format_history_for_retry` 与消费者 `llm_caller._post_dedup_safety_cap` 共用同一 API；每个重试上下文块必须恰好包含一对 marker/separator
  - `prompt_dedup.py` — Prompt 行级去重工具 `deduplicate_prompt_lines`，对重复出现的连续行块（默认 `min_block_lines=3`）用 `[DUPLICATED CONTENT: ...]` 引用首次出现，降低 prompt 体积
  - `prompt_history.py` — 基于 `prompt_toolkit` `FileHistory` 的交互式 prompt 历史（默认目录 `history/`，文件名 `prompt_history`，`MAX_ENTRIES=500`），支持上下箭头浏览过往输入
  - `sync_history.py` — Sync 流程的轻量 flow context（`SyncFlowContext`），生成 `flow_id` / `step_id`，使 `LLMCaller` 能通过既有 ChatHistory 基础设施自动记录 sync 的 prompt/response
- **任务与依赖图**
  - `task_description.py` — Task description 组合工具，将用户在 Ctrl-C 中断时输入的额外指令（持久化于 `flow.state.context["user_interjections"]`）渲染为统一的 `## Additional Instructions (added during run)` 段落，保证 `run.py:_handle_step_interrupt` 与 `state_machine._build_step_inputs` 产生 byte-identical 输出
  - `transitive_reduction.py` — 任务组依赖 DAG 的传递闭包削减 `transitive_reduce(groups)`，删除可通过其他路径推导出的冗余 `depends_on` 边
  - `stash_utils.py` — `git stash pop` 冲突恢复的共享辅助函数，由 DAG implement step（合并 leaf 分支回 parent worktree）与 `se3 merge` robust strategy（stash dirty 工作树）共用
- **截断与格式化**
  - `truncation.py` — LLM 消费内容的共享截断常量，集中维护 `self_check` / `verify_spec` / `test` 等 step 的 stdout/stderr 尾部截断上限，保证一致性并满足 flow-engine spec "LLM Content Truncation Strategy" 的下限要求
  - `output.py` — Flow engine 的核心输出格式化工具；用户可见内容委托给 `display.py` 渲染完整内容（不再做截断）
  - `output_formatter.py` — 输出格式化器注册表，集中注册并按名查找 formatter
  - `display.py` — 完整内容渲染工具（基于 `rich`），用于不截断地展示 LLM 输出、spec、proposal、design doc 等
  - `tool_formatters.py` — 基于 `TOOL_FORMATTERS` 字典注册表的 per-tool 的 `tool_use` / `tool_result` 预览格式化器，被 `llm_caller.py`（流式输出）与 `chat_history.py`（历史回看）共用；未知工具回退到通用 formatter
  - `step_renderers.py` — Step 输出渲染注册表，提供单一入口 `render_step_output`，按 step 类型分派渲染器，未注册类型使用默认通用渲染器
  - `logging_config.py` — 结构化（JSON 格式）日志配置，提供 step 跟踪、时延与 LLM 指标
- **事件流与可插拔 sink**
  - `event_stream.py` — `se3 run` 的统一结构化事件流：定义 `EventType` 枚举（`flow_started` / `step_started` / `step_output` / `step_completed` / `step_failed` / `flow_paused` / `flow_completed` / `flow_failed` / `interjection_needed` / `call_needed`）、`Event` dataclass、`new_event` 工厂函数与进程内 pub/sub 发射器 `EventEmitter`（`subscribe` / `unsubscribe` / `emit` / `scope`）。`se3 run` 内部只发射这一条流，不感知调用方
  - `sink.py` — 事件流末端的可插拔 sink：`Sink` ABC（`consume(event)`）及两个具体实现——`CliSink`（委托现有 `step_renderers.py` 渲染，保持 CLI 输出逐字节兼容）与 `JsonSink`（将事件序列化为 NDJSON，供 daemon 模式消费）。「CLI 还是 daemon」退化为最外层 `--output-format` 开关下的一次 sink 选择
- **子包**
  - `formatters/` — 输出 formatter 子包；当前包含 `task_formatter.py` 等具体 formatter 实现，由 `output_formatter.py` 注册
  - `utils/` — 引擎内通用工具子包；当前包含 `json_parser.py` 等共享解析辅助

### Requirement: Engine Step Implementations
`src/se3/engine/steps/` 是 `se3 run` 状态机各 step 的具体实现包，由 `engine/state_machine.py` 在流程推进过程中按 step 类型分派调用。每个 step 模块封装一个独立的流程阶段；新增 step 必须在此 spec 中登记，避免 LLM 子进程在缺少 CLAUDE.md 时无法发现可用的 step 边界与职责划分。

- `analyze.py` — analyze step，对用户任务做需求分析与影响面评估，识别相关 spec / 代码模块，产出后续 plan / implement 的输入上下文
- `plan.py` — plan step，在 analyze 基础上输出实现计划（步骤拆解、风险点、验证策略），作为 implement 的指令源
- `plan_tasks.py` — 任务级 plan step，将整体 plan 进一步拆解为可由 DAG 调度器执行的任务组（含 `depends_on` 依赖关系），供 `engine/dag_scheduler.py` 调度
- `implement.py` — implement step，按 plan / plan_tasks 输出执行实际代码改动；与 `engine/worktree.py` 配合在隔离工作树内完成实现
- `verify_spec.py` — verify_spec step，校验实现产物是否满足相关 spec 要求；其 LLM 消费内容的截断策略遵循 `engine/truncation.py` 中集中维护的上限
- `test.py` — test step，执行项目测试套件并解析结果；stdout/stderr 截断遵循 `engine/truncation.py` 中的共享上限
- `test_with_fail_loop.py` — 带失败循环的 test step 变体，测试失败时驱动 fix 子流程并重跑，受 `engine/loop_controller.py` 的迭代上限与防抖约束
- `self_check.py` — self_check step，由 agent 对当前阶段产物做自检；截断上限同样集中在 `engine/truncation.py`
- `_fix_context.py` — 私有辅助模块，为 fix / 重试类 step 构建上下文（如失败摘要、历史 diff 等），不直接作为 step 暴露
- `commit.py` — commit step，调用 `se3 commit` 等价逻辑落盘改动；强制测试通过、拦截敏感文件，确保 commit 消息含上下文
- `confirm.py` — confirm step，在关键节点向用户/agent 索取确认（如方案选择、风险接受），与 `engine/sync_interaction.py` 等交互模块协作
- `discovery.py` — discovery step，配合 `engine/issue_discovery.py` 扫描代码 / spec 并产出 issue 草稿
- `project_summary.py` — project_summary step，调用 `engine/project_context.py` 采集项目级上下文并产出摘要（同时为 `se3 summary` CLI 提供数据源）
- `summarize.py` — summarize step，对当前流程的阶段产出生成 markdown / JSON 摘要快照，落盘到 `se3/state/summary-*.md` / `summary-*.json`
- `update_spec.py` — update_spec step，根据实现 / 测试结果对相关 spec 文件做受控更新；写入路径限定于 `se3/specs/**/spec.md`，并受 spec guardrails 检查约束
- `version_analyze.py` — version_analyze step，依据当前流程的 commit 类型（bugfix / feature）与 `engine/version_bumper.py` 的版本基线，分析并准备版本提升所需的输入
- `conftest.py` — pytest 配置文件，作用域限定在 `src/se3/engine/steps/` 子包，用于阻止 step 实现模块（特别是名为 `test.py` 与以 `test_` 前缀命名的 step 文件，如 `test_with_fail_loop.py`）被 pytest 误收集为测试用例；该文件不是 step 实现，而是该目录的 pytest 收集行为配置，作为 Engine Step Implementations 中"每个 step 模块封装一个独立流程阶段"约束的受控例外存在

### Requirement: Engine Merge Submodules
`src/se3/engine/merge/` 包是 `se3 merge` 流程的实现核心，负责把多分支合并、LLM 冲突解决、spec guardrails、runtime 数据同步与版本聚合编排为一个端到端流程。每个子模块都有明确职责，新增 merge 子模块必须在此 spec 中登记，避免 LLM 子进程在缺少 CLAUDE.md 时无法发现 merge 流程的模块边界。

- `orchestrator.py` — `MergeOrchestrator`，按序合并多分支到当前分支的总编排：对每个分支调用 `git merge`，分派 clean merge / conflict / non-conflict failure 路径，调用 guardrails，并聚合 per-branch 结果
- `conflict_context.py` — `ConflictContextBuilder`，为 LLM 冲突解决采集三路 merge context：ours/theirs 分支名、merge-base SHA、HEAD commit 信息、每个冲突文件的 base/ours/theirs/工作树（含 `<<<<<<<` markers）四个版本、hunk 行范围、base 与各 side 之间的 oneline log，以及 spec 文件识别；含基于 magic-byte 签名的二进制文件检测
- `conflict_resolver.py` — `ConflictResolver`，基于 `ConflictContext` 构造 prompt 调用 `LLMCaller`，解析结构化 JSON 响应（resolved content、per-hunk confidence、flags）；默认对 resolved 文件内容设 5 MiB 上限以防御 OOM
- `strategy.py` — `StrategyDecider`，安全/严格/快速三策略决策矩阵；基于 LLM resolution 的 confidence、spec guardrail flags 与文件类型选择 `DecisionAction`
- `guardrails.py` — `MergeGuardrailsCheck`，merge 后对 `se3/specs/**/spec.md` 中改动的 spec 文件运行 guardrails，检测被删除的 requirement、被弱化的语言与被弱化的量词；同时导出可复用的纯函数 `check_spec_diff()` 给 `se3 guardrails` CLI 共用
- `guardrail_repair.py` — `GuardrailRepairer`，fast 策略下 guardrails 触发违规时由 LLM 驱动的 spec 修复：构造修复 prompt、调用 LLM、解析修正内容、写回工作树、提交修复（优先 fix-up commit on top of merge commit，回退到 amend），并复跑 guardrails 验证修复结果；所有 write-back 路径限定在 `se3/specs/**/spec.md`，且 amend 前必须保存 `pre_amend_sha` 以支持精确回滚
- `human_call.py` — `HumanCallWriter`，当 LLM 冲突解决触发 HUMAN_CALL 决策时，向 `se3/calls/` 写结构化 call 文件，包含冲突 context、LLM 解决方案与决策选项，供人工审查（响应文件由 `se3 merge-respond` 处理）
- `cleanup.py` — `CleanupManager`，实现 `--delete-merged` 标志：用 `git branch -d`（小写）安全删除已完全合并的分支及其绑定的 worktree；跳过受保护分支（main / master / 当前分支）；拒绝清理 dirty worktree；git 子命令以 `LC_ALL=C` 运行以匹配英文 stderr；删除前用 `git merge-base --is-ancestor` 显式校验
- `runtime_sync.py` — merge 成功后的 runtime 数据同步：`se3/` 下 git-ignored 的运行时数据不会被 git merge 自动合并，本模块将源分支绑定 worktree 的 tier A runtime 内容拷贝到当前分支 `se3/` 目录，并修复 leaf-symlink swap 的 TOCTOU 窗口
- `version_aggregator.py` — 所有分支顺序合并完成后，相对各分支与 pre-merge HEAD 的 merge-base 推断 SemVer bump 类型，取最大 bump 应用到 `pyproject.toml`，并 amend 最后一个 merge commit；含原子 write-temp + `os.replace` 写入、`Version.parse` 失败时 fail-loud、`version_already_at_target` 的显式区分（`VersionNotAdvanced` 异常类）、宽松 TOML version 正则（`version="1.2.3"` 与 `version = "1.2.3"` 均匹配）以及失败路径下 pyproject.toml 内容与暂存区的同步还原

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

### Requirement: utils.py Helper Functions
`src/se3/utils.py` 除 spec 发现入口（`discover_specs` / `_resolve_specs_dir`）外，还集中维护一组供 CLI 命令、流程引擎与文档/状态工具复用的辅助函数。新增此处函数必须在该 spec 中登记，便于子智能体在缺少 CLAUDE.md 时发现可复用工具。

- **Spec 解析与 change 发现**
  - `parse_spec(filepath)` — 解析单个 spec.md 文件，返回包含 `title`（`#` header）、`purpose`（`## Purpose` 内容）、`requirements`（`### Requirement:` 列表，含 `title` / `level` / `content` / `scenarios` / `line`）、`scenarios`（含 `title` / `when` / `then` / `line`）等键的字典
  - `discover_changes(path="se3/specs/_changelog")` — 在 changelog 目录下发现 change 名称列表，供 change 管理流程消费
  - `discover_specs_in_change(change_name, base_path="openspec/changes")` — 列出某个 change 目录内涉及的 spec 列表
- **执行结果与产物**
  - `get_exit_code(results)` — 基于一组结果记录聚合得到统一的 exit code，供 CLI 命令收尾使用
  - `discover_outputs(path)` — 枚举给定路径下的产物文件列表
  - `get_file_mtime(path)` — 安全读取文件 mtime，文件不存在时返回 `None`
  - `copy_file(src, dst)` — 在目标父目录不存在时自动创建后再复制文件
- **项目结构与文档一致性**
  - `get_source_mappings(project_root)` — 返回项目内源代码与 spec / 文档之间的映射关系
  - `find_verification_markers(scenario_id, search_paths)` — 在指定路径集合中查找特定 scenario 对应的验证标记
  - `get_framework_version(project_root)` — 读取并返回项目所引用的 SE3 框架版本字符串
  - `check_documentation_consistency(...)` — 对 README / spec / status 等文档做一致性检查
  - `has_framework_file_changes(project_root)` — 返回 `(bool, List[str])`，指示当前 working tree 中是否存在框架文件改动以及具体路径列表
  - `parse_status_md(filepath="./status.md")` — 解析项目 `status.md` 状态文件并返回结构化字典

### Requirement: Project Init Templates
- `src/se3/templates/` 存放新项目初始化所需的所有模板文件，由 `se3 init` 类命令在搭建新项目时使用
- 模板分为两类：
  - 直接拷贝的 markdown 模板（`base_spec.md`、`readme_md.md`、`versions_md.md`）— 通常含 `{project_name}`、`{project_description}`、`{project_overview}`、`{date}` 等占位字段，使用前需做字符串格式化替换
  - 可执行脚本模板（`version_script.py.tmpl`）— 落盘后作为目标项目内独立可运行的 Python 脚本
- `version_script.py.tmpl` 定义了 SE3 项目版本管理脚本的统一接口契约：
  - 必须支持三个子命令：`get`（读取当前版本）、`bump --type {major|minor|patch}`（按类型提升版本）、`set --version X.Y.Z`（直接设置版本）
  - 输出契约：成功时将版本字符串打印到 stdout 并以 exit code 0 退出；失败时将错误信息打印到 stderr 并以 exit code 1 退出
  - 此契约供 `engine/version_bumper.py` 等流程模块依赖，因此模板的输出格式与退出码不得在不更新调用方的情况下修改
- 新增 `templates/` 下文件时，必须在该 spec 与 `src/se3/templates/__init__.py` 中同步登记，便于上层命令发现并使用
- `src/se3/templates/__init__.py` 的当前实现仅包含一行包级 docstring（`"""SE3 templates for project initialization."""`），作为 Python package marker 存在；尚未在其中以 `__all__` / 常量 / 字典等形式枚举具体模板文件清单。新增模板时，登记动作至少必须发生在该 spec 文件中；在 `__init__.py` 中的同步登记机制可在后续随实际需要落地，但不得反过来削弱"必须登记"的约束

### Requirement: Engine Co-located Test Modules
作为 Coding Conventions 中"测试文件放在 `tests/` 目录"通用约定的受控例外，`src/se3/engine/` 下允许存在与引擎源码就近放置的 pytest 测试模块，用于覆盖引擎内部紧耦合的行为（私有辅助函数、内部状态机分支、step 内部细节等）。新增此类 co-located 测试模块必须在此 spec 中登记，避免 LLM 子进程在缺少 CLAUDE.md 时将其误判为应迁移到 `tests/` 的违规文件。

- `src/se3/engine/test_e2e.py` — 流程引擎端到端测试，覆盖 `se3 run` 状态机的跨 step 集成路径
- `src/se3/engine/test_engine.py` — 引擎核心（state machine / context / persistence 等）的单元/集成测试
- `src/se3/engine/test_steps.py` — `engine/steps/` 下各 step 实现的测试集合
- `src/se3/engine/test_version_bumper.py` — `engine/version_bumper.py` 的版本提升逻辑测试

### Requirement: Runtime Dependencies
- 项目运行时依赖在 `pyproject.toml` 的 `[project].dependencies` 中声明，包括：
  - `typer>=0.9.0` — CLI 框架
  - `pyyaml>=6.0` — YAML 配置解析（`se3.yaml`、`se3.local.yaml` 等）
  - `rich>=13.0.0` — 终端富文本渲染（日志、表格、状态显示等）
  - `prompt-toolkit>=3.0.0` — 交互式命令行输入支持（用于交互式 prompt、人工调用队列等需要终端输入的场景）
  - `psutil>=5.9.0` — 跨平台进程探测（`claude_runner.py` 的 hang 检测、`daemon/supervisor.py` 的本机 `se3 run` 进程发现），是核心运行时依赖
- 中心服务器的 web 重依赖经 `pyproject.toml` 的 `[project.optional-dependencies]` 隔离，仅在安装 `se3[server]` extra 时引入，不污染核心 CLI 安装：
  - `fastapi` — 中心服务器后端的 web 框架
  - `uvicorn` — ASGI 服务器，由 `se3-server` 入口启动
  - `websockets` — daemon↔服务器与前端的 WebSocket 协议支持
- 仅安装 `se3`（不含 `[server]`）时核心 `se3` 命令族不得因 server 代码而出现 import 错误；对 `se3.server` 包及其重依赖的引用必须延迟到 `se3-server` 入口实际运行时
- 新增运行时依赖时必须同步更新 `pyproject.toml`，且在该 spec 中登记，便于子进程 / 子智能体了解可用库

### Requirement: Daemon Modules

`src/se3/daemon/` 是常驻控制面 daemon 的实现包，由 `se3 daemon` 子命令（在 `cli.py` 中注册）驱动。daemon 比 `se3 run` 活得久，是 CLI 退出后唯一能持续聚合状态、对外提供稳定端点的部件。新增 daemon 子模块必须在此 spec 中登记，避免 LLM 子进程在缺少 CLAUDE.md 时无法发现模块边界。

- `daemon.py` — daemon 进程入口与生命周期：`DaemonConfig`（配置 dataclass）、`Daemon`（asyncio 事件循环）、`start_daemon` / `stop_daemon` / `daemon_status` 函数，以及 pidfile / 状态文件管理；`DaemonAlreadyRunning` 异常
- `supervisor.py` — `DaemonSupervisor`，发现并监管本机的 `se3 run` 进程（通过 `psutil` 扫描与 `engine.json` 读取），追踪进程生命周期与清理
- `spawner.py` — `DaemonSpawner`，以 `subprocess` 代为 spawn 新的 `se3 run --output-format json` 子进程（支持从远端发布新任务），管理其参数与环境；`SpawnedProcess` 记录
- `aggregator.py` — `DaemonAggregator`，轮询 `se3/state/`、`se3/logs/`、`se3/calls/`、`se3/issues/` 并聚合为统一状态快照（`MachineStatus`）
- `client.py` — `DaemonClient`，维持一条到中心服务器的出站 WebSocket 连接（daemon 主动拨入，对 NAT 友好），上报聚合状态、接收下发指令并路由到 supervisor / spawner；断线后按指数退避重连
- `protocol.py` — daemon↔服务器 WebSocket 协议的单一来源：`PROTOCOL_VERSION`、消息类型常量（`MSG_HELLO` / `MSG_WELCOME` / `MSG_STATUS_UPDATE` / `MSG_SPAWN_FLOW` / `MSG_RESPOND_CALL` / `MSG_CALL_NOTIFICATION` / `MSG_PING` / `MSG_PONG`）与 `make_*` 消息构造器；同时被 daemon 与 `se3.server` 包 import，确保协议 schema 不漂移

### Requirement: Server Modules

`src/se3/server/` 是中心服务器后端 + 自带网页前端的独立包，经 `pyproject.toml` 的 optional-dependencies（`se3[server]`）隔离 web 重依赖，通过独立 console_scripts 入口 `se3-server` 启动——不做成核心 `se3` 的子命令。新增 server 子模块必须在此 spec 中登记。

- `app.py` — FastAPI 应用入口：`create_app` 装配路由，`run` / `main` 通过 `uvicorn` 启动并解析 `--host` / `--port`；提供 REST API（机器 / 流程查询、远程发布新任务）并将 `static/` 挂载到 `/`
- `ws.py` — WebSocket 端点：管理 daemon 连接池（连接 / 断开 / 心跳）与前端 `UiHub` 广播通道，路由协议消息（daemon→server 状态上报、server→daemon 指令下发）
- `state.py` — `ServerState`，内存中的多机 / 多 flow 聚合状态存储（本次交付不含数据库持久化）
- `static/` — 纯静态网页前端（`index.html` / `style.css` / `app.js`），无构建步骤；通过 `/ws/ui` WebSocket 接收实时状态，提供查看进度、远程发布任务与响应 interjection/call 的界面