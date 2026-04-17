# flow-engine Specification

## Purpose

定义 SE3 3.0 的核心流程引擎（Flow Engine）：一个程序驱动的状态机，通过统一的 `se3 run` 入口控制开发流程的 13 个步骤编排，在每个步骤内调用 LLM 处理需要"思考"的部分。

## Requirements

### Requirement: 统一入口 `se3 run`

`se3 run` SHALL 作为 SE3 3.0 的唯一流程入口，取代 `se3:start` / `se3:work` / `se3:done` 的手动串联。

**Interface:**
```bash
# 新任务
se3 run "实现用户登录功能"

# 恢复中断的任务
se3 run --resume

# 循环模式（持续执行任务）
se3 run --loop

# 指定任务类型
se3 run "修复内存泄漏" --type=bugfix

# Discovery 模式（需求探索）
se3 run --discover "我想做一个用户管理功能"
```

#### Scenario: 新任务启动
- **WHEN** 用户执行 `se3 run "实现用户登录功能"`
- **THEN** 流程引擎创建新的流程实例
- **AND** 从 `analyze` 步骤开始执行

#### Scenario: Discovery 模式启动
- **WHEN** 用户执行 `se3 run --discover "初步想法"`
- **THEN** 流程引擎创建 discovery 类型的流程实例
- **AND** 从 `discovery` 步骤开始执行
- **AND** 通过多轮对话与用户探索需求
- **AND** 用户确认后使用精炼描述进入 `analyze` 步骤

#### Scenario: 恢复已有任务
- **WHEN** 用户执行 `se3 run` 且存在未完成的流程状态
- **THEN** 流程引擎提示恢复或新建
- **AND** 如果选择恢复，从中断点继续

#### Scenario: 循环模式
- **WHEN** 用户执行 `se3 run --loop`
- **THEN** 流程引擎在完成一个任务后继续执行下一个任务

#### Scenario: 循环模式分支隔离
- **WHEN** 用户执行 `se3 run --loop`（不带 `--no-worktree`）
- **THEN** 创建 `loop/{task_id}-{iteration}` 分支从当前 HEAD（task_id 由任务描述 slugify 后截断到 30 字符生成）
- **AND** 在 `se3/worktrees/{branch_safe_name}` 创建 git worktree
- **AND** 所有任务在 worktree 中执行（文件读写、commit 都在 worktree 内）
- **AND** 循环结束后提示用户选择：merge / later / discard
- **NOTE** 向后兼容：`list_loop_branches()` 同时匹配旧格式 `se3-loop/*`（标记为 `[legacy]`）和新格式 `loop/*`

#### Scenario: 循环模式无隔离
- **WHEN** 用户执行 `se3 run --loop --no-worktree`
- **THEN** 所有任务直接在当前分支上执行（无分支隔离）

#### Scenario: 延迟合并
- **WHEN** 用户执行 `se3 run --loop --merge loop/fix-auth-1`（或旧格式 `se3-loop/20260324-120000`）
- **THEN** 显示 diff 摘要并确认后将指定的 loop 分支合并到当前分支
- **AND** 如果有冲突，根据 `conflict_resolver.strategy` 配置处理（见 se3-config spec）

#### Scenario: 列出循环分支
- **WHEN** 用户执行 `se3 run --list-loops`
- **THEN** 显示所有未合并的 loop 分支及其 commit 数量
- **AND** 如果没有 loop 分支则提示无分支

#### Scenario: 循环模式外部包装架构
- **WHEN** `se3 run --loop` 执行循环迭代
- **THEN** 外层 `LoopController` 管理分支/worktree 生命周期、任务发现、迭代计数
- **AND** 内层 `run_flow()` 执行标准 10 步流程，对循环模式无感知
- **AND** 循环上下文仅通过 `set_extra_prompt(persistent=True)` 注入到 LLM 调用中
- **AND** 持久化 prompt 在多次 LLM 调用间保持，迭代结束后清理

#### Scenario: 循环模式任务重选避免
- **WHEN** 循环迭代中某任务失败
- **THEN** 该任务加入 `failed_tasks` 集合
- **AND** 后续迭代自动跳过已完成和已失败的任务

### Requirement: Discovery Workflow

`discovery` 步骤 SHALL 实现多轮对话机制，帮助用户在需求不明确时探索并澄清需求。

**工作流程：**
1. **初始探索**: 根据用户的初步描述，AI 提出澄清问题
2. **对话迭代**: 用户回答后，AI 继续追问或转向综合
3. **综合确认**: AI 总结理解并生成精炼的任务描述
4. **用户确认**: 用户确认或要求修改
5. **程序化确认门控**: LLM 确认后，程序向用户展示编号选项，要求人工明确选择才能继续
6. **进入分析**: 人工确认后使用精炼描述继续 `analyze` 步骤

**状态管理：**
- 对话历史保存在 `discovery_state` 中
- 支持任意轮次中断并通过 `se3 run --resume` 恢复
- 最大对话轮数限制（默认 10 轮）防止无限循环

**LLM 调用模式：**
- `question` 模式: 向用户提出具体问题
- `synthesis` 模式: 总结理解并生成精炼描述
- `confirmation` 模式: 用户确认后暂停等待程序化确认门控

**程序化确认门控：**

当 LLM 的 `confirmation` 模式判定需求已明确并生成精炼描述后，discovery 步骤不直接完成，而是返回 `PAUSED` 状态并设置 `awaiting_programmatic_confirm=True`。程序运行循环检测到此标志后，向用户展示编号选项：

1. **确认并继续** — 进入实现规划阶段
2. **还有问题** — 继续 discovery 对话

只有用户明确选择选项 1 时，流程才会继续。选择选项 2 将清除确认标志并重新进入 discovery 对话，用户可以提供额外的问题或反馈。

这确保了 LLM 的确认判断不会单方面推进流程，人工始终拥有最终决定权。

#### Scenario: 需求探索对话
- **GIVEN** 用户执行 `se3 run --discover "我想做一个用户相关功能"`
- **WHEN** discovery 步骤执行
- **THEN** AI 询问："这个用户功能是给谁用的？管理员还是普通用户？"
- **AND** 用户回答后继续追问或综合

#### Scenario: 生成精炼描述
- **GIVEN** 经过多轮对话后
- **WHEN** AI 进入 synthesis 模式
- **THEN** 生成结构化的任务描述
- **AND** 暂停等待用户确认

#### Scenario: Discovery 中断恢复
- **GIVEN** 用户在第 3 轮对话时中断（Ctrl+C）
- **WHEN** 用户执行 `se3 run --resume`
- **THEN** 恢复到 discovery 步骤
- **AND** 继续第 3 轮对话

#### Scenario: 程序化确认门控 — 用户确认继续
- **GIVEN** LLM 在 confirmation 模式下判定需求已明确
- **AND** discovery 步骤返回 PAUSED 且 `awaiting_programmatic_confirm=True`
- **WHEN** 程序向用户展示编号选项且用户选择"确认并继续"
- **THEN** 设置 `programmatic_confirmed=True` 到步骤输入
- **AND** 重新执行 discovery handler，handler 检测到此标志后直接完成步骤
- **AND** 生成 `discovery_summary` 并设置 `requirements_clarified=True`

#### Scenario: 程序化确认门控 — 用户继续探索
- **GIVEN** LLM 在 confirmation 模式下判定需求已明确
- **AND** discovery 步骤返回 PAUSED 且 `awaiting_programmatic_confirm=True`
- **WHEN** 程序向用户展示编号选项且用户选择"还有问题"
- **THEN** 清除 `awaiting_programmatic_confirm` 标志
- **AND** 提示用户输入问题或反馈
- **AND** 将用户输入作为新的 discovery 轮次继续对话

#### Scenario: Discovery 输出传递
- **GIVEN** discovery 步骤完成且用户已通过程序化确认门控确认
- **WHEN** 流程进入 `analyze` 步骤
- **THEN** `refined_description` 自动作为 `task_description` 传递给 analyze

#### Scenario: Discovery LLM JSON 提取失败时的友好错误提示
- **GIVEN** discovery 步骤执行 LLM 调用使用 two-phase JSON 模式
- **WHEN** LLM 返回叙述性文本而非有效 JSON，导致 `LLMCallError`（消息包含 "JSON extraction failed"）
- **THEN** discovery_handler 捕获 `LLMCallError` 并向用户展示友好的错误面板（通过 `render_full`），说明 LLM 未能返回有效 JSON 结构化输出
- **AND** 步骤返回 `StepStatus.FAILED`，由流程引擎的重试机制自动处理（最多 3 次）
- **AND** 不向用户暴露原始 traceback

#### Scenario: Discovery 其他 LLM 调用错误的友好提示
- **GIVEN** discovery 步骤执行 LLM 调用
- **WHEN** LLM 调用因非 JSON 提取原因失败，抛出 `LLMCallError`
- **THEN** discovery_handler 捕获错误并展示简洁的错误描述（包含原始错误消息）
- **AND** 步骤返回 `StepStatus.FAILED`

#### Discovery Message Display Rendering

The `_display_discovery_message()` function SHALL render LLM-generated content fields (`content` and `refined_description`) as markdown using `rich.markdown.Markdown`, while structural UI elements (section titles, numbered question lists, confirmation prompts) use Rich `Text` with appropriate styling. Multiple renderables are combined via `rich.console.Group` and displayed inside a Rich `Panel` titled "Discovery".

**Rendering rules by mode:**

| Mode | `content` field | `refined_description` field | Structural elements |
|------|----------------|---------------------------|---------------------|
| Confirmation (`is_confirmation=True`) | Markdown | Markdown | — |
| Synthesis + questions | Markdown | Markdown (under "Proposed Task Description:" heading) | Heading as styled `Text`, numbered questions as `Text` |
| Synthesis (no questions) | Markdown | Markdown (under "Proposed Task Description:" heading) | Heading as styled `Text`, confirmation prompt as styled `Text` |
| Question | Markdown | — | Numbered questions as `Text` |
| General | Markdown | — | — |

**Confirmation phase content display:**

When the discovery step enters the confirmation phase (`is_confirmation=True`), `_display_discovery_message()` SHALL display the full LLM analysis content (`content` field) followed by the `refined_description`, both rendered as markdown. This ensures users see the complete LLM analysis (reasoning, summaries, context) alongside the final proposed description before making their confirmation decision.

##### Scenario: Discovery message renders LLM content as markdown
- **GIVEN** the discovery step receives LLM response with `content` and `refined_description` fields containing markdown syntax (headings, lists, bold, etc.)
- **WHEN** `_display_discovery_message()` renders the message
- **THEN** `content` and `refined_description` are rendered via `rich.markdown.Markdown`
- **AND** structural elements (titles, numbered questions, confirmation prompts) use Rich `Text` with styling
- **AND** all renderables are combined via `rich.console.Group` into a single `Panel`

##### Scenario: Confirmation phase shows full LLM analysis content
- **GIVEN** LLM enters confirmation mode with both `content` (analysis text) and `refined_description`
- **WHEN** the confirmation display is rendered
- **THEN** the full `content` from the LLM response is displayed as markdown
- **AND** the `refined_description` is displayed as markdown below it
- **AND** the user can review the complete analysis before choosing to confirm or continue exploration

### Requirement: 状态机驱动流程

流程引擎 SHALL 以 Python 有限状态机实现，每个状态对应一个流程步骤。步骤之间的转换由程序逻辑控制，而非 LLM 决定。

**核心原则：**
1. 步骤转换是程序化的（programmatic）
2. LLM 只处理步骤内部的工作（思考、生成、分析）
3. LLM 的输出不改变步骤转换逻辑

**Flow Lifecycle API:**

The `StateMachine` SHALL expose the following public lifecycle API for orchestrators:

1. `create_flow(task_description, task_type)` — Create a new `FlowInstance` (or load an existing one for resume)
2. `init_flow(flow)` — Initialize flow metadata and baseline commit. Writes `_meta.json` (containing `se3_version`, `python_version`, `created_at`) to the session history directory and records the current git HEAD as `baseline_commit` on the flow instance for change detection during the commit step. Both operations are idempotent: if `_meta.json` already exists or `baseline_commit` is already set, they are skipped — making `init_flow` safe for both new and resumed flows.
3. `run_step(flow, step)` — Execute a single step
4. `transition_to_next(flow)` — Advance to the next step

The CLI orchestrator (`_run_flow_impl`) calls these methods in sequence: `create_flow()` → `init_flow()` → while loop of `run_step()`/`transition_to_next()`.

#### Scenario: 正常流程执行
- **WHEN** 用户执行 `se3 run` 并提供任务描述
- **THEN** 流程引擎从 `init` 状态开始
- **AND** 调用 `init_flow()` 写入 `_meta.json` 并记录 baseline commit
- **AND** 按程序定义的转换规则依次进入后续步骤
- **AND** 每个步骤内调用 LLM 处理该步骤的具体工作

#### Scenario: init_flow idempotent on resume
- **WHEN** a flow is resumed via `se3 run --resume`
- **AND** `init_flow()` is called on the loaded flow instance
- **THEN** `_meta.json` is not overwritten (file already exists guard)
- **AND** `baseline_commit` is not overwritten (already-set guard)

#### Scenario: 步骤池动态选择
- **WHEN** 流程引擎完成 `analyze` 步骤
- **THEN** 根据分析结果从固定步骤池中选取后续需要的步骤
- **AND** 步骤池是预定义的有限集合，不由 LLM 凭空生成

### Requirement: 13 步流程池

流程引擎 SHALL 定义固定的 13 步骤池，所有流程步骤从此池中选取。

| 步骤 | 职责 | LLM 参与 | JSON 模式 | Read-Only | 输入 | 输出 |
|------|------|---------|-----------|-----------|------|------|
| `discovery` | 需求探索（多轮对话） | 是 | STRICT | **是** | initial_description | refined_description, discovery_summary |
| `analyze` | 分析任务类型和范围；收集项目上下文；选择并加载 spec | 是 | STRICT | **是** | task_description | task_type, scope, complexity, reasoning, project_summary, relevant_specs, spec_content, selected_specs |
| ~~`read_spec`~~ | ~~读取相关 spec 文件~~ (deprecated — merged into analyze) | 否（程序自动） | - | **是** | scope | relevant_specs, spec_content |
| `plan` | 统一规划：提案+设计+任务分解（按 task_type 自适应深度） | 是 | TWO_PHASE | **是** | spec_content, task_description, task_type, project_summary | plan{proposal,design}, task_groups, spec_changes |
| `implement` | 编写代码实现 | 是 | TWO_PHASE | 否 | design_doc, task_groups | completion_status, files_changed, tests_added, implemented_groups, summary, incomplete_tasks, restricted_edits_applied, restricted_edits_failed |
| `test` | 运行测试验证 | 否（程序执行） | - | 否 | - | test_results, tests_passed |
| `self_check` | LLM 代码审查：逻辑完整性、代码健壮性、功能遗漏、测试未覆盖区域（不检查 spec 合规性） | 是 | TWO_PHASE | **是** | test_results, changes_made, spec_content, task_groups, fix_iteration | issues (structured list with description, severity, location), status |
| `verify_spec` | 检查实现与 spec 一致性 | 是 | EXTRACT | **是** | changes_made, spec_content, test_results, fix_iteration, spec_changes | verification_result, issues, fix_needed, fix_instructions, fix_context |
| `update_spec` | 更新 spec 记录变更 | 是 | EXTRACT | 否 | changes_made, verification_result, spec_changes, design_doc | updated_specs |
| `version_analyze` | 分析变更确定版本类型 + 生成 commit message | 是 | EXTRACT | **是** | changes_made, updated_specs, verification_result | bump_type, confidence, reasoning, commit_message |
| `commit` | 提交变更 | 否（程序执行） | - | 否 | changes_made, bump_type | commit_hash |
| `summarize` | 生成总结和 handoff | 是 | 文本 | **是** | all_previous_outputs | summary (Markdown 文本) |
| ~~`project_summary`~~ | ~~生成项目上下文摘要~~ (deprecated — merged into analyze) | 是 | 文本 | **是** | 项目状态 | 摘要字符串 |

**不同任务类型的步骤序列：**
- `discovery`: discovery → analyze → plan → implement → test → **self_check** → verify_spec → update_spec → **version_analyze** → commit
- `feature`: analyze → plan → implement → test → **self_check** → verify_spec → update_spec → **version_analyze** → commit
- `bugfix`: analyze → plan → implement → test → **self_check** → verify_spec → **version_analyze** → commit
- `review`: analyze → verify_spec
- `small`: analyze → implement → test → **version_analyze** → commit
- `directive`: analyze → plan → implement → **version_analyze** → commit

**Note:** The `summarize` step is not in any default sequence. It remains available in the step pool and can be added via `se3.yaml` configuration. When `summarize` is not in the sequence, the `commit` step generates a template-based summary document (`se3/state/summary-{flow_id}.md`) using structured data from the flow state.

#### Scenario: Feature 任务完整流程
- **WHEN** 任务类型为 `feature`
- **THEN** 执行完整的 10 步流程（plan 使用 full 深度），包含 self_check 步骤

#### Scenario: Small 任务简化流程
- **WHEN** 任务类型为 `small`
- **THEN** 跳过 plan 和 self_check 步骤

#### Scenario: SELF_CHECK 代码审查通过
- **WHEN** self_check 步骤完成 LLM 代码审查
- **AND** 未发现任何 severity 的遗漏（issues 列表为空）
- **THEN** self_check 返回 COMPLETED
- **AND** 流程继续到 verify_spec 步骤

#### Scenario: SELF_CHECK 发现遗漏触发 fix loop
- **WHEN** self_check 步骤完成 LLM 代码审查
- **AND** 发现任何 severity（critical/high/medium/low）的遗漏
- **THEN** self_check 返回 REVISION_NEEDED
- **AND** 附带 fix_context（遗漏列表）和 fix_instructions
- **AND** 触发现有 fix loop 机制回到 IMPLEMENT 步骤
- **AND** 修复后重跑 TEST → SELF_CHECK 直到遗漏列表为空或达到 max_fix_iterations 上限
- **NOTE** fix_iterations 是全局计数器，TEST、SELF_CHECK、VERIFY_SPEC 三者共享，总循环次数不超过 max_fix_iterations（默认 20）
- **NOTE** self_check handler 始终返回 REVISION_NEEDED（不在 handler 内判断耗尽），耗尽检测统一由 state_machine.transition_to_next() 处理
- **NOTE** 当 fix loop 耗尽时，state_machine 将 flow 状态设为 FAILED 并停止执行，同时通过 A-class issue discovery 生成 issue

#### Scenario: SELF_CHECK prompt injects plan task_groups as scope reference
- **WHEN** the `self_check` step builds its LLM prompt
- **AND** `step.inputs["task_groups"]` is a non-empty list (forwarded by state_machine from the plan step)
- **THEN** the prompt includes a `## Plan Task Groups (Scope Reference)` section summarizing each group's tasks and acceptance criteria
- **AND** the section body is head-truncated at `SELF_CHECK_TASK_GROUPS_MAX_CHARS` (default 2000) to bound prompt size
- **AND** the section wording explicitly states that task_groups is a **scope reference, NOT a strict specification**, that reasonable deviations (logic correct, functionality covered, quality acceptable) do NOT count as issues, and that self_check MUST NOT flag missing-plan-compliance as an issue — this is self_check, not a plan-conformance audit
- **AND** the LLM is instructed to use task_groups to cross-check the **Functional Gaps** review dimension (verifying each planned task's deliverables appear in the implementation), weighing it together with the original Task Description
- **WHEN** `task_groups` is absent, `None`, empty, or not a list (e.g., `small` / `bugfix` flows without a plan step)
- **THEN** the entire `## Plan Task Groups` section is omitted from the prompt — no orphan heading, no placeholder
- **AND** self_check still runs with its remaining inputs (task_description, changes_made, test_results, spec_content, fix_context)
- **NOTE** task_groups is intentionally the ONLY plan artifact injected into self_check — `proposal` is redundant with `design`, full `design` is withheld to preserve the verify_spec / self_check responsibility boundary, and neither is added
- **NOTE** `SELF_CHECK_TASK_GROUPS_MAX_CHARS` lives in `se3/engine/truncation.py` alongside the other shared self_check truncation constants

### Requirement: Deprecated Step Type Backward Compatibility

The step type enum SHALL retain deprecated values with stub handlers that forward to the appropriate current handler. This ensures persisted flows created before step unification/merges can resume without crashing.

**Retained entries (plan unification):**
- `StepTypeValue.PROPOSE` — deprecated, forwards to plan_handler
- `StepTypeValue.DESIGN` — deprecated, forwards to plan_handler
- `StepTypeValue.PLAN_TASKS` — deprecated, forwards to plan_handler

**Retained entries (analyze merge):**
- `StepTypeValue.PROJECT_SUMMARY` — deprecated, forwards to project_summary_handler
- `StepTypeValue.READ_SPEC` — deprecated, forwards to read_spec_handler

**Behavior:**
- Stub handlers log a deprecation warning with the flow ID and step ID
- The target handler executes normally regardless of which step type triggered it
- Display titles and renderers for deprecated types are retained so history/status views render correctly

#### Scenario: Resuming a persisted flow with old step types
- **WHEN** a flow persisted with `PROPOSE`, `DESIGN`, or `PLAN_TASKS` step types is resumed
- **THEN** the stub handler forwards execution to plan_handler
- **AND** a deprecation warning is logged

#### Scenario: Resuming a persisted flow with PROJECT_SUMMARY or READ_SPEC steps
- **WHEN** a flow persisted with `PROJECT_SUMMARY` or `READ_SPEC` step types is resumed
- **THEN** the stub handler forwards execution to the original handler (project_summary_handler or read_spec_handler respectively)
- **AND** a deprecation warning is logged

#### Scenario: New flows use unified PLAN step
- **WHEN** a new flow is created
- **THEN** the step sequence contains only `PLAN`, never `PROPOSE`, `DESIGN`, or `PLAN_TASKS`

#### Scenario: New flows do not include PROJECT_SUMMARY or READ_SPEC
- **WHEN** a new flow is created
- **THEN** the step sequence does not contain `PROJECT_SUMMARY` or `READ_SPEC`
- **AND** their functionality is provided by the `ANALYZE` step

### Requirement: 步骤内 LLM 调用

流程引擎 SHALL 在每个步骤内通过 subprocess 调用 LLM（`claude -p`），传入步骤特定的 prompt 和自动收集的 context。

**LLM 调用机制：**
1. 构建步骤特定的 prompt
2. 自动收集相关上下文（specs、前序步骤输出、项目状态）
3. 调用 Claude CLI 获取响应
4. 解析响应（支持 JSON 和文本）
5. 存储输出到步骤状态

**Large Prompt Auto-Filing:**

The CLI adapter (`ClaudeCodeRunner._resolve_args()`) SHALL automatically file prompt arguments to temporary files when their UTF-8 byte length exceeds 100 KB (102,400 bytes), preventing `execve()` `E2BIG` errors caused by Linux's `MAX_ARG_STRLEN` limit (128 KB).

- **Threshold:** 100 KB (102,400 bytes). This leaves ~28 KB safety margin below the 128 KB `MAX_ARG_STRLEN` hard limit, covering multi-byte UTF-8 encoding and environment variable space.
- **Mechanism:** When `-p`/`--prompt` is followed by a plain-text argument (not an `@file` reference) whose `len(prompt_arg.encode('utf-8'))` exceeds the threshold, the prompt content is written to a temp file in `se3/tmp/` (using `NamedTemporaryFile` with `.prompt` suffix, `delete=False`), and the command-line argument is replaced with `@{temp_file_path}`.
- **Below threshold:** The prompt argument is passed directly on the command line (existing behavior).
- **Scope:** `_resolve_args()` is called by all three execution paths (`run()`, `popen()`, `run_with_monitor()`), so the protection applies universally.
- **Temp file cleanup:**
  - `run()` and `run_with_monitor()`: temp files are tracked and cleaned up in `finally` blocks.
  - `popen()`: temp files are attached to the process as `proc._se3_temp_files` for caller cleanup; on `Popen` failure, temp files are cleaned up immediately.
  - On write failure during temp file creation, the orphan file is deleted before re-raising.
- **Chat history preservation:** `_record_prompt()` in `LLMCaller` executes before `_resolve_args()`, so chat history always records the original prompt text, not the `@file` reference.

#### Scenario: 自动注入上下文
- **WHEN** 流程引擎进入某个步骤
- **THEN** 程序自动收集该步骤所需的上下文
- **AND** 将上下文注入 LLM 调用的 prompt 中

#### Scenario: LLM 调用失败
- **WHEN** 步骤内的 LLM 调用失败（超时、API 错误、输出无效）
- **THEN** 流程引擎执行重试策略（最多 3 次）
- **AND** 如果重试仍失败，暂停流程并通知用户

#### Scenario: Large prompt auto-filed to temp file
- **WHEN** a `-p`/`--prompt` argument's UTF-8 byte length exceeds 100 KB (102,400 bytes)
- **THEN** `_resolve_args()` writes the prompt to a temp file in `se3/tmp/` with `.prompt` suffix
- **AND** replaces the command-line argument with `@{temp_file_path}`
- **AND** the temp file is cleaned up after execution completes

#### Scenario: Prompt below auto-filing threshold
- **WHEN** a `-p`/`--prompt` argument's UTF-8 byte length is at or below 100 KB
- **THEN** the prompt is passed directly as a command-line argument (no file creation)

#### Scenario: Auto-filing temp file write failure
- **WHEN** writing the prompt to a temp file fails (e.g., disk full)
- **THEN** the orphan temp file is deleted before the exception propagates
- **AND** no stale temp files are left behind

### Requirement: Read-Only Step Constraint Injection

The flow engine SHALL enforce a prompt-level file modification prohibition for read-only steps, preventing the LLM from accidentally modifying code during analysis-only steps.

**Read-Only Step Attribute:**

Each entry in the step pool (`STEP_POOL`) SHALL include a `read_only` boolean attribute. Steps marked `read_only: true` are:
- `discovery`, `analyze`, `plan`, `self_check`, `verify_spec`, `version_analyze`, `summarize`
- Deprecated steps (`project_summary`, `read_spec`)

Steps explicitly marked `read_only: false`:
- `implement`, `test`, `update_spec`, `commit`, `confirm`
- Deprecated steps (`propose`, `design`, `plan_tasks`)

**Injection Mechanism:**

1. `context_builder.get_read_only_injection(step_type)` queries STEP_POOL by step name to determine if the step is read-only. If so, it returns a constraint prompt; otherwise returns an empty string.
2. `LLMCaller.call()` invokes `get_read_only_injection()` after user extra_prompt injection but before mode dispatch. The constraint is appended to the prompt for all JSON modes (STRICT, EXTRACT, TWO_PHASE, OFF).

**Constraint Prompt Content:**

The injected prompt SHALL:
- Explicitly forbid use of Write, Edit, NotebookEdit tools
- Explicitly forbid creating new files
- Explicitly forbid shell commands that modify files (sed, awk, tee, redirects)
- Explicitly allow Read, Grep, Glob, and read-only Bash commands
- State that the step's purpose is analysis and reasoning only

#### Scenario: Read-only step receives constraint injection
- **WHEN** LLMCaller executes a step marked `read_only: true` (e.g., `analyze`, `plan`)
- **THEN** the read-only constraint prompt is appended to the LLM prompt
- **AND** the constraint forbids all file modification tools and commands

#### Scenario: Non-read-only step receives no constraint
- **WHEN** LLMCaller executes a step marked `read_only: false` (e.g., `implement`, `update_spec`)
- **THEN** no read-only constraint is injected
- **AND** the LLM can freely modify files

#### Scenario: Discovery step is read-only
- **WHEN** LLMCaller executes the `discovery` step
- **THEN** the read-only constraint prompt is appended to the LLM prompt
- **AND** the discovery step cannot modify files, consistent with its sole responsibility of producing the Proposed Task Description

#### Scenario: SELF_CHECK step is read-only
- **WHEN** LLMCaller executes the `self_check` step
- **THEN** the read-only constraint prompt is appended to the LLM prompt
- **AND** the self_check step cannot modify files, consistent with its sole responsibility of reviewing code for logic completeness and robustness

### Requirement: JSON 提取模式

流程引擎 SHALL 支持三种 JSON 提取模式，根据步骤特性选择最优策略：

| 模式 | 描述 | 适用场景 |
|------|------|----------|
| **STRICT** | 强制 JSON 格式，失败重试 | 简单输出（analyze） |
| **EXTRACT** | 要求 JSON 格式，失败时用 LLM 提取 | 中等复杂度（verify_spec, update_spec） |
| **TWO_PHASE** | 自然生成 + LLM 提取 | 复杂/大输出（plan, implement） |

**模式选择原则：**
- 简单输出（<1K tokens）：STRICT（成本低，可靠性高）
- 中等复杂度（1K-5K tokens）：EXTRACT（平衡可靠性和 token 效率）
- 大输出（>5K tokens）：TWO_PHASE（避免提示词污染，处理截断）

#### Scenario: STRICT 模式
- **WHEN** analyze 步骤需要简单的任务分类
- **THEN** 使用 STRICT 模式：prompt 添加强制 JSON 指令
- **AND** 如果输出非 JSON，重试整个调用

#### Scenario: EXTRACT 模式
- **WHEN** verify_spec 步骤生成验证结果
- **THEN** 使用 EXTRACT 模式：prompt 要求 JSON 格式
- **AND** 如果输出非 JSON，使用轻量级 LLM 调用来提取 JSON
- **AND** 不重试主调用，节省 token

#### Scenario: TWO_PHASE 模式
- **WHEN** implement 步骤生成包含大文件内容的输出
- **THEN** 使用 TWO_PHASE 模式：prompt 不添加 JSON 约束
- **AND** LLM 自然生成内容
- **AND** 第二次 LLM 调用从自然输出中提取 JSON
- **AND** 避免提示词污染，更好地处理截断

#### Scenario: TWO_PHASE fast path with required_keys validation
- **GIVEN** a step uses TWO_PHASE mode and passes `required_keys` to `LLMCaller.call()`
- **WHEN** Phase 1 output contains valid JSON
- **THEN** the fast path validates the parsed JSON against `required_keys` via `parse_json_response(output, required_keys=required_keys)`
- **AND** if all required keys are present, Phase 2 is skipped and the validated JSON is returned
- **AND** if any required key is missing, the fast path falls back to Phase 2 extraction instead of returning incomplete data
- **AND** Phase 2 extraction also receives `required_keys` for end-to-end validation consistency
- **NOTE** `required_keys` is an optional parameter (default `None`) on both `call()` and `_call_two_phase()`, preserving backward compatibility for callers that do not need key validation

### Requirement: 聊天记录系统（Chat History）

流程引擎 SHALL 记录每次 LLM 调用的 prompt 和回应，支持重试时注入对话上下文，并提供人类浏览接口。工具调用事件（tool_use / tool_result）SHALL 使用 per-tool 语义化格式渲染人类可读预览，格式化逻辑集中在 `tool_formatters` 模块中。

**存储格式：**
- 存储路径：`se3/history/{flow_id}/{step_id}.jsonl`
- 每行一个 ChatMessage（JSON 序列化）
- 存储层保存解析后的 JSON 对象数组（完整保真，无需双重编码）
- 给 LLM 重试时使用解析后的文本内容（减少 token 浪费）

**数据结构：**
- `ChatMessage`: role, content, raw_json, timestamp, step_type, attempt
  - `raw_json`: `list[dict]` - NDJSON 流解析后的 JSON 对象数组，每个元素是一行 NDJSON
- `ChatSession`: flow_id, step_id, step_type, messages

**核心功能：**
- `record_prompt()` — 记录发送的 prompt
- `record_response()` — 记录 LLM 原始回应
- `format_history_for_retry()` — 为重试格式化之前的对话上下文
- `extract_assistant_text()` — 从 NDJSON 提取 assistant 文本内容
- `segment_prompt()` — 将 prompt 拆分为标注段落，用于结构化展示
- `render_session_detailed()` — 渲染带结构化 prompt 和 response 的 Rich 可视输出
- `get_detailed_json()` — 获取包含分段 prompt 和完整 response 的结构化 JSON
- `_extract_final_text()` — 从 raw_json 中提取最后一个 assistant text block
- `split_implement_session_by_iterations()` — 将一个 implement ChatSession 按 test 会话时间戳切分为多个虚拟 per-iteration ChatSession（仅展示层，不落盘）
- `interleave_sessions_for_display()` — 将一个 flow 的所有 ChatSessions 重新排序，使虚拟 implement 分片与 test/self_check 按时间顺序穿插

**工具调用格式化（tool_formatters 模块）：**

`tool_formatters.py` 是工具调用预览格式化的唯一权威来源，由 `llm_caller.py`（流式输出）和 `chat_history.py`（历史渲染/重试上下文）共同消费。

- 公共 API：`format_tool_use_preview(tool_name, input_data)`、`format_tool_result_preview(tool_name, result_data)`、和 `format_tool_diff(tool_name, input_data, result_data, old_content=None)`
- 内部维护 `TOOL_FORMATTERS` 字典注册表（`{tool_name: {use: fn, result: fn, diff: str}}`），将工具名映射到专用格式化函数；可选的 `diff` 键标记该工具支持 diff 渲染
- 未注册的工具名回退到通用格式化器（key=value 截断预览）
- 提供 `truncate_preview()` 通用截断工具函数（用于非路径文本：命令字符串、错误信息、JSON 等）
- 提供 `truncate_path()` 文件路径专用截断函数：(1) 将绝对路径转为相对于 project root 的相对路径；(2) 若仍超过 `max_length`（默认 160 字符），缩略中间部分保留首段目录和文件名（格式 `first_dir/.../filename`）；(3) 文件名（最后一段）永远不被截断。Per-tool 格式化器中所有文件路径参数使用 `truncate_path` 而非 `truncate_preview`
- 提供模块级 `set_project_root(root)` / `get_project_root()` 函数管理项目根目录，供 `truncate_path` 在路径转换时使用；`LLMCaller` 在创建 `StreamJSONTracker` 前调用 `set_project_root()` 设置项目根目录
- 提供 `generate_edit_diff(old_string, new_string, file_path)` 使用 `difflib.unified_diff` 生成 unified diff（3 行上下文）

**内置 per-tool 格式化器：**

| Tool | tool_use preview | tool_result preview |
|------|-----------------|-------------------|
| Edit | `Edit: {file_path} ({n} lines → {m} lines)` | `Edit ✓ {file_path}` or error info |
| Write | `Write: {file_path} ({n} lines)` | `Write ✓ {file_path}` |
| Read | `Read: {file_path}:{offset}-{end}` | `Read ✓ ({n} lines)` |
| Bash | `Bash: {command preview}` | `Bash ✓ ({n} lines output)` |
| Grep | `Grep: /{pattern}/ in {path}` | `Grep ✓ ({n} matches)` |
| Glob | `Glob: {pattern} in {path}` | `Glob ✓ ({n} files)` |

#### Scenario: 记录 LLM 对话
- **WHEN** LLMCaller 发送 prompt 给 LLM
- **THEN** 自动记录 prompt 到 `se3/history/{flow_id}/{step_id}.jsonl`
- **AND** LLM 回应后记录解析后的 JSON 对象数组（`raw_json: list[dict]`）

#### Scenario: raw_json 格式存储
- **WHEN** LLM 返回 NDJSON 流（多行 JSON）
- **THEN** 将每行解析为 dict 并存储为数组
- **AND** 避免双重编码（不再将 JSON 转为字符串存储）
- **AND** 可直接用 jq 等工具查询历史记录

#### Scenario: 重试时注入对话上下文
- **WHEN** LLM 调用失败并重试
- **THEN** 从聊天记录中获取之前的对话上下文
- **AND** 将上下文注入到重试 prompt 前面
- **AND** 格式为 `[Previous conversation context for this step]: ... [The above attempt(s) failed.]`

#### Scenario: 工具调用语义化渲染
- **WHEN** LLM 流式输出包含 `tool_use` 或 `tool_result` 事件
- **OR** 聊天记录需要渲染历史中的工具调用
- **THEN** `StreamJSONTracker.process_line()` 解析 NDJSON 行：`tool_use` 块嵌套在 `type: "assistant"` 消息的 `message.content[]` 中；`tool_result` 块嵌套在 `type: "user"` 消息的 `message.content[]` 中（字段使用 snake_case：`tool_use_id`、`is_error`）
- **AND** 保留对 legacy 顶层 `type: "tool_result"` 格式的向后兼容处理（同时支持 `toolUseId`/camelCase 和 `tool_use_id`/snake_case）
- **AND** 共享的 `_handle_tool_result()` 辅助方法统一处理两种格式的 tool_result 逻辑，避免重复
- **AND** `format_tool_use_preview(tool_name, input_data)` 根据 `TOOL_FORMATTERS` 注册表路由到 per-tool 格式化函数
- **AND** `format_tool_result_preview(tool_name, result_data)` 同理路由到 per-tool 结果格式化函数
- **AND** Edit 工具显示文件路径和变更行数（e.g. `Edit: path/file.py (3 lines → 5 lines)`）
- **AND** Write 工具显示文件路径和内容行数
- **AND** Read 工具显示文件路径和读取范围
- **AND** Bash 工具显示命令截断预览
- **AND** Grep 工具显示搜索模式和路径
- **AND** Glob 工具显示匹配模式和路径
- **AND** 未注册的工具回退到通用格式化器（key=value 截断，最多 3 个参数）
- **AND** 格式化在 LLM 抽象层（tool_formatters 模块）完成，不依赖具体 agent 工具实现

#### Scenario: Edit/Write 工具 diff 渲染
- **WHEN** LLM 流式输出包含 Edit 或 Write 工具的 `tool_result` 事件
- **THEN** `StreamJSONTracker` 在 `tool_use` 事件时缓存 Edit/Write 工具的输入参数到 `_tool_use_id_to_input` 映射
- **AND** 对于 Write 工具，在 `tool_use` 事件时额外读取目标文件的当前内容并缓存到 `_tool_use_id_to_old_content` 映射（文件不存在或读取失败时缓存 `None`）
- **AND** 在 `tool_result` 事件时取出缓存的输入参数和原文件内容，调用 `format_tool_diff(tool_name, input_data, result_data, old_content=old_content)`
- **AND** Edit 工具：从 `old_string` / `new_string` 通过 `generate_edit_diff()` 生成 unified diff，调用 `display.render_diff()` 渲染红（删除）/绿（新增）/青（hunk 标记）/灰（上下文）着色的 diff 面板
- **AND** Write 工具（新建文件，`old_content` 为 `None`）：显示 `Created {file_path} ({n} lines)` 绿色标识，不展示行级 diff
- **AND** Write 工具（覆写已有文件，`old_content` 非 `None`）：通过 `generate_edit_diff(old_content, content, file_path)` 生成 unified diff 并渲染红/绿着色输出（文件 I/O 仅在 tracker 的 tool_use 阶段发生一次，formatter 层不访问文件系统）
- **AND** diff 超过 `max_lines`（默认 50 行）时截断并显示剩余行数摘要
- **AND** `display.render_diff()` 使用 Rich Panel + Text 对象逐行着色，面板标题为文件路径；每行添加 dim 样式的行号前缀（从 `@@ -a,b +c,d @@` hunk header 解析起始行号，删除行显示旧文件行号，新增行和上下文行显示新文件行号）；`total` 行数统计排除 `---`/`+++` 头部行
- **AND** 仅对 `TOOL_FORMATTERS` 注册表中包含 `diff` 键的工具执行 diff 渲染，其他工具为 no-op

#### Scenario: StreamJSONTracker 缓存管理
- **WHEN** `StreamJSONTracker` 缓存 tool_use 输入参数（`_tool_use_id_to_input`、`_tool_use_id_to_old_content`、`_tool_use_id_to_name`）
- **THEN** 正常流程中 `_handle_tool_result()` 的成功路径通过 `.pop()` 清理对应条目
- **AND** 错误路径（`is_error=True`）同样通过 `.pop()` 清理所有三个缓存字典中的对应条目
- **AND** `print_summary()` 方法在流结束时调用 `.clear()` 清空所有缓存字典，防止流异常中断时的内存泄漏
- **AND** 缓存容量限制为 `_MAX_CACHE_SIZE`（默认 100），超出时驱逐最旧条目（同时清理 `_tool_use_id_to_input`、`_tool_use_id_to_old_content` 和 `_tool_use_id_to_name`）

#### Scenario: 多组执行时流式输出添加组标识前缀
- **WHEN** implement step 分组执行（DAG parallel 或 sequential），多组分次调用 LLM
- **THEN** 每行 `[llm-stream]` 和 `[llm-caller]` 输出前添加组标识前缀，格式为 `[G1] [llm-stream] ...`
- **AND** 前缀通过 `LLMCaller` 构造函数的 `stream_prefix` 参数传入，再透传给 `StreamJSONTracker`
- **AND** `StreamJSONTracker` 在所有 `[llm-stream]` 打印行（tool_use、tool_result、error、summary）前插入 `stream_prefix`
- **AND** `LLMCaller` 在所有 `[llm-caller]` 打印行（Phase 2 提取、JSON 重试、缓存跳过等）前同样插入 `stream_prefix`
- **WHEN** 单组或单次 LLM 调用（无分组执行）
- **THEN** `stream_prefix` 为空字符串，不添加任何前缀，保持现有输出格式
- **WHEN** LOC 阈值触发多组合并为单次 LLM 调用
- **THEN** 不显示组前缀（单 LLM Call 只有一条执行流，合并组名前缀为冗余信息），与单组执行路径保持一致

#### Scenario: 人类浏览聊天记录
- **WHEN** 用户执行 `se3 history` 或 `se3 history list`
- **THEN** 展示所有 flow 的列表，聚合来自三个数据源：
  - `se3/state/engine.json` — 当前活跃 flow（source: active）
  - `se3/state/archive/engine_*.json` — 已归档的 flow（source: archived）
  - `se3/history/{flow_id}/` — 仅有聊天记录的历史 flow（source: history）
- **AND** 按 updated_at 降序排列，并展示 Source 列
- **AND** 支持 `--active-only` 和 `--archived-only` 过滤
- **AND** 支持 `--json` 输出 JSON 格式
- **AND** 支持按 flow_id 和 step_type 筛选查看
- **AND** 区分通讯 JSON（解析渲染）和 LLM 输出 JSON（原样展示）

#### Scenario: Prompt 段落自动分割
- **WHEN** `segment_prompt()` 处理一个 SE3 prompt 文本
- **THEN** 使用预编译的正则模式匹配已知段落标记（如 `CRITICAL: You MUST respond with ONLY valid JSON`、`READ-ONLY STEP CONSTRAINT`、`IMPORTANT: You MUST respond in`、`You are an expert`、`## Discovery Context`、`## Available Specifications`、`## Project Context`、`[Additional user instruction]` 等）
- **AND** 将 prompt 拆分为 `[{"title": str, "content": str}]` 数组
- **AND** 首段默认标题为 "Prompt"，后续段落标题由匹配的模式自动生成
- **AND** 通用 `## Heading` 模式作为 fallback 捕获未匹配的 markdown 二级标题

#### Scenario: 结构化详细渲染（非 verbose）
- **WHEN** `render_session_detailed(session, verbose=False)` 被调用
- **THEN** 返回 Rich renderables 列表
- **AND** 用户 prompt 按 `segment_prompt()` 分段，每段使用带标题的 Rich Panel 展示
- **AND** assistant response 仅展示最终 text block（通过 `_extract_final_text()` 提取最后一个 `type: "assistant"` 消息中最后一个 `type: "text"` 内容块）
- **AND** 若无 text 内容但有 tool 活动，fallback 到 `_render_ndjson_for_human()` 展示 tool 活动摘要
- **AND** 按 attempt 分组，多次 attempt 分开展示并标注序号

#### Scenario: 结构化详细渲染（verbose）
- **WHEN** `render_session_detailed(session, verbose=True)` 被调用
- **THEN** prompt 展示与非 verbose 模式相同（结构化分段）
- **AND** response 复用 `_render_ndjson_for_human()` 展示完整对话流，包括 text 内容和 tool 调用/结果摘要
- **AND** verbose 模式下 response 使用 `Text()` 而非 `Markdown()` 渲染，避免方括号格式（如 `[Edit: file.py]`）被误解析为 Rich markup

#### Scenario: 详细 JSON 输出
- **WHEN** `get_detailed_json(project_root, flow_id)` 被调用
- **THEN** 返回结构化数组，每个元素包含 `step_id`、`step_type` 和 `messages`
- **AND** user 消息包含 `segments`（`segment_prompt()` 分段结果）和原始 `content`
- **AND** assistant 消息包含 `content`（提取的文本）和 `raw_json`（原始 NDJSON 数据）

#### Scenario: Fix-loop implement iterations rendered as virtual per-iteration sessions
- **GIVEN** the state machine re-uses the same `implement` Step object across fix loop iterations (resetting `status=PENDING` rather than allocating a new step)
- **AND** this causes multi-iteration implement prompts to accumulate in a single on-disk history file (`se3/history/{flow_id}/04_implement_{id}.jsonl`), while each `test` / `self_check` iteration writes to its own file (05, 07, 09, 11, …)
- **WHEN** the display layer reads history for a flow via `interleave_sessions_for_display(sessions)` (used by both the Rich renderer in `history_cmd._show_detailed_sessions` and the programmatic `chat_history.get_detailed_json`)
- **THEN** each `implement` ChatSession is passed to `split_implement_session_by_iterations(session, test_timestamps)`, which partitions its messages into virtual per-iteration sessions using the first-message `timestamp` of each `test` session as a fence (iter1 = messages before the first test; iter{i+1} = messages between test[i-1] and test[i])
- **AND** each virtual session's `step_id` is the original implement step_id with a `-iter{N}` suffix appended (N starts at 1), for display titling only
- **AND** when the implement session has only one iteration (≤1 bucket), it is returned unchanged (no `-iter1` suffix injected) so non-fix-loop flows are unaffected
- **AND** all sessions (virtual implement splits plus untouched test/self_check/other sessions) are stable-sorted by first-message timestamp (tiebreaker: `step_id`), producing a chronological `implement-iter1 → test-1 → self_check-1 → implement-iter2 → test-2 → …` timeline
- **AND** on-disk history files are NOT rewritten, renamed, or split — this is a pure render-layer transformation, preserving backward compatibility with existing `engine.json` / `se3/history/` directories
- **NOTE** The underlying state-machine step-reuse behavior (`_transition_to_fix`) and the persistence file-naming convention are intentionally unchanged; the virtual split lives entirely in `chat_history` helpers consumed only by display paths

### Requirement: LLM Content Truncation Strategy

The flow engine SHALL apply content-aware truncation when feeding diagnostic output (stderr, stdout, tool results, step summaries) into LLM prompts, to maximize the LLM's ability to diagnose and fix issues within token constraints.

**Truncation Direction Policy:**
- **Error content** (stderr, error tool_results): tail-truncate (`content[-N:]`) — error root causes and tracebacks appear at the end
- **Non-error content** (stdout, normal tool_results): head-truncate (`content[:N]`) — context and setup appear at the start
- **Assistant responses** in retry/continue context: head+tail truncate (head 1000 chars + tail for remainder) — preserves initial step instructions and schema definitions at the start, plus final conclusions and tool results at the end
- **User prompts** in retry/continue context: not truncated (line-level deduplication controls size); a 50K-char safety cap prevents unbounded growth

**Minimum Truncation Limits for LLM-consumed content:**

| Context | Content Type | Min Chars | Direction |
|---------|-------------|-----------|-----------|
| Fix instructions (test, verify_spec) | stderr | 2000 | tail |
| Fix instructions (test, verify_spec) | failures section | 3000 | smart (per test block) |
| LLM prompt test results (verify_spec, self_check) | stderr per phase | 2000 | tail |
| LLM prompt test results (verify_spec, self_check) | stdout per phase | 2000 | tail |
| Chat history tool_result | content | 2000 | direction-aware |
| Chat history retry/continue | assistant response | 2000/4000 | head+tail (head 1000 + tail remainder) |
| JSON retry prompt (LLM caller) | previous response | 1500 | head |
| Test history record | stderr per phase | 2000 | tail |
| Test history record | stdout per phase | 2000 | tail |
| Loop iteration summaries | accumulated total | 8000 | FIFO eviction |
| Context.json step output values | string values | 1000 | head |
| Iteration summary diff (run.py) | git diff | 5000 | head |
| Salvage diff summary | git diff | 4000 | head |
| self_check prompt task_groups summary | plan task_groups markdown | 2000 | head |

**Shared Truncation Constants Module:**

Truncation limits consumed by step handlers (test, self_check, verify_spec) SHALL be defined as named constants in a shared `truncation.py` module (`se3/engine/truncation.py`), rather than hardcoded in each handler. This ensures consistency across handlers and provides a single location to adjust limits. Constants include `PHASE_STDOUT_TAIL_CHARS`, `PHASE_STDERR_TAIL_CHARS`, `TEST_HISTORY_STDOUT_TAIL_CHARS`, `TEST_HISTORY_STDERR_TAIL_CHARS`, `FIX_STDERR_TAIL_CHARS`, `FAILURES_SECTION_MAX_CHARS`, and `SELF_CHECK_TASK_GROUPS_MAX_CHARS`.

**Design rationale:** Stderr is the primary source of traceback and error diagnostics for LLM-driven fix loops. Previous limits (300-500 chars) were insufficient for a single Python traceback. The limits above ensure at least one complete error chain is preserved in all diagnostic contexts.

#### Scenario: Error content uses tail truncation
- **WHEN** the system truncates stderr or error tool_result content for LLM consumption
- **THEN** tail truncation (`content[-N:]`) is used to preserve the error root cause

#### Scenario: Assistant response uses head+tail truncation in retry context
- **WHEN** `format_history_for_retry()` truncates a previous assistant response
- **THEN** head+tail truncation is used: head (1000 chars) preserves step instructions and schema definitions, tail (remainder of budget) preserves final conclusions and tool results
- **AND** user prompts are preserved in full (with a 50K safety cap); repeated content is handled by `deduplicate_prompt_lines()` in LLMCaller

#### Scenario: Loop iteration summaries use FIFO eviction
- **WHEN** accumulated iteration summaries exceed the total character limit
- **THEN** earliest summaries are evicted first, replaced with a placeholder
- **AND** recent iteration summaries are preserved in full

#### Scenario: Truncation constants are centralized
- **WHEN** a step handler (test, self_check, verify_spec) truncates stdout or stderr content
- **THEN** the truncation limit is imported from the shared `truncation.py` module
- **AND** all handlers sharing the same truncation context use the same constant value

### Requirement: Prompt Line-Level Deduplication

The flow engine SHALL deduplicate repeated contiguous line blocks within a prompt before sending it to the LLM, to reclaim context window space wasted by spec content repeated across retry attempts.

**Deduplication Rules:**

- **Exact match**: Only lines that are character-for-character identical qualify — no fuzzy matching or lossy compression
- **Contiguous blocks**: Only contiguous blocks of >= `min_block_lines` (default 3) identical lines are deduplicated
- **First-occurrence preserved**: The first occurrence of a block in the prompt is kept verbatim; subsequent occurrences are replaced with a marker of the form `[DUPLICATED CONTENT: N lines #HASH, from "FIRST_LINE" to "LAST_LINE"]`, where HASH is a short content hash for disambiguation
- **Per-call independence**: Deduplication is performed independently for each LLM call; no cross-call state is maintained
- **Pure string operation**: The function operates on raw line text without understanding prompt structure or semantics
- **Blank-line exclusion**: Blocks consisting entirely of blank lines are not deduplicated
- **Marker passthrough**: Lines that are existing dedup markers (from prior dedup passes) are skipped during matching

**Integration Point:**

- The `deduplicate_prompt_lines(prompt, min_block_lines=3)` function is defined in an independent module (`se3/engine/prompt_dedup.py`), decoupled from retry logic, chat history, or any specific step handler
- Called in `LLMCaller._call_with_retry()` after `effective_prompt` is fully assembled (including retry context, extra prompt, read-only constraints) and before subprocess arguments are built
- Only applied on retries (`is_retry=True`); first calls have no internal repetition by definition
- `_record_prompt` records the deduped prompt, keeping history consistent with actual LLM input
- Failures in deduplication are caught and logged as warnings; the original prompt is used as fallback

**Design rationale:** Character-count truncation of user prompts (the prior approach) discards unique content that appears after repeated spec blocks (e.g., task-specific instructions following embedded specs). Line-level deduplication is a lossless alternative: it removes only provably identical content while preserving all unique portions of the prompt.

#### Scenario: First LLM call is a no-op
- **WHEN** `_call_with_retry()` makes the first call for a step (not a retry)
- **THEN** `deduplicate_prompt_lines()` is not invoked
- **AND** the prompt is passed to the LLM unchanged

#### Scenario: Retry deduplicates repeated spec content
- **WHEN** `_call_with_retry()` retries an LLM call and `effective_prompt` contains spec content repeated across retry context entries
- **THEN** `deduplicate_prompt_lines()` replaces subsequent occurrences of repeated blocks with dedup markers
- **AND** the first occurrence of each block is preserved verbatim
- **AND** all unique content (task instructions, error diagnostics) is preserved regardless of position

#### Scenario: Recorded prompt matches LLM input
- **WHEN** `_record_prompt()` saves the prompt to history after a retry call
- **THEN** the recorded prompt is the deduped version (same as what the LLM received)

### Requirement: 状态持久化与恢复

流程引擎 SHALL 将运行状态持久化为 JSON 文件（`se3/state/engine.json`），支持任意步骤中断后精确恢复。

**持久化内容：**
- 流程实例元数据（flow_id, task_description, task_type, status）
- 当前步骤状态（current_step_id, current_step_index）
- 已选步骤序列（selected_steps）
- 所有步骤历史（step_history, steps）
- 每个步骤的输入/输出

**原子写入：**
- 先写入临时文件，再 rename 到目标路径
- 避免写入中途中断导致状态文件损坏

#### Scenario: 中断恢复
- **WHEN** 流程在某步骤执行中被中断（ctrl-c、进程终止、系统崩溃）
- **AND** 用户重新执行 `se3 run`
- **THEN** 流程引擎从 JSON 状态文件恢复到中断前的步骤
- **AND** 提示用户当前恢复的位置和上下文

#### Scenario: Ctrl+C 中断注入
- **WHEN** 用户在中断时输入额外指令
- **THEN** 将指令注入到当前步骤的 LLM prompt 中
- **AND** 重新执行当前步骤

#### Scenario: Step outputs JSON serializability
- **WHEN** a step handler stores a result in `step.outputs`
- **THEN** the value MUST be a JSON-serializable primitive (string, number, bool, dict, list, or null)
- **AND** enum values (e.g. `StepStatus`) MUST be converted to their string `.value` before storing
- **AND** `json.dumps` calls that serialize step outputs SHOULD use `default=str` as a defensive fallback, consistent with `persistence.py`

### Requirement: 步骤间输入传递

流程引擎 SHALL 自动构建步骤输入，将前序步骤的输出传递给后续步骤。

**输入构建规则：**
- 所有步骤接收 `task_description` 和 `flow_id`
- `analyze` 输出 `task_type`、`scope`、`complexity`、`reasoning`、`project_summary`、`relevant_specs`、`spec_content`、`selected_specs`；其中 `project_summary` 由 `ProjectContextCollector.collect()` 程序化生成（非 LLM），`spec_content` 由后处理程序化加载（base spec 自动附加 + LLM 选择的 spec）
- `plan` 接收 `spec_content`（从 analyze）、`task_type`、`scope`、`project_summary`（从 analyze），输出 `plan`（含 proposal + design）、`task_groups` 和 `spec_changes`（仅 full depth）
- `implement` 接收 `design_doc`（从 plan.design 映射）、`task_groups`、`spec_content`（从 analyze）、`project_summary`（从 analyze）
- `self_check` 接收 `test_results`（从 test）、`changes_made`（从 implement）、`spec_content`（从 analyze）、`task_groups`（从 plan，用作「功能遗漏」维度的 scope 参考）、`fix_iteration`（当前 fix loop 迭代次数）
- `verify_spec` 接收 `changes_made`、`spec_content`（从 analyze）、`test_results`、`fix_iteration`、`spec_changes`（从 plan 步骤传递，用于区分有意变更与回归）和 `relevant_specs`（从 analyze）
- `update_spec` 接收 `changes_made`、`verification_result`、`spec_changes`（从 plan 步骤传递，作为变更指引清单）和 `design_doc`（从 plan.design 映射，提供架构上下文）
- `commit` 接收 `changes_made`、`commit_message`（from version_analyze）、`bump_type`（from version_analyze）
- `summarize` 接收所有前序输出（when included in step sequence）

#### Scenario: 步骤输入自动构建
- **WHEN** 流程转换到新步骤
- **THEN** 根据规则自动构建步骤输入
- **AND** 包含所有相关的前序输出

### Requirement: Version Analyze 步骤

`version_analyze` 步骤 SHALL 使用 LLM 智能分析实际变更内容，依据 Semantic Versioning 2.0.0 规则确定版本变更类型。

**分析输入：**
- `updated_specs`: Spec 变更（API 契约变化）- **主要判断依据**
- `changes_made`: 变更的文件列表和详细说明
- `verification_result`: 与 spec 的一致性检查结果
- `task_type`: 任务类型（作为参考，不作为决定因素）
- `task_description`: 原始任务描述
- `current_version`: 当前版本号

**分析输出：**
```json
{
  "bump_type": "major|minor|patch|none",
  "reasoning": "基于 SemVer 2.0.0 的详细解释",
  "confidence": "high|medium|low",
  "suggested_version": "X.Y.Z",
  "commit_message": "Concise imperative commit summary (max 72 chars)"
}
```

**commit_message 生成规则：**
- 使用祈使语气（如 "Add feature" 而非 "Added feature"）
- 以动词开头，描述实际完成的工作
- 最多 72 字符
- 不包含任务类型前缀（如 "feat:" 或 "fix:"）——前缀由 commit 步骤自动添加
- 当 version_analyze 未能提供 commit_message 时，commit 步骤按优先级回退：proposal summary → implement_summary → task description template

**决策规则：**
- **MAJOR**: 不兼容的 API 变更、删除功能、破坏性行为变更
- **MINOR**: 向后兼容的新功能、新增可选参数、功能增强
- **PATCH**: 向后兼容的 bug 修复、性能优化、内部重构
- **NONE**: 无版本价值的变更（仅格式化、注释等）

**Verification result formatting:**
- When `verification_result` includes an `issues` list, all issues SHALL be included in the LLM prompt (no display cap).
- Issue severity is read from the `priority` field (matching verify_spec's unified priority system: `critical/high/medium/low`), not the `severity` field.
- The summary counts critical/high priority issues as the primary indicator of unresolved problems.

#### Scenario: 智能版本分析识别破坏性变更
- **GIVEN** 任务类型为 `small`
- **AND** 实际变更删除了公共函数的参数
- **WHEN** `version_analyze` 步骤执行
- **THEN** LLM 识别为 breaking change
- **AND** 返回 `bump_type: major`

#### Scenario: Version analyze shows all verification issues
- **GIVEN** `verification_result` contains 15 issues of varying priority
- **WHEN** `version_analyze` formats the verification result for the LLM prompt
- **THEN** all 15 issues are included (no truncation to a fixed count)
- **AND** the summary line uses the `priority` field to count critical/high issues

#### Scenario: 低置信度处理
- **GIVEN** `version_analyze` 返回 `confidence: low`
- **AND** `auto_bump: true` (默认)
- **WHEN** 进入 commit 步骤
- **THEN** 系统仍应用建议的 bump 类型
- **AND** 记录警告日志

### Requirement: Commit 步骤版本管理

`commit` 步骤 SHALL 集成自动版本更新功能，根据 `version_analyze` 的结果自动 bump 版本号，并更新相关文档。commit 步骤不包含独立的 LLM 调用——commit message 来自 `version_analyze` 步骤。

**Commit Message Priority Chain:**
1. `commit_message` from `version_analyze` step (via `step.inputs`)
2. `proposal.summary` from plan step (fallback)
3. `implement_summary` from implement step (fallback)
4. Template from task description (last resort)

The commit step prepends the `task_type` prefix (e.g., `feature:`, `bugfix:`) to the message automatically.

**Template Summary Generation:**
- When the `summarize` step is NOT in the flow's step sequence, the commit step generates a template-based summary document at `se3/state/summary-{flow_id}.md`
- The template uses structured data from the flow state: commit message, changed files, test results, version info
- When the `version_analyze` step's `reasoning` field is available (non-empty) in `step.inputs`, the template includes a `### Version Analysis` section after the Version line and before the Commit Message section, containing the full reasoning text. Only the `reasoning` field is included — `bump_type` and `confidence` are not repeated here (version info is already shown in the Version line). If `reasoning` is absent or empty, this section is omitted.
- No LLM call is needed — this is a deterministic template operation
- When the `summarize` step IS in the sequence, it generates a richer LLM-based summary (existing behavior)

**版本更新流程：**
1. 检测项目类型（Python/Node.js）并定位版本文件（pyproject.toml/package.json）
2. 从 `version_analyze` 步骤获取 `bump_type` 和 `confidence`
3. 如果智能分析不可用或禁用，回退到基于任务类型的规则
4. 根据配置决定是否应用自动 bump（`auto_bump` 和 `confidence_threshold`）
5. 使用语义化版本规范（SemVer 2.0.0）计算新版本
6. 更新版本文件中的版本号
7. 自动更新 README.md 和 VERSIONS.md（如配置了模板）
8. 将版本文件和文档变更一起提交

**版本回滚机制：**
- 如果提交失败，自动回滚版本文件到原始版本
- 成功提交后清除备份，使版本变更永久生效

**配置选项（se3.yaml）：**
```yaml
version:
  enabled: true                    # 启用自动版本更新
  file_path: null                  # 版本文件路径（null=自动检测）
  include_in_commit_message: true  # 在提交消息中包含版本号
  
  # 智能版本分析
  smart_version_analysis: true     # 启用 LLM 分析
  auto_bump: true                  # 自动应用 bump（无需确认）
  confidence_threshold: null       # 置信度阈值（null=总是自动）
  
  # 回退规则（智能分析禁用时使用）
  bump_rules:
    feature: minor
    bugfix: patch
    breaking: major
  
  # 文档更新模板
  templates:
    readme_badge: "![Version](https://img.shields.io/badge/version-{version}-blue)"
    versions_entry: "## {version} - {date}\n\n{changes}\n"
```

#### Scenario: Feature 任务自动更新版本
- **GIVEN** 当前版本为 1.2.3
- **AND** `smart_version_analysis: true`
- **WHEN** `version_analyze` 分析变更后建议 `minor` bump
- **THEN** 版本自动 bump 为 1.3.0
- **AND** README.md 和 VERSIONS.md 自动更新
- **AND** 所有变更一起提交

#### Scenario: Bugfix 任务自动更新版本
- **GIVEN** 当前版本为 1.2.3
- **WHEN** 执行 bugfix 类型的任务
- **AND** `version_analyze` 返回 `bump_type: patch`
- **THEN** 版本自动 bump 为 1.2.4
- **AND** 提交消息包含新版本号

#### Scenario: 版本更新失败回滚
- **GIVEN** 版本已成功 bump 但提交失败
- **WHEN** commit 步骤检测到提交错误
- **THEN** 自动将版本文件回滚到原始版本
- **AND** 报告错误信息

### Requirement: 错误处理和重试

流程引擎 SHALL 提供错误处理和重试机制。

**错误处理策略：**
- 步骤失败时自动重试（最多 3 次）
- 超过重试次数后询问用户：重试、跳过、中止
- 用户可以选择跳过失败步骤继续执行

#### Scenario: 步骤失败重试
- **WHEN** 某个步骤执行失败
- **THEN** 自动重试该步骤
- **AND** 达到最大重试次数后询问用户

#### Scenario: 跳过失败步骤
- **WHEN** 用户选择跳过失败步骤
- **THEN** 将步骤标记为完成
- **AND** 继续执行后续步骤

### Requirement: plan estimated_loc Output

The `plan` step SHALL include an `estimated_loc` field (integer) in each task within task_groups, representing the estimated number of lines of code to be added or modified. This field is used by the `implement` step to decide execution strategy.

**Task output fields (in addition to existing fields):**
- `estimated_loc`: Integer — estimated lines of code. Used as an objective, quantitative measure.

The `complexity` field is preserved unchanged. `estimated_loc` is additive and does not replace any existing field.

#### Scenario: Task includes estimated_loc
- **WHEN** `plan` produces task_groups
- **THEN** each task includes an `estimated_loc` integer field
- **AND** the `complexity` field remains unchanged

### Requirement: Plan spec_changes Output

The `plan` step at full depth (task_type `feature` or `discovery`) SHALL output a `spec_changes` array declaring expected spec modifications. At medium and shallow depths (bugfix, directive, small), `spec_changes` is omitted from the JSON schema and defaults to an empty array.

**spec_changes entry schema:**
```json
{
    "spec_name": "flow-engine",
    "change_type": "add_requirement|modify_requirement|add_scenario|deprecate_requirement",
    "target": "Requirement: Example Requirement Name",
    "description": "What this change entails",
    "rationale": "Why this change is needed"
}
```

**Prompt composition:**
- At full depth, the plan prompt includes a "Spec Changes Declaration" section instructing the LLM to analyze the gap between current specifications and the planned implementation.
- The JSON schema at full depth includes the `spec_changes` array.
- The plan handler extracts `spec_changes` from the LLM response via `result.get("spec_changes", [])` and stores it in `step.outputs["spec_changes"]`.
- The state machine forwards `spec_changes` to downstream steps (`verify_spec`, `update_spec`) via `_build_step_inputs`.

**Data flow:**
- `plan` → `verify_spec`: `spec_changes` allows verify_spec to distinguish intentional deviations from regressions.
- `plan` → `update_spec`: `spec_changes` serves as a guided checklist for spec updates.

#### Scenario: Full-depth plan includes spec_changes
- **WHEN** the plan step executes at full depth (feature or discovery task)
- **THEN** the LLM prompt includes the "Spec Changes Declaration" section
- **AND** the output JSON schema includes the `spec_changes` array
- **AND** `step.outputs["spec_changes"]` contains the declared changes (may be empty)

#### Scenario: Non-full-depth plan omits spec_changes from schema
- **WHEN** the plan step executes at medium or shallow depth (bugfix, directive, small)
- **THEN** the LLM prompt does not include the "Spec Changes Declaration" section
- **AND** `step.outputs["spec_changes"]` defaults to an empty array

#### Scenario: spec_changes forwarded to downstream steps
- **WHEN** the state machine builds inputs for `verify_spec` or `update_spec`
- **THEN** `spec_changes` from the plan step is included in the inputs
- **AND** if no plan step completed or spec_changes was not produced, defaults to an empty array

### Requirement: verify_spec Unified Priority and Scope Mechanism

The `verify_spec` step SHALL use the unified issue priority system (`critical/high/medium/low`) and a scope dimension (`in_scope/out_of_scope`) to classify issues found during verification.

**Priority Levels (unified with issue system):**
- `critical`: Core functionality broken, data loss/corruption possible, security vulnerabilities introduced
- `high`: Requirement not met, specified behavior incorrect, or tests fail due to implementation bugs
- `medium`: Partial implementation gap, missing edge case handling, non-critical deviation from spec
- `low`: Minor style issues, documentation gaps, suggestions for improvement that don't affect correctness

**Scope Dimension:**
- `in_scope`: Issue directly introduced by current task's implementation, or current task claims to address it but has not. Blocks current flow and must be fixed.
- `out_of_scope`: Pre-existing problem discovered during verification, or relates to functionality outside current task boundaries. Filed as an issue via `IssueManager.create()`, does not block current flow.

**verified Field (Rule-Based Computation):**
- The `verified` field is NOT determined by LLM output — it is computed by code: `verified = (in_scope_count == 0) and tests_passed`
- If the LLM outputs a `verified` field, it is ignored/overridden by the rule-based computation
- This eliminates inconsistency between displayed verification status and actual flow behavior

**REVISION_NEEDED Logic:**
- Triggered when `in_scope_count > 0` (spec compliance issues) OR `tests_passed == False` (test failures)
- verify_spec handler always returns REVISION_NEEDED when issues are found (does not check exhaustion internally)
- Exhaustion detection is centralized in `state_machine.transition_to_next()`: when `fix_iteration >= max_fix_iterations` (default 20), the flow is set to FAILED status, an A-class issue is generated, and execution stops

**Out-of-Scope Issue Filing:**
- Out-of-scope issues are deterministically filed via `IssueManager.create()` with the issue's priority, tagged with `auto-discovered`, `source:verify-spec`, and `out-of-scope`
- This replaces the probabilistic B-class discovery mechanism for verify_spec

**Planned Change Awareness:**

The `verify_spec` step SHALL receive `spec_changes` from the plan step and use it to distinguish intentional spec deviations from unintended regressions.

- When `spec_changes` is non-empty, the verify_spec prompt instructs the LLM to treat deviations matching plan-declared changes as intentional (low priority, out_of_scope), not regressions.
- Deviations NOT covered by planned changes are still flagged at their normal priority.
- When `spec_changes` is empty (e.g., bugfix tasks), all deviations from spec are evaluated at their normal priority.

**Prompt section:**
The "Planned Spec Changes" section is formatted into the prompt using `_format_spec_changes()`, which renders each entry as `- [change_type] spec_name :: target` with an optional description line.

#### Scenario: In-scope issue triggers REVISION_NEEDED
- **GIVEN** verify_spec finds an issue classified as `in_scope` with priority `high`
- **THEN** verify_spec returns REVISION_NEEDED
- **AND** `verified` is computed as `False`
- **NOTE** verify_spec no longer checks exhaustion internally; exhaustion is handled by the state machine

#### Scenario: Out-of-scope issue does not block flow
- **GIVEN** verify_spec finds an issue classified as `out_of_scope` with priority `medium`
- **WHEN** no in-scope issues exist and tests pass
- **THEN** verify_spec returns COMPLETED
- **AND** `verified` is computed as `True`
- **AND** the out-of-scope issue is filed via `IssueManager.create()`

#### Scenario: Intentional deviation classified as low/out_of_scope
- **GIVEN** the plan step declared a spec_change: `[add_requirement] flow-engine :: Requirement: New Feature X`
- **AND** the implementation introduces behavior not covered by current specs but matching the declared change
- **WHEN** verify_spec executes
- **THEN** the deviation is classified as low priority, out_of_scope
- **AND** verify_spec does NOT trigger REVISION_NEEDED for this deviation

#### Scenario: Unplanned deviation classified as in_scope
- **GIVEN** the plan step declared no spec_changes (or the deviation does not match any declared change)
- **AND** the implementation deviates from the current spec
- **WHEN** verify_spec executes
- **THEN** the deviation is classified at its normal priority with scope `in_scope`
- **AND** verify_spec may trigger REVISION_NEEDED

#### Scenario: Empty spec_changes preserves existing behavior
- **GIVEN** the plan step produced an empty `spec_changes` array (e.g., bugfix task)
- **WHEN** verify_spec executes
- **THEN** all deviations from spec are evaluated at their normal priority with scope determined by the LLM

#### Scenario: verified field computed by rule
- **GIVEN** LLM outputs `verified: true` but `in_scope_count > 0`
- **WHEN** verify_spec processes the result
- **THEN** `verified` is overridden to `False` by the rule: `(in_scope_count == 0) and tests_passed`

### Requirement: update_spec Guided Execution

The `update_spec` step SHALL receive `spec_changes` and `design_doc` from the plan step, shifting from pure inference mode to guided execution when guidance is available.

**Two modes:**
1. **Guided mode** (when `spec_changes` is non-empty): The LLM uses `spec_changes` as a primary checklist, executing each declared change intent (add, modify, deprecate) in the corresponding spec files. `design_doc` provides architectural rationale to produce more accurate and well-motivated spec updates.
2. **Inference mode** (when `spec_changes` is empty): The LLM determines which specs need updating by analyzing the changes made and verification results, as before.

**Prompt composition:**
- The "Spec Change Guidance" section is formatted using `_format_spec_changes()` which renders each entry with spec_name, change_type, target, description, and rationale.
- The "Design Context" section is formatted using `_format_design_doc()` which renders the design overview, components, and architecture decisions.

#### Scenario: Guided spec update with spec_changes
- **GIVEN** the plan step produced non-empty `spec_changes` and a `design_doc`
- **WHEN** update_spec executes
- **THEN** the LLM uses `spec_changes` as the primary checklist for updates
- **AND** the LLM uses `design_doc` to understand architectural rationale
- **AND** spec updates align with the declared change intents

#### Scenario: Inference spec update without guidance
- **GIVEN** the plan step produced empty `spec_changes` (e.g., bugfix task)
- **WHEN** update_spec executes
- **THEN** the LLM infers which specs need updating from changes_made and verification_result
- **AND** update_spec behavior is identical to pre-refactoring

### Requirement: Implement Step DAG Execution Strategy

The `implement` step SHALL use an intelligent execution strategy that adapts based on total estimated lines of code and DAG topology.

**Execution Strategy Selection:**
- If there is exactly one task group, it is executed as a single LLM call directly (no threshold comparison needed).
- If there are multiple groups, the implement step computes total `estimated_loc` across all tasks in all groups (tasks missing the field default to 50 LOC each).
  - If total LOC ≤ `implement.group_loc_threshold` (default: 300, configurable in `se3.yaml`), all groups are merged into a single LLM call regardless of grouping.
  - If total LOC > threshold, groups are executed according to the DAG parallel strategy.
- The `plan` step grouping principles (high cohesion, low coupling) are preserved — the implement step only decides whether to collapse groups at execution time.

#### Scenario: Single group uses single LLM call directly
- **GIVEN** `plan` produced exactly 1 group with estimated_loc = 141
- **WHEN** the implement step executes
- **THEN** the group is executed as a single LLM call without LOC threshold comparison

#### Scenario: Small multi-group implementation collapses groups
- **GIVEN** `plan` produced 3 groups with total estimated_loc = 180
- **AND** `implement.group_loc_threshold` is 300
- **WHEN** the implement step executes
- **THEN** all groups are merged into a single LLM call

#### Scenario: Large implementation uses DAG parallel
- **GIVEN** `plan` produced 4 groups with total estimated_loc = 500
- **WHEN** the implement step executes
- **THEN** groups are executed via DAG parallel strategy with relay branching

**Transitive Reduction:**
- Before DAG parallel execution, the implement step performs transitive reduction on group `depends_on` edges.
- An edge u→v is redundant if there is a longer path from u to v through intermediate nodes (standard graph theory algorithm using BFS).
- Example: G2 depends on [G1], G3 depends on [G1, G2] → after reduction: G3 depends on [G2] only (G1 is reachable through G2).
- This reduces unnecessary pre-merge operations and wait times.
- After reduction, the step logs which redundant edges were removed per group (group ID and sorted list of removed deps), or logs that no redundant edges were found.

#### Scenario: Transitive reduction removes redundant edges
- **GIVEN** G2 depends on [G1] and G3 depends on [G1, G2]
- **WHEN** transitive reduction is applied
- **THEN** G3's depends_on becomes [G2] only
- **AND** a log entry reports that G3 removed redundant dep [G1]

#### Scenario: Transitive reduction finds no redundant edges
- **GIVEN** all `depends_on` edges are minimal (no transitive shortcuts)
- **WHEN** transitive reduction is applied
- **THEN** no edges are removed
- **AND** a log entry reports that no redundant edges were found

**Branch Relay Strategy:**
- The implement step uses a branch relay strategy instead of per-group branch creation with pre-merge and merge-back.
- For linear chains (G1 → G2 → G3): G1 creates a worktree; G2 reuses G1's worktree/branch; G3 reuses G2's. Only the chain endpoint merges back.
- For forks (G1 → G2, G1 → G3): G1 executes; G2 (primary heir, lowest group_order) reuses G1's worktree; G3 forks G1's branch into a new worktree. G2 and G3 execute in parallel.
- For convergence points (G2, G3 → G4): G4 inherits the primary predecessor's worktree and merges secondary predecessor branches before executing.
- The relay plan is produced by `classify_chains()` which computes `RelayPlan` containing: relay_map, fork_from, leaf_nodes, convergence_points, and root_nodes.

#### Scenario: Linear chain relay
- **GIVEN** groups form a linear chain G1 → G2 → G3
- **WHEN** DAG parallel executes
- **THEN** G1 creates a new worktree
- **AND** G2 reuses G1's worktree and branch
- **AND** G3 reuses G2's worktree and branch
- **AND** only G3 (leaf) merges back to the original branch

#### Scenario: Fork relay
- **GIVEN** G1 has two dependents G2 and G3 (G2 has lower group_order)
- **WHEN** DAG parallel executes
- **THEN** G2 reuses G1's worktree (primary heir)
- **AND** G3 forks G1's branch into a new worktree
- **AND** G2 and G3 execute in parallel

#### Scenario: Convergence point
- **GIVEN** G4 depends on both G2 and G3
- **WHEN** G4 is about to execute
- **THEN** G4 inherits the primary predecessor's worktree
- **AND** merges secondary predecessor branches into it before executing

**Leaf-Only Merge with LLM Conflict Resolution:**
- Only leaf nodes (groups with no downstream dependents) merge back to the original branch.
- Merge conflicts are resolved by LLM with full context including: task descriptions, per-group summaries and files_changed, conflicting file content with conflict markers, and spec content.
- The LLM resolver retries up to 3 times on failure.
- There is no fallback to `--theirs` or `pending_human` — the LLM must resolve all conflicts.

#### Scenario: Leaf merge succeeds
- **GIVEN** a leaf group completed its work
- **WHEN** merging back to the original branch
- **THEN** a standard git merge is attempted
- **AND** if no conflicts, the merge completes normally

#### Scenario: Leaf merge with LLM conflict resolution
- **GIVEN** a leaf group's merge produces conflicts
- **WHEN** the merge conflict handler runs
- **THEN** the LLM receives full context (task descriptions, group summaries, conflict markers, specs)
- **AND** the LLM resolves all conflicting files
- **AND** the resolver retries up to 3 times if needed
- **AND** there is no fallback to `--theirs` or `pending_human`

**DAG Branch Cleanup:**
- After all leaf merges complete, the DAG parallel strategy cleans up implementation branches.
- Branch names for cleanup SHALL be collected from actual `GroupResult.branch_name` values (not constructed from group IDs), because relay chains cause downstream groups to reuse a root group's branch rather than having their own.
- Recovered group branches (from `prior_outputs.implemented_groups` during DAG resume) that were already deleted during the recovery phase SHALL be tracked and skipped during final cleanup, preventing duplicate deletion errors.
- Recovered group branches that were NOT successfully deleted during recovery SHALL still be included in final cleanup as a fallback.

#### Scenario: DAG branch cleanup uses actual branch names
- **GIVEN** a relay chain where G2 reuses G1's branch
- **WHEN** final branch cleanup runs after leaf merges
- **THEN** branch names are collected from each GroupResult's `branch_name` field
- **AND** duplicate branch names are deduplicated (via set)
- **AND** no "branch not found" error occurs for groups sharing a relay branch

#### Scenario: DAG resume skips already-deleted branches in final cleanup
- **GIVEN** a DAG resume recovered groups G2 and G4 from prior_outputs
- **AND** their branches were successfully merged and deleted during the recovery phase
- **WHEN** final branch cleanup runs
- **THEN** G2 and G4 branches are skipped because they were already deleted
- **AND** no "branch not found" error occurs

#### Scenario: DAG resume retains fallback for failed recovery deletions
- **GIVEN** a DAG resume recovered group G3 from prior_outputs
- **AND** branch deletion failed during the recovery phase
- **WHEN** final branch cleanup runs
- **THEN** G3's branch is included in final cleanup as a fallback attempt

**DAG Worktree History Management:**
- Before a worktree executes, `_restore_history_to_worktree` copies LLM chat history from the main repo into the worktree so that retry context injection works inside the worktree.
  - Only non-group files (e.g. `discovery`, `analyze`, `plan` step history) are copied; files matching `_G\d+\.jsonl$` are **skipped** to prevent them being double-appended when the worktree is later salvaged.
- After a worktree finishes, `_salvage_history_from_worktree` copies history files back to the main repo before the worktree is removed.
  - Only files whose names match `_G\d+\.jsonl$` (i.e. group-specific history generated by this worktree) are salvaged; all other files are skipped.
  - This prevents shared prior-step history files (discovery, analyze, plan, confirm, etc.) from being appended to the main repo N times — once per worktree — which would cause them to display duplicated content in `se3 history show`.
- If the target file already exists in the main repo (relay chain scenario where multiple `GroupResult` objects share the same worktree), the salvaged file's content is appended (NDJSON is line-based, safe to concatenate).
- The salvage function deduplicates worktree paths so that a single worktree is only salvaged once even when shared by multiple groups.

#### Scenario: Salvage only copies group-specific history files
- **GIVEN** a worktree has completed execution
- **AND** its `se3/history/{flow_id}/` directory contains both shared files (e.g. `discovery.jsonl`, `analyze.jsonl`) and group-specific files (e.g. `implement_G2.jsonl`)
- **WHEN** `_salvage_history_from_worktree` runs
- **THEN** only files matching `_G\d+\.jsonl$` are copied to the main repo
- **AND** shared prior-step files are skipped

#### Scenario: Restore only copies shared context files to worktree
- **GIVEN** a worktree is about to be created for group G3
- **AND** the main repo has history files including `implement_G1.jsonl` and `analyze.jsonl`
- **WHEN** `_restore_history_to_worktree` runs
- **THEN** only non-group files (e.g. `analyze.jsonl`) are copied into the worktree
- **AND** `implement_G1.jsonl` is skipped (matches `_G\d+\.jsonl$`)

#### Scenario: Relay chain worktrees share the same worktree path
- **GIVEN** a relay chain where G2 reuses G1's worktree
- **AND** both G1 and G2 produce `GroupResult` objects referencing the same worktree_path
- **WHEN** history salvage runs after all groups complete
- **THEN** the shared worktree is only salvaged once (deduplication by worktree_path)

### Requirement: Worktree Cleanup Resilience

The worktree cleanup subsystem SHALL be resilient to cascading failures. `force_cleanup_worktree` executes a multi-step cleanup pipeline where each step is independently fault-tolerant — a single step timing out or raising an exception MUST NOT prevent subsequent steps from executing.

**Cleanup Pipeline (6 steps):**
1. Unlock the worktree (ignore errors if not locked)
2. `git worktree remove -f -f` (double-force removal)
3. Remove the worktree directory via `shutil.rmtree`
4. `git worktree prune` (prune stale entries)
5. Direct `.git/worktrees/<safe_name>` metadata removal as last resort
6. Verification — check whether the worktree is still registered

**Fault Tolerance:**
- Steps 1, 2, and 4 (git commands) each catch `TimeoutExpired` and general `Exception` independently
- Steps 3, 5, and 6 each catch `Exception` independently
- A failure in any step is logged at WARNING level but does not abort the pipeline

**Timeout:**
- All `_run_git` calls within `force_cleanup_worktree` SHALL use a 60-second timeout (overriding the default 30s), to accommodate slow filesystem or lock-release operations

**Git Worktree Metadata Cleanup:**
- `_cleanup_git_worktree_metadata` directly removes the `.git/worktrees/<safe_name>` directory using `shutil.rmtree`
- This bypasses standard git commands and is used only as a last resort after Steps 1–4
- If the metadata directory does not exist, it is a no-op
- Removal failures are logged at WARNING level but do not raise

**Branch Deletion Worktree Verification:**
- `delete_branch` SHALL check whether a worktree is still registered for the target branch before attempting deletion
- If a worktree is detected, `force_cleanup_worktree` is invoked first
- After cleanup, a re-check verifies the worktree is gone; if it persists, a warning is logged
- Branch deletion proceeds regardless of worktree cleanup outcome

#### Scenario: Force cleanup with Step 1 timeout
- **GIVEN** a worktree exists for a branch
- **WHEN** `force_cleanup_worktree` runs and Step 1 (unlock) times out
- **THEN** Steps 2–6 still execute
- **AND** the worktree is cleaned up by subsequent steps

#### Scenario: Force cleanup with Step 2 exception
- **GIVEN** a worktree exists for a branch
- **WHEN** `force_cleanup_worktree` runs and Step 2 (remove) raises an exception
- **THEN** Steps 3–6 still execute
- **AND** the directory is removed by Step 3 and metadata by Step 5

#### Scenario: Git worktree metadata cleanup removes stale metadata
- **GIVEN** `.git/worktrees/<safe_name>` exists but standard git commands failed to remove it
- **WHEN** Step 5 (metadata cleanup) runs
- **THEN** the metadata directory is deleted via `shutil.rmtree`

#### Scenario: Git worktree metadata absent
- **GIVEN** `.git/worktrees/<safe_name>` does not exist
- **WHEN** `_cleanup_git_worktree_metadata` is called
- **THEN** it returns immediately with no side effects

#### Scenario: Branch deletion with lingering worktree
- **GIVEN** a worktree is still registered for a branch
- **WHEN** `delete_branch` is called
- **THEN** `force_cleanup_worktree` runs before the `git branch -D` command
- **AND** the worktree registration is re-checked after cleanup
- **AND** the branch is deleted regardless of worktree cleanup outcome

#### Scenario: Branch deletion without worktree
- **GIVEN** no worktree is registered for a branch
- **WHEN** `delete_branch` is called
- **THEN** the branch is deleted directly without invoking `force_cleanup_worktree`

### Requirement: Implement-Test 契约

implement 步骤 SHALL 在输出中声明 `tests_added` 和 `test_mapping`，形成与 test 步骤的显式契约。

**输出字段：**
- `tests_added`: 列表，本次新增的测试文件路径（相对于项目根目录）
- `test_mapping`: 字典，键为测试 ID，值为 spec scenario 标识（`{spec_name}::{scenario_name}`）

**测试 ID 格式（语言相关）：**
| 语言 | 格式 | 示例 |
|------|------|------|
| Python (pytest) | `file::function` | `tests/test_auth.py::test_login_success` |
| JavaScript (jest/vitest) | `file > describe > it` | `tests/auth.test.js > LoginService > authenticates user` |
| Go | `package.TestFunc` | `auth.TestLoginSuccess` |
| Rust | `module::test_func` | `auth::test_login_success` |

**Base Spec 约定引用：**
- 测试文件的放置和命名遵循 base spec 的 Coding Conventions 和 Directory Structure

#### Scenario: implement 步骤声明新增测试
- **WHEN** implement 步骤完成实现
- **THEN** 输出包含 `tests_added` 列表
- **AND** 输出包含 `test_mapping` 字典

#### Scenario: 无新增测试的实现
- **WHEN** implement 步骤完成但未新增测试文件
- **THEN** `tests_added` 为空列表
- **AND** `test_mapping` 为空字典

### Requirement: Implement Step Output Rendering

The `implement` step SHALL use a custom renderer that presents structured, human-readable output instead of raw JSON key-value listing.

**Status Bar:**
- A top-line status bar shows completion status with visual icons: `✓` (complete/green), `◐` (partial/yellow), `✗` (failed/red).
- The status bar includes counters for groups, files changed, and tests added.

**Sections (displayed in order when data is present):**
1. **Summary** — semicolon-delimited per-group breakdown, each part prefixed with its group ID (e.g. `G1.`, `G2.`).
2. **Files Changed** — files grouped by top-level directory (e.g. `src/`, `tests/`), with per-directory counts. Files without a directory component are grouped under `./` (sorted last).
3. **Tests Added** — list of new test file paths.
4. **Incomplete Tasks** — tasks that were not completed, showing task ID and reason/error.
5. **Restricted Edits** — counts of applied restricted edits, and details of any that failed.
6. **Error** — step-level error message if present.

**Output keys consumed by the renderer:**
- `completion_status`, `files_changed`, `tests_added`, `implemented_groups`, `summary`, `incomplete_tasks`, `restricted_edits_applied`, `restricted_edits_failed`

#### Scenario: Successful implementation rendering
- **WHEN** the implement step completes with `completion_status: "complete"`
- **THEN** the renderer displays a green `✓ Complete` status bar
- **AND** files are grouped by directory under "Files Changed"
- **AND** tests are listed under "Tests Added" if any exist

#### Scenario: Partial implementation rendering
- **WHEN** the implement step completes with `completion_status: "partial"`
- **THEN** the renderer displays a yellow `◐ Partial` status bar
- **AND** incomplete tasks are listed with their task IDs and reasons

### Requirement: Implement Step Task Plan Display

The `implement` step SHALL display a structured task plan panel at the start of execution, before any LLM calls are made, across all execution paths (single-call, LOC-merged single-call, DAG parallel, sequential).

**Panel Contents:**

The plan is rendered as a Rich `Panel` titled "Implementation Plan" containing up to four sections:

1. **Strategy Line** — shows the selected execution strategy with a visual icon:
   - `⚡ Single group → single LLM call` — when there is only one task group (with total LOC)
   - `⚡ Single LLM call` — when multiple groups are merged under the LOC threshold (with LOC and threshold info)
   - `🔀 DAG parallel` — when using DAG parallel execution (with LOC, threshold, and group count)
   - `📋 Sequential` — when executing groups sequentially (with group count)

2. **Task Groups Tree** — a hierarchical tree labeled "Task Groups" showing:
   - Each group with its ID, name, and task count
   - Group dependencies (if any)
   - Each task within its group, displaying: task ID, description, complexity badge (color-coded: green/small, yellow/medium, red/large), and estimated LOC

3. **Execution Topology** (DAG parallel only) — a layered diagram showing how groups are scheduled across execution waves, rendered using Rich Text with ANSI styling. Only displayed when the execution strategy is `dag_parallel` and a relay plan is available:
   - Groups are arranged into topologically-sorted **waves**; groups within the same wave execute in parallel.
   - Each wave is labeled with its sequential **LLM call numbers** (e.g., `LLM #1, #2`).
   - Each group node is annotated with its relationship type:
     - `● root` (green) — creates a new worktree
     - `→ relay` (blue) — reuses the predecessor's worktree (linear chain continuation)
     - `⑂ fork` (magenta) — forks a new branch from a predecessor's worktree
     - `⊕ merge ←` (yellow) — convergence point that merges secondary predecessor branches before executing
     - `◆ leaf` (yellow) — chain endpoint that merges back to the base branch
   - Waves are connected by `│▼` vertical connectors.

4. **LOC Summary** — total estimated LOC with per-group LOC distribution

**Error Handling:**
- Display failures are caught and logged at DEBUG level; they SHALL NOT block execution.

#### Scenario: Task plan displayed before single-group execution
- **GIVEN** `plan` produced exactly one task group
- **WHEN** the implement step begins execution
- **THEN** the task plan panel is displayed with "Single group → single LLM call (N LOC)" strategy
- **AND** the panel shows the task group and LOC summary before the LLM call starts

#### Scenario: Task plan displayed before LOC-merged single-call execution
- **GIVEN** `plan` produced multiple groups with total LOC ≤ threshold
- **WHEN** the implement step begins execution
- **THEN** the task plan panel is displayed with "Single LLM call (N LOC ≤ T threshold)" strategy
- **AND** the panel shows all task groups and LOC summary before the LLM call starts

#### Scenario: Task plan displayed before DAG parallel execution
- **GIVEN** `plan` produced groups with total LOC > threshold and DAG topology
- **WHEN** the implement step begins execution
- **THEN** the task plan panel is displayed with "DAG parallel" strategy
- **AND** the panel shows group dependencies and per-group LOC estimates
- **AND** the panel includes an Execution Topology section showing waves, LLM call numbering, and relay/fork/merge annotations

#### Scenario: Task plan display failure does not block execution
- **GIVEN** the task plan rendering raises an exception
- **WHEN** the implement step attempts to display the plan
- **THEN** the exception is caught and logged at DEBUG level
- **AND** execution proceeds normally without the plan display

### Requirement: Step Output Rendering — Analyze, Self Check, Verify Spec, Update Spec, Commit

The `analyze`, `self_check`, `verify_spec`, `update_spec`, and `commit` steps SHALL each use a custom renderer that presents structured, human-readable output instead of raw JSON key-value listing. All renderers use `render_full()` as their sole output interface, consistent with the Implement renderer's style.

#### Analyze Renderer

The `analyze` step renderer SHALL display a top-line status bar followed by reasoning and relevant specs.

**Status Bar:**
- A single line showing `task_type`, `complexity`, and `scope` separated by `│` delimiters (e.g. `feature  │  medium  │  src/engine`).

**Sections (displayed in order when data is present):**
1. **Reasoning** — the analysis reasoning as a body paragraph.
2. **Relevant Specs** — a bullet list showing only spec names (extracted from `name` or `spec_name` fields of spec objects).

**Hidden fields:** `spec_content` and `project_summary` are intentionally omitted from display — they are downstream data payloads, not user-facing information.

**Output keys consumed by the renderer:**
- `task_type`, `complexity`, `scope`, `reasoning`, `relevant_specs`

##### Scenario: Analyze rendering with all fields
- **WHEN** the analyze step completes with `task_type`, `complexity`, `scope`, `reasoning`, and `relevant_specs`
- **THEN** the renderer displays a status bar with task_type, complexity, and scope
- **AND** reasoning is shown as a labeled body paragraph
- **AND** relevant specs are listed by name only

##### Scenario: Analyze rendering hides internal data
- **WHEN** the analyze step outputs include `spec_content` or `project_summary`
- **THEN** these fields are not displayed to the user

#### Self Check Renderer

The `self_check` step renderer SHALL display review status and issues grouped by severity.

**Status Line:**
- `✗ FAILED` in red when `step.status` is `FAILED` (takes precedence over all other conditions)
- `✓ PASSED` in green when no actionable issues found; `✗ ISSUES FOUND` in red when any actionable issues exist (all severity levels are actionable).

**Sections (displayed in order when data is present):**
1. **Issues by severity** — issues grouped by severity level (critical, high, medium, low). Each issue shows its description and location.

**Output keys consumed by the renderer:**
- `status`, `issues`

##### Scenario: Self check step failed rendering
- **WHEN** the self_check step has `step.status == FAILED` (e.g., LLM call failed before producing outputs)
- **THEN** the renderer displays a red `✗ FAILED` status
- **AND** does not display `✓ PASSED` regardless of `actionable_count` defaulting to 0

##### Scenario: Self check passed rendering
- **WHEN** the self_check step completes successfully with no issues
- **THEN** the renderer displays a green `✓ PASSED` status

##### Scenario: Self check found issues rendering
- **WHEN** the self_check step completes with any severity issues
- **THEN** issues are grouped by severity with appropriate color coding
- **AND** each issue shows its description and location

#### Verify Spec Renderer

The `verify_spec` step renderer SHALL display verification status, issues grouped by severity, and recommendations.

**Status Line:**
- `✓ PASSED` in green when `verified` is true; `✗ FAILED` in red when false.

**Sections (displayed in order when data is present):**
1. **Summary** — verification summary as body text.
2. **Issues by severity** — issues grouped into `error` (red prefix), `warning` (yellow prefix), and `info` (dim prefix) sections. Each issue shows its `message`, and optionally a `suggestion` line below it.
3. **Recommendations** — a bullet list of recommendations.

**Hidden fields:** `fix_context`, `fix_iteration`, `fix_needed`, `max_fix_iterations`, `verification_result` and other internal mechanism fields are omitted from display.

**Field fallback:** `summary` and `recommendations` are first read from top-level `outputs`; if absent or empty, the renderer falls back to reading them from `outputs["verification_result"]` (a nested dict produced by the verify_spec handler). This ensures the renderer works regardless of whether these fields are extracted to the top level.

**Output keys consumed by the renderer:**
- `verified`, `summary`, `issues`, `recommendations`, `verification_result` (fallback source)

##### Scenario: Spec verification passed
- **WHEN** the verify_spec step completes with `verified: true`
- **THEN** the renderer displays a green `✓ PASSED` status

##### Scenario: Spec verification failed with issues
- **WHEN** the verify_spec step completes with `verified: false` and issues of mixed severity
- **THEN** issues are grouped by severity (error, warning, info) with appropriate color coding
- **AND** each issue shows its message and optional suggestion

#### Update Spec Renderer

The `update_spec` step renderer SHALL display updated specs as a checklist or a no-update message.

**When specs were updated:**
- Each updated spec is shown as `✓ spec-name: change description` (green checkmark, bold name).
- `new_capabilities` are listed as a bullet list under a "New Capabilities" heading.

**When no specs were updated:**
- Displays `No spec updates needed` as a dim message.

**Output keys consumed by the renderer:**
- `updated_specs` (or `specs_updated`), `new_capabilities`

##### Scenario: Specs updated rendering
- **WHEN** the update_spec step completes with one or more entries in `updated_specs`
- **THEN** each spec is rendered as a `✓ name: description` line

##### Scenario: No spec updates
- **WHEN** the update_spec step completes with empty `updated_specs` and empty `new_capabilities`
- **THEN** the renderer displays `No spec updates needed`

#### Commit Renderer

The `commit` step renderer SHALL display commit details or a no-changes message.

**When committed is false:**
- Displays `No changes to commit` as a dim message.

**When committed is true:**
- Top line shows the short commit hash (first 7 characters), optionally followed by the version (e.g. `v1.2.3`) if `version_bumped` is true.
- Commit message is displayed below a separator.

**Output keys consumed by the renderer:**
- `committed`, `commit_hash`, `commit_message`, `version_bumped`, `version`

##### Scenario: Commit with version bump
- **WHEN** the commit step completes with `committed: true` and `version_bumped: true`
- **THEN** the renderer displays the short hash and version on the top line
- **AND** the commit message is displayed below

##### Scenario: No changes committed
- **WHEN** the commit step completes with `committed: false`
- **THEN** the renderer displays `No changes to commit`

### Requirement: Test 步骤配置与多阶段执行

test 步骤 SHALL 支持通过 `se3.yaml` 的 `test:` 配置段进行多阶段测试，并输出结构化结果。

**se3.yaml 配置：**
```yaml
test:
  command: null                # 主测试命令（null=自动检测）
  timeout: 1800                # 秒
  phases:                      # 额外测试阶段
    - name: "e2e"
      command: "python -m pytest tests/e2e -v"
      cwd: null                # 工作目录（null=项目根目录，支持绝对/相对路径）
      timeout: 600
      required: false          # false=失败只警告
      in_fix_loop: false       # false=fix loop 中跳过
```

**结构化输出：**
```json
{
  "new_tests": {"passed": [...], "failed": [...], "count": 0},
  "regression": {"passed": [...], "failed": [...], "count": 0},
  "phases": [{"name": "default", "passed": true, ...}],
  "overall_passed": true
}
```

**分类逻辑：**
- `new_tests`: 文件路径匹配 implement 步骤的 `tests_added`
- `regression`: 其余所有测试
- `overall_passed`: 所有 `required: true` 阶段全部通过

**verify_spec 消费 test_mapping：**
- 对比 `test_mapping` 值与 spec 中的 scenario 列表
- 未覆盖的 scenario 记录为 warning 级别 issue

#### Scenario: 无配置时的默认行为
- **WHEN** `se3.yaml` 不包含 `test:` 配置
- **THEN** 使用自动检测的测试命令（现有行为）
- **AND** 所有测试归入 `regression` 类别

#### Scenario: 多阶段测试执行
- **WHEN** 配置了多个 `phases`
- **THEN** 按顺序执行每个阶段
- **AND** 每个阶段结果独立记录
- **AND** `overall_passed` 基于 `required: true` 阶段

#### Scenario: fix loop 中的选择性执行
- **WHEN** test 步骤在 fix iteration 中执行
- **THEN** 跳过 `in_fix_loop: false` 的阶段

#### Scenario: test 失败触发 fix loop
- **WHEN** test 步骤执行完成且存在 new test failures、net-new regressions 或 unparseable failures
- **THEN** test 步骤返回 `REVISION_NEEDED` 状态
- **AND** 流程直接进入 fix loop 返回 implement 步骤
- **AND** 跳过 verify_spec 步骤（因为问题已通过测试发现）
- **AND** fix instructions 包含 `_extract_failures_section()` 智能提取的诊断信息（FAILURES/ERRORS 段），而非简单的 stdout 末尾截断

#### Scenario: Pre-existing failures 不触发 fix loop
- **WHEN** test 步骤执行完成且 `overall_passed` 为 false
- **AND** 所有失败测试均为 pre-existing failures（存在于 `se3/state/known_test_failures.json` 中）
- **THEN** test 步骤返回 `COMPLETED` 状态（不触发 fix loop）
- **AND** `step.outputs["pre_existing_failures"]` 记录这些已知失败
- **AND** 通过 A-class issue discovery 创建 medium 优先级 issue 报告这些 pre-existing failures
- **AND** 日志记录 "Tests failed but all failures are pre-existing — not triggering fix loop"

#### Scenario: Known test failures 持久化
- **WHEN** test 步骤完成执行
- **THEN** 所有 regression failures 写入 `se3/state/known_test_failures.json`（atomic write）
- **AND** 已有条目更新 `last_seen` 时间戳
- **AND** 新条目记录 `reason`（从 pytest 输出提取）、`first_seen`、`last_seen`

#### Scenario: 智能 failure 诊断提取
- **WHEN** test 步骤需要构建 fix instructions
- **THEN** `_extract_failures_section()` 从 pytest 输出中定位 `= FAILURES =` 或 `= ERRORS =` 段
- **AND** 如果段内容超过 max_chars（默认 3000），按 test block 截断并保留 assertion 和 traceback 尾部
- **AND** 如果未找到 FAILURES/ERRORS 段，回退到 stdout 末尾截断

#### Scenario: Revision flow previous_output serialization
- **WHEN** the state machine transitions to a revision (confirm review loop or fix loop)
- **AND** the revised step's `previous_output` is serialized to JSON for the LLM prompt
- **THEN** `json.dumps` MUST use `default=str` as a defensive fallback
- **AND** step handlers that store `StepStatus` in `step.outputs["result"]` MUST convert it to its string `.value` before storing (root cause prevention)

#### Fix History Structure

When the state machine records a fix loop iteration in `fix_history`, each entry SHALL store structured issue data instead of a truncated text summary of fix_instructions.

**Fix history entry schema:**
```json
{
  "iteration": 1,
  "trigger_step_type": "test|self_check|verify_spec",
  "implement_step_id": "...",
  "reason": "test_failure|spec_compliance|...",
  "issues": [
    {"severity": "high", "priority": "high", "description": "...", "location": "...", "message": "..."}
  ]
}
```

**Issue field normalization (`_normalize_issue_fields`):**
- self_check issues use `severity`; verify_spec issues use `priority`. The state machine normalizes both onto every issue dict before storing in fix_history, so downstream consumers (e.g., `_format_fix_history` in the implement step) can use a single canonical field.
- If `severity` is present but `priority` is absent, `priority` is set to `severity`'s value, and vice versa.

**Fix history formatting for implement prompts:**
- `_format_fix_history()` renders each iteration using the structured `issues` list (showing up to 5 issues with severity, description, and location) rather than a truncated text summary.
- The `issues` list is already capped at 10 entries per iteration (via `_cap_issue_list`), keeping the prompt bounded regardless of LLM verbosity.
- Backward compatibility: old fix_history entries carrying `fix_instructions_summary` are still supported as a fallback.

**Source-aware storage policy:**
- Fix instructions from test.py (raw test output: "Tests are failing..." + failures + stderr) are NOT stored in fix_history — the `reason` field ("test_failure") records the trigger, and current test output is always available in the next iteration.
- Fix instructions from verify_spec (LLM-generated analysis and repair guidance) are preserved via the structured `issues` list, which captures the LLM's diagnostic intent for avoiding repeated fix directions.

#### Scenario: Fix history stores structured issues
- **WHEN** the state machine records a fix loop entry from verify_spec
- **THEN** the entry's `issues` list contains normalized issue dicts with both `severity` and `priority` fields
- **AND** no `fix_instructions_summary` field is stored

#### Scenario: Fix history prev_issues cap aligned at 20
- **WHEN** the state machine builds inputs for the verify_spec step during a fix iteration
- **THEN** `prev_issues` is capped at 20 entries, matching the verify_spec prompt's display limit

#### Scenario: test 通过后进行代码自检
- **WHEN** test 步骤执行完成且 `overall_passed` 为 true
- **THEN** test 步骤返回 `COMPLETED` 状态
- **AND** 流程继续到 self_check 步骤进行 LLM 代码审查（对于 feature/bugfix/discovery 工作流）
- **AND** self_check 通过后继续到 verify_spec 步骤进行 spec 合规性检查

#### Scenario: verify_spec 检查 spec coverage
- **WHEN** verify_spec 接收到 `test_mapping`
- **THEN** 检查 spec scenario 的测试覆盖
- **AND** 未覆盖的 scenario 记为 warning

#### Scenario: verify_spec 代码可达性验证
- **WHEN** verify_spec 检查新增代码
- **THEN** 验证新增的函数/方法从实际调用路径可达
- **AND** 禁止将新逻辑放在从未被调用的函数中
- **AND** 未被调用的新增代码记为 error 级别 issue

#### Scenario: verify_spec 端到端集成验证
- **WHEN** verify_spec 检查涉及多组件协作的功能
- **THEN** 验证完整链路（注入→传递→消费）而非仅验证各组件独立正确
- **AND** 缺少端到端验证的多组件功能记为 warning 级别 issue

#### Scenario: verify_spec 死代码检查
- **WHEN** verify_spec 检查新增代码
- **THEN** 验证新增的函数/方法有调用者
- **AND** 验证新增的参数被使用
- **AND** 无调用者的新增代码记为 warning 级别 issue

### Requirement: update_spec 支持创建新 spec

`update_spec` 步骤 SHALL 在实现引入新的子系统或机制时，创建对应的新 spec 文件，而不仅仅是更新已有 spec。

#### Scenario: 新子系统触发新 spec 创建
- **WHEN** 实现引入了一个新的子系统（如 Issue Discovery）
- **AND** 该子系统没有对应的 spec 文件
- **THEN** update_spec 步骤在 `se3/specs/` 下创建新的 spec 目录和 `spec.md`
- **AND** 新 spec 包含 Purpose、Requirements、Scenarios 等标准结构

## Architecture

### 核心组件

```
┌─────────────────────────────────────────────────────────────┐
│                     se3 run (CLI)                           │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                  State Machine                              │
│  ┌─────────────┐ ┌───────────┐ ┌───────────┐ ┌──────────────────┐ │
│  │ create_flow │→│ init_flow │→│ run_step  │→│transition_to_next│ │
│  └─────────────┘ └───────────┘ └───────────┘ └──────────────────┘ │
│         ↑                           │                  │          │
│         └───────────────────────────┴──────────────────┘          │
└───────────────────────────┬─────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Step Handler │    │ Persistence  │    │ LLM Caller   │
│  (13 steps)  │    │(engine.json) │    │(claude -p)   │
│  +discovery  │    │              │    │              │
└──────────────┘    └──────────────┘    └──────┬───────┘
                                               │
                                        ┌──────▼───────┐
                                        │ JSON Extract │
                                        │  (3 modes)   │
                                        └──────────────┘
```

### 数据模型

**FlowInstance:**
- flow_id: 唯一标识
- task_description: 任务描述
- task_type: 任务类型
- status: 流程状态 (INIT, RUNNING, PAUSED, COMPLETED, FAILED)
- state: 状态对象（当前步骤、步骤历史、已选步骤）

**Step:**
- step_id: 唯一标识
- step_type: 步骤类型（13 种之一，包括 discovery 和 self_check）
- status: 步骤状态 (PENDING, RUNNING, COMPLETED, FAILED, RETRYING, PAUSED)
- inputs: 输入字典
- outputs: 输出字典（所有值必须是 JSON 可序列化的原始类型；枚举值存入前须转换为字符串 `.value`）
- retry_count: 重试次数

**Discovery 步骤特殊字段：**
- `discovery_state`: { round, history, mode }
- `refined_description`: 精炼后的任务描述
- `conversation_history`: 对话历史记录

## CLI 命令

### se3 run

主入口命令，创建或恢复流程实例并执行。

```bash
se3 run [TASK_DESCRIPTION] [OPTIONS]

Options:
  --resume, -r      恢复中断的流程
  --loop, -l        循环模式
  --type, -t TYPE   指定任务类型 (feature|bugfix|review|small|directive|discovery)
  --change, -c NAME 关联到指定 change
  --discover, -d    Discovery 模式（需求探索）
  --flow-id ID      恢复指定流程 ID
  --no-worktree     禁用循环模式的分支隔离
  --merge BRANCH    合并已有的 loop 分支（如 loop/fix-auth-1 或旧格式 se3-loop/20260324-120000）
```

### se3 status

显示当前项目状态，包括流程状态、git 状态、pending human calls 等。

```bash
se3 status [--format json]
```

## 状态文件

流程状态保存在 `se3/state/engine.json`：

```json
{
  "flow_id": "uuid",
  "task_description": "...",
  "task_type": "feature",
  "status": "RUNNING",
  "state": {
    "current_step_id": "...",
    "selected_steps": ["analyze", "read_spec", ...],
    "steps": {...}
  }
}
```
