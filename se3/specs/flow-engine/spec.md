# flow-engine Specification

## Purpose

定义 SE3 3.0 的核心流程引擎（Flow Engine）：一个程序驱动的状态机，通过统一的 `se3 run` 入口控制开发流程的 11 个步骤编排，在每个步骤内调用 LLM 处理需要"思考"的部分。

## Requirements

### Requirement: 统一入口 `se3 run`

`se3 run` SHALL 作为 SE3 3.0 的唯一流程入口，取代 `se3:start` / `se3:work` / `se3:done` 的手动串联。

**Interface:**
```bash
# 新任务
se3 run "实现用户登录功能"

# 恢复中断的任务
se3 run --resume

# 循环模式（自动寻找并执行任务）
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
- **THEN** 流程引擎在完成一个任务后自动寻找下一个任务
- **AND** 支持从 backlog、roadmap、TODO 中发现任务

#### Scenario: 循环模式分支隔离
- **WHEN** 用户执行 `se3 run --loop`（不带 `--no-worktree`）
- **THEN** 创建 `se3-loop/{timestamp}` 分支从当前 HEAD
- **AND** 在 `se3/worktrees/{branch_safe_name}` 创建 git worktree
- **AND** 所有任务在 worktree 中执行（文件读写、commit 都在 worktree 内）
- **AND** 循环结束后提示用户选择：merge / later / discard

#### Scenario: 循环模式无隔离
- **WHEN** 用户执行 `se3 run --loop --no-worktree`
- **THEN** 所有任务直接在当前分支上执行（无分支隔离）

#### Scenario: 延迟合并
- **WHEN** 用户执行 `se3 run --loop --merge se3-loop/20260324-120000`
- **THEN** 显示 diff 摘要并确认后将指定的 loop 分支合并到当前分支
- **AND** 如果有冲突，显示冲突文件列表并提供手动解决指引

#### Scenario: 列出循环分支
- **WHEN** 用户执行 `se3 run --list-loops`
- **THEN** 显示所有未合并的 loop 分支及其 commit 数量
- **AND** 如果没有 loop 分支则提示无分支

#### Scenario: 循环模式外部包装架构
- **WHEN** `se3 run --loop` 执行循环迭代
- **THEN** 外层 `LoopController` 管理分支/worktree 生命周期、任务发现、迭代计数
- **AND** 内层 `run_flow()` 执行标准 11 步流程，对循环模式无感知
- **AND** 循环上下文仅通过 `set_extra_prompt(persistent=True)` 注入到 LLM 调用中
- **AND** 持久化 prompt 在多次 LLM 调用间保持，迭代结束后清理

#### Scenario: 循环模式任务重选避免
- **WHEN** 循环迭代中某任务失败
- **THEN** 该任务加入 `failed_tasks` 集合
- **AND** 后续迭代的 `find_next_task()` 自动跳过已完成和已失败的任务

### Requirement: Discovery Workflow

`discovery` 步骤 SHALL 实现多轮对话机制，帮助用户在需求不明确时探索并澄清需求。

**工作流程：**
1. **初始探索**: 根据用户的初步描述，AI 提出澄清问题
2. **对话迭代**: 用户回答后，AI 继续追问或转向综合
3. **综合确认**: AI 总结理解并生成精炼的任务描述
4. **用户确认**: 用户确认或要求修改
5. **进入分析**: 确认后使用精炼描述继续 `analyze` 步骤

**状态管理：**
- 对话历史保存在 `discovery_state` 中
- 支持任意轮次中断并通过 `se3 run --resume` 恢复
- 最大对话轮数限制（默认 10 轮）防止无限循环

**LLM 调用模式：**
- `question` 模式: 向用户提出具体问题
- `synthesis` 模式: 总结理解并生成精炼描述
- `confirmation` 模式: 用户确认后完成 discovery

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

#### Scenario: Discovery 输出传递
- **GIVEN** discovery 步骤完成且用户已确认
- **WHEN** 流程进入 `analyze` 步骤
- **THEN** `refined_description` 自动作为 `task_description` 传递给 analyze

### Requirement: 状态机驱动流程

流程引擎 SHALL 以 Python 有限状态机实现，每个状态对应一个流程步骤。步骤之间的转换由程序逻辑控制，而非 LLM 决定。

**核心原则：**
1. 步骤转换是程序化的（programmatic）
2. LLM 只处理步骤内部的工作（思考、生成、分析）
3. LLM 的输出不改变步骤转换逻辑

#### Scenario: 正常流程执行
- **WHEN** 用户执行 `se3 run` 并提供任务描述
- **THEN** 流程引擎从 `init` 状态开始
- **AND** 按程序定义的转换规则依次进入后续步骤
- **AND** 每个步骤内调用 LLM 处理该步骤的具体工作

#### Scenario: 步骤池动态选择
- **WHEN** 流程引擎完成 `analyze` 步骤
- **THEN** 根据分析结果从固定步骤池中选取后续需要的步骤
- **AND** 步骤池是预定义的有限集合，不由 LLM 凭空生成

### Requirement: 12 步流程池

流程引擎 SHALL 定义固定的 12 步骤池，所有流程步骤从此池中选取。

| 步骤 | 职责 | LLM 参与 | JSON 模式 | 输入 | 输出 |
|------|------|---------|-----------|------|------|
| `discovery` | 需求探索（多轮对话） | 是 | STRICT | initial_description | refined_description, discovery_summary |
| `analyze` | 分析任务类型和范围 | 是 | STRICT | task_description | task_type, scope, complexity, reasoning |
| `read_spec` | 读取相关 spec 文件 | 否（程序自动） | - | scope | relevant_specs, spec_content |
| `propose` | 生成变更提案 | 是 | EXTRACT | spec_content, task_description | proposal, files_to_modify, files_to_create |
| `design` | 设计方案和架构决策 | 是 | EXTRACT | proposal, spec_content | design_doc, decisions, components |
| `plan_tasks` | 分解为具体可执行任务 | 是 | EXTRACT | design_doc | task_list |
| `implement` | 编写代码实现 | 是 | TWO_PHASE | design_doc, task_list | implementation, files_changed |
| `test` | 运行测试验证 | 否（程序执行） | - | - | test_results, tests_passed |
| `verify_spec` | 检查实现与 spec 一致性 | 是 | EXTRACT | implementation, spec_content | verification_result, issues |
| `update_spec` | 更新 spec 记录变更 | 是 | EXTRACT | changes_made | updated_specs |
| `version_analyze` | 分析变更确定版本类型 | 是 | EXTRACT | changes_made, updated_specs, verification_result | bump_type, confidence, reasoning |
| `commit` | 提交变更 | 否（程序执行） | - | changes_made, bump_type | commit_hash |
| `summarize` | 生成总结和 handoff | 是 | 文本 | all_previous_outputs | summary (Markdown 文本) |
| `project_summary` | 生成项目上下文摘要 | 是 | 文本 | 项目状态 | 摘要字符串 |

**不同任务类型的步骤序列：**
- `discovery`: discovery → analyze → read_spec → propose → design → plan_tasks → implement → test → verify_spec → update_spec → **version_analyze** → commit → summarize
- `feature`: analyze → read_spec → propose → design → plan_tasks → implement → test → verify_spec → update_spec → **version_analyze** → commit → summarize
- `bugfix`: analyze → read_spec → propose → plan_tasks → implement → test → verify_spec → **version_analyze** → commit → summarize
- `review`: analyze → read_spec → verify_spec → summarize
- `small`: analyze → implement → test → **version_analyze** → commit → summarize
- `directive`: analyze → read_spec → plan_tasks → implement → **version_analyze** → commit → summarize

#### Scenario: Feature 任务完整流程
- **WHEN** 任务类型为 `feature`
- **THEN** 执行完整的 11 步流程

#### Scenario: Small 任务简化流程
- **WHEN** 任务类型为 `small`
- **THEN** 跳过 propose、design、plan_tasks 步骤

### Requirement: 步骤内 LLM 调用

流程引擎 SHALL 在每个步骤内通过 subprocess 调用 LLM（`claude -p`），传入步骤特定的 prompt 和自动收集的 context。

**LLM 调用机制：**
1. 构建步骤特定的 prompt
2. 自动收集相关上下文（specs、前序步骤输出、项目状态）
3. 调用 Claude CLI 获取响应
4. 解析响应（支持 JSON 和文本）
5. 存储输出到步骤状态

#### Scenario: 自动注入上下文
- **WHEN** 流程引擎进入某个步骤
- **THEN** 程序自动收集该步骤所需的上下文
- **AND** 将上下文注入 LLM 调用的 prompt 中

#### Scenario: LLM 调用失败
- **WHEN** 步骤内的 LLM 调用失败（超时、API 错误、输出无效）
- **THEN** 流程引擎执行重试策略（最多 3 次）
- **AND** 如果重试仍失败，暂停流程并通知用户

### Requirement: JSON 提取模式

流程引擎 SHALL 支持三种 JSON 提取模式，根据步骤特性选择最优策略：

| 模式 | 描述 | 适用场景 |
|------|------|----------|
| **STRICT** | 强制 JSON 格式，失败重试 | 简单输出（analyze, read_spec） |
| **EXTRACT** | 要求 JSON 格式，失败时用 LLM 提取 | 中等复杂度（propose, design, plan_tasks, verify_spec, update_spec） |
| **TWO_PHASE** | 自然生成 + LLM 提取 | 复杂/大输出（implement） |

**模式选择原则：**
- 简单输出（<1K tokens）：STRICT（成本低，可靠性高）
- 中等复杂度（1K-5K tokens）：EXTRACT（平衡可靠性和 token 效率）
- 大输出（>5K tokens）：TWO_PHASE（避免提示词污染，处理截断）

#### Scenario: STRICT 模式
- **WHEN** analyze 步骤需要简单的任务分类
- **THEN** 使用 STRICT 模式：prompt 添加强制 JSON 指令
- **AND** 如果输出非 JSON，重试整个调用

#### Scenario: EXTRACT 模式
- **WHEN** design 步骤生成嵌套的设计文档
- **THEN** 使用 EXTRACT 模式：prompt 要求 JSON 格式
- **AND** 如果输出非 JSON，使用轻量级 LLM 调用来提取 JSON
- **AND** 不重试主调用，节省 token

#### Scenario: TWO_PHASE 模式
- **WHEN** implement 步骤生成包含大文件内容的输出
- **THEN** 使用 TWO_PHASE 模式：prompt 不添加 JSON 约束
- **AND** LLM 自然生成内容
- **AND** 第二次 LLM 调用从自然输出中提取 JSON
- **AND** 避免提示词污染，更好地处理截断

### Requirement: 聊天记录系统（Chat History）

流程引擎 SHALL 记录每次 LLM 调用的 prompt 和回应，支持重试时注入对话上下文，并提供人类浏览接口。

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

#### Scenario: 人类浏览聊天记录
- **WHEN** 用户执行 `se3 history`
- **THEN** 展示所有 flow 的对话概要
- **AND** 支持按 flow_id 和 step_type 筛选查看
- **AND** 区分通讯 JSON（解析渲染）和 LLM 输出 JSON（原样展示）

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

### Requirement: 步骤间输入传递

流程引擎 SHALL 自动构建步骤输入，将前序步骤的输出传递给后续步骤。

**输入构建规则：**
- 所有步骤接收 `task_description` 和 `flow_id`
- `read_spec` 接收 analyze 的 `scope`
- `propose` 接收 `relevant_specs` 和 `spec_content`
- `design` 接收 `proposal`
- `plan_tasks` 接收 `design_doc`
- `implement` 接收 `design_doc` 和 `task_list`
- `verify_spec` 接收 `implementation`
- `commit` 接收 `changes_made`
- `summarize` 接收所有前序输出

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
  "suggested_version": "X.Y.Z"
}
```

**决策规则：**
- **MAJOR**: 不兼容的 API 变更、删除功能、破坏性行为变更
- **MINOR**: 向后兼容的新功能、新增可选参数、功能增强
- **PATCH**: 向后兼容的 bug 修复、性能优化、内部重构
- **NONE**: 无版本价值的变更（仅格式化、注释等）

#### Scenario: 智能版本分析识别破坏性变更
- **GIVEN** 任务类型为 `small`
- **AND** 实际变更删除了公共函数的参数
- **WHEN** `version_analyze` 步骤执行
- **THEN** LLM 识别为 breaking change
- **AND** 返回 `bump_type: major`

#### Scenario: 低置信度处理
- **GIVEN** `version_analyze` 返回 `confidence: low`
- **AND** `auto_bump: true` (默认)
- **WHEN** 进入 commit 步骤
- **THEN** 系统仍应用建议的 bump 类型
- **AND** 记录警告日志

### Requirement: Commit 步骤版本管理

`commit` 步骤 SHALL 集成自动版本更新功能，根据 `version_analyze` 的结果自动 bump 版本号，并更新相关文档。

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
- **WHEN** test 步骤执行完成且 `overall_passed` 为 false
- **THEN** test 步骤返回 `REVISION_NEEDED` 状态
- **AND** 流程直接进入 fix loop 返回 implement 步骤
- **AND** 跳过 verify_spec 步骤（因为问题已通过测试发现）

#### Scenario: test 通过后进行 spec 验证
- **WHEN** test 步骤执行完成且 `overall_passed` 为 true
- **THEN** test 步骤返回 `COMPLETED` 状态
- **AND** 流程继续到 verify_spec 步骤进行 spec 合规性检查

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
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ create_flow │→ │  run_step   │→ │ transition_to_next  │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│         ↑                │                      │           │
│         └────────────────┴──────────────────────┘           │
└───────────────────────────┬─────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Step Handler │    │ Persistence  │    │ LLM Caller   │
│  (12 steps)  │    │(engine.json) │    │(claude -p)   │
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
- step_type: 步骤类型（12 种之一，包括 discovery）
- status: 步骤状态 (PENDING, RUNNING, COMPLETED, FAILED, RETRYING, PAUSED)
- inputs: 输入字典
- outputs: 输出字典
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
  --merge BRANCH    合并已有的 loop 分支（如 se3-loop/20260324-120000）
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
