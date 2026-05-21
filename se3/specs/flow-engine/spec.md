<!-- spec-format: v1 -->

# flow-engine Specification

## Purpose

定义 SE3 3.0 的核心流程引擎（Flow Engine）：一个程序驱动的状态机，通过统一的 `se3 run` 入口控制开发流程的 16 个步骤编排（5 个活跃步骤 + CONFIRM + DISCOVERY + 4 个已废弃步骤 + 其他），在每个步骤内调用 LLM 处理需要"思考"的部分。

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
- **THEN** 调用 `create_loop_branch(project_root, task_id=..., iteration=...)` 创建 `loop/{task_id}-{iteration}` 分支从当前 HEAD（task_id 由任务描述 slugify 后截断到 30 字符生成；slugify 结果为空字符串时回退为 `task`）
- **AND** 在 `se3/worktrees/{branch_safe_name}` 创建 git worktree
- **AND** 所有任务在 worktree 中执行（文件读写、commit 都在 worktree 内）
- **AND** 循环结束后提示用户选择：merge / later / discard
- **NOTE** 向后兼容：`list_loop_branches()` 同时匹配旧格式 `se3-loop/*`（标记为 `[legacy]`）和新格式 `loop/*`

#### Scenario: create_loop_branch legacy fallback when task_id/iteration omitted
- **GIVEN** a caller invokes `create_loop_branch(project_root, ...)` without providing both `task_id` and `iteration` (either argument is `None`, or both are omitted; both parameters are optional in the function signature, defaulting to `None`)
- **WHEN** the branch name is computed inside `create_loop_branch` (src/se3/engine/worktree.py:112-146)
- **THEN** the function falls back to the legacy `se3-loop/{timestamp}` naming format, using the provided `timestamp` argument when present and defaulting to `datetime.now().strftime("%Y%m%d-%H%M%S")` otherwise
- **AND** the new `loop/{task_id}-{iteration}` format is used only when **both** `task_id` is truthy AND `iteration is not None` are satisfied
- **NOTE** this fallback path exists at the API level inside `create_loop_branch` itself, independent of the `list_loop_branches()` matcher's legacy compatibility; both branch-name formats remain valid creation outputs of the function and the API continues to accept callers that have not yet adopted the task_id/iteration convention

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
4. **程序化确认门控**: LLM 判定需求已明确并生成精炼描述后，直接 PAUSED 进入程序化确认门控，由用户最终裁决
5. **进入分析**: 用户确认后使用精炼描述继续 `analyze` 步骤

**状态管理：**
- 对话历史保存在 `discovery_state` 中
- 支持任意轮次中断并通过 `se3 run --resume` 恢复
- 最大对话轮数限制（默认 10 轮）防止无限循环

**LLM 调用模式：**
- `question` 模式: 向用户提出具体问题
- `synthesis` 模式: 总结理解并生成精炼描述
- `confirmation` 模式: LLM 判定需求已明确，返回精炼描述后暂停，等待程序化门控

**评估/询问类初始描述的处理：**

当用户的 initial description 表现为对已有代码/方案/改动的评估、判断、审查或询问（例如 "这样做对吗"、"评判 X 是否合理"、"Y 方案有问题吗"、"仔细评估这个改动"、"Is this correct?"、"Evaluate X"、"Review this change"，或内嵌具体代码/文件/commit 引用的提问）时，`INITIAL_DISCOVERY_PROMPT` 与 `CONTINUE_DISCOVERY_PROMPT` SHALL 指示 LLM 避免反问「任务是什么 / 任务范围 / 你想做什么」这类对任务定义本身的澄清问题，而应：

1. 先读取相关代码/上下文（Read、Grep、Glob、Bash 等工具）
2. 形成具体、实质性的评估/意见
3. 就评估内容本身与用户交换观点、提出针对内容的追问或反论
4. 通过多轮对话收敛到一个「正确做法」共识（可以是保持原状、局部修复、全盘重做、换方案等任一结论）
5. 将**共识得出的正确做法**作为 `refined_description` 提交给确认门控 / `analyze` 步骤，而非原样透传用户的评估请求

对「产出形式 / 交付边界 / 优先级 / 约束」等非任务定义层面的合理追问仍被允许。识别依赖 LLM 依 prompt 指令自行判断，接受边界模糊情况的不确定性，不追求 100% 规避；不在代码层引入关键词匹配或分类启发式。`CONTINUE_DISCOVERY_PROMPT` SHALL 在后续轮次同样维持实质讨论姿态，禁止中途漂回「让我重新确认任务范围」。

**程序化确认门控：**

当 LLM 的 `confirmation` 模式判定需求已明确并生成精炼描述后，discovery 步骤不直接完成，而是返回 `PAUSED` 状态并设置 `awaiting_programmatic_confirm=True`。程序运行循环检测到此标志后，在 discovery 的普通输入框读取用户输入：

- 若 `user_input.rstrip('\n\r') == "1"`（仅剥离尾部换行符以兼容多行输入 UI 产生的 trailing newline 工件；不做其它 strip/normalize，不允许 `1.`、`1 ok`、` 1 `（前导或中间空格）、`yes` 等变体）—— 视为**确认并继续**，进入实现规划阶段
- 若 `user_input` 为空（用户直接按回车）—— 视为 **no-op**：不清除 `awaiting_programmatic_confirm` 标志，不推进 discovery 对话，不触发任何 LLM 调用，仅重绘已缓存的确认 Panel
- 其它非空输入 —— 视为**还有问题**：清除 `awaiting_programmatic_confirm` 标志，该输入直接作为下一轮 discovery 的用户输入，不再单独提示输入问题

选择 `1` 而非 `yes` 的理由：`1` 是语言无关的通用符号，为未来非英语界面预留空间。`rstrip('\n\r')` 后严格 `==` 判定避免 `1. 我还想补充…` 被误判为确认；尾部换行剥离仅用于兼容多行输入 UI 在用户键入 `1` + 回车时附带的 trailing `\n`，不引入更宽松的归一化语义。

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

#### Scenario: 评估/询问类初始描述 — 不反问任务范围
- **GIVEN** 用户执行 `se3 run --discover "你仔细、全面、客观地评判一下这个改动是否合理"`（或同类评估/询问式输入，英文 "Is this change reasonable?"、"Review this modification carefully" 等）
- **WHEN** discovery 步骤初始轮执行
- **THEN** LLM 按 prompt 指示先读取相关代码/上下文，形成对该改动的实质性评估
- **AND** 不输出「你想做什么 / 任务范围是什么 / 你的目标是什么」这类对任务定义本身的澄清问题
- **AND** 可以针对评估内容本身与用户交换观点或提出针对内容的追问
- **AND** 经多轮讨论收敛后，`refined_description` 描述的是讨论得出的正确做法（保持原状 / 局部修复 / 重做 / 换方案等），而非用户原始的评估请求

#### Scenario: Discovery 中断恢复
- **GIVEN** 用户在第 3 轮对话时中断（Ctrl+C）
- **WHEN** 用户执行 `se3 run --resume`
- **THEN** 恢复到 discovery 步骤
- **AND** 继续第 3 轮对话

#### Scenario: Discovery 确认阶段恢复显示
- **GIVEN** discovery 步骤在 confirmation 模式下暂停
- **AND** `awaiting_programmatic_confirm=True`
- **AND** `step.outputs["refined_description"]` 包含精炼描述
- **AND** `step.outputs` 中不存在 `proposed_description`
- **WHEN** 用户执行 `se3 run --resume`
- **THEN** `_restore_discovery_display()` 从 `step.outputs` 读取描述时优先取 `proposed_description`
- **AND** 当 `proposed_description` 不存在时，回退到 `refined_description`
- **AND** `refined_description` 作为 markdown 正确渲染
- **AND** 在 discovery 普通输入框提示用户输入 `1` 以确认并继续，其它输入作为下一轮 discovery 的用户输入

#### Scenario: 程序化确认门控 — 用户确认继续
- **GIVEN** LLM 在 confirmation 模式下判定需求已明确
- **AND** discovery 步骤返回 PAUSED 且 `awaiting_programmatic_confirm=True`
- **WHEN** 程序在 discovery 普通输入框读取用户输入且 `user_input.rstrip('\n\r') == "1"`（仅剥离尾部换行符以兼容多行输入 UI 工件，不做其它 strip/normalize）
- **THEN** 设置 `programmatic_confirmed=True` 到步骤输入
- **AND** 重新执行 discovery handler，handler 检测到此标志后直接完成步骤
- **AND** 生成 `discovery_summary` 并设置 `requirements_clarified=True`

#### Scenario: 程序化确认门控 — 用户继续探索
- **GIVEN** LLM 在 confirmation 模式下判定需求已明确
- **AND** discovery 步骤返回 PAUSED 且 `awaiting_programmatic_confirm=True`
- **WHEN** 程序在 discovery 普通输入框读取的用户输入经 `rstrip('\n\r')` 后不严格等于 `"1"` 且去除尾部换行后非空（包括 `1.`、`1 ok`、` 1 `（前导或中间空格）、`yes` 等变体）
- **THEN** 清除 `awaiting_programmatic_confirm` 标志
- **AND** 该用户输入直接作为新一轮 discovery 的用户输入，不单独提示输入问题

#### Scenario: 程序化确认门控 — 空输入 no-op
- **GIVEN** LLM 在 confirmation 模式下判定需求已明确
- **AND** discovery 步骤返回 PAUSED 且 `awaiting_programmatic_confirm=True`
- **WHEN** 程序在 discovery 普通输入框读取的用户输入为空（用户直接按回车）
- **THEN** `awaiting_programmatic_confirm` 标志保持不变
- **AND** 不创建新的 discovery 对话轮次
- **AND** 不触发任何 LLM 调用，仅使用已缓存的 `refined_description` 重绘确认 Panel
- **AND** 用户可再次看到确认提示并输入 `1` 确认或输入其它内容继续探索

#### Scenario: Discovery 输出传递
- **GIVEN** discovery 步骤完成且用户已通过程序化确认门控确认
- **WHEN** 流程进入 `analyze` 步骤
- **THEN** `refined_description` 自动作为 `task_description` 传递给 analyze

**非交互模式下的澄清问答：**

当 discovery 步骤运行在非交互模式（由 daemon 代为 spawn、`--output-format json`、无可用终端）时，没有可阻塞读取的输入框。此时 discovery 步骤 SHALL NOT 阻塞终端输入，而是将澄清提问写为 `se3/calls/` 目录下的 call 文件并返回 `PAUSED` 状态，复用既有的 call/response 机制让用户在网页通过「Respond to Flow」交互应答；用户的响应文件被消费后，流程通过 `se3 run --resume` 等价路径恢复并进入下一轮 discovery。不为网页起步的 discovery 任务新建专门的交互式对话界面 —— 多轮澄清问答统一复用既有 call/response 通道。

#### Scenario: 非交互模式 discovery 通过 call/response 提问
- **GIVEN** discovery 任务由 daemon 代为 spawn（`se3 run --discover --output-format json`，无可用终端）
- **WHEN** discovery 步骤需要向用户提出澄清问题
- **THEN** 不阻塞终端输入，而是将提问写为 `se3/calls/` 下的 call 文件
- **AND** 步骤返回 `PAUSED` 状态
- **AND** 用户在网页通过既有的「Respond to Flow」交互应答该 call
- **AND** 响应文件被消费后流程恢复并进入下一轮 discovery 对话

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

The `_display_discovery_message()` function SHALL render LLM-generated content fields (`content` and `refined_description`) as markdown using `rich.markdown.Markdown`, while structural UI elements (section titles, numbered question lists, confirmation prompts) use Rich `Text` with appropriate styling. Multiple renderables are combined via `rich.console.Group` and printed inside a reverse-color block titled `## Discovery` (white text on blue background) — produced via the shared helpers described in the **Block Rendering Visual Style** Requirement — followed by a blank line, the renderables, another blank line, and a matching short reverse-color blue footer block. This replaces the previous outer `Panel(title="Discovery", border_style="blue")` framing — no border/box is drawn to the terminal's left and right edges, while the original blue accent color and "Discovery" label are preserved as the reverse-color block title and footer.

**Narrative prefix from raw LLM output:**

When `raw_result_text` is provided (the raw LLM output from which JSON was extracted), `_display_discovery_message()` SHALL strip both (a) all fenced JSON code blocks and (b) any trailing bare JSON object that follows the last narrative line. JSON detection for both paths SHALL use the same lenient parse semantics as `parse_json_response` (full repair chain: `json.loads` → `_repair_json` → `_repair_unescaped_quotes` → combined), exposed via the shared helpers `looks_like_json` (fenced blocks; any JSON value type, including arrays and scalars) and `looks_like_json_object` (trailing bare JSON; dict-only, matching `parse_json_response`'s dict-only return contract). Strict `json.loads` MUST NOT be used for this detection: LLM responses commonly contain unescaped ASCII double quotes inside JSON string values (or analogous quirks) that the repair chain handles but strict parsing rejects — without this alignment, the same fenced block can be successfully extracted as `content` while simultaneously leaking back into the narrative prefix, causing the same data to render twice under the Discovery heading (once formatted, once as a raw fenced block). If the remaining narrative text is non-empty after stripping whitespace, it SHALL be rendered as `rich.markdown.Markdown` and placed before all other renderables in the Group printed under the heading, separated by a blank line. If the remaining text is empty (e.g., Phase 2 pure-JSON output), the rendering is identical to the no-narrative case, with no additional prefix. This rule applies uniformly across all five rendering modes.

**Rendering rules by mode:**

| Mode | `content` field | `refined_description` field | Structural elements |
|------|----------------|---------------------------|---------------------|
| Confirmation (`is_confirmation=True`) | Markdown | Markdown, wrapped in a nested cyan reverse-color block (see Proposed Task Description block) | Confirmation prompt hint as styled `Text` (see Confirmation phase content display) |
| Synthesis + questions | Markdown | Markdown, wrapped in a nested cyan reverse-color block (see Proposed Task Description block) | Numbered questions as `Text` |
| Synthesis (no questions) | Markdown | Markdown (under "Proposed Task Description:" heading) | Heading as styled `Text`, confirmation prompt as styled `Text` |
| Question | Markdown | — | Numbered questions as `Text` |
| General | Markdown | — | — |

**Proposed Task Description block:**

In the **Confirmation** (`is_confirmation=True`) and **Synthesis + questions** modes, the LLM-produced `refined_description` SHALL NOT be appended as bare Markdown (nor introduced by a single plain `Proposed Task Description:` heading line). Instead it SHALL be wrapped in a *nested* se3 reverse-color block that gives the user a framework-rendered, unambiguous start/end boundary for the proposed description. This block is produced by a shared module-private helper (`_proposed_description_block`) so both modes render byte-identically. The block reuses the same reverse-color primitives that back `render_block_header` / `render_block_footer` — `_reverse_title` (a reverse-color title row) and `_reverse_footer` (a fixed-width, whitespace-only reverse-color footer block) from the **Block Rendering Visual Style** Requirement — but is constructed as embeddable Rich renderables rather than direct `console.print` calls, so it can be placed inside the single `Group` printed under the `## Discovery` heading. The block uses the `cyan` accent color, distinct from the outer blue Discovery block, so it is visually layered apart from the surrounding LLM `content` text and (in Synthesis + questions mode) the trailing yellow Questions section. The renderable sequence is: a cyan `_reverse_title` row (e.g. `Proposed Task Description / 最终任务描述`), a blank line, the `refined_description` rendered via `rich.markdown.Markdown`, a blank line, a cyan `_reverse_footer` block, and a trailing blank line. The block is purely a framework-level visual container: it does not modify the LLM-produced text, and the footer block contains only whitespace characters styled with a background color, so copying it to the clipboard yields only blank characters with no visible border glyphs.

**Confirmation phase content display:**

When the discovery step enters the confirmation phase (`is_confirmation=True`), `_display_discovery_message()` SHALL display the full LLM analysis content (`content` field) followed by the `refined_description`, both rendered as markdown. This ensures users see the complete LLM analysis (reasoning, summaries, context) alongside the final proposed description before making their confirmation decision. The confirmation rendering SHALL include a styled prompt hint at the bottom of the Group printed under the `## Discovery` heading, outside the markdown content area, rendered as Rich `Text` (not markdown). This hint is the only mechanism by which the user learns the `1` affordance — without it, the rendering spec is silent on input expectations. **Non-normative:** The exact hint wording is non-normative; only the `1` affordance is normative. Implementations MAY localize or reword the hint freely (e.g., `输入 1 确认并继续，输入其它内容继续探索；直接回车重新显示本提示`), provided the `1` confirmation key is communicated to the user and the empty-input no-op behavior is preserved.

##### Scenario: Discovery message renders LLM content as markdown
- **GIVEN** the discovery step receives LLM response with `content` and `refined_description` fields containing markdown syntax (headings, lists, bold, etc.)
- **WHEN** `_display_discovery_message()` renders the message
- **THEN** `content` and `refined_description` are rendered via `rich.markdown.Markdown`
- **AND** structural elements (titles, numbered questions, confirmation prompts) use Rich `Text` with styling
- **AND** all renderables are combined via `rich.console.Group` and printed inside a reverse-color block titled `## Discovery` (white on blue) with a matching short reverse-color blue footer block closing the section; no outer `Panel` border, `Rule`, or horizontal-line element is drawn (see **Block Rendering Visual Style** Requirement)

##### Scenario: Confirmation phase shows full LLM analysis content
- **GIVEN** LLM enters confirmation mode with both `content` (analysis text) and `refined_description`
- **WHEN** the confirmation display is rendered
- **THEN** the full `content` from the LLM response is displayed as markdown
- **AND** the `refined_description` is displayed below it as markdown, wrapped in the nested cyan reverse-color block (see Proposed Task Description block)
- **AND** a styled prompt hint is rendered at the bottom of the Group (under the `## Discovery` heading) as Rich `Text` (not markdown), communicating the `1` confirmation affordance
- **AND** the user can review the complete analysis before choosing to confirm or continue exploration

##### Scenario: Refined description wrapped in nested cyan boundary block
- **GIVEN** the discovery step renders a message in either Confirmation (`is_confirmation=True`) mode or Synthesis + questions mode, with a non-empty `refined_description`
- **WHEN** `_display_discovery_message()` renders the message
- **THEN** the `refined_description` is wrapped in a nested se3 reverse-color block produced by the shared `_proposed_description_block` helper: a cyan `_reverse_title` row, a blank line, the `refined_description` as `rich.markdown.Markdown`, a blank line, a cyan `_reverse_footer` block, and a trailing blank line
- **AND** the nested block uses the `cyan` accent color, visually distinct from the outer blue Discovery block
- **AND** the user can determine the complete start/end extent of the `refined_description` solely from the se3-rendered reverse-color title and footer, without relying on dashed lines or any "最终任务描述" wording that may incidentally appear inside the LLM text
- **AND** the `_reverse_footer` block contains only whitespace characters, so copying it yields no visible border glyphs
- **AND** both modes render the block identically, and no single plain `Proposed Task Description:` heading line is emitted

##### Scenario: Narrative text from raw LLM output prefixed to Discovery rendering
- **GIVEN** `last_raw_result` contains narrative text outside any JSON code block (e.g., Phase 1 LLM output with analysis followed by a fenced JSON object)
- **WHEN** `_display_discovery_message()` renders the message with `raw_result_text` provided
- **THEN** the narrative text (after stripping all JSON code blocks) is rendered as `Markdown` and appears as the first renderable in the Group printed under the `## Discovery` heading
- **AND** a blank line separates the narrative prefix from the existing renderables (`content`, `refined_description`, structural elements)
- **AND** all subsequent renderables follow their existing mode-specific rules unchanged

##### Scenario: Pure JSON raw output renders unchanged
- **GIVEN** `last_raw_result` is pure JSON (e.g., Phase 2 output) or empty, so stripping JSON code blocks leaves no remaining text
- **WHEN** `_display_discovery_message()` renders the message with `raw_result_text` provided
- **THEN** the rendering under the `## Discovery` heading is identical to the behavior before this change, with no additional prefix renderable
- **AND** the `content` and other fields follow their existing mode-specific rules

##### Scenario: Fenced JSON with unescaped quotes is stripped (no duplicate display)
- **GIVEN** `last_raw_result` contains a fenced JSON block whose string values include unescaped ASCII double quotes (e.g., `content` referencing a phrase like `"是否重写..."`) — text that strict `json.loads` rejects but the `parse_json_response` repair chain successfully recovers as a dict
- **WHEN** `_display_discovery_message()` renders the message with `raw_result_text` provided
- **THEN** narrative extraction recognizes the fenced block as JSON via the same lenient parse helpers (`looks_like_json` / `looks_like_json_object`) used upstream, and strips it from the narrative prefix
- **AND** the rendering under `## Discovery` shows only the formatted `content` once — the raw fenced block does NOT appear alongside it
- **AND** the same alignment applies to a trailing bare JSON object after the last narrative line

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

### Requirement: 16 步流程池

流程引擎 SHALL 定义固定的 16 步骤池（StepType 枚举），所有流程步骤从此池中选取。该池由 5 个活跃步骤 + CONFIRM + DISCOVERY + 4 个已废弃步骤 + 其他组成。下表列出主要步骤；deprecated 步骤的兼容行为见 *Deprecated Step Type Backward Compatibility* requirement。

| 步骤 | 职责 | LLM 参与 | JSON 模式 | Read-Only | 输入 | 输出 |
|------|------|---------|-----------|-----------|------|------|
| `discovery` | 需求探索（多轮对话） | 是 | STRICT | **是** | initial_description | refined_description, discovery_summary, requirements_clarified |
| `analyze` | 分析任务类型和范围；收集项目上下文；选择并加载相关 spec items | 是 | STRICT | **是** | task_description | task_type, scope, complexity, reasoning, project_summary, relevant_specs, spec_content |
| `plan` | 统一规划：提案+设计+任务分解（按 task_type 自适应深度） | 是 | TWO_PHASE | **是** | spec_content, task_description, task_type, scope, project_summary | plan{proposal,design}, task_groups, spec_changes, total_complexity, estimated_effort |
| `implement` | 编写代码实现 | 是 | TWO_PHASE | 否 | design_doc, task_groups | implemented_groups, files_changed, total_groups |
| `test` | 运行测试验证 | 否（程序执行） | - | 否 | - | test_results, tests_passed |
| `self_check` | LLM 代码审查：逻辑完整性、代码健壮性、功能遗漏、测试未覆盖区域（不检查 spec 合规性） | 是 | TWO_PHASE | **是** | test_results, changes_made, spec_content, task_groups, fix_iteration, self_check_pass_index, self_check_passes_required, self_check_convergence_enabled, prev_self_check_issues (conditional) | self_check_result, issues (structured list with description, severity, location), actionable_count |
| `verify_spec` | 检查实现与 spec 一致性 | 是 | EXTRACT | **是** | changes_made, spec_content, test_results, fix_iteration, spec_changes | verification_result, issues, in_scope_count, out_of_scope_count, fix_needed, fix_instructions, fix_context, **verified** (rule-based, computed by code: `(in_scope_count == 0) and tests_passed` — see *verify_spec Unified Priority and Scope Mechanism*) |
| `update_spec` | 更新 spec 记录变更 | 是 | EXTRACT | 否 | changes_made, verification_result, spec_changes, design_doc, selected_items | updated_specs, new_capabilities, spec_decisions, notes |
| `version_analyze` | 分析变更确定 suggested_version（权威）+ 生成 commit message | 是 | EXTRACT | **是** | changes_made, summary, verification_result, task_type | **suggested_version**（权威）, bump_type, confidence, reasoning, commit_message |
| `commit` | 提交变更 | 否（程序执行） | - | 否 | changes_made, bump_type, commit_message, proposal, updated_specs | commit_hash |
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
- **AND** 自本轮 fix-loop 进入 self_check 起，已累计连续 `workflow.self_check_passes_required`（默认 1）次全部 clean 的实例
- **THEN** 流程推进到 verify_spec 步骤
- **NOTE** 当 N>1 且累计 clean 次数尚未达到 N 时，状态机不前进 `current_step_index`，而是创建下一个 self_check Step 实例继续执行（参见「重复 N 次直至全部 clean」场景）

#### Scenario: SELF_CHECK 发现遗漏触发 fix loop
- **WHEN** self_check 步骤完成 LLM 代码审查
- **AND** 发现任何 severity（critical/high/medium/low）的遗漏
- **THEN** self_check 返回 REVISION_NEEDED
- **AND** 附带 fix_context（遗漏列表）和 fix_instructions
- **AND** 触发现有 fix loop 机制回到 IMPLEMENT 步骤
- **AND** 本轮剩余的 self_check 实例（若 pass_index < N）不会被创建，当前 fix-loop 立即转入修复
- **AND** 修复后重跑 TEST → SELF_CHECK 直到遗漏列表为空或达到 max_fix_iterations 上限
- **NOTE** fix_iterations 是全局计数器，TEST、SELF_CHECK、VERIFY_SPEC 三者共享，总循环次数不超过 max_fix_iterations（默认 100；配置为 `0` 或 `null` 表示 unlimited，跳过上限检查）
- **NOTE** self_check handler 始终返回 REVISION_NEEDED（不在 handler 内判断耗尽），耗尽检测统一由 state_machine.transition_to_next() 处理
- **NOTE** 当 fix loop 耗尽时，state_machine 将 flow 状态设为 FAILED 并停止执行，同时通过 A-class issue discovery 生成 issue

#### Scenario: SELF_CHECK 重复 N 次直至全部 clean
- **GIVEN** `workflow.self_check_passes_required` 配置为 N（N>=1，默认 1）
- **WHEN** 一轮 fix-loop 进入 self_check 步骤
- **THEN** state_machine 创建 self_check Step 实例 #1，inputs 注入 `self_check_pass_index=1`、`self_check_passes_required=N`
- **AND** 实例 #1 完成且 issues 列表为空时，若 N>1 则 state_machine 在 transition_to_next 中检测到「连续 COMPLETED 的 self_check 实例数 < N」，新建 self_check Step 实例 #2（pass_index=2），`current_step_index` 不前进
- **AND** 该过程重复直到累计连续 N 次全部 clean，才推进到 verify_spec
- **AND** N 个 self_check 实例在 step 历史、`se3 history` 输出、日志中显示为 N 个独立 step 实例（日志前缀 `#1/N`、`#2/N`、…）
- **AND** 同一轮内的 N 次 self_check 之间不做任何对比（同一轮只有「累计连续 clean 次数」一个状态量，不引入任何收敛或 issues 比较逻辑）

#### Scenario: SELF_CHECK 任意一次报告 issues 即短路触发 fix-loop
- **GIVEN** N>1，且 self_check 实例 #i（1 <= i <= N）执行中
- **WHEN** 实例 #i 报告任意 severity 的 issues
- **THEN** 实例 #i 立即返回 REVISION_NEEDED 并触发 fix-loop（流程跳回 IMPLEMENT）
- **AND** 实例 #(i+1)..#N 不被创建，step 历史中只记录实际跑过的 i 个实例
- **AND** 下一轮 fix-loop 重新进入 self_check 时，pass_index 计数从 1 重新开始（不继承上一轮的 pass_index）

#### Scenario: SELF_CHECK 收敛检测（默认关闭、仅跨 fix-loop 轮）
- **GIVEN** `workflow.self_check_convergence_enabled` 默认为 `false`
- **WHEN** self_check 实例完成 LLM 代码审查并发现 issues
- **THEN** 在默认配置下，state_machine 不调用 `_issues_converged`，handler 直接返回 REVISION_NEEDED 进入 fix-loop
- **AND** 即使本轮 issues 与上一轮 fix-loop 末尾 self_check 的 issues 完全相同，也不会被短路为 COMPLETED
- **WHEN** 用户在 `se3.yaml` 中显式设置 `workflow.self_check_convergence_enabled: true`
- **THEN** 收敛检测仅作用于「本轮 fix-loop 的第一个 self_check 实例（pass_index=1）」与「上一轮 fix-loop 最后一个 self_check 实例的 issues」之间
- **AND** 同一轮内的 #2..#N 不参与收敛比较（`prev_self_check_issues` 仅在 pass_index=1 注入，其余实例强制为空）
- **AND** 收敛判定为 True 时，self_check 直接返回 COMPLETED，跳出 fix-loop（视为达到稳定点，相当于一次「人为 clean」）
- **NOTE** 默认关闭的语义变更属于行为默认值变更，本次 spec 修订显式记录，不在 changelog 或启动日志中额外提示

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

#### Scenario: SELF_CHECK prompt excludes decisions owned by a downstream specialized step
- **WHEN** the `self_check` step builds its LLM prompt
- **THEN** the `## What NOT to check` section includes an exclusion stating the general principle that, as a fix-loop checker, self_check MUST NOT report concerns that a later specialized step decides — because `implement` cannot resolve them within its own responsibility, the fix-loop reaches a standoff and spins (re-reporting and re-deferring the same concern without convergence)
- **AND** the version number and version files (e.g. the `version` field in `pyproject.toml`) — whether and how to bump them — are named as the concrete example of such a downstream-owned decision, which the downstream `version_analyze` step decides against the pre-session baseline
- **AND** self_check MUST NOT report "version not bumped" or "version number is wrong" as an issue
- **NOTE** the exclusion is deliberately phrased as the general "downstream specialized step owns it" principle (with version as the named example) so it generalizes beyond `pyproject.toml` to other version files and to other future downstream-owned concerns, rather than matching one file type
- **NOTE** the fix is applied on the checker side (self_check) so it covers all task sources — the task may originate from `discovery` or directly from user input; the discovery prompt and `verify_spec` are intentionally left unchanged, and no convergence brake is introduced (`self_check_convergence_enabled` remains default-off — see *SELF_CHECK 收敛检测* scenario), because an unresolved issue staying unresolved is the desired behavior; the root-cause fix is removing the over-reach at its source, not capping the loop

### Requirement: CONFIRM Step (Dynamically Inserted Review Gate)

The step pool SHALL include a `CONFIRM` step type that does not appear in any default task-type step sequence and is instead inserted dynamically after configured step types, acting as a review gate that can approve the reviewed step (flow continues) or request revision (flow goes back to the reviewed step).

**Step pool attributes:**

| Step | uses_llm | Read-Only | Inputs | Outputs |
|------|----------|-----------|--------|---------|
| `confirm` | conditional (LLM reviewer only) | **No** | step_to_review_id, step_to_review_type, reviewer, agents (LLM mode only), max_iterations, task_description, _llm_review_iteration | review_result {approved, feedback, step_to_review_id, step_to_review_type, reviewer?}, revision_feedback, call_file (human mode) |

The CONFIRM step's `read_only` attribute is `False` — the step itself produces no source-file edits, but it is deliberately not marked read-only because (a) on the revision path it triggers re-execution of a non-read-only step and (b) the human reviewer path writes call-file artifacts to `se3/calls/`.

**Configuration-driven insertion:**

CONFIRM steps are inserted via `insert_confirmation_steps(steps, project_root)` (in `se3/config.py`), invoked by `StateMachine._insert_confirmation_steps()` immediately after the default step sequence for a task type is selected. A CONFIRM step is appended after each step type `S` if and only if:

1. `S` appears in the selected step sequence, AND
2. `S` is a key in the merged `confirmation.steps` dict (global `~/.se3/config.yaml` + project `se3.yaml`).

There is no global on/off switch — opting a step out of confirmation simply means omitting it from `confirmation.steps`. Each `confirmation.steps.<step>` entry has the schema:

```yaml
confirmation:
  steps:
    plan:
      reviewer: human          # 'human', null (use llm_caller.defaults), or an agent name
      max_iterations: 3        # LLM-reviewer iteration cap; ignored on human path
```

**Reviewer modes:**

The `reviewer` field, resolved by `resolve_confirm_inputs(project_root, reviewed_type)` and injected into `step.inputs["reviewer"]` by `state_machine._build_step_inputs`, dispatches to one of two paths:

1. **Human reviewer** (`reviewer == "human"`):
   - `confirm_handler` writes a JSON call file to `se3/calls/confirm_{step_id}_{timestamp}.json` containing the step being reviewed, the change/flow id, and the call type.
   - Returns `StepStatus.PAUSED` so the run loop can prompt the user interactively or via an out-of-band response file.
   - On resume, the handler scans `se3/calls/` for the matching call file and reads its sibling `.response` file (`{stem}.response` JSON with `approved` and `feedback` fields). If the response is present, the handler returns `COMPLETED` (approved) or `REVISION_NEEDED` (changes requested) without re-creating the call file. `max_iterations` is not consulted on this path.

2. **LLM reviewer** (`reviewer != "human"` — either an explicit agent name or `null` to fall back to `llm_caller.defaults`):
   - `confirm_handler._llm_review()` builds a review prompt via `build_llm_review_prompt(reviewed_type, step_output, task_description, revision_feedback, project_root)` and dispatches it through `LLMCaller` with `step_type="confirm_llm_review"` and `json_mode="two_phase"`.
   - The LLM response is parsed via `parse_json_response(response, required_keys=["approved"])`; the handler reads `approved` (bool) and `feedback` (string).
   - The handler maintains `step.inputs["_llm_review_iteration"]` and auto-approves with feedback `Auto-approved: max review iterations (N) reached.` when the iteration counter reaches `max_iterations` (default `3`), preventing infinite review loops.
   - If the LLM call itself raises, the handler auto-approves with feedback prefixed `Auto-approved due to LLM call failure:` rather than blocking the flow. Malformed JSON responses are treated as `approved == False` with a parse-failure feedback message.
   - The synchronous path never returns `PAUSED` and never writes a call file.

**State machine routing:**

`StateMachine.transition_to_next()` inspects `current_step.outputs["review_result"]` after a CONFIRM step completes:

- `approved == True`: normal forward progression to the next step in the selected sequence.
- `approved == False`: the state machine calls `_transition_to_revision(flow, confirm_step, step_to_review_id)`, which re-executes the originally reviewed step with the prior output and revision feedback available to it.

**Defensive fallback:**

If a CONFIRM step is present in the sequence but `resolve_confirm_inputs` returns `None` for the reviewed step type (indicating YAML drift between when `insert_confirmation_steps` ran and when `_build_step_inputs` runs — e.g., the user edited `se3.yaml` mid-flow), the state machine logs a warning and defaults to `reviewer = "human"` with `max_iterations = None`, so the user can manually unblock the flow.

#### Scenario: CONFIRM step inserted only when configured
- **GIVEN** `confirmation.steps` contains an entry for `plan` but not for `implement`
- **WHEN** the state machine builds the step sequence for a `feature` task
- **THEN** a CONFIRM step is appended immediately after `plan` in the sequence
- **AND** no CONFIRM step is appended after `implement`

#### Scenario: CONFIRM step absent from defaults when nothing is configured
- **GIVEN** `se3.yaml` has no `confirmation.steps` section (or it is empty)
- **WHEN** the state machine builds the step sequence
- **THEN** no CONFIRM step is inserted anywhere in the sequence
- **AND** the sequence matches the default task-type sequences defined under "16 步流程池"

#### Scenario: Human reviewer pauses awaiting response
- **WHEN** `confirm_handler` runs with `step.inputs["reviewer"] == "human"` and no prior response file exists
- **THEN** a JSON call file is written to `se3/calls/confirm_{step_id}_{timestamp}.json`
- **AND** the call-file path is stored in `step.outputs["call_file"]`
- **AND** the handler returns `StepStatus.PAUSED`

#### Scenario: Human reviewer resume reads sibling response file
- **GIVEN** a CONFIRM step previously returned `PAUSED` and wrote `se3/calls/confirm_{step_id}_{ts}.json`
- **AND** a sibling `se3/calls/confirm_{step_id}_{ts}.response` file exists with `{"approved": true, "feedback": "..."}`
- **WHEN** the flow resumes and re-runs `confirm_handler` for the same step
- **THEN** the handler reads the response file, populates `step.outputs["review_result"]` with `approved`, `feedback`, `step_to_review_id`, and `step_to_review_type`
- **AND** returns `StepStatus.COMPLETED` (or `REVISION_NEEDED` when `approved == false`)

#### Scenario: LLM reviewer approval
- **WHEN** `confirm_handler` runs with `step.inputs["reviewer"] != "human"` (an agent name or `None`)
- **AND** the LLM responds with parseable JSON `{"approved": true, "feedback": "..."}`
- **THEN** the handler returns `StepStatus.COMPLETED`
- **AND** `step.outputs["review_result"]` includes `reviewer: "llm"`, `approved: true`, and the LLM's feedback
- **AND** no call file is created

#### Scenario: LLM reviewer revision request
- **WHEN** the LLM responds with `{"approved": false, "feedback": "..."}`
- **THEN** the handler returns `StepStatus.REVISION_NEEDED`
- **AND** the state machine's `transition_to_next()` invokes `_transition_to_revision()` to re-execute the originally reviewed step

#### Scenario: LLM reviewer max iterations auto-approval
- **GIVEN** `confirmation.steps.<step>.max_iterations` is configured to N (default 3)
- **WHEN** `step.inputs["_llm_review_iteration"]` has reached N before the LLM call
- **THEN** the handler skips the LLM call, returns `StepStatus.COMPLETED`, and stores feedback `Auto-approved: max review iterations (N) reached.`

#### Scenario: LLM reviewer call failure does not block flow
- **WHEN** the LLM call inside `_llm_review` raises any exception
- **THEN** the handler returns `StepStatus.COMPLETED` with `approved: true` and feedback prefixed `Auto-approved due to LLM call failure:`
- **AND** the flow continues forward rather than stalling on a broken reviewer

#### Scenario: Revision routes back to the reviewed step
- **GIVEN** a CONFIRM step completes with `approved == false` and `review_result.step_to_review_id == X`
- **WHEN** `transition_to_next()` runs
- **THEN** `_transition_to_revision(flow, confirm_step, X)` is invoked
- **AND** the step identified by X is re-executed with revision feedback available
- **AND** the state machine does NOT advance to the step that would normally follow CONFIRM in the selected sequence

#### Scenario: Defensive fallback when CONFIRM config is missing at build-input time
- **GIVEN** the sequence contains a CONFIRM step (because `confirmation.steps.<reviewed_type>` existed when `insert_confirmation_steps` ran)
- **AND** `resolve_confirm_inputs(project_root, reviewed_type)` now returns `None` (e.g., `se3.yaml` was edited mid-flow)
- **WHEN** `_build_step_inputs` constructs inputs for the CONFIRM step
- **THEN** `step.inputs["reviewer"]` is set to `"human"` and `step.inputs["max_iterations"]` is `None`
- **AND** a warning is logged identifying the reviewed step type

### Requirement: Deprecated Step Type Backward Compatibility

The step type enum SHALL retain deprecated values with stub handlers that forward to the appropriate current handler. This ensures persisted flows created before step unification/merges can resume without crashing.

**Retained entries (plan unification):**
- `StepTypeValue.PROPOSE` — deprecated, forwards to plan_handler
- `StepTypeValue.DESIGN` — deprecated, forwards to plan_handler
- `StepTypeValue.PLAN_TASKS` — deprecated, forwards to plan_handler

**Retained entries (analyze merge):**
- `StepTypeValue.PROJECT_SUMMARY` — deprecated, forwards to project_summary_handler

**Behavior:**
- Stub handlers log a deprecation warning with the flow ID and step ID
- The target handler executes normally regardless of which step type triggered it
- Display titles and renderers for deprecated types are retained so history/status views render correctly

#### Scenario: Resuming a persisted flow with old step types
- **WHEN** a flow persisted with `PROPOSE`, `DESIGN`, or `PLAN_TASKS` step types is resumed
- **THEN** the stub handler forwards execution to plan_handler
- **AND** a deprecation warning is logged

#### Scenario: Resuming a persisted flow with PROJECT_SUMMARY steps
- **WHEN** a flow persisted with `PROJECT_SUMMARY` step type is resumed
- **THEN** the stub handler forwards execution to project_summary_handler
- **AND** a deprecation warning is logged

#### Scenario: New flows use unified PLAN step
- **WHEN** a new flow is created
- **THEN** the step sequence contains only `PLAN`, never `PROPOSE`, `DESIGN`, or `PLAN_TASKS`

#### Scenario: New flows do not include PROJECT_SUMMARY
- **WHEN** a new flow is created
- **THEN** the step sequence does not contain `PROJECT_SUMMARY`
- **AND** its functionality is provided by the `ANALYZE` step

### Requirement: 步骤内 LLM 调用

流程引擎 SHALL 在每个步骤内通过 subprocess 调用 LLM（`claude -p`），传入步骤特定的 prompt 和自动收集的 context。

**LLM 调用机制：**
1. 构建步骤特定的 prompt
2. 自动收集相关上下文（specs、前序步骤输出、项目状态）
3. 调用 Claude CLI 获取响应
4. 解析响应（支持 JSON 和文本）
5. 存储输出到步骤状态

**Large Prompt Routing via stdin:**

The CLI adapter (`ClaudeCodeRunner._resolve_args()`) SHALL automatically reroute oversized prompt arguments through the spawned Claude subprocess's standard input when their UTF-8 byte length exceeds 100 KB (102,400 bytes), preventing `execve()` `E2BIG` errors caused by Linux's `MAX_ARG_STRLEN` limit (128 KB). No temporary file is written.

- **Threshold:** 100 KB (102,400 bytes). This leaves ~28 KB safety margin below the 128 KB `MAX_ARG_STRLEN` hard limit, covering multi-byte UTF-8 encoding and environment variable space.
- **Mechanism:** When `-p`/`--prompt` is followed by a plain-text argument (not an `@file` reference) whose `len(prompt_arg.encode('utf-8'))` exceeds the threshold, `_resolve_args()` keeps the `-p`/`--prompt` flag in argv but drops its value, and returns the dropped value as a separate `stdin_prompt` string alongside the resolved argv. The execution paths (`run()`, `popen()`, `run_with_monitor()`) feed `stdin_prompt` to the Claude subprocess via stdin so Claude treats it as the user message.
- **Why not a temp file:** An earlier `-p @tmpfile` design caused Claude Code to read the file via its Read tool (subject to that tool's 25k-token ceiling) rather than treating the contents as the user message. Routing through stdin delivers the full prompt as the user message regardless of size.
- **Below threshold:** The prompt argument is passed directly on the command line (existing behavior); `stdin_prompt` is `None`.
- **`@file` passthrough:** Arguments starting with `@` and explicit `-p @file` forms are left unchanged — that is Claude CLI's documented file-reference syntax and callers using it have asked for that semantic.
- **Multiple oversized prompts:** Multiple oversized `-p` values in a single invocation is not a supported pattern; the last value wins and a `warnings.warn` is emitted.
- **Scope:** `_resolve_args()` is called by all three execution paths (`run()`, `popen()`, `run_with_monitor()`), so the protection applies universally.
- **stdin writer threading:** When the subprocess is spawned with a `stdin_prompt`, `_spawn_stdin_writer()` writes the payload to `proc.stdin` in a daemon thread, flushes, and closes the stream so Claude observes EOF and proceeds. Performing the write from a background thread prevents deadlock when the prompt exceeds the OS pipe buffer (typically 64 KB) and the child is waiting for EOF before draining stdin. Write failures (`BrokenPipeError`, `OSError`) are swallowed in the writer; they surface as a subprocess error via stdout/returncode.
- **Chat history preservation:** `_record_prompt()` in `LLMCaller` executes before `_resolve_args()`, so chat history always records the original prompt text.

#### Scenario: 自动注入上下文
- **WHEN** 流程引擎进入某个步骤
- **THEN** 程序自动收集该步骤所需的上下文
- **AND** 将上下文注入 LLM 调用的 prompt 中

#### Scenario: LLM 调用失败
- **WHEN** 步骤内的 LLM 调用失败（超时、API 错误、输出无效）
- **THEN** 流程引擎执行重试策略（最多 3 次）
- **AND** 如果重试仍失败，暂停流程并通知用户

#### Scenario: Large prompt rerouted to subprocess stdin
- **WHEN** a `-p`/`--prompt` argument's UTF-8 byte length exceeds 100 KB (102,400 bytes)
- **THEN** `_resolve_args()` keeps the `-p`/`--prompt` flag in argv but drops its value, and returns the dropped value as a separate `stdin_prompt` string
- **AND** the execution path feeds `stdin_prompt` to the spawned Claude subprocess via stdin (no temp file is written)
- **AND** for `popen()` callers, `_spawn_stdin_writer()` writes the payload from a daemon thread, flushes, and closes `proc.stdin` so Claude observes EOF

#### Scenario: Prompt below stdin-routing threshold
- **WHEN** a `-p`/`--prompt` argument's UTF-8 byte length is at or below 100 KB
- **THEN** the prompt is passed directly as a command-line argument
- **AND** `stdin_prompt` is `None` and the subprocess's stdin is not used for the prompt

#### Scenario: Multiple oversized -p values in one invocation
- **WHEN** a single argv contains more than one oversized `-p`/`--prompt` value
- **THEN** the last oversized value is the one routed to stdin (last-wins)
- **AND** `_resolve_args()` emits a `UserWarning` via `warnings.warn(..., stacklevel=2)` indicating that multiple oversized prompts in a single invocation are not supported and only the last is routed to stdin
- **AND** no exception is raised — execution continues with last-wins semantics so callers passing exactly one oversized prompt are unaffected

#### Scenario: Explicit @file reference is passed through unchanged
- **WHEN** an argument starts with `@`, or `-p`/`--prompt` is followed by an `@file` reference
- **THEN** `_resolve_args()` leaves the argument untouched regardless of size
- **AND** no stdin rerouting is performed for that argument

### Requirement: Read-Only Step Constraint Injection

The flow engine SHALL enforce a prompt-level file modification prohibition for read-only steps, preventing the LLM from accidentally modifying code during analysis-only steps.

**Read-Only Step Attribute:**

Each entry in the step pool (`STEP_POOL`) SHALL include a `read_only` boolean attribute. Steps marked `read_only: true` are:
- `discovery`, `analyze`, `plan`, `self_check`, `verify_spec`, `version_analyze`, `summarize`
- Deprecated steps (`project_summary`)

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

### Requirement: Spec Names Injection for Downstream Steps

The flow engine SHALL extend spec awareness beyond the `analyze` step's predetermined selection by injecting "the list of all available specs + a soft hint to read on demand" into designated downstream LLM sub-process steps. This gives those steps whole-spec-set awareness and lets them supplement their context via the Read/Glob tools (reading `se3/specs/<name>/spec.md`) when `analyze` missed a relevant spec.

**Helper API:**

```python
get_spec_names_injection(
    step_type: str,
    project_root: Path,
    relevant_specs: list[str] | None = None,
) -> str
```

Returns the injection prompt fragment, or an empty string when the step is not in the whitelist. Handlers call the helper at prompt-build time and append the return value to their prompt, mirroring the existing `get_issue_discovery_injection` / `get_step_language_instruction` call sites.

**Three-tier whitelist (mirrors `issue_discovery`):**

1. `SPEC_NAMES_INJECTION_FORBIDDEN_STEPS = {"summarize", "commit"}` — hard block; always returns empty even if `se3.yaml` lists the step.
2. `SPEC_NAMES_INJECTION_DEFAULT_STEPS = ["plan", "plan_tasks", "implement", "verify_spec", "update_spec", "self_check"]` — default whitelist applied when `se3.yaml` has no override. Deprecated step types (`propose`, `design`) are intentionally absent: their stub handlers forward to the unified `plan_handler`, which looks up injection under `"plan"`, so listing them would be dead code.
3. `se3.yaml` override key `spec_names_injection.steps` — replaces the default list when present. FORBIDDEN still takes precedence. Non-list override values (bare string, dict, etc.) are silently ignored in favor of the defaults.

**Covered steps by default:**

| Step | Injected | Rationale |
|------|----------|-----------|
| `plan`, `plan_tasks` | yes | Task decomposition benefits from whole-spec-set awareness |
| `implement` | yes | Implementation may discover need for additional specs (e.g., versioning) |
| `verify_spec` | yes | Verification needs full spec set to judge compliance |
| `update_spec` | yes | Already prompted to use Read; spec-names list makes it more reliable |
| `self_check` | yes | Self-review may touch unpreselected specs |
| `design`, `propose` (deprecated) | yes, via forwarding | Deprecated stub handlers forward to `plan_handler`, which looks up the injection under `"plan"`. They are therefore covered transitively and are **not** listed in `SPEC_NAMES_INJECTION_DEFAULT_STEPS` themselves. |
| `analyze`, `discovery` | no | Already natively list specs via their own prompt templates |
| `summarize`, `commit` | no (FORBIDDEN) | No spec awareness needed for summary/commit |
| `confirm_llm_review` | no (initial) | Review output aligns with task_description; conservative default |

**Injection prompt content (soft constraint):**

- Begins with heading `## Available Specifications`.
- Line `All available specs in this project: <sorted names>.` — sourced by scanning `project_root/se3/specs/*/spec.md`, sorted alphabetically.
- Line `Specs already loaded above: <loaded names or "none">.` — derived from `relevant_specs` argument so the LLM does not re-read specs already embedded in the prompt.
- Soft guidance: the LLM **MAY** (not MUST) read additional specs via `Read` at path pattern `se3/specs/<name>/spec.md`.
- Anti-abuse wording: "Only consult specs that directly help the task — avoid reading broadly."

**Abuse prevention:**

- The MAY phrasing discourages blanket reads that would inflate token usage.
- The already-loaded list prevents duplicate reads of specs `analyze` already embedded.
- `SPEC_NAMES_INJECTION_FORBIDDEN_STEPS` hard-blocks steps with no spec-awareness need.
- Errors in loading `se3.yaml` fall back to defaults (no crash).

**Compatibility:**

- The existing `spec_content` and `relevant_specs` input fields are unchanged; the injection is purely additive to the prompt suffix.
- `analyze`'s pre-selection logic is untouched — this mechanism is a safety net, not a replacement.

#### Scenario: Default whitelist receives spec names injection
- **WHEN** a default whitelisted step (e.g., `plan`, `implement`, `verify_spec`, `update_spec`, `self_check`) builds its LLM prompt
- **AND** `se3.yaml` has no `spec_names_injection.steps` override
- **THEN** `get_spec_names_injection(step_type, project_root, relevant_specs)` returns a non-empty prompt fragment containing `## Available Specifications`, the sorted `All available specs in this project:` line, and a `Specs already loaded above:` line derived from `relevant_specs`
- **AND** the handler appends the fragment to its prompt suffix

#### Scenario: FORBIDDEN steps never receive injection
- **WHEN** the step type is `summarize` or `commit`
- **THEN** `get_spec_names_injection(step_type, project_root, relevant_specs)` returns an empty string
- **AND** even when `se3.yaml` explicitly lists the step in `spec_names_injection.steps`, the FORBIDDEN set takes precedence and the return remains empty

### Requirement: Runtime Environment Capabilities Injection

The flow engine SHALL inject a fixed `## se3 Runtime Environment` section into the LLM sub-process prompt of designated downstream steps, advertising the se3-tool-provided **read-only capabilities the LLM MAY proactively invoke** (history / issue inspection) and a **write-operation guardrail blacklist** of commands the LLM MUST NOT proactively invoke. This makes downstream LLM sub-processes aware of the se3 capabilities available in the host project so they can (a) inspect prior session history when the user references "the previous session / last run / history", (b) consult related issue context when the task touches an existing issue, and (c) refrain from accidentally invoking destructive write operations (`merge` / `salvage` / `sync` / etc.) merely because those commands exist.

The injected text is owned by se3 itself, evolves with the se3 version, and is **NOT written into the downstream project's spec files**. Both new and existing projects receive the injection without any migration.

**Helper API:**

```python
get_runtime_environment_injection(
    step_type: str,
    project_root: Path,
) -> str
```

Returns the runtime-environment prompt fragment (with a leading `\n\n` consistent with sibling injections), or an empty string when the step is not in the whitelist or when the source markdown cannot be loaded. Handlers call the helper immediately after `get_spec_names_injection` / `get_issue_discovery_injection` at prompt-build time and append the return value to their prompt.

**Source file:** The injected content is stored in `src/se3/engine/runtime_environment.md` as a standalone markdown file, shipped with the se3 wheel and read at runtime via the helper. The markdown content is process-cached after first read; a missing or unreadable file returns `""` and logs one warning, never raising.

**Three-tier whitelist (mirrors `spec_names_injection` / `issue_discovery`):**

1. `RUNTIME_ENV_INJECTION_FORBIDDEN_STEPS = {"commit", "version_analyze"}` — hard block; always returns empty even if `se3.yaml` lists the step.
2. `RUNTIME_ENV_INJECTION_DEFAULT_STEPS = ["analyze", "plan", "plan_tasks", "implement", "verify_spec", "update_spec", "self_check", "discovery", "summarize"]` — default whitelist (9 steps) applied when `se3.yaml` has no override.
3. `se3.yaml` override key `runtime_environment_injection.steps` — replaces the default list when present and shaped as a list. FORBIDDEN still takes precedence. Null / malformed / non-list override values are silently ignored in favor of the defaults.

**Injection content structure (excerpt, not full markdown):**

The injected markdown SHALL contain a top-level `## se3 Runtime Environment` heading followed by:

- A **read-only whitelist** with two categories the LLM is encouraged to invoke in matching scenarios:
  1. *History inspection (read-only)* — triggers: user mentions "last/previous session", "where did we leave off", "history", "earlier run", etc. Covered tools:
     - `se3 history list` — list recent flow runs
     - `se3 history show <flow_id>` — show structured details of one run
     - `se3 history archived` — list archived engine states
     - Free-text fallback: read/grep `se3/history/<flow_id>/<step>.jsonl` and `se3/state/archive/engine_*.json`
     - Recommended workflow sentence: first `se3 history list` to locate `flow_id`, then `se3 history show <flow_id>`; for keyword search inside conversation content, `grep -r 'keyword' se3/history/<flow_id>/`.
  2. *Issue context inspection (read-only)* — triggers: the task references a known issue or the user mentions an issue id / related requirement. Covered tools:
     - `se3 issue list` — list issues
     - `se3 issue show <id>` — show issue detail
     - Free-text fallback: grep `se3/issues/open/*.yaml`, `se3/issues/closed/*.yaml`
     - Recommended workflow sentence: first `se3 issue list` to scan the inventory; for keyword search inside issue bodies, `grep -r 'keyword' se3/issues/`.

- A **write-operation blacklist** explicitly telling the LLM "do not proactively invoke unless the user explicitly asks in this session":
  - `se3 history restore` — rolls back flow state
  - `se3 issue create` / `se3 issue reset` — write operations; the LLM should not autonomously create issues
  - `se3 salvage` — auto-commits / creates issues / archives sessions; rescue-only
  - `se3 merge` / `se3 merge respond` — mutates git merge state
  - `se3 sync` / `se3 sync respond` — modifies spec files; high impact
  - `se3 init` — only for fresh-project initialization

The markdown body is the single source of truth; it MAY be iterated independently of the Python helper without breaking the injection contract, as long as the heading and the white/blacklist commands above remain present.

**Abuse prevention and compatibility:**

- The whitelist is phrased as **"MAY when the user references the matching scenario"**, not "MUST", to discourage blanket invocation.
- The blacklist is phrased as **"do not proactively invoke"** rather than "forbidden", leaving the user explicit-request override intact.
- The injection is purely additive to the prompt suffix and does not change any existing handler input / output schema.
- FORBIDDEN steps (`commit`, `version_analyze`) are mechanical / non-LLM-deliberative; they receive no injection so the prompt remains tight.
- Errors loading `runtime_environment.md` (missing / IO failure) degrade gracefully to `""` plus a single warning log.

#### Scenario: Default whitelist step receives runtime environment injection
- **WHEN** a default whitelisted step (e.g., `analyze`, `plan`, `plan_tasks`, `implement`, `verify_spec`, `update_spec`, `self_check`, `discovery`, `summarize`) builds its LLM prompt
- **AND** `se3.yaml` has no `runtime_environment_injection.steps` override
- **THEN** `get_runtime_environment_injection(step_type, project_root)` returns a non-empty prompt fragment containing the heading `## se3 Runtime Environment`, the whitelist entries (`se3 history list`, `se3 history show`, `se3 history archived`, `se3 issue list`, `se3 issue show`), path references (`se3/history/<flow_id>`, `se3/issues/`), the two recommended-workflow sentences, and the blacklist entries (`se3 history restore`, `se3 issue create`, `se3 issue reset`, `se3 salvage`, `se3 merge`, `se3 sync`, `se3 init`)
- **AND** the handler appends the fragment to its prompt suffix immediately after the `get_spec_names_injection` call site

#### Scenario: FORBIDDEN step never receives runtime environment injection
- **WHEN** the step type is `commit` or `version_analyze`
- **THEN** `get_runtime_environment_injection(step_type, project_root)` returns an empty string
- **AND** even when `se3.yaml` explicitly lists the step in `runtime_environment_injection.steps`, the FORBIDDEN set takes precedence and the return remains empty

### Requirement: JSON 提取模式

流程引擎 SHALL 支持四种 JSON 提取模式，根据步骤特性选择最优策略：

| 模式 | 描述 | 适用场景 |
|------|------|----------|
| **STRICT** | 强制 JSON 格式，失败重试 | 简单输出（analyze） |
| **EXTRACT** | 要求 JSON 格式，失败时用 LLM 提取 | 中等复杂度（verify_spec, update_spec） |
| **TWO_PHASE** | 自然生成 + LLM 提取 | 复杂/大输出（plan, implement） |
| **OFF** | 无 JSON 约束，原样返回 LLM 文本 | 自由文本输出（summarize） |

**模式选择原则：**
- 简单输出（<1K tokens）：STRICT（成本低，可靠性高）
- 中等复杂度（1K-5K tokens）：EXTRACT（平衡可靠性和 token 效率）
- 大输出（>5K tokens）：TWO_PHASE（避免提示词污染，处理截断）
- 自由文本（Markdown / 散文）：OFF（无 JSON 约束，直接返回原始文本）

**模式解析（`get_json_mode` / `_resolve_json_mode`）：**

`LLMCaller.call()` 与模块级 `get_json_mode()` 按以下优先级解析最终模式：

1. 显式 `json_mode` 参数（字符串 `"strict" | "extract" | "two_phase" | "off"`，大小写不敏感；或对应的 `JsonMode` 枚举值）
2. `two_phase_json=True` → `TWO_PHASE`
3. `require_json=True` → `STRICT`
4. 默认 → `OFF`

当 `json_mode` 字符串无法识别为上述四值之一时，记录一条 warning 日志并回退到 `OFF`，不抛出异常。

#### Scenario: OFF 模式返回原始文本
- **WHEN** 步骤（如 `summarize`）以 `JsonMode.OFF` 调用 LLM
- **THEN** prompt 不被 JSON 约束包装
- **AND** LLM 输出原样返回，不经过 JSON 解析或提取
- **AND** 适用于生成 Markdown 总结或其他自由文本

#### Scenario: 默认模式为 OFF
- **WHEN** 调用方未提供 `json_mode`、`require_json`、`two_phase_json` 中的任何一个
- **THEN** `get_json_mode()` 返回 `JsonMode.OFF`

#### Scenario: 未知 json_mode 字符串回退到 OFF
- **WHEN** 调用方传入无法识别的 `json_mode` 字符串
- **THEN** 记录 warning 日志（`Unknown json_mode '<value>', defaulting to 'off'`）
- **AND** 最终解析为 `JsonMode.OFF`，不抛出异常

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
- **AND** Edit 工具：从 `old_string` / `new_string` 通过 `generate_edit_diff()` 生成 unified diff，调用 `display.render_diff()` 渲染红（删除）/绿（新增）/青（hunk 标记）/灰（上下文）着色的 diff 输出
- **AND** Write 工具（新建文件，`old_content` 为 `None`）：显示 `Created {file_path} ({n} lines)` 绿色标识，不展示行级 diff
- **AND** Write 工具（覆写已有文件，`old_content` 非 `None`）：通过 `generate_edit_diff(old_content, content, file_path)` 生成 unified diff 并渲染红/绿着色输出（文件 I/O 仅在 tracker 的 tool_use 阶段发生一次，formatter 层不访问文件系统）
- **AND** diff 超过 `max_lines`（默认 50 行）时截断并显示剩余行数摘要
- **AND** `display.render_diff()` 使用 Rich `Text` 对象逐行着色（无 Panel 边框、无 Rule、无横线），按 **Block Rendering Visual Style** 输出反色色块标题 `## Diff: {file_path}`（白字黄底）并空一行；每行添加 dim 样式的行号前缀（列宽固定 4，从 `@@ -a,b +c,d @@` hunk header 解析起始行号，删除行显示旧文件行号，新增行和上下文行显示新文件行号）；`total` 行数统计排除 `---`/`+++` 头部行；达到 `max_lines`（默认 50）时追加 dim 样式的 `... (N more lines)` 摘要后中止；diff 内容下方再空一行并打印固定宽度（4 字符）的反色黄色色块作为下边界；当 `displayed == 0` 时不输出标题、内容、空行与下边界色块
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
- **AND** 用户 prompt 按 `segment_prompt()` 分段后整体被包裹在反色色块标题 `## Prompt`（白字蓝底；attempt 重试时为 `## Prompt ({attempt_label})`）与匹配的固定宽度反色蓝色色块下边界之间，段内每个 segment 标题与内容左对齐渲染，不再绘制 Rich `Panel` 边框、Rule 或横线；assistant response 同样使用反色色块标题 `## Response`（白字绿底，重试时附加 attempt 标签）+ 内容主体（最终文本 `Markdown` 或 verbose 模式下的 `Text(_render_ndjson_for_human(...))`）+ 反色绿色色块下边界 + 末尾空行展示。颜色沿用先前 `border_style` 的语义（prompt → blue，response → green）；视觉规范统一由 **Block Rendering Visual Style** 要求提供。
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
- **User prompts** in retry/continue context: not truncated per-entry (line-level deduplication controls size); a post-dedup whole-prompt safety cap prevents unbounded growth after deduplication runs

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
- **AND** user prompts are preserved in full by `format_history_for_retry()` — no per-entry character cap is applied; repeated content is handled by `deduplicate_prompt_lines()` in LLMCaller, and a separate post-dedup whole-prompt safety cap (see Prompt Line-Level Deduplication) provides bounded-growth fallback

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

**Post-Dedup Whole-Prompt Safety Cap:**

- After `deduplicate_prompt_lines()` runs, `LLMCaller._call_with_retry()` applies a single whole-prompt length check against `POST_DEDUP_SAFETY_LIMIT` (default 500,000 characters; env-overridable via the `SE3_POST_DEDUP_SAFETY_LIMIT` environment variable for testing and operational tuning — values that fail to parse as a positive integer fall back to the default)
- The cap operates on the deduped `effective_prompt` as a whole, NOT on each history entry individually — this ensures repeated spec content is collapsed by dedup before the cap decides whether truncation is necessary
- When the deduped prompt exceeds the cap, the cap locates the retry-history region using shared marker/separator constants (defined in a neutral module `se3/engine/retry_context.py` and consumed by both `chat_history.py` and `llm_caller.py`) via a trailing `rfind` of the separator (robust to retry-of-retry replays where multiple history regions may appear), and truncates the **head of the history region** while preserving the **new prompt tail** in full — the semantically most important portion for the model's current task
- Kept body is rounded to line boundaries to avoid mid-line cuts
- Distinct warnings are emitted for each observable branch: no-op (under limit), normal trigger (history head trimmed), tail-alone exceeds the limit, and separator-missing (defensive fallback)
- Shared constants `RETRY_HISTORY_MARKER`, `RETRY_HISTORY_SEPARATOR`, and `POST_DEDUP_SAFETY_LIMIT` live in `retry_context.py` so that `chat_history.format_history_for_retry()` and `llm_caller._call_with_retry()` reference the same symbols without cross-module underscore-prefixed imports

**Design rationale:** Character-count truncation of individual user prompts (the prior approach) discarded unique content that appears after repeated spec blocks (e.g., task-specific instructions following embedded specs), and — critically — fired *before* `deduplicate_prompt_lines()` had a chance to collapse those repeated specs, so content that would have been removed as duplicates was instead prematurely truncated. Line-level deduplication is a lossless alternative: it removes only provably identical content while preserving all unique portions of the prompt. The post-dedup whole-prompt cap serves as the bounded-growth fallback for the degenerate case where dedup is ineffective and the prompt is genuinely unbounded — it runs after dedup, measures the whole prompt, and preserves the current-task tail.

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

#### Scenario: Post-dedup cap is a no-op when dedup reclaims space
- **GIVEN** a retry where prior user prompts contain large spec blocks that are also present in the new prompt
- **WHEN** `_call_with_retry()` runs `deduplicate_prompt_lines()` and then the post-dedup whole-prompt cap
- **THEN** the deduped prompt is below `POST_DEDUP_SAFETY_LIMIT`
- **AND** no truncation is performed and no `User prompt in retry context hit safety limit` style warning is emitted

#### Scenario: Post-dedup cap trims history head when prompt genuinely exceeds limit
- **GIVEN** a retry where prior user prompts contain large non-repeating content that dedup cannot collapse
- **AND** the deduped `effective_prompt` length still exceeds `POST_DEDUP_SAFETY_LIMIT`
- **WHEN** the post-dedup cap runs
- **THEN** the retry-history region is located via trailing `rfind` of the shared separator (robust to retry-of-retry replays)
- **AND** the **head** of the history region is truncated while the **new prompt tail** is preserved in full
- **AND** the kept body boundary is rounded to a line boundary (no mid-line cuts)
- **AND** a warning is logged describing that the cap was triggered

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
- `analyze` 输出 `task_type`、`scope`、`complexity`、`reasoning`、`project_summary`、`relevant_specs`、`spec_content`；其中 `project_summary` 由 `ProjectContextCollector.collect()` 程序化生成（非 LLM），`spec_content` 由后处理程序化加载（base spec 自动附加 + LLM 选择的 spec items）
- `plan` 接收 `spec_content`（从 analyze）、`task_type`、`scope`、`project_summary`（从 analyze），输出 `plan`（含 proposal + design）、`task_groups` 和 `spec_changes`（仅 full depth）
- `implement` 接收 `design_doc`（从 plan.design 映射）、`task_groups`、`spec_content`（从 analyze）、`project_summary`（从 analyze）
- `self_check` 接收 `test_results`（从 test）、`changes_made`（从 implement）、`spec_content`（从 analyze）、`task_groups`（从 plan，用作「功能遗漏」维度的 scope 参考）、`fix_iteration`（当前 fix loop 迭代次数）、`self_check_pass_index`（本轮 fix-loop 内的 1..N 序号）、`self_check_passes_required`（来自 `workflow.self_check_passes_required`）、`self_check_convergence_enabled`（来自 `workflow.self_check_convergence_enabled`，默认 false）、`prev_self_check_issues`（仅在 `convergence_enabled=true` 且 `pass_index=1` 时注入，承载上一轮 fix-loop 末尾 self_check 的 issues 作为收敛对比基线）
- `verify_spec` 接收 `changes_made`、`spec_content`（从 analyze）、`test_results`、`fix_iteration`、`spec_changes`（从 plan 步骤传递，用于区分有意变更与回归）和 `relevant_specs`（从 analyze）
- `update_spec` 接收 `changes_made`、`verification_result`、`spec_changes`（从 plan 步骤传递，作为变更指引清单）、`design_doc`（从 plan.design 映射，提供架构上下文）；默认以 `full_spec` 模式加载所有 spec 全文，支持命名查重和跨 spec 一致性检查
- `version_analyze` 接收 `changes_made`、`summary`、`verification_result`、`task_type`
- `commit` 接收 `changes_made`、`commit_message`（from version_analyze）、`bump_type`（from version_analyze）、`proposal`（from plan，供 commit message fallback chain 使用）、`updated_specs`（from update_spec，便于在 commit 时将 spec 变更与代码变更一起提交）
- `summarize` 接收所有前序输出（when included in step sequence）

#### Scenario: 步骤输入自动构建
- **WHEN** 流程转换到新步骤
- **THEN** 根据规则自动构建步骤输入
- **AND** 包含所有相关的前序输出

#### Scenario: update_spec 默认以 full_spec 模式加载
- **WHEN** state_machine 为 update_spec 构建输入
- **AND** `se3.yaml` 未显式配置 `spec_loading.steps.update_spec`
- **THEN** `update_spec` 的 `spec_content` 包含所有相关 spec 的完整文本（非 item 节选）
- **AND** LLM 可基于完整内容判断命名冲突和跨 spec 一致性

#### Scenario: update_spec 走新建 spec 判据后创建新 spec
- **GIVEN** update_spec 步骤以 full_spec 模式加载，看到所有现有 spec
- **WHEN** 实现引入了一个新子系统（如 Issue Discovery）
- **THEN** update_spec 的 LLM 在追加新 Requirement 前显式回答 4 项判据
- **AND** 判据结果指向 new_spec 时，在 `se3/specs/` 下创建新的 spec 目录和 `spec.md`
- **AND** 新 spec 文件被创建在 `se3/specs/issue-discovery/spec.md`

### Requirement: Version Analyze 步骤

`version_analyze` 步骤 SHALL 使用 LLM 智能分析实际变更内容，依据 Semantic Versioning 2.0.0 规则确定版本变更类型。

**分析输入：**
- `changes_made`: 变更的文件列表和详细说明
- `summary`: 前序步骤生成的工作摘要
- `verification_result`: 与 spec 的一致性检查结果
- `task_type`: 任务类型（作为参考，不作为决定因素）

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

**权威字段：** `suggested_version` 是 commit step 写入版本文件时直接使用的权威值。`bump_type`、`reasoning`、`confidence` 仅作为展示/commit message 辅助字段，不再用于按 `current_version + bump_type` 反推新版本号。当 `version_analyze` 失败或未输出 `suggested_version` 时，commit step 报错中断流程（见 Commit 步骤版本管理 requirement）。

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
- **THEN** 系统仍直接采用 `suggested_version` 写入版本文件
- **AND** 记录低置信度警告日志

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
2. 从 `version_analyze` 步骤读取 **`suggested_version`**（权威字段）
3. 若 `version_analyze` 失败或未输出 `suggested_version`，commit step 报错并中断流程（携带 current_version 与人工介入提示），不再进行默认 bump 兜底。这两个失败模式分别抛出独立的 `RuntimeError`，错误信息有所区分以辅助诊断：
   - `version_analyze` 步骤的最终状态为 `FAILED`：错误信息以 `"version_analyze step failed; cannot determine target version"` 开头，表明上游步骤本身失败（例如 LLM 调用或解析失败），即便存在残留 outputs 也不被信任为权威 `suggested_version`
   - `version_analyze` 步骤未失败但未产出有效 `suggested_version`（缺失、非字符串、或空字符串）：错误信息以 `"version_analyze did not produce a suggested_version"` 开头，表明步骤名义上完成但未承担版本权威字段的契约
   - 两条错误均携带 `current_version`（来自 `version_analyze.outputs.current_version`，回退到 `step.inputs.current_version`，再回退到 `"<unknown>"`）以及统一的人工介入指引：重跑 `version_analyze` 或在 `se3/calls/` 下创建 human call 手动提供版本号
4. 将 `suggested_version` 原样写入版本文件（原子写入 + 备份用于回滚）
5. 自动更新 README.md 和 VERSIONS.md（如配置了模板）
6. 将版本文件和文档变更一起提交

`bump_type` 不再参与新版本号的计算，仅作为 commit message / 渲染层的辅助字段。

**版本回滚机制：**
- 如果提交失败，自动回滚版本文件到原始版本
- 成功提交后清除备份，使版本变更永久生效

**配置选项（se3.yaml）：**
```yaml
version:
  enabled: true                    # 启用自动版本更新
  file_path: null                  # 版本文件路径（null=自动检测）
  include_in_commit_message: true  # 在提交消息中包含版本号
  auto_bump: true                  # 自动应用 suggested_version（无需确认）
  confidence_threshold: null       # 置信度阈值（null=总是自动）
  script_path: null                # 自定义版本脚本路径
  auto_generate_script: true       # 缺失时自动生成版本脚本

  # 文档更新模板
  templates:
    readme_badge: "![Version](https://img.shields.io/badge/version-{version}-blue)"
    versions_entry: "## {version} - {date}\n\n{changes}\n"
```

旧版 `version` 段中按 task_type 静态映射 bump_type 的配置项与智能分析总开关字段已废弃；在 `se3.yaml` 中保留也会被加载器静默忽略，不再影响版本流程。版本规则的项目级定制改由可选的 `se3/version-rules.md` 自然语言文件承载（见 `se3-versioning` *Custom Version Rules File* requirement）。

#### Scenario: Feature 任务自动更新版本
- **GIVEN** 当前版本为 1.2.3
- **AND** `version_analyze` 返回 `suggested_version: 1.3.0`
- **WHEN** commit 步骤执行
- **THEN** 版本文件被写入 `1.3.0`（直接采用 `suggested_version`）
- **AND** README.md 和 VERSIONS.md 自动更新
- **AND** 所有变更一起提交

#### Scenario: Bugfix 任务自动更新版本
- **GIVEN** 当前版本为 1.2.3
- **AND** `version_analyze` 返回 `suggested_version: 1.2.4`
- **WHEN** 执行 bugfix 类型的任务的 commit 步骤
- **THEN** 版本文件被写入 `1.2.4`
- **AND** 提交消息包含新版本号

#### Scenario: 版本更新失败回滚
- **GIVEN** 版本已成功 bump 但提交失败
- **WHEN** commit 步骤检测到提交错误
- **THEN** 自动将版本文件回滚到原始版本
- **AND** 报告错误信息

#### Scenario: suggested_version 缺失时 commit 报错中断
- **GIVEN** `version_analyze` 步骤状态非 FAILED，但输出中没有 `suggested_version`（缺失、非字符串、或空字符串）
- **WHEN** commit 步骤被触发
- **THEN** commit 步骤抛出 `RuntimeError`，错误信息以 `"version_analyze did not produce a suggested_version"` 开头，并包含当前版本与人工介入指引
- **AND** 不进行任何 patch bump 静默兜底
- **AND** 流程中断，等待用户重新运行 `version_analyze`、修订 `se3/version-rules.md` 或通过已有的人工介入机制提供版本号

#### Scenario: version_analyze 步骤 FAILED 时 commit 抛出独立错误
- **GIVEN** 最近一次 `version_analyze` 步骤的最终状态为 `FAILED`（例如 LLM 调用失败或解析失败）
- **WHEN** commit 步骤被触发
- **THEN** commit 步骤抛出 `RuntimeError`，错误信息以 `"version_analyze step failed; cannot determine target version"` 开头（与「未产出 suggested_version」场景的错误信息明确区分）
- **AND** 即便 FAILED 步骤的 outputs 中残留有 `suggested_version` 字段，commit 步骤也不会将其作为权威值采用
- **AND** 错误信息包含当前版本（`current_version`，回退链为 `version_analyze.outputs.current_version` → `step.inputs.current_version` → `"<unknown>"`）与统一的人工介入指引
- **AND** 流程中断，等待用户重新运行 `version_analyze` 或通过 `se3/calls/` 下的 human call 手动提供版本号

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

### Requirement: Step Status Lifecycle

The `StepStatus` enum (`se3/engine/models.py`) SHALL define the complete set of states a `Step` instance may occupy during a flow's execution. The full set comprises:

| Status | Value | Semantics |
|--------|-------|-----------|
| `PENDING` | `pending` | Step has not started yet |
| `RUNNING` | `running` | Step is currently executing |
| `COMPLETED` | `completed` | Step finished successfully |
| `PARTIAL` | `partial` | Step partially completed due to an unrecoverable constraint (e.g., permission restrictions) that the retry mechanism cannot resolve. **Distinct from `FAILED`**: `PARTIAL` is a terminal status for the current attempt — it does NOT trigger the standard retry loop and does NOT count as a successful completion either. The flow may continue with the partial outputs available, or escalate based on downstream consumers' tolerance. |
| `FAILED` | `failed` | Step failed after exhausting retries |
| `RETRYING` | `retrying` | Step is currently being retried after a transient failure |
| `PAUSED` | `paused` | Step is paused awaiting external input (user response, human MCP call response, programmatic confirmation, etc.) |
| `REVISION_NEEDED` | `revision_needed` | Step requested that the flow revise a prior step (e.g., self_check / verify_spec found issues, or a CONFIRM reviewer rejected the reviewed step). The state machine routes back to the appropriate prior step rather than advancing forward. |

**Persistence and JSON-serialization:** Step status values are stored in `engine.json` as their string `.value` (lowercase identifier). When a step handler stores its own status into `step.outputs` (e.g., as `step.outputs["result"]`), it MUST convert the enum to its `.value` before storing, consistent with the **Step outputs JSON serializability** scenario.

#### Scenario: PARTIAL is distinct from FAILED
- **WHEN** a step encounters an unrecoverable permission constraint (or analogous condition) that prevents full completion but does not warrant a retry
- **THEN** the handler SHALL set the step's status to `PARTIAL` rather than `FAILED`
- **AND** the standard retry loop is NOT triggered for `PARTIAL`
- **AND** the step is NOT treated as a successful `COMPLETED` either — downstream consumers that depend on full outputs may degrade or escalate accordingly

#### Scenario: All StepStatus values round-trip through persistence
- **GIVEN** a step in any of `PENDING`, `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED`, `RETRYING`, `PAUSED`, `REVISION_NEEDED`
- **WHEN** the flow is persisted to `engine.json` and later loaded
- **THEN** the loaded step's status equals the original enum value
- **AND** serialized values use the lowercase string form defined in the enum

### Requirement: Flow Status Lifecycle

The `FlowStatus` enum (`se3/engine/models.py`, mirrored in `se3/engine/schema.py`) SHALL define the complete set of overall states a `FlowInstance` may occupy. The full set comprises:

| Status | Value | Semantics |
|--------|-------|-----------|
| `INIT` | `init` | Flow has just been created; no step has started executing yet |
| `RUNNING` | `running` | Flow is actively executing steps |
| `PAUSED` | `paused` | Flow is paused awaiting user input, a programmatic confirmation, or a human MCP call response |
| `COMPLETED` | `completed` | All selected steps finished successfully |
| `FAILED` | `failed` | Flow encountered an unrecoverable condition (e.g., fix-loop exhaustion) and cannot continue |
| `RECOVERING` | `recovering` | Reserved state used while the engine is attempting to recover a flow from a prior interruption (e.g., loading state mid-resume). Persisted flows MAY round-trip through this state; downstream tooling SHALL accept it as a valid status value. |

**Persistence and JSON-serialization:** Flow status values are stored in `engine.json` as their string `.value` (lowercase identifier), and the loader SHALL accept every value listed above without raising. Forward compatibility: tooling that consumes `engine.json` SHALL treat `RECOVERING` as a transient state and not assume it is one of `INIT/RUNNING/PAUSED/COMPLETED/FAILED`.

#### Scenario: All FlowStatus values round-trip through persistence
- **GIVEN** a flow in any of `INIT`, `RUNNING`, `PAUSED`, `COMPLETED`, `FAILED`, `RECOVERING`
- **WHEN** the flow is persisted to `engine.json` and later loaded
- **THEN** the loaded flow's status equals the original enum value
- **AND** serialized values use the lowercase string form defined in the enum

#### Scenario: RECOVERING is accepted by deserialization
- **GIVEN** an `engine.json` whose top-level `status` field is `"recovering"`
- **WHEN** the engine loads the file
- **THEN** the flow is reconstructed with `status == FlowStatus.RECOVERING`
- **AND** no exception is raised for an unrecognized status value

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
- Exhaustion detection is centralized in `state_machine.transition_to_next()`: when `fix_iteration >= max_fix_iterations` (default 100), the flow is set to FAILED status, an A-class issue is generated, and execution stops
- **Sentinel:** when `max_fix_iterations == 0` (i.e. user configured `0` or `null`, both normalized to `0`), the exhaustion check is skipped entirely — the flow continues to dispatch fix loops indefinitely, prompts/log lines render the iteration as `N (unlimited)` rather than `N of M`, and no A-class fix-loop-exhaustion issue is filed. Negative integers are rejected fail-fast at config load (must be `>= 0`); only an explicit `0`/`null` opts into unlimited mode. The default `100` keeps the bound finite to avoid surprising token consumption; users opt into unlimited mode explicitly.

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
- The "New Spec Decision" section requires the LLM to evaluate four criteria (conceptual independence, dependency direction, naming test, cross-scenario reusability) before appending any new Requirement. See the `spec-guardrails` spec for the full criteria definition.
- `update_spec` defaults to `full_spec` loading mode so the LLM sees all spec files and can perform naming collision checks.

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

#### Scenario: update_spec enforces new spec decision criteria
- **GIVEN** the implementation introduces a new Requirement that does not fit any existing spec
- **WHEN** update_spec executes
- **THEN** the LLM evaluates the four criteria before deciding to append or create a new spec
- **AND** the JSON output includes `spec_decisions` with `decision: new_spec` and `reasoning`
- **AND** a new spec file is created following the standard structure

### Requirement: Implement Step DAG Execution Strategy

The `implement` step SHALL use an intelligent execution strategy that adapts based on total estimated lines of code and DAG topology.

**Execution Strategy Selection:**
- If there is exactly one task group, it is executed as a single LLM call directly (no threshold comparison needed).
- If there are multiple groups, the implement step computes total `estimated_loc` across all tasks in all groups (tasks missing the field default to 50 LOC each).
  - If total LOC ≤ `implement.group_loc_threshold` (default: 300, configurable in `se3.yaml`), all groups are merged into a single LLM call regardless of grouping.
  - If total LOC > threshold, the step would normally execute groups according to the DAG parallel strategy, subject to the two short-circuit rules below.
- **Short-circuit 1 — explicit disable:** If `implement.use_worktree` is `false`, the DAG parallel path is never entered. All groups execute sequentially in `group_order` order on the original branch, with no per-group worktree or `impl/*` branch. This holds regardless of DAG topology, including forked DAGs with real parallelism potential.
- **Short-circuit 2 — linear chain fallback:** If the DAG's `RelayPlan` is a linear chain (no forks and exactly one root — i.e. every topological layer has at most one node), the implement step falls back to sequential execution even when `use_worktree=true`. Linear chains yield no parallelism benefit, so the worktree/branch overhead is skipped.
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
- **AND** the groups' dependency graph has at least one fork (some node has more than one direct dependent)
- **AND** `implement.use_worktree` is `true` (default)
- **WHEN** the implement step executes
- **THEN** groups are executed via DAG parallel strategy with relay branching

#### Scenario: use_worktree=false forces sequential execution
- **GIVEN** `plan` produced groups with total estimated_loc > `implement.group_loc_threshold`
- **AND** the groups' dependency graph contains forks (would normally trigger DAG parallel)
- **AND** `implement.use_worktree: false` in `se3.yaml`
- **WHEN** the implement step executes
- **THEN** no `impl/*` branch or worktree is created for any group
- **AND** groups are executed sequentially in `group_order` order on the original branch
- **AND** the execution-strategy panel shows the sequential strategy, not `dag_parallel`
- **AND** the panel strategy line includes the short-circuit reason `use_worktree=False`

#### Scenario: Linear chain falls back to sequential
- **GIVEN** `plan` produced groups forming a linear chain G1 → G2 → G3
- **AND** total estimated_loc > `implement.group_loc_threshold`
- **AND** `implement.use_worktree` is `true` (default)
- **WHEN** the implement step executes
- **THEN** `_relay_plan_is_linear()` returns `true` for the computed `RelayPlan`
- **AND** the implement step falls back to sequential execution on the original branch
- **AND** no `impl/*` branch or worktree is created
- **AND** a log entry explains that the DAG was linear so worktree creation was skipped
- **AND** the panel strategy line includes the short-circuit reason `linear chain`

#### Scenario: Natural small-LOC sequential has no reason label
- **GIVEN** `plan` produced a single group or a multi-group plan whose routing naturally selects the sequential path without any short-circuit
- **WHEN** the implement step renders the task plan panel
- **THEN** the sequential strategy line omits the `reason:` suffix, preserving the original display

**Transitive Reduction:**
- Before DAG parallel execution, the implement step performs transitive reduction on group `depends_on` edges by calling the `transitive_reduce(groups)` function defined in `se3/engine/transitive_reduction.py`.
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
- Branch Relay applies only when the DAG parallel strategy is actually selected (see short-circuit rules above). For linear chains and `use_worktree=false`, the implement step executes sequentially on the original branch and the relay strategy is not used.
- For forks (G1 → G2, G1 → G3): G1 executes; G2 (primary heir, lowest group_order) reuses G1's worktree; G3 forks G1's branch into a new worktree. G2 and G3 execute in parallel.
- For convergence points (G2, G3 → G4): G4 inherits the primary predecessor's worktree and merges secondary predecessor branches before executing.
- The relay plan is produced by `classify_chains()` which computes `RelayPlan` containing: relay_map, fork_from, leaf_nodes, convergence_points, and root_nodes. Helper `_relay_plan_is_linear(plan)` is used by `implement_handler` to detect the linear-chain short-circuit — it returns `true` iff `plan.fork_from` is empty and `plan.root_nodes` has exactly one element.

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
- The LLM resolver retries up to a bounded number of attempts on failure.
- When the LLM resolver is exhausted, the leaf-merge path SHALL fall back to a deterministic `_take_theirs_fallback`: for every still-conflicting file, run `git checkout --theirs -- <file>` (i.e., accept the leaf branch's version verbatim) and `git add <file>`, then complete the merge commit with `git commit --no-edit`. Rationale: the leaf branch encapsulates the DAG implement output — the commits that MUST be preserved; `ours` (the pre-merge target) is typically upstream content the user already accepted overriding by running implement.
- Whenever the take-theirs fallback fires, an audit issue SHALL be filed via `IssueManager.create()` recording the flow_id, branch, and list of conflict files that were resolved deterministically (priority `medium`, type `task`, tags `["merge-fallback", "audit"]`). Audit-issue write failures are logged but never block the merge.
- If the take-theirs fallback's commit itself fails (e.g., from a misconfigured git state), the leaf merge aborts: the merge index is cleaned up and the caller is signaled to fail. There is no `pending_human` escalation on this path.
- **Scope:** this take-theirs fallback applies ONLY to the DAG leaf-merge inside the `implement` step. The `se3 merge` command path has separate, strictly stricter rules (see *`se3 merge` Conflict Resolution Mechanism*) — every `se3 merge` strategy resolves via LLM-as-editor or escalates to a human MCP call, and SHALL NEVER take-theirs. The two paths are intentionally distinct contracts.

#### Scenario: Leaf merge succeeds
- **GIVEN** a leaf group completed its work
- **WHEN** merging back to the original branch
- **THEN** a standard git merge is attempted
- **AND** if no conflicts, the merge completes normally

#### Scenario: Leaf merge with LLM conflict resolution
- **GIVEN** a leaf group's merge produces conflicts
- **WHEN** the merge conflict handler runs
- **THEN** the LLM receives full context (task descriptions, group summaries, conflict markers, specs)
- **AND** the LLM resolves all conflicting files within the bounded retry budget
- **AND** if all conflicts are resolved, the merge completes normally

#### Scenario: Leaf merge LLM exhaustion falls back to take-theirs
- **GIVEN** a leaf group's merge produces conflicts
- **AND** the LLM conflict resolver exhausts its retry budget without resolving every file
- **WHEN** the leaf-merge handler runs the deterministic fallback
- **THEN** for every still-conflicting file, `git checkout --theirs -- <file>` is invoked and the file is staged
- **AND** the merge is completed with `git commit --no-edit`
- **AND** an audit issue is filed via `IssueManager.create()` (priority `medium`, tags `["merge-fallback", "audit"]`) listing the flow_id, branch, and conflict files
- **AND** the merge is reported as successful to the caller

#### Scenario: Take-theirs fallback commit failure aborts the merge
- **GIVEN** the take-theirs fallback was triggered after LLM exhaustion
- **WHEN** the final `git commit --no-edit` fails
- **THEN** the merge index is cleaned up and the leaf merge is reported as failed
- **AND** no `pending_human` escalation is created on this path

#### Scenario: Take-theirs scope does not extend to `se3 merge`
- **GIVEN** any `se3 merge` invocation
- **WHEN** conflicts arise in that command's resolution loop
- **THEN** the DAG leaf-merge take-theirs fallback is NOT invoked
- **AND** `se3 merge` follows its own strategy contract (LLM-as-editor or human MCP escalation per `fast` / `safe` / `strict`), never silently accepting either side

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
- **Append-on-existence policy:** When salvaging a group-history file, if the target path already exists in the main repo, the salvaged file's content SHALL be appended to the existing file (line-based NDJSON concatenation) rather than overwritten. This policy is unconditional — it applies in every case where the target file already exists, including but not limited to the relay-chain scenario where multiple `GroupResult` objects share the same worktree, and also resume/retry paths where a prior partial salvage may have already written content for the same group. NDJSON is line-delimited, so concatenation preserves a valid stream and the merged file is replay-safe.
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

#### Scenario: Salvage appends when the target group-history file already exists
- **GIVEN** the main repo already contains a group-history file at `se3/history/{flow_id}/{step_id}_G{n}.jsonl` (e.g. from a prior partial salvage, a resumed DAG run, or an earlier worktree that contributed to the same group's history)
- **AND** a worktree being salvaged carries its own copy of that same target path under its `se3/history/{flow_id}/` directory
- **WHEN** `_salvage_history_from_worktree` copies the file back to the main repo
- **THEN** the worktree's file content is appended to the existing main-repo file (NDJSON line-based concatenation)
- **AND** the existing file content is preserved — no overwrite occurs
- **AND** the resulting merged file remains a valid NDJSON stream readable by `chat_history` consumers

### Requirement: `se3 merge` Conflict Resolution Mechanism

The standalone `se3 merge` orchestrator SHALL resolve every git-level merge conflict through an LLM-as-editor loop. This Requirement is the central contract for the conflict-resolution layer used by every merge strategy tier; per-tier escalation policies are defined separately under the `se3-commands` spec.

**Core rules:**

1. **Never take-theirs.** The orchestrator SHALL NOT, under any code path, fall back to `git checkout --theirs <file>`, `git merge -X theirs`, or any equivalent silent acceptance of the incoming side. This applies to every failure mode (context-build failures, LLM exceptions, parse failures, apply failures, human-call write failures, repair stalls). Every conflict — including conflicts in pyproject.toml or other deterministic-version files — is resolved either by the LLM editor loop or by escalation to a human MCP call (when the active strategy permits it); silent acceptance of either side is prohibited.

2. **LLM edits all conflict files in a single batched call.** When `git merge` produces conflicts, the orchestrator SHALL build a single LLM prompt that lists every conflicting file in that one merge invocation, together with merge metadata (ours/theirs branch names, merge-base, both HEAD hashes and oneline logs since merge-base) and the three-way base/ours/theirs contents plus the working-tree file containing `<<<<<<<` / `=======` / `>>>>>>>` markers for each. The LLM SHALL directly edit those working-tree files (e.g., via an `Edit` tool) to remove every conflict marker and produce a semantically reasonable merge — it MUST NOT emit a JSON `decision` field or a `resolved_content` blob for the orchestrator to splice in. Per-file LLM invocations are NOT used; a single batched edit call is the unit of work.

3. **Unresolved files trigger a whole-batch retry up to a configured upper bound.** After each edit round, the orchestrator SHALL scan every target file for any remaining `<<<<<<<` / `=======` / `>>>>>>>` marker. Files still containing a marker — together with their current residual content and the previous round's prompt context — are gathered into a new batch and re-submitted to the LLM as the next round. The iteration cap is `merge.max_conflict_resolve_iterations` (default 10; see se3-config). The orchestrator SHALL NOT abandon the batch early because of a single file's apparent failure: the loop continues, with the same not-take-theirs guarantee, until either every file is clean (no markers anywhere) or the iteration cap is reached.

4. **The active strategy decides what happens on cap exhaustion — LLM-only vs human fallback.** When the iteration cap is reached with at least one file still containing a conflict marker, the orchestrator SHALL hand control to the strategy layer:
   - `fast` mode SHALL fail the merge invocation outright, never invoking a human reviewer and never taking either side.
   - `safe` mode SHALL escalate to a human MCP call (`reviewer: human`, same mechanism as the `confirmation` step's human reviewer): the user edits the still-conflicting files until no marker remains, and the merge then resumes.
   - `strict` mode SHALL NOT enter the LLM editor loop at all — every conflict is routed directly to a human MCP call from the first iteration, without any LLM attempt.

   The choice is made entirely by the merge strategy (see se3-commands `se3 merge` Command); the conflict-resolver layer does not consult `conflict_resolver.strategy` or any other ad-hoc config — there is no `merge.conflict_resolver` configuration knob.

#### Scenario: Take-theirs is never invoked
- **GIVEN** any `se3 merge` invocation in any strategy
- **WHEN** the LLM throws, the prompt context cannot be built, a round of edits leaves files still containing conflict markers, the iteration cap is reached, or a human-call write fails
- **THEN** the orchestrator SHALL NOT silently checkout either side of the conflict
- **AND** no `git checkout --theirs`, `git checkout --ours`, or `--strategy-option theirs` invocation appears anywhere in the merge code path

#### Scenario: Single batched LLM call edits every conflicting file
- **GIVEN** a `git merge` produces conflicts in files `a.py`, `b.py`, and `se3/specs/x/spec.md`
- **WHEN** the conflict resolver builds the first round's prompt
- **THEN** all three files appear in one LLM call with their three-way contents and working-tree markers
- **AND** the LLM directly edits the working-tree files to remove every `<<<<<<<` / `=======` / `>>>>>>>` marker
- **AND** the orchestrator does NOT splice content from a JSON `decision` / `resolved_content` field — file state on disk is the sole source of truth

#### Scenario: Unresolved files re-enter the batch for another round
- **GIVEN** a first round of LLM edits leaves `a.py` clean but `b.py` and `c.py` still containing markers
- **WHEN** the orchestrator scans the working tree after the round
- **THEN** `b.py` and `c.py` (and only those) form the next round's batch
- **AND** the new round's prompt includes both the previous round's prompt context and the current residual state of those two files

#### Scenario: Fast mode exits without human fallback when the cap is hit
- **GIVEN** `merge.max_conflict_resolve_iterations` is 10 and `merge.strategy` is `fast`
- **WHEN** ten rounds of LLM edits still leave at least one conflict marker in some file
- **THEN** the merge invocation exits with a failure
- **AND** no human MCP call is created
- **AND** no take-theirs fallback is attempted

#### Scenario: Safe mode escalates to a human call when the cap is hit
- **GIVEN** the same setup with `merge.strategy: safe`
- **WHEN** the iteration cap is reached and conflict markers persist
- **THEN** a human MCP call is created (same mechanism as `confirmation.steps.<step>.reviewer: human`)
- **AND** the flow waits until the user has edited the residual files to remove every conflict marker
- **AND** no take-theirs fallback is attempted

#### Scenario: Strict mode never enters the LLM editor loop
- **GIVEN** `merge.strategy: strict` and a `git merge` reports conflicts in any file
- **WHEN** the orchestrator handles the conflict
- **THEN** no LLM call is issued
- **AND** a human MCP call is created from the very first iteration
- **AND** no take-theirs fallback is attempted

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

implement 步骤 SHALL 在输出中声明 `tests_added`、`test_mapping` 和 `estimated_test_duration`，形成与 test 步骤的显式契约。

**输出字段：**
- `tests_added`: 列表，本次新增的测试文件路径（相对于项目根目录）
- `test_mapping`: 字典，键为测试 ID，值为 spec scenario 标识（`{spec_name}::{scenario_name}`）
- `estimated_test_duration`: 整数，预估测试套件运行秒数。LLM 基于 `tests_added` 的数量与复杂度估算。test 步骤据此计算动态 timeout（详见 "Test Dynamic Timeout" 需求）。未提供或无效时，test 步骤回退至 `se3.yaml` 中配置的 `test.timeout`。

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

#### Scenario: implement 提供测试时长预估
- **WHEN** implement 步骤完成实现
- **THEN** 输出包含 `estimated_test_duration` 整数字段
- **AND** 状态机将该字段从 implement.outputs 转发到 test.inputs，供 test 步骤计算动态 timeout

#### Scenario: 超时后的预估修正
- **WHEN** test 步骤因动态 timeout 超时触发 fix loop
- **AND** implement 在 fix iteration 中收到包含 `timeout_reason`、`previous_timeout`、`previous_estimated_test_duration`、`timeout_multiplier` 的 `fix_context`
- **THEN** implement 在新的 JSON 输出中提供一个更大的 `estimated_test_duration`
- **AND** 下次 test 执行基于更新后的预估计算 timeout，避免反复超时的死循环

### Requirement: Block Rendering Visual Style

All SE3 user-facing rendered output blocks SHALL use a uniform reverse-color block visual: a reverse-color title at the top, the content body in the middle, and a fixed-width reverse-color footer at the bottom — no `Panel` border, no `Rule`, no horizontal lines, no left-side color bar, no terminal-width-adaptive visual elements.

**Visual primitives:**

- **Title block** — rendered as a Rich `Text` instance styled with `Style(color="white", bgcolor=<role_color>, bold=True)`. The text uses the markdown form `## Title` padded with a single space on each side (e.g. ` ## Discovery `) so the colored background visually frames the title. Using a `Text` object (not markup string interpolation) avoids markup-escape pitfalls in titles that contain user data.
- **Footer block** — a fixed-width reverse-color block of `_BLOCK_FOOTER_WIDTH = 4` spaces, rendered with `Style(bgcolor=<role_color>)` matching the title's role color. The footer width is constant (4 columns) regardless of terminal size; it SHALL NOT scale with the console width.
- **Layout order:** `title_block`, blank line, `content`, blank line, `footer_block`, blank line. The blank line between content and footer is mandatory — it prevents the footer from being copied when the user selects content.
- **Copy safety:** boundary markers are drawn purely via `bgcolor` over space characters, not via visible characters (no `─`, `═`, `┃`, `▔`, etc.). A user selecting and copying the content body never picks up boundary noise; selecting the footer copies only insignificant whitespace.

**Helper functions (single source of truth in `se3/engine/display.py`):**

- `_reverse_title(title: str, color: str) -> Text` — module-private helper that returns the title `Text` object.
- `_reverse_footer(color: str, width: int = _BLOCK_FOOTER_WIDTH) -> Text` — module-private helper that returns the fixed-width footer `Text` object.
- `render_block_header(title: str, color: str)` and `render_block_footer(color: str)` — thin public wrappers that print the header/footer (with appropriate blank-line spacing) directly to the console. These are the entry points used by modules outside `display.py` so external call sites do not construct `[reverse <color>] ## ... [/reverse <color>]` markup inline.

**Consumers:**

- All 8 render functions in `display.py` (`render_full`, `render_proposal`, `render_design`, `render_spec_content`, `render_text`, `render_code`, `render_diff`, `render_markdown`) use `_reverse_title` and `_reverse_footer` for their boundaries. The three delegating renderers (`render_proposal`, `render_design`, `render_spec_content`) inherit the visual transparently via `render_full`.
- `task_formatter._md_heading` and `_heading_group` are thin re-wraps of the display helpers. `_heading_group` SHALL append `_reverse_footer` (plus a trailing blank line) inside the returned `Group`, so all 8 task_formatter callers automatically gain the bottom boundary without per-callsite changes; callers continue to `console.print(...)` the returned renderable without changing their contract.
- External display call sites in `chat_history`, `sync`, `issue_cmd`, `worktree.py` (Merge Conflict prompt, red), `sync_interaction.py` (Decision Input panel, blue), and `discovery.py` (Discovery message, blue) SHALL invoke `display.render_block_header(title, color)` before the body and `display.render_block_footer(color)` after, rather than constructing markup strings directly.

**Color → role mapping (preserved from the prior `border_style` semantics):**

| Color | Role |
|-------|------|
| `blue` | general / prompt / discovery / decision input |
| `green` | code / response |
| `yellow` | diff |
| `magenta` | markdown |
| `red` | error / merge conflict |
| `cyan` | summary (task_formatter local use) |

#### Scenario: Footer width is constant across terminal widths
- **GIVEN** any rendered block produced by a `display.render_*` function or via `render_block_footer`
- **WHEN** the terminal is resized to any width (narrow or wide)
- **THEN** the footer block remains exactly `_BLOCK_FOOTER_WIDTH` (4) columns wide
- **AND** no part of the title/footer scales with terminal width
- **AND** no `Rule`, `Panel` outline, or horizontal-line element appears around the body

#### Scenario: Content selection excludes boundary characters
- **GIVEN** a rendered block consisting of reverse-color title, blank line, content, blank line, reverse-color footer
- **WHEN** the user selects and copies the content body
- **THEN** the copied text contains only the content body — no boundary characters leak in, because the title and footer are drawn entirely via background-colored spaces, not via visible glyphs
- **AND** the title and content are separated by a blank line, and the content and footer are separated by a blank line, so the footer is never adjacent to the last content line

#### Scenario: External modules use public block wrappers
- **WHEN** code outside `display.py` needs to render a titled block (e.g. `worktree.py` Merge Conflict prompt, `sync_interaction.py` Decision Input, `discovery.py` Discovery message, `chat_history`/`sync`/`issue_cmd` panels)
- **THEN** the call site invokes `display.render_block_header(title, color)` before the body and `display.render_block_footer(color)` after
- **AND** the call site does NOT hand-roll `[reverse <color>] ## Title [/reverse <color>]` markup strings inline
- **AND** no `Panel`, `Rule`, or other border element is added around the body

#### Scenario: task_formatter heading group auto-appends footer
- **GIVEN** a `task_formatter` helper that returns a `rich.console.Group` built via `_heading_group(title, color, *body)`
- **WHEN** the caller prints that Group to the console
- **THEN** the Group already contains the reverse-color title at the top and a matching reverse-color footer at the bottom (with the required blank-line spacing)
- **AND** the caller does not need to print a footer separately

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

The `implement` step SHALL display a structured task plan view at the start of execution, before any LLM calls are made, across all execution paths (single-call, LOC-merged single-call, DAG parallel, sequential).

**Rendering style:**

The plan is rendered inside a reverse-color block titled `## Implementation Plan` (white text on blue background), followed by a blank spacer, the body (a `rich.console.Group` of the sections below), a blank line, and a trailing fixed-width reverse-color blue footer block — produced via the shared helpers described in the **Block Rendering Visual Style** Requirement. No outer `Panel` border, `Rule`, or horizontal-line element is drawn. The `task_formatter` module exposes internal helpers `_md_heading(title, color)` and `_heading_group(title, color, *body)` that thin-wrap the shared display helpers; `_heading_group` SHALL append the matching reverse-color footer (plus trailing blank line) inside the returned `Group`, so all other formatter return values (Task Plan, Task Summary, Dependencies, task detail, etc.) automatically gain symmetric upper/lower boundaries. The helpers remain Group-returning functions — callers continue to `console.print(...)` the returned renderable without changing their contract. Color mapping from the previous `border_style` is preserved per call site (blue/green/cyan as appropriate).

**Body Contents:**

The body (rendered as a `rich.console.Group` under the heading) contains up to four sections:

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
- **THEN** the task plan view is displayed under the `## Implementation Plan` heading with "Single group → single LLM call (N LOC)" strategy
- **AND** the view shows the task group and LOC summary before the LLM call starts

#### Scenario: Task plan displayed before LOC-merged single-call execution
- **GIVEN** `plan` produced multiple groups with total LOC ≤ threshold
- **WHEN** the implement step begins execution
- **THEN** the task plan view is displayed under the `## Implementation Plan` heading with "Single LLM call (N LOC ≤ T threshold)" strategy
- **AND** the view shows all task groups and LOC summary before the LLM call starts

#### Scenario: Task plan displayed before DAG parallel execution
- **GIVEN** `plan` produced groups with total LOC > threshold and DAG topology
- **WHEN** the implement step begins execution
- **THEN** the task plan view is displayed under the `## Implementation Plan` heading with "DAG parallel" strategy
- **AND** the view shows group dependencies and per-group LOC estimates
- **AND** the view includes an Execution Topology section showing waves, LLM call numbering, and relay/fork/merge annotations

#### Scenario: Task plan display failure does not block execution
- **GIVEN** the task plan rendering raises an exception
- **WHEN** the implement step attempts to display the plan
- **THEN** the exception is caught and logged at DEBUG level
- **AND** execution proceeds normally without the plan display

### Requirement: Step Output Rendering — Analyze, Self Check, Verify Spec, Update Spec, Commit

The `analyze`, `self_check`, `verify_spec`, `update_spec`, and `commit` steps SHALL each use a custom renderer that presents structured, human-readable output instead of raw JSON key-value listing. All renderers use `render_full()` as their sole output interface, consistent with the Implement renderer's style.

#### Analyze Renderer

The `analyze` step renderer SHALL display a top-line status bar followed by reasoning and relevant spec items.

**Status Bar:**
- A single line showing `task_type`, `complexity`, and `scope` separated by `│` delimiters (e.g. `feature  │  medium  │  src/engine`).

**Sections (displayed in order when data is present):**
1. **Reasoning** — the analysis reasoning as a body paragraph.
2. **Relevant Spec Items** — a bullet list of selected spec identifiers rendered from `relevant_specs`.

**Hidden fields:** `spec_content` and `project_summary` are intentionally omitted from display — they are downstream data payloads, not user-facing information.

**Output keys consumed by the renderer:**
- `task_type`, `complexity`, `scope`, `reasoning`, `relevant_specs`

##### Scenario: Analyze rendering with all fields
- **WHEN** the analyze step completes with `task_type`, `complexity`, `scope`, `reasoning`, and `relevant_specs`
- **THEN** the renderer displays a status bar with task_type, complexity, and scope
- **AND** reasoning is shown as a labeled body paragraph
- **AND** relevant spec items are listed

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
- `status`, `issues`, `actionable_count`

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
- Top line shows the short commit hash (first 7 characters).
- Commit message is displayed below a separator.

**Output keys consumed by the renderer:**
- `committed`, `commit_hash`, `commit_message`

##### Scenario: Commit displays short hash and message
- **WHEN** the commit step completes with `committed: true`
- **THEN** the renderer displays the short hash on the top line
- **AND** the commit message is displayed below

##### Scenario: No changes committed
- **WHEN** the commit step completes with `committed: false`
- **THEN** the renderer displays `No changes to commit`

### Requirement: Step Output Renderer Registry

The flow engine SHALL provide a centralized registry mechanism for step output rendering, with a single public entry point (`render_step_output(step)`) that dispatches to step-specific renderers or falls back to a generic default renderer.

**Registry Mechanism (`step_renderers.py`):**

- `STEP_RENDERERS: Dict[StepType, Callable[[Step], None]]` — module-level registry mapping step types to their custom renderer functions.
- `register_renderer(step_type)` — decorator that registers a function as the renderer for a given `StepType`.
- `render_step_output(step)` — public entry point: looks up `step.step_type` in `STEP_RENDERERS`; if found, calls the registered renderer; otherwise, falls back to `_default_render(step, title)`.
- `STEP_DISPLAY_TITLES` — module-level dict mapping every `StepType` (including deprecated types `PROJECT_SUMMARY`, `PROPOSE`, `DESIGN`, `PLAN_TASKS`) to a human-readable title used by both custom renderers and the default fallback.

**Default Renderer (`_default_render`):**

When no custom renderer is registered for a step type, the default renderer iterates `step.outputs`, formatting each key-value pair. Long string values (>300 chars) are previewed (first 200 chars) with a length suffix. Non-completed status is shown explicitly. Error messages are rendered in red at the bottom.

**Helper for partial rendering (`_render_remaining`):**

Custom renderers MAY call `_render_remaining(step, title, skip_keys)` to display any output keys not already covered by their specialized rendering. The remaining keys are rendered under a "{title} — Additional Details" header using the same generic formatting as the default renderer.

**Registered renderers beyond the base set:**

In addition to `analyze`, `self_check`, `verify_spec`, `update_spec`, `commit`, and `implement` (covered under the **Step Output Rendering** Requirement), the registry SHALL include custom renderers for the following step types:

#### Plan Renderer (`PLAN`)

Renders three sections in order when data is present:

1. **Proposal** — when `outputs["plan"]["proposal"]` is a dict, delegates to `render_proposal()`; when a string, renders as a labeled line under title "Planning — Proposal".
2. **Design** — when `outputs["plan"]["design"]` is a dict, delegates to `render_design()`; when a string, renders as a labeled line under title "Planning — Design".
3. **Task Groups** — for each group in `outputs["task_groups"]`, renders a line containing group_id (bold), name, task count, total estimated LOC (summed from each task's `estimated_loc`), and dependencies (comma-separated `depends_on` list, or "none").

After the three sections, `_render_remaining` is invoked with skip set `{"plan", "task_groups", "total_complexity", "estimated_effort"}` to render any other output keys. If none of the three sections rendered anything, falls back to `_default_render`.

#### Version Analyze Renderer (`VERSION_ANALYZE`)

Renders:
- **Top line** — `current_version → suggested_version` (with `suggested_version` styled as cyan bold, marking it as authoritative).
- **Sub-line** — dim style showing `{bump_type} bump  │  confidence: {confidence}`.
- **Reasoning** — when present, rendered as a labeled section below a dim horizontal separator.
- **Error** — when `step.error_message` is present, rendered in red.

Rendered under title "Version Analysis".

#### Summarize Renderer (`SUMMARIZE`)

When `outputs["summary"]` is non-empty, delegates to `render_markdown(summary, title="Work Summary")`. When absent, renders nothing.

#### Test Renderer (`TEST`)

When `outputs["test_results"]` is not a dict, falls back to `_default_render`. Otherwise renders:
- **Status line** — `PASSED` (green bold) or `FAILED` (red bold) based on `overall_passed` (or legacy `passed`).
- **Phases line** — count of passed vs. failed phases, followed by a per-phase bullet list with `✓` (green) / `✗` (red) indicators and phase names.
- **Command line** — the underlying test command, when present.

Rendered under title "Testing".

#### Propose Renderer (`PROPOSE`, deprecated)

Searches `outputs` for the first present key in `("proposal", "proposal_data")` that maps to a dict; when found, delegates to `render_proposal()`, then renders remaining outputs via `_render_remaining` with skip set `{proposal_key, "summary", "files_to_modify", "files_to_create"}`. When no proposal dict is found, falls back to `_default_render`.

This renderer is retained for backward compatibility with persisted flows containing pre-unification `PROPOSE` steps (see **Deprecated Step Type Backward Compatibility** Requirement).

#### Design Renderer (`DESIGN`, deprecated)

Searches `outputs` for the first present key in `("design", "design_doc", "design_document")` that maps to a dict; when found, delegates to `render_design()`, then renders remaining outputs via `_render_remaining` with skip set `{design_key, "decisions", "components", "implementation_plan"}`. When no design dict is found, falls back to `_default_render`.

Retained for backward compatibility with persisted flows containing pre-unification `DESIGN` steps.

#### Scenario: Custom renderer dispatch
- **WHEN** `render_step_output(step)` is invoked with `step.step_type` registered in `STEP_RENDERERS`
- **THEN** the registered renderer function is called with the step

#### Scenario: Default renderer fallback
- **WHEN** `render_step_output(step)` is invoked with a step type not in `STEP_RENDERERS`
- **THEN** `_default_render(step, title)` is invoked, iterating all outputs with generic formatting
- **AND** the title is looked up from `STEP_DISPLAY_TITLES`, falling back to `step.step_type.value`

#### Scenario: Plan renderer displays task groups with LOC
- **WHEN** the plan step completes with non-empty `task_groups`
- **THEN** each group renders one line containing group_id, name, task count, summed `estimated_loc`, and dependencies

#### Scenario: Version analyze highlights suggested_version
- **WHEN** the version_analyze step completes
- **THEN** `suggested_version` is rendered in cyan bold, distinguishing it as the authoritative value used by the commit step
- **AND** `bump_type` and `confidence` render as dim auxiliary text

#### Scenario: Summarize renderer uses markdown
- **WHEN** the summarize step completes with non-empty `outputs["summary"]`
- **THEN** the summary is rendered via `render_markdown` under title "Work Summary"

#### Scenario: Test renderer with structured results
- **WHEN** the test step completes with `outputs["test_results"]` containing `phases`
- **THEN** the renderer shows per-phase pass/fail indicators and an overall PASSED/FAILED status line

#### Scenario: Deprecated PROPOSE renderer retained
- **WHEN** a persisted flow with a `PROPOSE` step is loaded and rendered
- **THEN** `_render_propose` extracts the proposal dict (under key `proposal` or `proposal_data`) and delegates to `render_proposal()`
- **AND** remaining outputs are rendered via `_render_remaining`

#### Scenario: Deprecated DESIGN renderer retained
- **WHEN** a persisted flow with a `DESIGN` step is loaded and rendered
- **THEN** `_render_design` extracts the design dict (under key `design`, `design_doc`, or `design_document`) and delegates to `render_design()`
- **AND** remaining outputs are rendered via `_render_remaining`

### Requirement: Test 步骤配置与多阶段执行

test 步骤 SHALL 支持通过 `se3.yaml` 的 `test:` 配置段进行多阶段测试，并输出结构化结果。

**se3.yaml 配置：**
```yaml
test:
  command: null                # 主测试命令（null=自动检测）
  timeout: 1800                # 秒（动态 timeout 不可用时的回退值）
  timeout_multiplier: 2.0      # 动态 timeout 乘数（与 implement 的 estimated_test_duration 相乘）
  min_dynamic_timeout: 30      # 动态 timeout 下限（秒）
  max_dynamic_timeout: 14400   # 动态 timeout 上限（秒，防止预估失控）
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
- Backward compatibility — legacy `fix_instructions_summary` read path: old fix_history entries persisted before the structured-issues migration may carry a `fix_instructions_summary` string instead of an `issues` list. `_format_fix_history()` SHALL treat this field as an optional fallback: when an iteration entry has no `issues` list (or the list is empty) but does carry a non-empty `fix_instructions_summary`, the formatter renders that summary text in place of the issues block so the legacy entry remains usable in the implement prompt. This read path exists solely to keep pre-migration flows renderable; new entries written by the state machine SHALL NOT include `fix_instructions_summary`, and downstream tooling SHOULD NOT rely on its presence.

**Source-aware storage policy:**
- Fix instructions from test.py (raw test output: "Tests are failing..." + failures + stderr) are NOT stored in fix_history — the `reason` field ("test_failure") records the trigger, and current test output is always available in the next iteration.
- Fix instructions from verify_spec (LLM-generated analysis and repair guidance) are preserved via the structured `issues` list, which captures the LLM's diagnostic intent for avoiding repeated fix directions.

**Sliding-window cap (`FIX_HISTORY_MAX_ENTRIES`):**

The `State.fix_history` list SHALL be capped at `FIX_HISTORY_MAX_ENTRIES` (defined in `se3/engine/models.py`, default `100`) by a tail-keep policy: when an append via `State.increment_fix_iteration()` causes the list to exceed the cap, the oldest entries are dropped so only the most recent `FIX_HISTORY_MAX_ENTRIES` entries are retained. The same tail-keep policy is applied at deserialization time (`State.from_dict`) so `engine.json` files written by older builds without the cap are clamped on load.

**Rationale:** the cap bounds memory, on-disk `engine.json` size, and per-transition deepcopy cost in unlimited mode (`workflow.max_fix_iterations = 0`). The default value is held at the same numeric floor as `DEFAULT_MAX_FIX_ITERATIONS` so a flow running with the default fix-loop bound never silently drops history entries — trimming can only happen when the user explicitly raises `workflow.max_fix_iterations` above the cap (e.g. 200) or runs in unlimited mode for more than `FIX_HISTORY_MAX_ENTRIES` iterations.

**Downstream impact on prompts:**
- The shared `render_fix_context()` helper used by verify_spec and self_check accepts `fix_history` as a parameter but deliberately does NOT render it into the `{fix_context}` block. Only `prev_issues` (the issues reported in the immediately preceding review pass) is rendered. The `fix_history` parameter is kept in the signature for back-compatibility with existing callers and treated as an explicit no-op (`_ = fix_history`) inside the function. **Rationale:** the iteration-count + trigger-type metadata in fix_history biases self_check / verify_spec reviewers toward over-flagging ("we've been at this N rounds, surely something is still wrong"); for review steps the signal is more harmful than helpful, so the cap on fix_history has no impact on review-step prompts regardless of size.
- `implement._format_fix_history` iterates the full persisted list; for iterations beyond `FIX_HISTORY_MAX_ENTRIES`, early-iteration entries are dropped from the implement-step prompt. A fix loop running past this point on the same task is considered a stuck loop where ancient history adds noise rather than signal.

#### Scenario: fix_history capped at FIX_HISTORY_MAX_ENTRIES on append
- **GIVEN** `State.fix_history` already contains `FIX_HISTORY_MAX_ENTRIES` entries
- **WHEN** `State.increment_fix_iteration()` appends another entry
- **THEN** the list is trimmed back to `FIX_HISTORY_MAX_ENTRIES` entries by dropping the oldest
- **AND** the most recently appended entry is retained as the last element

#### Scenario: fix_history capped on deserialization
- **GIVEN** an `engine.json` written by an older build whose persisted `fix_history` exceeds `FIX_HISTORY_MAX_ENTRIES`
- **WHEN** `State.from_dict()` loads the file
- **THEN** the loaded `fix_history` is clamped to the most recent `FIX_HISTORY_MAX_ENTRIES` entries via the same tail-keep policy

#### Scenario: Fix history stores structured issues
- **WHEN** the state machine records a fix loop entry from verify_spec
- **THEN** the entry's `issues` list contains normalized issue dicts with both `severity` and `priority` fields
- **AND** no `fix_instructions_summary` field is stored

#### Scenario: Fix history prev_issues cap aligned at 20
- **WHEN** the state machine builds inputs for the verify_spec step during a fix iteration
- **THEN** `prev_issues` is capped at 20 entries, matching the verify_spec prompt's display limit

#### Scenario: prev_issues render-time tail cap in fix-context block
- **GIVEN** the shared `render_fix_context()` helper (`se3/engine/steps/_fix_context.py`), consumed by both `verify_spec` and `self_check` to render the `{fix_context}` slot of their LLM prompts
- **WHEN** the `prev_issues` list passed in contains more than `PREV_ISSUES_RENDER_TAIL` (default 20) entries
- **THEN** only the first `PREV_ISSUES_RENDER_TAIL` issues are rendered into the `## Previously Reported Issues` section of the prompt
- **AND** a trailing line of the form `- ... and N more issues (truncated)` is appended, where N is `len(prev_issues) - PREV_ISSUES_RENDER_TAIL`
- **AND** this render-time cap is independent of the state-machine input-plumbing cap (also 20): both defaults are deliberately set to the same value so the two layers stay aligned, while the render-time cap acts as the last line of defense if any upstream caller bypasses the input-plumbing cap
- **AND** `PREV_ISSUES_RENDER_TAIL` is exposed at module level in `_fix_context.py` so tests and other consumers can reference it by name rather than relying on positional substring matches

#### Scenario: test 通过后进行代码自检
- **WHEN** test 步骤执行完成且 `overall_passed` 为 true
- **THEN** test 步骤返回 `COMPLETED` 状态
- **AND** 流程继续到 self_check 步骤进行 LLM 代码审查（对于 feature/bugfix/discovery 工作流）
- **AND** self_check 通过后继续到 verify_spec 步骤进行 spec 合规性检查

#### Scenario: verify_spec 检查 spec coverage
- **WHEN** verify_spec 接收到 `test_mapping`
- **THEN** 检查 spec scenario 的测试覆盖
- **AND** 未覆盖的 scenario 记为 warning

### Requirement: Test Dynamic Timeout

test 步骤的主测试命令 SHALL 支持基于 implement 步骤预估的动态 timeout 机制，避免对所有项目使用同一个固定 timeout 值。

**计算公式：**
```
effective_timeout = clamp(
    estimated_test_duration * timeout_multiplier,
    min = test.min_dynamic_timeout,
    max = test.max_dynamic_timeout,
)
```

**参数来源：**
- `estimated_test_duration`: 来自 implement 步骤的 JSON 输出（经由状态机从 implement.outputs 转发到 test.inputs）
- `timeout_multiplier`: 来自 `se3.yaml` 的 `test.timeout_multiplier`（默认 2.0，加载时被 clamp 到 >= 1.0）
- `min_dynamic_timeout` / `max_dynamic_timeout`: 来自 `se3.yaml`（默认 30 / 14400 秒），防止预估过小或在超时修复循环中失控放大

**Fallback 规则：**
- 当 `estimated_test_duration` 缺失、非整数、或 <= 0 时，主命令的 timeout 回退为 `se3.yaml` 中配置的 `test.timeout`（默认 1800 秒）
- 这确保了旧项目（implement 未提供该字段）或 LLM 遗漏该字段时的向后兼容

**作用范围：**
- 动态 timeout **仅作用于** test 步骤执行的主测试命令
- `phases` 中显式配置的阶段 **不受影响**，继续使用各自 phase 配置中的 `timeout` 值

**超时检测与 fix loop：**

当主命令因动态 timeout 被终止时（例如 Python subprocess 返回 returncode == -1，或 stderr 包含 `Timeout after` 标记），test 步骤 SHALL：

1. 将失败作为普通 test failure 处理，返回 `REVISION_NEEDED` 触发 fix loop
2. 在 `fix_context` 中附加以下超时元数据：
   - `timeout_reason`: 人类可读的超时原因文本
   - `previous_timeout`: 本次实际使用的 timeout 秒数
   - `previous_estimated_test_duration`: 本次使用的预估值（若有）
   - `timeout_multiplier`: 本次使用的乘数
   - `timeout_at_cap`: 布尔值，指示本次 timeout 是否已触及 `max_dynamic_timeout` 上限

implement 步骤的 FIX_PROMPT SHALL 识别 `fix_context` 中的超时元数据，并在新一轮 JSON 输出中提供一个严格大于 `previous_estimated_test_duration` 的 `estimated_test_duration` 值。这使得下一次 test 执行具有更大的 timeout，打破「估算不足 → 超时 → 再次估算不足」的死循环。

#### Scenario: 主命令使用动态 timeout
- **GIVEN** implement 输出 `estimated_test_duration: 120`
- **AND** `se3.yaml` 中 `test.timeout_multiplier: 2.0`
- **WHEN** test 步骤运行主命令
- **THEN** 主命令使用的 timeout 为 240 秒（120 × 2.0，在 min/max 范围内）

#### Scenario: estimated_test_duration 缺失时的回退
- **GIVEN** implement 输出中不包含 `estimated_test_duration`（或值无效）
- **WHEN** test 步骤运行主命令
- **THEN** 主命令使用 `se3.yaml` 中的 `test.timeout`（默认 1800 秒）

#### Scenario: 动态 timeout 下限钳制
- **GIVEN** implement 输出 `estimated_test_duration: 5`
- **AND** `se3.yaml` 中 `test.timeout_multiplier: 2.0` 且 `test.min_dynamic_timeout: 30`
- **WHEN** test 步骤运行主命令
- **THEN** 主命令使用的 timeout 被钳制到 30 秒（而非 10 秒）

#### Scenario: 动态 timeout 上限钳制
- **GIVEN** implement 输出了极大的 `estimated_test_duration`
- **WHEN** 计算结果超过 `test.max_dynamic_timeout`
- **THEN** 实际 timeout 被钳制到 `max_dynamic_timeout`
- **AND** 传递给 implement 的 `fix_context` 中 `timeout_at_cap` 为 true

#### Scenario: phases 不受动态 timeout 影响
- **GIVEN** implement 输出 `estimated_test_duration: 120`
- **AND** `se3.yaml` 中一个 phase 显式配置 `timeout: 600`
- **WHEN** test 步骤执行该 phase
- **THEN** 该 phase 仍然使用其自身配置的 600 秒 timeout，不应用动态计算

#### Scenario: 主命令超时触发 timeout-aware fix loop
- **WHEN** 主命令因动态 timeout 被终止
- **THEN** test 步骤返回 `REVISION_NEEDED`
- **AND** `fix_context` 包含 `timeout_reason`、`previous_timeout`、`previous_estimated_test_duration`、`timeout_multiplier` 和 `timeout_at_cap`

#### Scenario: implement 在 fix iteration 中提升预估
- **GIVEN** test 步骤因超时失败并在 `fix_context` 中附带超时元数据
- **WHEN** implement 在 fix iteration 中重新执行
- **THEN** implement 的 JSON 输出中 `estimated_test_duration` 严格大于 `previous_estimated_test_duration`
- **AND** 下一次 test 执行使用更新后的预估计算更大的 timeout

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

**New Spec vs Append 判据（由 spec-guardrails 定义，update_spec 强制执行）：**
1. **概念独立性** — 新内容是否与现有 spec 属于同一概念域。
2. **依赖方向** — 添加新 Requirement 是否会导致现有 Requirement 反向依赖它。
3. **命名测试** — 新 Requirement 是否能在现有 spec 标题下自然命名。
4. **跨场景共享** — 新内容是否会被多个 capability 复用（跨切面关注点应独立成 spec）。

#### Scenario: 新子系统触发新 spec 创建
- **WHEN** 实现引入了一个新的子系统（如 Issue Discovery）
- **AND** 该子系统没有对应的 spec 文件
- **THEN** update_spec 步骤在追加前走 4 项判据
- **AND** 判据结果指向 new_spec 时，在 `se3/specs/` 下创建新的 spec 目录和 `spec.md`
- **AND** 新 spec 包含 Purpose、Requirements、Scenarios 等标准结构
- **AND** 新 spec 首行包含 `<!-- spec-format: v1 -->`
- **AND** `spec_decisions` 输出记录该决策及 reasoning

### Requirement: Workflow Config Tolerant Parsing

The `WorkflowConfig.from_dict()` parser in `se3/config.py` SHALL apply tolerant parsing to the `workflow.*` fields it accepts from `se3.yaml`, distinguishing between recoverable type-coercion cases (warn and fall back to default) and invariant violations (fail-fast via `ConfigError`). This ensures malformed YAML produces actionable diagnostics without crashing flow startup for benign type mismatches.

**Parsing policy by field:**

| Field | Default | Tolerant Cases (warn + fallback) | Fail-Fast Cases (`ConfigError`) |
|-------|---------|----------------------------------|---------------------------------|
| `max_fix_iterations` | `DEFAULT_MAX_FIX_ITERATIONS` (100) | `bool`, `float` (incl. `0.0` — explicit warning that the unlimited sentinel must be literal `int 0` or `null`/`None`, not `0.0`), arbitrary strings or other types not coercible via `int()` | Negative integers (`< 0`) — rejects typos that would otherwise silently disable exhaustion. Only literal `0` or `null` opts into unlimited mode (with `null` normalized to `0`). |
| `self_check_passes_required` | `DEFAULT_SELF_CHECK_PASSES_REQUIRED` (1) | `bool`, `float`, arbitrary strings or other types not coercible via `int()` | `< 1` after coercion — preserves the documented invariant that at least one pass is required. |
| `self_check_convergence_enabled` | `DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED` (`false`) | Coerced via `_coerce_bool()`. If `raw_convergence is not None and not isinstance(raw_convergence, (bool, int, float))`: when the value is a string whose stripped/lowercased form is NOT in `{"true", "1", "yes", "on", "false", "0", "no", "off"}`, a warning is emitted; for any other non-(bool/int/float/str) type, a warning is also emitted. Valid string forms (e.g. `"true"`, `"YES"`, `" off "`) coerce silently. | — (no fail-fast cases; always falls back to default on invalid input) |

**Warning format:**

Warnings SHALL include the offending raw value (via `repr()`), the field name, and the fallback default value, so users can locate the malformed entry in their YAML. Example: `workflow.self_check_convergence_enabled='maybe' is not a valid boolean; falling back to default False`.

**Loading entry points:**

- `WorkflowConfig.from_dict(data)` — pure parser, accepts a dict (typically the contents of `se3.yaml`).
- `WorkflowConfig.load(project_root)` — reads the project YAML via `load_project_yaml` and delegates to `from_dict`; returns defaults when the YAML is missing or empty.
- `load_workflow_config(project_root=None)` — module-level convenience wrapper defaulting `project_root` to `Path.cwd()`.

#### Scenario: Unlimited sentinel via null
- **GIVEN** `se3.yaml` contains `workflow.max_fix_iterations: null`
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** `max_fix_iterations` is set to `0` (unlimited)
- **AND** no warning is emitted

#### Scenario: Float 0.0 is rejected with explicit guidance
- **GIVEN** `se3.yaml` contains `workflow.max_fix_iterations: 0.0`
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a warning is emitted explaining that the unlimited sentinel must be the literal int `0` or `null`/`None`, not `0.0`
- **AND** `max_fix_iterations` falls back to `DEFAULT_MAX_FIX_ITERATIONS`

#### Scenario: Negative max_fix_iterations fails fast
- **GIVEN** `se3.yaml` contains `workflow.max_fix_iterations: -1`
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a `ConfigError` is raised explaining that the value must be `>= 0`
- **AND** the error message guides the user to `0` or `null` for unlimited mode

#### Scenario: self_check_passes_required less than 1 fails fast
- **GIVEN** `se3.yaml` contains `workflow.self_check_passes_required: 0`
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a `ConfigError` is raised explaining the value must be `>= 1`

#### Scenario: self_check_passes_required non-integer falls back
- **GIVEN** `se3.yaml` contains `workflow.self_check_passes_required: "two"` (or a bool/float)
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a warning is emitted naming the offending value and the fallback default
- **AND** `self_check_passes_required` is set to `DEFAULT_SELF_CHECK_PASSES_REQUIRED`

#### Scenario: self_check_convergence_enabled string coercion succeeds silently
- **GIVEN** `se3.yaml` contains `workflow.self_check_convergence_enabled: "true"` (or `"YES"`, `" off "`, etc. — any form in the recognized truthy/falsy set after strip/lowercase)
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** the value is coerced via `_coerce_bool()` without warning
- **AND** `self_check_convergence_enabled` reflects the coerced boolean

#### Scenario: self_check_convergence_enabled unrecognized string warns and falls back
- **GIVEN** `se3.yaml` contains `workflow.self_check_convergence_enabled: "maybe"`
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a warning is emitted naming the offending value and the fallback default
- **AND** `self_check_convergence_enabled` is set to `DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED` (`false`)

#### Scenario: self_check_convergence_enabled non-scalar warns and falls back
- **GIVEN** `se3.yaml` contains `workflow.self_check_convergence_enabled` set to a list, dict, or other non-(bool/int/float/str/None) value
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a warning is emitted naming the offending value and the fallback default
- **AND** `self_check_convergence_enabled` is set to `DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED`

#### Scenario: Missing or empty workflow section uses defaults
- **GIVEN** `se3.yaml` has no `workflow:` section, or `workflow:` is not a dict
- **WHEN** `WorkflowConfig.from_dict()` (or `load()`) is invoked
- **THEN** a default `WorkflowConfig` is returned with no warnings

### Requirement: FlowInstance Persistence Schema

The `FlowInstance` dataclass (`se3/engine/models.py`) SHALL persist a fixed set of tracking, integration, and loop-mode fields across runs via `to_dict()` / `from_dict()`. These fields extend the core `flow_id`/`task_description`/`task_type`/`status`/`state` set documented in the **数据模型** section and are required for `se3 run --resume`, `se3 run --loop`, change-tracking integration, and multi-worktree baseline detection to work correctly.

**Field schema:**

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `created_at` | `datetime` | `datetime.now()` | Flow creation timestamp; ISO-formatted on disk. |
| `updated_at` | `datetime` | `datetime.now()` | Last-mutation timestamp; ISO-formatted on disk. Drives the ordering shown in `se3 history`. |
| `completed_at` | `datetime \| None` | `None` | Set when the flow reaches a terminal status; ISO-formatted on disk when present. |
| `change_name` | `str \| None` | `None` | Optional SE3 change name the flow is associated with. Set by `se3 run --change <name>`; consumed by SE3 change-tracking integration. |
| `change_path` | `Path \| None` | `None` | Filesystem path to the associated change; serialized as a string and rehydrated to `Path` on load. |
| `source_issue_id` | `str \| None` | `None` | Issue ID (from issue-discovery) that originated this flow, when the flow was triggered from an open issue. Used to thread completion back to the issue tracker. |
| `baseline_commit` | `str \| None` | `None` | Git HEAD recorded by `init_flow()` at the start of the flow. Used by the commit step's change-detection logic and by DAG worktree salvage (see *步骤间输入传递* and the implement-step worktree management scenarios) so re-entries compute deltas against the original baseline rather than current HEAD. |
| `is_loop_mode` | `bool` | `False` | True when the flow was started via `se3 run --loop`. Distinguishes loop-mode flows for resume routing, history filtering, and the loop-iteration prompt prefixing described in *循环模式外部包装架构*. |
| `loop_branch` | `str \| None` | `None` | The `loop/{task_id}-{iteration}` (or legacy `se3-loop/{timestamp}`) branch created for this loop iteration via `create_loop_branch()`. `None` when `is_loop_mode=False` or `--no-worktree` is in effect. |
| `loop_worktree_path` | `str \| None` | `None` | Filesystem path of the git worktree backing `loop_branch`. `None` when the flow is not running inside a loop worktree. |
| `loop_original_branch` | `str \| None` | `None` | The branch that was checked out when `se3 run --loop` was invoked. Captured so the loop controller can offer `merge` / `later` / `discard` against the correct destination after the iteration finishes (see *延迟合并* scenario). |

**Persistence rules:**

- `to_dict()` SHALL include every field above using the JSON-friendly representation indicated (ISO strings for datetimes, `str(path)` for `Path`, raw value otherwise).
- `from_dict()` SHALL accept missing optional fields and substitute their defaults via `data.get(...)`, so flows persisted by older builds (which may not have written every field) continue to load without error.
- `from_dict()` SHALL convert `change_path` from string back to `Path` when present, and SHALL substitute `False` for a missing `is_loop_mode`.

#### Scenario: All FlowInstance fields round-trip through persistence
- **GIVEN** a `FlowInstance` populated with non-default values for every field listed above
- **WHEN** the instance is serialized via `to_dict()` and reconstructed via `from_dict()`
- **THEN** every field equals its original value (with `Path` correctly rehydrated and datetimes round-tripping through ISO format)

#### Scenario: Older engine.json without loop-mode fields loads cleanly
- **GIVEN** an `engine.json` written by a build that predates `is_loop_mode` / `loop_branch` / `loop_worktree_path` / `loop_original_branch`
- **WHEN** `FlowInstance.from_dict()` loads the file
- **THEN** the missing loop-mode fields default to `False` / `None` and no `KeyError` is raised
- **AND** the loaded flow can be resumed normally (loop-controller logic interprets the absence of loop fields as "not a loop-mode flow")

#### Scenario: change_path round-trips as Path
- **GIVEN** a `FlowInstance` with `change_path = Path("se3/changes/auth-rewrite")`
- **WHEN** the flow is persisted and reloaded
- **THEN** the loaded instance's `change_path` is a `Path` object (not a `str`) equal to the original

#### Scenario: source_issue_id threads the originating issue
- **GIVEN** a flow created from an open issue (e.g. via issue-discovery resolution)
- **WHEN** `source_issue_id` is set on the `FlowInstance` and the flow is persisted
- **THEN** `to_dict()` includes the issue id and `from_dict()` restores it
- **AND** downstream tooling (issue tracker integration) can read `flow.source_issue_id` to thread completion back to the originating issue

#### Scenario: baseline_commit anchors change detection
- **GIVEN** `init_flow()` records the git HEAD as `baseline_commit` on a fresh flow
- **WHEN** the same flow is resumed in a later session (potentially after intermediate commits on the branch)
- **THEN** `init_flow()` does not overwrite `baseline_commit` (idempotent on resume, per the *init_flow idempotent on resume* scenario)
- **AND** the commit step and DAG worktree salvage continue to compute their deltas against the original baseline, not current HEAD

### Requirement: State Tracking Fields and Helper API

The `State` dataclass (`se3/engine/models.py`) SHALL persist the execution state of a flow, including step tracking, global context, review/fix loop counters, and history. It exposes a helper API used by step handlers and the state machine to advance review and fix loops.

**Field schema:**

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `current_step_id` | `str \| None` | `None` | ID of the currently active step. |
| `step_history` | `List[str]` | `[]` | Ordered list of step IDs executed in this flow (in append order). |
| `steps` | `Dict[str, Step]` | `{}` | Map of `step_id` → `Step` instances for all steps created in this flow. |
| `context` | `Dict[str, Any]` | `{}` | Global context shared across steps. Used as a side-channel for the state machine to expose computed values (e.g. `resolved_type`, `fix_iterations`, `fix_history` mirrors) to handlers without changing step inputs. |
| `selected_steps` | `List[StepType]` | `[]` | The dynamically selected sequence of step types for this flow (chosen by `analyze` and the CONFIRM-insertion step). |
| `current_step_index` | `int` | `0` | Position within `selected_steps` of the next step to execute. The state machine advances this when transitioning forward; it does NOT advance during fix-loop or revision routing back. |
| `review_iterations` | `Dict[str, int]` | `{}` | Per-step review iteration counter, keyed by the **reviewed** step's `step_id`. Used by the CONFIRM step's LLM reviewer to bound iterations against `confirmation.steps.<step>.max_iterations`. |
| `fix_iterations` | `int` | `0` | Global fix-loop counter shared by `test`, `self_check`, and `verify_spec`. Bounded by `workflow.max_fix_iterations` (see *verify_spec Unified Priority and Scope Mechanism*). |
| `fix_history` | `List[Dict[str, Any]]` | `[]` | Append-only log of fix-loop iterations, capped at `FIX_HISTORY_MAX_ENTRIES` (see *Fix History Structure*). Each entry follows the schema documented under that Requirement. |

**Step ID auto-generation:** `add_step(step)` SHALL auto-generate `step.step_id` when not already set, using the format `NN_steptype_uuid8` where `NN` is the 1-based sequence number derived from `len(step_history) + 1` and `uuid8` is the first eight hex characters of a fresh UUID (e.g. `01_analyze_844c2cf8`). The format is human-readable and sortable so on-disk history files (`se3/history/{flow_id}/{step_id}.jsonl`) order chronologically.

**Helper API:**

- `get_current_step() -> Optional[Step]` — returns the step pointed to by `current_step_id`, or `None`.
- `add_step(step) -> None` — registers a step in `steps` and appends its ID to `step_history` (idempotent on history append).
- `get_step_to_review(confirm_step_id) -> Optional[Step]` — given a CONFIRM step's ID, returns the immediately preceding step in `step_history` (the step under review). Returns `None` when the confirm step is not in history or has no predecessor.
- `increment_review_iteration(step_id) -> int` / `get_review_iteration(step_id) -> int` — bump and read the per-step review counter (1-based; `get_*` returns `0` before the first increment).
- `increment_fix_iteration(fix_context=None) -> int` — bump the global fix counter, append a `fix_history` entry containing `iteration`, `timestamp`, and any caller-supplied `fix_context` keys, apply the sliding-window cap, and mirror `fix_iterations`/`fix_history` into `context` so handlers can read them via the global context channel.
- `get_fix_iteration() -> int` — read the current global fix counter (`0` before any increment).
- `update_task_type(task_type) -> None` / `is_type_pending() -> bool` — record the LLM-resolved task type from `analyze` into `context["resolved_type"]`, and check whether resolution has happened yet. Used to distinguish CLI-provided task types (which arrive on `FlowInstance.task_type`) from the analyze step's classification.

**Context mirroring on fix-iteration increment:** Whenever `increment_fix_iteration()` runs, the post-increment `fix_iterations` integer SHALL be written to `context["fix_iterations"]` and the (clamped) `fix_history` list SHALL be written to `context["fix_history"]`. `from_dict()` SHALL re-mirror the clamped `fix_history` into `context["fix_history"]` when that key is already present in the loaded context dict, so resumed flows do not carry a stale oversized copy.

**Persistence:**

- `to_dict()` SHALL serialize every field above. `selected_steps` is persisted as a list of `StepType.value` strings; `steps` is persisted as a `{step_id: step.to_dict()}` map.
- `from_dict()` SHALL accept missing keys and substitute defaults (`get_current_step_id` → `None`, `step_history`/`steps`/`context`/`review_iterations` → empty containers, `fix_iterations` → `0`, `current_step_index` → `0`).
- `from_dict()` SHALL apply the same tail-keep `FIX_HISTORY_MAX_ENTRIES` cap on load that `increment_fix_iteration()` applies on append, so engine.json files written by older builds (or builds with a larger cap) are clamped at deserialization time rather than only on the next increment.

#### Scenario: Step IDs follow NN_steptype_uuid8 format
- **WHEN** `State.add_step(step)` is called with a Step whose `step_id` is unset
- **THEN** `step.step_id` is set to `f"{NN:02d}_{step.step_type.value}_{uuid8}"`, where `NN = len(step_history) + 1` (1-based) and `uuid8` is the first 8 hex characters of a fresh UUID
- **AND** the step is registered in `steps` and its ID is appended to `step_history`

#### Scenario: Review iteration counter is per-step
- **GIVEN** two CONFIRM-reviewed steps with IDs `X` and `Y` in the same flow
- **WHEN** `increment_review_iteration(X)` runs twice and `increment_review_iteration(Y)` runs once
- **THEN** `get_review_iteration(X)` returns `2`, `get_review_iteration(Y)` returns `1`, and unrelated step IDs return `0`

#### Scenario: Fix iteration increment mirrors into context
- **WHEN** `increment_fix_iteration({"step_id": "...", "reason": "test_failure"})` runs
- **THEN** the new entry appended to `fix_history` contains `iteration`, `timestamp`, and the caller-supplied fields
- **AND** `context["fix_iterations"]` equals the post-increment counter
- **AND** `context["fix_history"]` is the same (clamped) list as `state.fix_history`

#### Scenario: Task type resolution is tracked via context
- **GIVEN** a fresh `State` with no `resolved_type` in context
- **WHEN** `is_type_pending()` is called
- **THEN** it returns `True`
- **WHEN** `update_task_type("feature")` runs and `is_type_pending()` is called again
- **THEN** it returns `False` and `state.context["resolved_type"] == "feature"`

#### Scenario: get_step_to_review returns the predecessor in history
- **GIVEN** `step_history == ["X", "Y_confirm"]` where `Y_confirm` is a CONFIRM step
- **WHEN** `get_step_to_review("Y_confirm")` is called
- **THEN** it returns the `Step` object stored under ID `X`
- **WHEN** `get_step_to_review("missing_id")` is called
- **THEN** it returns `None` rather than raising

#### Scenario: State round-trips through persistence
- **GIVEN** a `State` populated with non-default values for every field above (including non-empty `review_iterations`, `fix_iterations`, `fix_history`, `context`, and `selected_steps`)
- **WHEN** the state is serialized via `to_dict()` and reconstructed via `from_dict()`
- **THEN** every field equals its original value (with `selected_steps` rehydrated from `.value` strings back to `StepType` enum members and `steps` rehydrated to `Step` instances)
- **AND** an oversized `fix_history` in the persisted dict is clamped on load via the same tail-keep policy used by `increment_fix_iteration`

### Requirement: Transition Data Model

The `Transition` dataclass (`se3/engine/models.py`) SHALL describe a single step-to-step transition rule as a serializable record, exposed publicly via `se3.engine.__init__` alongside `Step`, `State`, and `FlowInstance` so external tooling (e.g. flow-graph inspectors, visualizers, persisted transition-history consumers) can read and write transition records without depending on the state machine's internal routing logic. The live `StateMachine` implements its forward / fix-loop / revision routing in code rather than by interpreting `Transition` instances at runtime; `Transition` exists as a data-model contract for serialization.

**Field schema:**

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `from_step` | `StepType` | required | Source step of the transition. |
| `to_step` | `StepType` | required | Destination step of the transition. |
| `condition` | `str \| None` | `None` | Optional name of a conditional predicate gating the transition (e.g., a task-type label or guard name). When `None`, the transition is unconditional. |
| `description` | `str` | `""` | Free-text description for documentation and visualization. |

**Persistence:**

- `Transition.to_dict()` SHALL serialize `from_step` / `to_step` as their `StepType.value` strings, and pass `condition` / `description` through verbatim.
- `Transition.from_dict()` SHALL accept the same shape, substituting `None` for a missing `condition` and `""` for a missing `description`, and rehydrating the step fields via `StepType(value)`.

#### Scenario: Transition round-trips through to_dict/from_dict
- **GIVEN** a `Transition` populated with non-default values for `from_step`, `to_step`, `condition`, and `description`
- **WHEN** the instance is serialized via `to_dict()` and reconstructed via `from_dict()`
- **THEN** every field equals its original value, with `from_step` / `to_step` rehydrated as `StepType` enum members

#### Scenario: Transition deserialization tolerates missing optional fields
- **GIVEN** a dict containing only `from_step` and `to_step` values (no `condition`, no `description`)
- **WHEN** `Transition.from_dict()` loads it
- **THEN** `condition` defaults to `None` and `description` defaults to `""`
- **AND** no exception is raised for the missing optional keys

### Requirement: Event Stream and Sink Interface

`se3 run` SHALL emit a single unified structured event stream internally and SHALL NOT branch its behavior on the caller. Rendering degrades to a pluggable *sink* at the tail of that stream; "CLI vs daemon" degrades to one sink selection at the outermost layer.

**Event stream (`se3/engine/event_stream.py`):**

- `EventType` — a `str`-valued enum covering the flow lifecycle: `FLOW_STARTED`, `STEP_STARTED`, `STEP_OUTPUT`, `STEP_COMPLETED`, `STEP_FAILED`, `FLOW_PAUSED`, `FLOW_COMPLETED`, `FLOW_FAILED`, `INTERJECTION_NEEDED`, `CALL_NEEDED`.
- `Event` — a dataclass carrying `type`, `timestamp`, optional `flow_id` / `step_id` / `step_type`, and a `data` payload dict; it exposes `to_dict()` for serialization.
- `new_event(event_type, *, flow_id=None, step_id=None, step_type=None, timestamp=None, **data)` — convenience factory used by `run_flow`; keyword payload arguments are collected into `data`.
- `EventEmitter` — an in-memory pub/sub hub. `subscribe(sink)` / `unsubscribe(sink)` manage an ordered subscriber list; `emit(event)` fans the event out to every subscribed sink in subscription order. A sink that raises during `consume()` MUST NOT abort delivery to the remaining sinks — the event stream is best-effort and a rendering fault MUST NOT break the flow. `scope()` is a context manager that restores the subscriber list on exit.

**Sink interface (`se3/engine/sink.py`):**

- `Sink` — an ABC declaring `consume(event: Event) -> None`.
- `CliSink` — the CLI-mode tail. It delegates step-output rendering entirely to the pre-existing `step_renderers.render_step_output(step)` and adds no rendering logic of its own, keeping CLI output byte-for-byte identical to today's `se3 run`. Flow-level lifecycle events and raw `STEP_STARTED` / `STEP_OUTPUT` events are deliberately a no-op in `CliSink` because the `se3 run` orchestrator already renders those directly; having the sink render them too would double the CLI output.
- `JsonSink` — the daemon-mode tail. It serializes each event via `Event.to_dict()` and writes one line of JSON (NDJSON) per event, using `default=str` so non-serializable payload values degrade gracefully. It supports a compact (default) and a `pretty` mode.

#### Scenario: EventEmitter fans out to all subscribed sinks
- **WHEN** an `Event` is emitted on an `EventEmitter` with multiple subscribed sinks
- **THEN** every subscribed sink's `consume()` is invoked, in subscription order
- **AND** a sink that has been `unsubscribe()`d no longer receives subsequently emitted events

#### Scenario: A failing sink does not abort delivery
- **GIVEN** an `EventEmitter` with two subscribed sinks where the first raises in `consume()`
- **WHEN** an event is emitted
- **THEN** the exception from the first sink is swallowed
- **AND** the second sink still receives the event

#### Scenario: CliSink renders step output via the existing renderer
- **WHEN** `CliSink` consumes a `STEP_COMPLETED` or `STEP_FAILED` event whose `data` carries a `"step"` object
- **THEN** the event is routed to `step_renderers.render_step_output(step)` — the same entry point the current CLI uses
- **AND** flow-level lifecycle events (`FLOW_STARTED` / `FLOW_COMPLETED` / `FLOW_PAUSED` / etc.) and raw `STEP_OUTPUT` / `STEP_STARTED` events are a no-op in `CliSink`

#### Scenario: JsonSink emits one NDJSON line per event
- **WHEN** `JsonSink` consumes an event
- **THEN** it writes exactly one newline-terminated line of valid JSON (the `Event.to_dict()` payload) to its destination stream

#### Scenario: se3 run --output-format selects the outermost sink
- **WHEN** the user runs `se3 run "<task>"` without `--output-format` (or with `--output-format cli`)
- **THEN** a `CliSink` is subscribed and CLI output is byte-for-byte identical to current `se3 run` behavior
- **WHEN** the user runs `se3 run "<task>" --output-format json` (the form a daemon uses when it spawns a flow)
- **THEN** a `JsonSink` is subscribed and the flow emits its structured NDJSON event stream
- **AND** `se3 run` itself does not branch on the caller — only the tail sink differs
- **AND** an unrecognized `--output-format` value is rejected with a clear error and a non-zero exit

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
│  (16 steps)  │    │(engine.json) │    │(claude -p)   │
│              │    │              │    │              │
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
- status: 流程状态 (INIT, RUNNING, PAUSED, COMPLETED, FAILED, RECOVERING) — 详见 *Flow Status Lifecycle* requirement
- state: 状态对象（当前步骤、步骤历史、已选步骤）

**Step:**
- step_id: 唯一标识
- step_type: 步骤类型（16 种之一，包括 discovery、self_check、confirm 与 4 个 deprecated 步骤）
- status: 步骤状态 (PENDING, RUNNING, COMPLETED, PARTIAL, FAILED, RETRYING, PAUSED, REVISION_NEEDED) — 详见 *Step Status Lifecycle* requirement
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
    "selected_steps": ["analyze", "plan", ...],
    "steps": {...}
  }
}
```
