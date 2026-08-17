# tianluo (田螺) Framework — Charter

## Purpose
项目宪章（charter）。此文件由 `luo init` / `luo migrate` 生成，在每个 `luo run`
step 中**无条件全量注入**，并兼任沙箱子进程的 conventions 通道（子进程读不到
CLAUDE.md，只能经 charter 获得项目级约定）。

charter 只收录**代码说不出、且全项目每个 step 都需要全量看到**的高层内容：项目
身份、顶层架构、项目级横切强制约定、版本管理。它**不**承载每模块/每符号的定位
信息——那是 code-index（`luo code-index`）的职责，按需钻取，不进 charter（复制
进来只会得到一份随规模膨胀、又不如代码准的镜像）。

## Requirements

### Requirement: Project Identity
- 项目名称: tianluo（田螺；曾用名 se3，方法论仍称 SE 3.0）
- 简述: SE 3.0 的 code-first 软件工程流程引擎——**以 CLI 为本**：core `tianluo` 是一套
  CLI 命令（单次 `luo run` 流程），其上可**可选**叠加常驻 daemon 与中央 web-server
  控制平面（`tianluo[server]`，依赖隔离、独立部署）。代码是唯一权威真相源，通过
  code-index、本 charter 与同位 why-comments 向人类与 agent 暴露其当前状态（已退役
  `tianluo/specs/` 的 spec 镜像治理）。
- 主要语言/框架: Python 3.9+、Typer（CLI）、PyYAML、Rich、prompt-toolkit

### Requirement: Top-Level Architecture
顶层架构的全局图景——主要子系统是什么、它们如何拼合、跨子系统的边界在哪里。
只写**需主观判断、无单一代码归属**的架构决策（『为何这些模块归为一个子系统』
这类语义分层）。

tianluo 由若干子系统拼合而成；以下只记录需要主观判断、无单一代码归属的架构分层与边界。

**Code-first 知识模型。** 项目源码是唯一权威真相源。知识通过三件套对外暴露：
`tianluo/charter.md`（本宪章——项目身份/顶层架构/横切约定）、`tianluo/code-index.md`
（代码自身的结构地图，按需钻取到符号级）、以及与代码同位的 why-comments（记录
某段代码『为何如此』的意图）。已退役的 spec 镜像（`tianluo/specs/` 的 code↔spec
双向治理）不再是真相源；当 charter/why-comments 与代码不一致时，以代码为准。
未来意图通过 issue（`luo issue`）进入，而非改写知识文件去描述尚未实现的未来。

**流程引擎（`luo run`）——程序驱动的状态机。** run 流程由状态机（而非 LLM
决策）编排开发步骤序列，LLM 只在每个 step 内部被调用、处理需要『思考』的部分。
这一『程序驱动、LLM 只填思考空位』的边界是核心架构决策：步骤路由、上下文流转、
流程的可中断/可续跑都由代码确定性掌控；任何 flow 都可在确切的中断点恢复。

**Task type vs. PLAN decomposition — two distinct levels.** Task type
(feature / bugfix / small / review / survey …) decides the step composition and
type semantics of a whole flow; there is no second routing axis that adds or
removes steps. Every flow whose type carries a PLAN step runs it — what varies
is the *decomposition doctrine and granularity* PLAN works under, and those
decide only the execution shape of the PLAN → IMPLEMENT segment. The doctrine
sizes coarse groups by what one autonomous implement call can safely carry, and
the shape downstream is read off the resulting group count rather than off a
flag: one group is executed as a single whole-task call, two or more enter the
dependency DAG. Flows whose type has no PLAN step have no such surface at all.
The doctrine and granularity are decided once, at flow creation, persisted
together with their reason, and never re-decided mid-flow: a resumed flow keeps
executing the grouping it already entered, whatever the configuration says
later. Where a runner owns a native
continuous-execution loop, that loop only enhances execution *inside* a single
implementation call; it never becomes authoritative state — tianluo's persisted
flow state, workspace, history and quality gates remain the only authority on
whether a flow is complete.

**执行栈分层：LLMCaller 之上、AgentRunner 之下。** 所有 LLM 调用经统一的两层
结构。上层 LLMCaller 负责 agent 列表轮换、prompt 注入、JSON 抽取模式与重试上下文
重建；其下每个 AgentRunner 适配器只封装『一条 CLI 命令的一次调用』。关键边界：
多命令的轮换/回退归 LLMCaller 所有，单个 runner 永不自行轮换。LLM-无关的关注点
（stream-json NDJSON 契约、历史记录、重试、Web 渲染）被共享，并以 Claude 的
stream-json 模型为准；LLM-相关的关注点（CLI 参数构造、输出解析）各 runner 自理，
经 `build_call_args` 意图翻译缝合——使 LLMCaller 无需感知任何具体 LLM 的 CLI 细节，
从而可在不改上层调用方的前提下接入新 runner 类型。

**控制平面：core CLI / daemon / 中央 server 三层。** core `luo` CLI 是基础；
daemon 是常驻进程，其生命周期长于单次 `luo run`，负责发现/监管本机 flow、聚合
`tianluo/state|logs|calls|issues` 状态，并对中央 server 维持唯一一条出站连接；中央
server 是独立部署件（经 `tianluo-server` 入口启动，非 core 子命令）。硬边界：server
的重 Web 依赖经 `pyproject.toml` optional-dependencies（`tianluo[server]`）隔离，仅装
core（`luo`）时绝不能因 server 代码触发 import 错误——对 `tianluo.server` 及其重依赖
的引用一律延迟到 `tianluo-server` 入口真正运行时。

**隔离与合并。** `luo run --worktree` 在独立 git worktree 中跑**完全相同**的
flow（相同步骤/状态持久化/`--resume`/`--type`），成功后经重量级 `luo merge`
编排器自动合回原分支。主 worktree 互斥锁（`tianluo/state/merge.lock`，阻塞式
queue-and-wait）将同步 run 与所有 merge 相互串行化；worktree 模式的 flow body
不持该锁，故多个 `--worktree` run 可并发执行，仅在各自最终 merge 处竞争。

**E2E isolation subsystem (opt-in).** e2e is a capability offered to every managed
project regardless of its shape, gated by a single user-owned switch (`e2e.enabled`
in `tianluo.yaml`): with the switch off the state machine behaves exactly as it did
before the subsystem existed, and the flow never flips it on by itself. Two
boundaries define the subsystem. First, framework vs. project — the scenario
executor, config schema and image templates live in tianluo and evolve with it,
while a managed project carries only declarative content config and baseline assets
under `tianluo/e2e/` (in git, incrementally maintained by the flow), never a copy of
framework code. Second, execution vs. environment — the isolation backend is a
deliberately narrow pluggable abstraction (environment create / start / exec /
snapshot / destroy), whose only implementation is a container backend; a VM-level
backend later is a new class behind the same interface, with nothing above the line
changing.

**Cross-machine single writer.** Project state may live on a filesystem shared by
several machines, so process liveness is not decidable from the local process table
alone. Every on-disk execution-ownership marker (the merge lock holder record and the
run pid file) carries a stable machine identity: a marker may be probed against the
local process table — and hence declared stale and reclaimed — only when it belongs to
the current machine. A marker owned by another machine is always treated as held; it is
never auto-broken, and clearing a duplicate process on the far side goes through an
explicit operator entry point. Markers written before machine identity existed are
treated as local, preserving pre-upgrade behaviour.

**注意:** 每个目录/模块/符号『在哪、干嘛、有哪些关键符号』这类机械定位信息
**不写在这里**，由 code-index 自动维护、按需查阅（`luo code-index` 显示顶层
地图，`luo code-index show <path>` 钻取到函数级）。charter 只承载机械结构
层级表达不了的语义/架构分层。

### Requirement: Coding Conventions
项目级、横切全项目的编码约定（不随单个模块变化、每个 step 都应遵守的那部分）。
- Python 代码遵循标准 PEP 8。
- CLI 命令用 Typer 注册：复杂命令用 sub-typer（`add_typer`）成组，带位置参数的
  简单命令用 `@app.command`。
- 日志统一用 `logging` 模块，每个模块声明 `logger = logging.getLogger(__name__)`。
- **用户可见文案经 i18n 渲染**：CLI 与 WebUI 面向用户的固定文案一律不得硬编码，须经
  语言资源按 key 渲染；en-US 为基准语言包（持有 key 全集），所选语言缺失某 key 或语言码
  不受支持时回落 en-US。面向开发者的 logging 输出、以及发给 LLM 的 prompt 指令本体不在
  此约束内。
- **两个语言设置的职责边界**：`language.language` 是统一的『人类语言』——同时决定 CLI 的
  UI 文案语言与 LLM human-facing 步骤输出的语言；`language.spec_language` 则是『知识资产
  语言』——charter 与 code-index 的书写语言。语言设置变更只影响此后新生成/更新的内容，
  不回溯翻译既有知识资产。
- 类型注解采用 `from __future__ import annotations` 风格。
  In a module that does not carry that import, annotations are evaluated eagerly at
  import time, so every such annotation MUST resolve on the `requires-python` floor
  declared in `pyproject.toml` — that declared floor is the authoritative statement of
  the interpreter this package actually runs on, and it moves only in step with what
  the code really requires. A repo-side hard guard checks this across the whole
  package, because a newer development interpreter (lazy annotation evaluation) will
  not surface a violation on its own. Where an annotation cannot resolve on the floor,
  deferring that module's annotations is the accepted fix.
- 测试放在 `tests/` 目录，命名 `test_*.py`，使用 pytest。受控例外：
  `src/tianluo/engine/` 允许与引擎源码同位放置 pytest 测试模块，用于覆盖紧耦合的引擎
  内部行为（私有 helper、状态机内部分支、step 内部细节）。
- **关键 why-comment 标记前缀**：记载绑定意图/不变量（binding intent/invariant）
  的关键 why-comment 应以 `WHY:` 或 `INVARIANT:` 前缀显式标注。此类被标记的注释受
  `invariant_check` 硬守卫保护——diff 删除或改写它而未恢复、亦未以更新后的
  `WHY:`/`INVARIANT:` 注释显式声明新理由时，将触发 REVISION_NEEDED；普通注释不受此
  约束。存量关键注释的前缀回填经后续 issue 渐进进行。

### Requirement: Key Constraints
项目级强制约束（违反即视为错误的硬约定）。
- **Bootstrapping 约束**：本项目是自举项目——它既生成新规范，又依据已发布规范
  开发自身。生成新规范时，`.claude/` 下的已发布规范不得修改。
- **LLM 子进程隔离**：流程引擎的若干 step（如 analyze、plan）以 LLM 子进程执行，
  这些子进程读不到 CLAUDE.md，只能经本 charter（及沙箱 conventions 通道）获得
  项目级约定。因此项目级约定必须落在 charter，而非 CLAUDE.md。
- **Optional-extra dependency isolation**: a core-only install of the `luo` command
  family must never raise an import error because of code behind an optional extra.
  References to the `tianluo.server` package and its heavy dependencies stay deferred
  until the `tianluo-server` entry point actually runs. `tianluo[e2e]` follows the same
  rule, with one clarification: an extra isolates a third-party *dependency*, not
  tianluo's own code — the e2e framework and its templates ship in every wheel,
  references to extra-only dependencies are deferred until e2e actually executes, and a
  missing extra must surface an actionable `pip install 'tianluo[e2e]'` message rather
  than a raw ModuleNotFoundError.
- **代码即真相源**：未来意图经 issue（`luo issue`）进入，不得改写 charter /
  code-index / why-comments 去描述尚未构建的未来。
- **check 类步骤的 finding 只有一条去向**：任何 check 类步骤（self_check /
  invariant_check 及后继同类步骤）报出的 finding，唯一去向是当场进入 fix loop
  修复。不得引入丢弃、豁免、按 severity 分级放行，或『记为 issue 以后再修』等
  任何绕过当场修复的通道。抑制无价值报告只能通过提高 prompt 侧的报告门槛与
  证据落地校验来实现——前者留下痕迹，后者只筛『证据是否成立』而非『值不值得修』。
- **Requirement authority of check steps**: what a check step accepts against is
  the effective task description chain (the original or refined task description,
  user interjections, and the adjudicated description); charter, `WHY:` /
  `INVARIANT:` comments and code invariants remain the project-side constraints.
  Plan output — task groups included — is *derived scheduling data* (division of
  work, dependency ordering, isolated execution, progress recovery, history
  display). It may serve as a historical or locating hint, but must never narrow,
  widen, or override the requirements a check step verifies.

### Requirement: Workflow Conventions
- 用 `luo commit` 而非 `git commit`（强制测试通过、拦截敏感文件）。
- 提交信息必须包含上下文，便于下一会话顺利接续。
- 仅在测试已运行通过后，才可将功能标记为完成。
- 主入口命令：`luo run "任务描述"`。

### Requirement: Version Management

项目 SHALL 使用语义化版本控制（Semantic Versioning 2.0.0）作为版本管理标准。

**版本号文件（单一真相源）:**
- Python 项目: `pyproject.toml` 中的 `project.version` 字段
- Node.js 项目: `package.json` 中的 `version` 字段
- 其他项目: 在 `tianluo.yaml` 中显式指定 `version.file_path`

**版本格式:**
遵循 SemVer 2.0.0: `MAJOR.MINOR.PATCH[-prerelease][+build]`
- MAJOR: 不兼容的 API 修改
- MINOR: 向下兼容的功能添加
- PATCH: 向下兼容的问题修复

**版本决策模型:**
- `version_analyze` 步骤的 `suggested_version` 字段是新版本号的唯一权威来源
  （由 LLM 基于实际变更内容、SemVer 2.0.0 默认规则以及可选的项目级规则文件推导）
- 可选自定义规则: 在 `tianluo/version-rules.md` 写入自然语言规则，
  `version_analyze` 会将其注入 LLM prompt 作为决策依据；文件不存在时回落到默认 SemVer 2.0.0 规则
- `commit` 步骤直接采用 `suggested_version` 写入版本文件；若该字段缺失或步骤失败，
  流程报错中断并提示人工介入（不再有静默 patch bump 兜底）

**文档更新:**
- README.md: 显示当前版本徽章/头部
- VERSIONS.md: 维护版本历史变更日志
