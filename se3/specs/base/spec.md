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
  - `prompt_markers.py` — 集中定义 step prompt 中『系统指令样板前缀』『用户字面输入区段』与『框架后续注入后缀』之间的三段式 sentinel marker（`TEMPLATE_PREFIX_END` / `USER_CONTENT_BEGIN` / `USER_CONTENT_END`，均为 HTML 注释字面量以避免 LLM 误回显）及三个拼装 helper：`inject_boundary(template, before)` 在已有模板字符串内、`before` 锚点之前一次性插入两段式 marker 对（`TEMPLATE_PREFIX_END` + `USER_CONTENT_BEGIN`），幂等；`wrap_user_content(prefix, content)` 直接把两段字符串拼接并插入两段式 marker；`wrap_user_section(prefix, user_content, suffix)` 在 prefix 与 suffix 之间用全部三段 marker 包裹 user_content,产生『prefix + TEMPLATE_PREFIX_END + USER_CONTENT_BEGIN + user_content + USER_CONTENT_END + suffix』,幂等。三段式语义的核心约束是：被 `USER_CONTENT_BEGIN` / `USER_CONTENT_END` 包围的 user-content 区段只允许装『来自用户的字面输入』（如 discovery 的 `initial_description` / `user_response`、interjection.text 等），其它任何 se3 框架自己写入的字符串（Project Context / Available Specs / Discovery Context 包裹层 / JSON 格式约束 / Guidelines / 语言指令 / Runtime Environment 注入 / READ-ONLY CONSTRAINT 等）一律属于 prefix 或 suffix。所有 step prompt 模块（analyze / plan / plan_tasks / implement 的 `IMPLEMENT_PROMPT` / `IMPLEMENT_GROUP_PROMPT` / `FIX_PROMPT` / discovery 初始与续问 / self_check / verify_spec / update_spec / summarize / version_analyze）必须在模板拼装处调用其中之一：有真正用户字面输入的 step（目前实际为 discovery）使用 `wrap_user_section`,无用户字面输入的 step 沿用 `inject_boundary` / `wrap_user_content` 的两段式 marker 即可。`wrap_user_content` / `inject_boundary` 作为兼容入口继续保留,产生的两段式 marker 在前端识别时退化为『prefix + 空 user-content + 整体 suffix』。该协议供 web running-flow console 把 user 消息切分为『默认收起的系统前缀 chip + 默认展开的用户内容 bubble + 收起的框架后缀』（详见 running-flow-console spec 的 *Role-Based Message Collapse* requirement）；marker 不完整或缺失的旧 user 消息回退为整体 chip
  - `prompt_history.py` — 基于 `prompt_toolkit` `FileHistory` 的交互式 prompt 历史（默认目录 `history/`，文件名 `prompt_history`，`MAX_ENTRIES=500`），支持上下箭头浏览过往输入
  - `sync_history.py` — Sync 流程的轻量 flow context（`SyncFlowContext`），生成 `flow_id` / `step_id`，使 `LLMCaller` 能通过既有 ChatHistory 基础设施自动记录 sync 的 prompt/response
- **人机交互 call 文件**
  - `interaction_calls.py` — 运行中 flow 一切需人介入环节的统一 call 文件通道（`se3/calls/`）。每个待处理交互——待响应 MCP call、Ctrl-C 中途插话、重试/失败决策、CLI 子进程确认提示、非交互 discovery 确认门控（`discovery_confirm`，携带 `value` 为 `"1"` 的一键确认 `options`）——都收敛为同一种 `kind`-tagged JSON 文件（`kind` 取 `protocol.py` 的 `CALL_KIND_*` 常量），并携带 `prompt` / `context` / `options` 等展示元数据，供 daemon aggregator 与网页控制台无需猜测文本即可渲染与路由。提供 `write_call` / `read_call`（缺 `kind` 字段的旧 call 文件默认归类为 `CALL_KIND_CALL`，保持后向兼容）/ `read_response` / `write_response`、`write_retry_decision_call`（无 TTY 失败决策 call）、`write_interjection_request`（daemon 侧插话写入）/ `drain_interjection_requests`（`se3 run` step 边界消费）、以及 `make_cli_confirm_handler`（把 CLI 子进程确认提示桥接为 `cli_confirm`-kind call 并回写答案到子进程 stdin）
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
  - `sink.py` — 事件流末端的可插拔 sink：`Sink` ABC（`consume(event)`）及三个具体实现——`CliSink`（委托现有 `step_renderers.py` 渲染，保持 CLI 输出逐字节兼容；并通过 `_CLI_SKIP_STEP_TYPES`={`confirm`,`discovery`,`plan`} 跳过这些交互/特殊步骤的终态事件渲染，因其 CLI 输出由 orchestrator 的交互/专用路径呈现，重复渲染会重复输出）、`JsonSink`（将事件序列化为 NDJSON，供 daemon 模式消费）、`HistorySink`（把 `step_completed` 事件追加到 per-step jsonl 历史文件，供 daemon `history.py` 增量上报、网页 console 据此为每个 step 渲染默认展开的结构化 report 卡片；CLI 历史查看器 `chat_history.get_step_history` 主动跳过这类记录以避免重复渲染）。`HistorySink` 由 `src/se3/commands/run.py` 在 `_run_flow_impl` 中无条件 subscribe，不受 `--output-format` 影响。orchestrator 现对**每种** step 类型（含交互 `confirm`/`discovery`、`plan`、`summarize`）在终态结果（`COMPLETED`/`PARTIAL`/`FAILED`）时发射终态事件，使 `HistorySink`/`JsonSink` 能持久化并转发其结构化输出；`CliSink` 的上述跳过保证 CLI 输出不被重复渲染。「CLI 还是 daemon」退化为最外层 `--output-format` 开关下的一次 `CliSink`/`JsonSink` 选择
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

- `daemon.py` — daemon 进程入口与生命周期：`DaemonConfig`（配置 dataclass，含 `pid_dir`，默认 `~/.se3`、可经 `SE3_DAEMON_DIR` 覆盖，并排存放 `daemon.pid` / `daemon_status.json` / `daemon.log`）、`Daemon`（asyncio 事件循环）、`start_daemon` / `stop_daemon` / `daemon_status` 函数，以及 pidfile / 状态文件管理；`DaemonAlreadyRunning` 异常。机器本地的**持久项目根注册表**亦落在 `pid_dir` 下（`PROJECT_ROOTS_FILENAME` 常量 → `DaemonConfig.project_roots_file`，如 `~/.se3/project_roots.json`），由 `_read_project_roots(path)`（缺失 / 损坏返回 `[]`）与 `_append_project_root(path, root)`（`realpath` 去重后用既有 `_atomic_write_json` 原子写回）两个 helper 维护；挂在 `pid_dir` 下自动继承 `SE3_DAEMON_DIR` / `DaemonConfig(pid_dir=...)` 覆盖，从而天然获得测试隔离（测试以 `DaemonConfig(pid_dir=tmp_path)` 隔离落盘，不污染真实 `~/.se3`）。`Daemon.__init__` 把基于 `config.project_roots_file` 的读 / 写回调（`registry_load` / `registry_persist`）注入 `DaemonAggregator`（以回调而非直接 import 避免 aggregator↔daemon 顶层循环 import），并把历史 reader 的 `project_roots_provider` 由 `aggregator.project_roots`（裸活跃集）改为 `aggregator.all_project_roots`，使 `build_index` 在零活进程、甚至 daemon 重启后仍能取到注册表根而正确列出磁盘历史
- `supervisor.py` — `DaemonSupervisor`，发现并监管本机的 `se3 run` 进程（通过 `psutil` 扫描与 `engine.json` 读取），追踪进程生命周期与清理
- `spawner.py` — `DaemonSpawner`，以 `subprocess` 代为 spawn 新的 `se3 run --output-format json` 子进程（支持从远端发布新任务），管理其参数与环境；`SpawnedProcess` 记录。同时暴露 `ensure_se3_project(project_root)` 钩子：检测 `<project_root>/se3/specs/base/spec.md` 是否存在；若不存在则以子进程方式执行 `se3 init -p <project_root>` 完成初始化，并在 init 返回后再次校验 marker 文件存在；任何阶段失败都通过结构化 `EnsureResult` / 等价错误回报给调用方（`client._handle_spawn` 在 ensure 失败时直接走 SPAWN_FAILED 通道、不进入 spawn）；目录本身已是 se3 项目时直接放行，跳过 init。client.py 在每次处理 `MSG_SPAWN_FLOW` 时必须先调用该钩子，再委托原 spawn 流程。spawn `se3` 子进程时的 argv 前缀由模块级 helper `_resolve_se3_command()` 统一解析，所有 spawn 路径（`spawn` / `spawn_init` / `spawn_run`）共用该解析结果，**严禁**绕过它直接调用 `shutil.which('se3')` 或写死 `"se3"` 字面量——daemon 自身所在 Python 环境的 bin 目录未必在 PATH 最前（典型场景：daemon 跑在 `~/.se3-stable`、PATH 上 pixi/pip 环境的 bin 目录靠前），若按 PATH 解析会命中另一个环境里旧版本的 se3 console script，导致子进程写入 `_meta.json` 的 `se3_version` 与 daemon 不一致、discovery 三段 user-content markers 缺失、`StreamJSONTracker` 不再向 jsonl 写 `stream_progress` 等跨版本回归。`_resolve_se3_command()` 采用 **same-prefix first** 三级回退：(1) 首选 `Path(sys.executable).parent / 'se3'`（Windows 上同时探测 `se3.exe`），存在即返回 `[<console_script>]`，保证子进程与 daemon 同 Python 环境同 wheel 同版本；(2) 同前缀不存在时回退到 `[sys.executable, '-m', 'se3']`，复用同一解释器加载 `se3` 包、避开 PATH 干扰；(3) 末选 `shutil.which('se3')`（仅在 `sys.executable` 缺失等极端打包场景下生效）。返回类型保持 `List[str]`（既可能是单元素 `[path]`，也可能是 `[python, '-m', 'se3']`），三个调用点拼接 `argv` 时无需区分
- `aggregator.py` — `DaemonAggregator`，轮询 `se3/state/`、`se3/logs/`、`se3/calls/`、`se3/issues/` 并聚合为统一状态快照（`MachineStatus`）；`MachineStatus` 同时携带 `project_roots: List[str]`，由 aggregator 在每轮 snapshot 时经 `all_project_roots()` 合并四个来源后去重（按 `realpath` 归一）并稳定排序：(a) supervisor / spawner / client 通过 `_project_roots` 集合活跃注册的 root（仅含当前进程生命周期内发现 / 注册的活跃 root，亦为 per-flow 快照轮询的来源），(b) 机器本地的**持久项目根注册表**（`DaemonConfig.pid_dir/project_roots.json`，见 `daemon.py`）——凡『跑过 se3 的项目根』在 spawn / ensure / resume / 轮询发现等任一注册路径上经 `add_project_root` 写穿落盘，daemon 重启后回读，与活进程无关（写入式自动注册：『跑过即注册』，无需任何预配置），(c) `src/se3/daemon/history.py::enumerate_historical_project_roots()` 被喂入『活跃根 ∪ 注册表根』并集后从 `se3/history/<flow_id>/_meta.json` 与 `se3/state/archive/engine_*.json` 中增补的所有历史 `project_root`，(d) 任何已通过 `ensure_se3_project` 在 spawn 前补 init 的新 root（client 注册回 `_project_roots` 并同样经 `add_project_root` 写穿注册表）。`add_project_root` 在加入内存活跃集的同时 best-effort 写穿持久注册表（去重；I/O 经注入的 `registry_load` / `registry_persist` 回调完成），把持久化集中在单一写穿点而非散落在四个注册调用点，确保不漏注册；`all_project_roots()` 是该合并视图的唯一来源，既经 `_merge_project_roots` 委托产出 `MachineStatus.project_roots`，又作为历史索引 `build_index` 的 `project_roots_provider`（见 `daemon.py` / `history.py`），因此即便零活进程、甚至 daemon 重启后，历史列表与 New Task 下拉都不再为空；per-flow 快照轮询（`get_snapshot`）仍只用 `_project_roots` 活跃集，避免每 tick 轮询全部历史根。该字段经 `STATUS_UPDATE` 上报，供网页 New Task 表单的 Project 下拉联动，覆盖 daemon 重启后『活跃集合为空但历史项目仍可用』场景；枚举 `se3/calls/` 待应答 call 时跳过已存在同名响应文件（`.response` / `.response.json`）的项，避免历史已应答 call 被误报为 pending；除该 sibling-response 判定外，`_enumerate_calls` / `_snapshot_for_root` 还必须依据 flow 当前进度判定 pending call 是否仍有效：当某 call 所属 `context.step_id` 不再是 flow 的 current step（或该 step 已 `COMPLETED` / `REVISION` 处理完）时，即使没有写出 `.response` 兄弟文件，也要把该 call 从该 flow 的 `pending_calls` 中剔除——因为 CLI 终端应答的交互式 call（`call` / `cli_confirm` / `discovery_confirm`）通常被 run loop 直接消费而不写 sibling 文件，否则陈旧『待回复』chip 会整轮运行期不清除；仍停留在当前未应答 step 的 call 保持 pending。此外，对同一 `(flow_id, step_id)` 仅保留最新的未应答 call（`_dedup_calls_by_step`）：discovery 的多轮澄清复用同一 `step_id`，每轮写一份新 call 文件，旧 call 被新一轮取代后即成陈旧残留，须从该 flow 的 `pending_calls` 中剔除以防『待回复』chip 累积；无法定位 key（缺 `flow_id` 或 `step_id`）的 call 不参与去重、原样保留。解析每个 call 文件的 `kind` 字段（`call` / `interjection` / `retry_decision` / `cli_confirm`，缺失时按 `call` 处理）并连同 `prompt` / `context` / `options` 展示元数据富化为 `PendingCall`，经 `STATUS_UPDATE` 上报，使网页控制台可按 `kind` 区分渲染各类待处理交互
- `client.py` — `DaemonClient`，维持一条到中心服务器的出站 WebSocket 连接（daemon 主动拨入，对 NAT 友好），上报聚合状态、接收下发指令并路由到 supervisor / spawner；断线后按指数退避重连；连接后上报历史 session 索引（`MSG_HISTORY_INDEX`），以增量 append 模式推送活跃 session 的历史数据（`MSG_HISTORY_DATA`），并处理入站的按需历史拉取请求（`MSG_HISTORY_REQUEST`）回以 `MSG_HISTORY_DATA`，以及入站的索引重推请求（`MSG_HISTORY_INDEX_REQUEST`）——收到即调用 `_push_history(ws, force_index=True)` 强制重建并立即重推最新历史 index（`force_index=True` 绕过基于 `engine.json` 变更的去抖，使 server 端在 `GET /api/history` 上挂起的等待者能及时收到重推、从而取到最新历史而非陈旧的上次推送快照）；为消除『CLI 已推进但 web 要等下个 status tick 才看到』的运行期冻结，历史增量推送不再仅由固定 `status_interval` tick 驱动，而是由 `engine.json` 变更驱动（检测到 `engine.json` mtime/内容变化即触发一次基于 mtime 去抖的推送），并保留定时 tick 作为兜底；`_history_cursors` 必须跨 tick / 跨 step 正确累进，使运行进行中（含 PAUSED/resume）的 flow 步骤推进能在一个推送周期内增量反映到 web；处理入站的 `MSG_INTERJECT_FLOW`，把网页控制台输入的中途插话写为目标 flow 的 `interjection`-kind call 文件，交由该 `se3 run` 进程在下个 step 边界消费。**事件循环不可被同步磁盘 I/O 阻塞**：`_handle_history_request` 中的 `HistoryReader.read_flow`、`_push_history` 中的 `build_index`（含 `force_index=True` 时的强制重建 / 目录遍历）与 `read_active_flows`（fan-out 到多个 per-step jsonl 读取与 `json.loads`）等会做大量 `Path.read_text` / 目录遍历 / JSON 解析的同步调用，必须通过 `asyncio.to_thread(...)` 在工作线程中执行；同步实现保留在 `daemon/history.py`，async 只在 daemon/client.py 的 async 入口处包一层 `to_thread`。这是为了在选中含大 jsonl 的 session 时，daemon 仍能在不超过 server 端 `HISTORY_PULL_TIMEOUT` 与 client 心跳超时（`HEARTBEAT_TIMEOUT`）的窗口内继续响应 PING/PONG、PUSH 与新指令，避免出现『拉历史时 504 同时 daemon 被判离线、UI 上 machine 变灰、并触发重连退避』的连锁回归；只做 `stat` 的轻量探测路径（如 `active_flow_signature` 之类签名扫描）可保持同步，无需 `to_thread`。处理 `MSG_SPAWN_FLOW` 时 client 先调用 daemon 内部的 ensure 路径（`_handle_ensure_request` → `spawner.ensure_se3_project`）：若目标 `project_root` 不是已有 se3 项目，先在 daemon 主机上以子进程方式补 `se3 init`，成功后把该 root 注册进 aggregator 的 `_project_roots` 再继续标准 spawn 流程；ensure 失败时通过现有 spawn-failure 上报通道（`SPAWN_FAILED` / 等价错误回执）告知 server，不进入 spawn 阶段
- `history.py` — 历史 session 索引构建与按游标增量读取：枚举 `se3/history/` 与 `se3/state/archive/` 构建历史 session 索引（任务描述、状态、时间等），区分活跃 / 非活跃 session；按 per-flow per-step 的 jsonl 文件游标做增量读取，使活跃 session 的对话记录可增量上报而无需每轮重传整份 jsonl。读取时还从每个 per-step jsonl 文件名（约定 `NN_<step_type>_<hash>(_Gk)`）解析出权威 step_type 并在信封层注入，使每条上报记录形如 `{step_id, step_type, message}`：原始 daemon 推送的 `message` 本身不含 step_type，注入后前端无需再从文件名猜测，而 `message` 内容保持原样不动以维持向后兼容与 raw 可见性。解析由纯函数 `parse_step_type_from_step_id(stem)` 完成——剥离前导 `NN_` 序号、尾部 `_G\d+` 分组后缀、尾部十六进制 hash 段后，把剩余中段（可自带下划线，如 `version_analyze`）作为 step_type，对照内置 `_KNOWN_STEP_TYPES` 软校验；遵循命名约定但类型未登记时仍返回解析出的中段（自描述、容忍漂移），而无序号无 hash 的旧命名（如 `commit_summary`）回退为原 stem，永不抛异常。`read_active_flows` 的『活跃』判定必须覆盖**运行进行中**的 flow（含 `PAUSED` 等待用户输入与 `--resume` 续跑态），不能因 flow 不是 RUNNING 终值就提前剔除，否则运行期 web 会冻结、等归档后才一次性补齐；增量游标必须在每轮读取中正确推进——新出现的 per-step jsonl 文件、同一文件的追加行都要被纳入，且做到**不丢、不重、不截断后续读取**（一条记录读过就不再重读，未读到的尾部下一轮继续）。同时导出 helper `enumerate_historical_project_roots() -> list[str]`：遍历 `se3/history/<flow_id>/_meta.json` 与 `se3/state/archive/engine_*.json`，提取每条记录的 `project_root` 字段并去重排序，供 `aggregator.py` 在 rebuild snapshot 时合并到 `MachineStatus.project_roots`；当被喂入『活跃根 ∪ 持久注册表根』并集（而非仅活跃集）时，对并集中的每个根增补其磁盘历史根，并对非目录 / 无产物的根健壮跳过、不抛异常。历史索引构建（`build_index`）的 `project_roots_provider` 由 daemon 注入为 `aggregator.all_project_roots`（活跃根 ∪ 注册表根 ∪ 上述磁盘历史根的有序并集），取代此前直接读取裸活跃集 `aggregator.project_roots` 的写法——后者在『本机当前没有 se3 run 进程』时为空，正是历史列表此前一律落空（`No history sessions reported.`）的根因；改用 `all_project_roots` 后，无活进程、甚至 daemon 重启后历史索引仍非空
- `protocol.py` — daemon↔服务器 WebSocket 协议的单一来源：`PROTOCOL_VERSION`（当前为 `"2"`）、`DEFAULT_SERVER_PORT`（中心服务器默认端口 `8080`，由 `se3-server` 的 `--port` 默认值与 daemon 客户端的 URL 端口补全共同引用）、消息类型常量（`MSG_HELLO` / `MSG_WELCOME` / `MSG_STATUS_UPDATE` / `MSG_SPAWN_FLOW` / `MSG_RESPOND_CALL` / `MSG_CALL_NOTIFICATION` / `MSG_PING` / `MSG_PONG`，以及历史相关的 `MSG_HISTORY_INDEX`（daemon→server 历史 session 索引）/ `MSG_HISTORY_INDEX_REQUEST`（server→daemon 请求 daemon 重建并立即重推其历史 session 索引，已登记进 `SERVER_TO_DAEMON` / `ALL_MESSAGE_TYPES`）/ `MSG_HISTORY_REQUEST`（server→daemon 按需拉取某历史 session 记录）/ `MSG_HISTORY_DATA`（daemon→server 历史数据响应，支持 full / append 两种模式与游标），以及 `MSG_INTERJECT_FLOW`（server→daemon 把一条网页控制台输入的中途插话下发给运行中 flow））与 `make_*` 消息构造器（含 `make_history_index` / `make_history_index_request` / `make_history_request` / `make_history_data` / `make_interject_flow`；`make_spawn_flow` 增加 `discover` 字段以透传「从 discovery 起步」标志）。该模块还集中定义运行中 flow 人机交互 call 文件的 `kind` 常量 `CALL_KIND_*`（`CALL_KIND_CALL` / `CALL_KIND_INTERJECTION` / `CALL_KIND_RETRY_DECISION` / `CALL_KIND_CLI_CONFIRM`）与 `CALL_KINDS` 集合，使 `se3/calls/` 下 call 文件的 `kind` 取值在引擎、daemon 与服务器之间不漂移。同时被 daemon 与 `se3.server` 包 import，确保协议 schema 不漂移。对端遇到未知 `type` 应忽略，以保证新旧版本混连不崩

### Requirement: Server Modules

`src/se3/server/` 是中心服务器后端 + 自带网页前端的独立包，经 `pyproject.toml` 的 optional-dependencies（`se3[server]`）隔离 web 重依赖，通过独立 console_scripts 入口 `se3-server` 启动——不做成核心 `se3` 的子命令。新增 server 子模块必须在此 spec 中登记。

- `app.py` — FastAPI 应用入口：`create_app` 装配路由，`run` / `main` 通过 `uvicorn` 启动并解析 `--host` / `--port` / `--version`（`--version` 仅作为 argparse 帮助文本完整性补充，真正的拦截在 `src/se3/server/__init__.py:main` 里于 FastAPI / uvicorn import 之前完成，输出格式 `se3-server version {__version__}` 与核心 `se3 version` 对齐）；提供 REST API（机器 / 流程查询、远程发布新任务、应答待处理 call/interjection）并将 `static/` 挂载到 `/`；含历史查询端点 `GET /api/history`（历史 session 列表）与 `GET /api/history/{flow_id}`（某 session 的 step 对话记录）：`GET /api/history` 在每次被请求时先经 `broadcast_index_refresh` 向所有已连接 daemon 广播 `MSG_HISTORY_INDEX_REQUEST`、有界等待各 daemon 强制重推其最新 index（经 `IndexRefreshRegistry` 登记/解除等待者）后再聚合返回，使『进入历史列表即取到最新历史』而无需用户手动硬刷新；当无已连接 daemon 或等待超时时稳健降级为返回当前缓存的 index（不阻塞、不报错）；`GET /api/history/{flow_id}` 在缓存未命中时向对应 daemon 发 `MSG_HISTORY_REQUEST` 并等待数据返回，等待上限由模块级常量 `HISTORY_PULL_TIMEOUT` 控制（当前 `30.0` 秒），其值必须足够覆盖含数 MB jsonl 的大 session 在 daemon 端冷启动首次拉取的真实磁盘读取耗时——即使 daemon 已按 *Daemon Modules* 中 `client.py` 的要求把读取卸载到工作线程，磁盘读取本身仍有可观延迟，过窄的窗口会在 daemon 健康但仍在读取时回 504；新增 `POST /api/flows/{flow_id}/interject` 端点，把网页控制台输入的中途插话经 `make_interject_flow` 封装为 `MSG_INTERJECT_FLOW` 下发给拥有该 flow 的 daemon（fire-and-forget 派发，返回 `202`）；提供 `GET /api/version` 端点返回 `{"version": __version__}`，供前端在顶栏渲染当前 se3 版本号，版本读取自 `se3.__version__` 这一单一来源
- `ws.py` — WebSocket 端点：管理 daemon 连接池（连接 / 断开 / 心跳）与前端 `UiHub` 广播通道，路由协议消息（daemon→server 状态上报、server→daemon 指令下发）；同时路由历史消息（`MSG_HISTORY_INDEX` / `MSG_HISTORY_DATA`）写入 `ServerState` 并向 `/ws/ui` 广播；并提供 `IndexRefreshRegistry`（按 `machine_id` 登记在途的 index 重推等待，与 `HistoryRequestRegistry` 同构、但以机器而非 flow 为键）与 `broadcast_index_refresh` helper（向每个在线 daemon 下发 `MSG_HISTORY_INDEX_REQUEST` 并登记对应 future），收到 daemon 的 `MSG_HISTORY_INDEX` 时除更新缓存外还 resolve 对应等待者，供 `GET /api/history` 在聚合前同步到各 daemon 重推后的最新 index
- `state.py` — `ServerState`，内存中的多机 / 多 flow 聚合状态存储（本次交付不含数据库持久化）；`MachineRecord` 同时携带 `project_roots: List[str]`，由 `update_status` 从 daemon snapshot 中读取并保存，`/api/machines` 在 `include_flows=False` 概览中也返回该字段供前端联动；额外缓存历史 session 索引与已拉取的历史数据（仅作内存中转 / 缓存，不落地持久化）
- `static/` — 纯静态网页前端（`index.html` / `style.css` / `app.js`），无构建步骤；通过 `/ws/ui` WebSocket 接收实时状态，提供查看进度、远程发布任务与响应 interjection/call 的界面；顶栏渲染一个版本号 label（`#se3-version`），页面加载时通过 `GET /api/version` 拉取并显示；新建任务表单提供「从 discovery step 起步」选项与「Project」联动下拉（按所选 machine 的 `project_roots` 填充：0 项禁用 submit 并提示、1 项自动选中、多项必填）以保证提交 `POST /api/flows` 时携带非空 `project_root`；并提供历史 session 列表、单个 session 的 step 对话详情与活跃 session 实时滚动三块历史视图——历史列表的空列表语义被拆为三种 class 可区分的状态：(1) `loading-refresh`「正在刷新历史…」——每次刷新（进入历史 / `fetchHistoryIndex` 触发的 `/api/history` 往返在途）期间显示；(2) `loading-connect`「正在连接 / 正在等待历史数据…」——当尚未确认拿到历史数据（daemon 未连接，或已连接但尚未通过 WS 推送 `history_index`）时持续显示，**不**因 `/api/history` 在 `HISTORY_INDEX_REFRESH_TIMEOUT`（2s）内空手返回就回落为空态（daemon 连上并推送 `history_index` 往往要约一分钟，这一分钟内必须显示加载态而非空态）；(3) `empty-confirmed`「No history sessions reported.」——仅当确实已确认（daemon 已连接且确认零条会话，经 WS 推送空 `history_index` 或 `/api/history` 返回非空数据集而其中无会话）时才显示。判定逻辑抽为纯函数（如 `historyListEmptyState(...)`）以便前端纯逻辑测试覆盖；底层 `broadcast_index_refresh` → daemon `force_index` 实时拉取机制不变。运行中 flow 不再使用右侧窄抽屉与独立的无上下文 call-modal 弹窗，而是对标历史视图改为全屏 `#flow-view`：conversation 为可滚动主体区域，Overview / Steps / 机器等辅助信息收入侧栏；该视图是运行中 flow 的唯一交互面，其行为契约见 `running-flow-console` spec。`#flow-view` 进入时通过 `history.pushState` 压入一个 `#flow/<id>` history entry，关闭路径（✕ 按钮 / Escape / 浏览器后退键）统一收敛到单一的 `popstate` 触发的内部关闭函数：✕ 与 Escape 委托给 `history.back()` 反向触发 `popstate`，使浏览器后退键能够关闭 flow-view 回到上层列表而不是退出整个站点。docked 回复区的 textarea 始终保持 enabled（仅在 submit in-flight 期间短暂禁用），Send 仅在存在可发送 target（选中的 chip 或激活的 interject 模式）时启用；Interject 按钮以内联小图标形式置于 textarea 左侧、与 Send 左右对称，点击切换 interject 模式。所有会话记录先经统一归一化层解开服务端 `{step_id, step_type, message}` 外层包装，并优先采信信封层的权威 step_type（回退到 inner `message.step_type`，再回退为空）作为 step 分派、步骤标题（`DISCOVERY` 等而非 `01_discovery_975607bb` 丑文件名）与 report 卡片的类型来源，再按 step 分组、组内按时间顺序以 user/assistant/system 气泡呈现一问一答；assistant 文本走轻量 Markdown 渲染并识别内联工具调用标记渲染为独立块；超长记录默认折叠并可展开（展开后自动 `scrollIntoView({block:"nearest"})` 把新出现的内容滚入视口，折叠不动）；每条记录提供「查看原始」入口切换查看 `raw_json` / `raw_ndjson`