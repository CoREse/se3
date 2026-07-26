# tianluo（田螺）— Software Engineering 3.0 流程引擎

![Version](https://img.shields.io/badge/version-12.0.0-blue)
![Python](https://img.shields.io/badge/python-3.8+-green)
![License](https://img.shields.io/badge/license-Apache--2.0-lightgrey)

[English](README.md) | **中文**

> **一个项目级、跨 session 的流程框架——由程序而非人作监工来监管 AI agent。你只 prompt 一次，走开，回来时交付物已经躺在 commit 里。**

*名字来自田螺姑娘：主人下地干活，素女从螺中出来把家务悄悄做完——不打扰、不追问，回家只见做好的成果。这正是本工具的契约。写下来的地方用全名 tianluo，喊它干活时叫小名：命令是 `luo`。*

tianluo（曾以 *se3* 之名发布；方法论仍叫 **SE 3.0**）不是单次会话的 prompting 工具，不是 skill、subagent，也不是 dynamic workflow。后者是*单 session*内的增强手段，作用是放大一次"人在回路"的对话。tianluo 在它们上一层：一个 CLI 引擎 + 持久化状态机 + 一套 code-first 知识体系（code-index + charter + why-注释），监管 AI coding agent 跨多个 session、跨多台机器，直到项目级任务真正做完。

---

## 设计哲学

### 1. 全新范式：程序作监工，人 out-of-the-loop

skill、subagent、dynamic workflow 让*一次 AI 对话*更聪明、或在内部更好地并行。它们是好工具，但前提是有人盯着读输出、每步介入引导。

tianluo 押的是另一边。工作的最小单位不是一次对话，而是一项**项目级任务**。在 `luo run "…"` 与最终 commit 之间，可能跨过 plan / implement / test / self-check / invariant-check / commit 等数十次 LLM 调用、多次 agent 轮换、fix loop，甚至通过 daemon + 中心服务器在多台机器之间协作。所有这些的*监工*是 tianluo 引擎——一段跑在 Python 里的确定性状态机——而不是一个盯着终端的人。

| 工具类别 | 作用范围 | 谁作监工 | 状态在哪里 |
|---------|---------|---------|-----------|
| skill / subagent / dynamic workflow | 单 session 内一次对话（或一次对话内的 fan-out） | 人在回路、读输出 | 对话上下文 |
| **tianluo** | 一项跨多 session、多机器的项目任务 | 程序（引擎 + daemon） | 持久化文件（`tianluo/state/` / `tianluo/history/` / `tianluo/issues/`） |

### 2. 真正的痛点：attention is all you need

LLM 不是瓶颈，*人的注意力*才是。任何 agentic 系统的成本，最终都以"逼一个人去读、去判、去决策"的次数为单位。tianluo 的北极星是**节省人的 attention**。

理想的 tianluo session 长这样：

1. **Prompt** — 你打一句 `luo run "…"`（或开一次 discovery）。
2. **Discover** — 引擎用少量精准的澄清问题逼近真实需求。
3. **发射后不管** — 你走开。引擎自动 plan / implement / test / self-check / 用已记录的不变量校验 diff / 标记 charter 漂移 / version bump / commit。
4. **拿走交付物** — 回来时，分支上是干净的 commit，版本、history、code-index 已经对齐。

只有第 1、2 步真正需要 attention，其它都是程序应该自己干完的活。

### 3. 撑起这种范式的四件套护城河

只有在框架本身具备以下四件套时，"程序作监工"的范式才立得住——这正是 session 内工具做不到的事：

- **跨 session 状态机** — `tianluo/state/engine.json` 持久化每个 flow 的精确 step、attempt、上下文与 fix-loop 历史；`luo daemon` 提供一个常驻进程监管本机所有 `luo run` flow；`tianluo-server` 把多台 daemon 聚合成一个网页视图；`luo run --loop` 在隔离的 git worktree 上自动串起多次任务。flow 跨终端退出、机器重启、跨机器接管依然存活。*这一范式为何离不开它：* 没有持久化状态，"走开再回来"就等于丢工作。
- **code-first 知识体系（code-index + charter + why-注释）** — 事实源（source of truth）就是代码本身。一张 `tianluo/code-index.md` 结构地图（自动维护、自鲜）给 agent 一张"项目里有哪些模块/符号、各在何处"的 orientation map；一份小巧、人工维护的 `tianluo/charter.md` 只承载每个 step 都需要全量看到的高层事实（项目身份、顶层架构、项目级横切不变量）；colocated 的 why-注释承载代码表达不了的意图。*这一范式为何离不开它：* 一个长时间无人盯着的 agent 必须能在每个 step 廉价地在代码里定位自己，而不必在旁边养一份会腐烂的代码镜像。它为何胜过被它取代的 spec 镜像，见下文 [知识体系](#知识体系code-index--charter--why-注释)。
- **失败回收内建** — `luo salvage` 在 session 异常崩溃后做尽力抢救：commit 未提交改动、为遗留问题补 issue、归档 session；测试基线缓存区分"新引入的回归"与"历史既存的红测试"；issue discovery 把任何未解决的隐患落成 `tianluo/issues/` 记录。*这一范式为何离不开它：* 没人盯着的时候，框架必须自己接住自己的失败，而不是把脏状态留给下一次。
- **底座可移植性** — 引擎本身是纯 Python + 文件系统；LLM 调用层是一层薄薄的 `AgentRunner` 适配；当前的具体 runner 是 Claude Code CLI，但抽象（`AgentRunner` / `RunResult` / `InfraErrorType`）是 provider-neutral 的。*这一范式为何离不开它：* 押在一种范式上，不应同时押在单一供应商上。

### luo vs Claude Code Dynamic Workflows（互补，而非竞争）

Dynamic Workflows 解决的是*单 session 内*的并行编排：确定性 fan-out、judge panel、pipeline，都在一次对话里完成。它把"一次对话"做得更全、更可信。

tianluo 解决的是*跨 session*的项目治理：持久化状态、code-first 知识体系、失败回收，以及一个能跨越任何单次对话的可移植底座。

两者可以叠加。未来某个 tianluo step 可以把它在 step 内部的并行工作委托给一次 Dynamic Workflow 调用，外层的状态机不变。我们故意不在 README 里钉死 DW 的具体 API 名字——DW 仍在 research preview，API 还会演进。

---

## 知识体系：code-index + charter + why-注释

早期 tianluo 维护一份平行的 `tianluo/specs/**/spec.md` 语料——一份代码的散文镜像——外加一整套治理机械来防它漂移：`luo sync` 的轮次、per-requirement 漂移基线、`verify_spec` / `update_spec` / `spec_gate` flow step，以及整套 `sync_*` analyzer/loop/state/discovery。tianluo 用三件 colocated、且事实源即代码本身的产物，取代了那份镜像。

### 三件套

- **code-index** — 项目的*结构地图*。结构来自代码本身的确定性提取（文件树遍历 + Python AST 符号枚举：目录/包 → 文件/模块 → 类 → 函数/方法）；每一级挂一句**自底向上**合成的 LLM 摘要（目录的摘要由其文件的摘要合成，文件的由其符号的合成）。它落成**单一自给自足的文件** `tianluo/code-index.md` — **权威产物，纳入版本控制**。它*就是*那张地图，是 `luo code-index` 渲染的对象，也是注入每个 flow step 的东西。因为它是 diff 里的纯文本，摘错的一条可被人在 review 中发现并纠正，且纠正 durably 落地。每个节点行还内嵌一枚内容指纹（一条简短、渲染时不可见的 HTML 注释），所以**仅凭**已提交的 md 就能判断什么变了：重建时只对指纹变更的节点重跑 LLM 摘要，未变者沿用既有摘要（从而保住人工纠正），且构建途中会周期性 flush 落盘，崩溃后能从断点续跑。**没有独立的缓存文件**——结构、摘要、指纹全都在这一个已提交、人类可 diff 的文件里。

  结构来自**代码**而非 json；json 只是再生加速器。显示只读 `.md`。优化目标是**结构覆盖的完整性，而非单符号摘要的深度**——地图回答*有哪些模块/符号、各在何处*，刻意**不下沉到实现细节**（那是源码本身的职责，复制进 index 只会得到一份不如代码准的镜像）。

- **charter** — `tianluo/charter.md`，旧 base spec 收缩、改名后的继任者。它在每个 step 被**全量注入**，并兼任沙箱子进程的 conventions 通道（子进程读不到 `CLAUDE.md`）。一个 *altitude gate* 只准入"**代码说不出、且全项目每个 step 都需要全量看到**"的内容：项目身份、顶层架构、项目级横切不变量。曾让 base spec 膨胀的每模块 locator 索引被甩掉了——那是 code-index 的职责。字节阈值只是监控灯、不是硬墙：因 charter 内容与项目规模解耦（只随架构复杂度增长，不随 LOC 增长），全量加载在大项目上仍然廉价；若真的大到难以全量加载，那是低层内容泄漏进来的红灯，而非给 charter 建索引的理由。

- **why-注释** — colocated 注释，*只*承载代码表达不了的 why/意图，仅在 why 变化时更新。它们不作为 code-index 的来源，故无 per-change 同步税；implement step 的 prompt 只是约定：当一处改动的意图变化时，同步更新 colocated 的 why-注释。如实承认这是 prompt 级的软约定（与其它 conventions 同等强度），是把注释纪律压到最小面，而非根治。

### 真正变好了什么（诚实的账）

本重构**并不**提升代码描述的语义正确性：LLM 生成的摘要会以与手写 spec 一模一样的方式出错。真正的收益在别处：

- **事实源回归代码本身。** 定位与意图就活在代码旁边，而不是一份需要被持续校真的独立语料里。
- **消除 staleness。** code-index 零纪律地增量再生：确定性枚举器每次构建都重新遍历整棵树，新增的 symbol 被枚举、已删的被剪除，只有指纹变更者重跑摘要。完整性是*枚举器的性质*，而非 LLM 的自觉——LLM 只对交付给它的 symbol 生成摘要、从不决定收录谁，因此它无机会漏掉任何 symbol，而摘错的一条仍出现在地图上。
- **治理维护面骤减。** 整套 `sync_*`、`verify_spec`、`update_spec`、`spec_gate`、per-requirement 漂移基线、旧 `spec_check` 一并退役。留下的是两个廉价的锚定式检查：`INVARIANT_CHECK`（diff 是否违反了任一*已被记录*的 binding invariant——锚定于 {task 描述, charter, 所触代码的 why-注释}？）与 `CHARTER_FRESHNESS`（一个仅在 diff 可能触动 charter 三类内容时才提示、否则廉价空过的 advisory）。
- **粒度与准入变为显式旋钮。** code-index 的粒度底 = 该文件类型能提供的最小*自然*语义单元（代码 → 函数/方法；结构化非代码 → 其自然单元；不透明文件 → 文件级一行），行/字节切分仅作最后降级模式、且须三条同时满足；四个阈值经 `luo config` 暴露。charter 内容由一份你能读、能执行的准入标准把关。两者都是你拧的旋钮，而非你要对抗的涌现行为。
- **charter 体积与项目规模解耦。** 它随架构增长，不随代码行数增长。
- **失败态地板高于旧体系。** 即便所有软纪律都失效，那个唯一自动维护的产物——code-index——仍自鲜存活。因此系统下限严格优于旧体系"腐烂的 spec 语料 + grep"的下限。

### 一个具体的前后对照——以及 spec-index 为何永远赢不了这一局

以旧的 `spec_index.py`（约 1130 行——它本身已被本次重构退役）为例。假设你要回答一个关于它的*定位*问题：它在哪、干嘛、有哪些关键符号？

没有 code-index 时，光是回答这个，你就得把整份约 1130 行源码全量读进 context。有 code-index 后，你先看地图上关于该文件的那几行——比如，*"构建 item 级 spec 索引，增量失效 mtime + size + sha256；关键符号 `load_or_build` / `_make_summary` / `_extract_locator` / `_h4_dividers`。"* 定位类问题根本不必碰源码。而一个*精确*类问题——比如某条启发式的具体边界判断——也只需定点读那约 30 行，而非全文。

**与 spec-index 的对照是最锋利的一击。** spec / spec-index 的优势上限被一个根本事实封顶：*它与代码不在同一层*。即便假设某份 spec 绝对准确、绝对完整，它仍停在 spec 的高度、无法呈现代码层面的实际细节——所以它替你定位到文件之后，你照样得回去读代码，且为求全得通读全文。spec 大概率的不准确、不完整，只是在此之上雪上加霜；它**并非**输给 code-index 的根因。code-index 从根上不受这条封顶约束，因为它的事实源*就是*代码、且把你直接导向那约 30 行。

这正是 **coverage > depth** 这一押注的兑现：地图的职责是告诉你该翻哪约 30 行——而不是替代那约 30 行。而这种 context 节省不是一次性的：它在**每个 step、每个 flow**上复利式累积，这恰恰正是 code-index 要砍掉的成本。

> 历史决策、以及被保留下来的"已移除功能但意图保留"这类意图，都不进 charter；它们继续走 issue 通道（`luo issue`）。跨文件、无单一归属的架构决策进 charter，人工维护，接受其无法自动同步的代价。

---

## 安装

```bash
# 核心 CLI（Python 3.8+）
pip install tianluo

# 含中心服务器 / 网页控制台
pip install 'tianluo[server]'

# 含 headless-browser e2e 测试依赖（之后需 `playwright install chromium`）
pip install 'tianluo[browser]'
```

当前版本：**12.0.0**。安装后注册的 console script：

| 脚本 | 用途 |
|------|------|
| `luo` | **主命令**。tianluo 的小名——文件上写全名，喊它干活叫 `luo` |
| `tianluo` | 全名入口，与 `luo` 完全等价（文档、演示、可搜索性） |
| `se3` | 改名过渡别名；调用时在 stderr 打一行迁移提示，13.0.0 移除 |
| `tianluo-server` | 中心网页服务器（仅在装了 `server` extra 时可用） |
| `se3-server` | `tianluo-server` 的过渡别名，13.0.0 移除 |

核心 CLI 永远不会 import web 依赖栈，所以不装 `[server]` 也能保持依赖面最小。

> 嫌 `luo` 还不够短？`alias tl=luo`。我们刻意不发 `tl`（Teal 编译器占用了它，
> 而且两个并存的短命令会分裂文档与社区语言）。

### 存量 se3 项目迁移

整个 12.x 期间一切照旧：`se3` 命令、老的 `se3/` 运行时目录、`se3.yaml` /
`se3.local.yaml` 配置全部继续可用。想把项目一步搬到新布局（一条可 review、
可 `git revert` 的 commit）：

```bash
luo migrate run rename-to-tianluo   # git mv se3/ → tianluo/、配置改名、.gitignore 重写
```

所有 legacy 回落在 **13.0.0** 移除。

---

## 快速上手

```bash
# 1. 初始化项目（写 tianluo.yaml、tianluo/charter.md、.gitignore，并按需 git init）
cd your-project
luo init

# 2. 可选：先通过多轮 discovery 厘清模糊需求
luo run --discover "我想要一个能做 X 的 CLI 工具"

# 3. 端到端跑一次任务（analyze → plan → implement → test → self_check →
#    invariant_check → charter_freshness → version_analyze → commit → summarize）
luo run "Add JWT authentication"

# 4. 从中断的 flow 处精确续跑
luo run --resume

# 5. 用结构地图导航代码库
luo code-index                          # 自适应根地图：一棵按预算缩放的目录树
luo code-index index src/tianluo/engine     # 下钻一个字面层级（目录的直接子项）
luo code-index show src/tianluo/cli.py      # 某个文件的完整函数/方法详情
```

### 三种运行形态

- **`--loop`** — 在隔离的 git worktree 分支（`loop/<slug>-<n>`）上连续跑多轮任务。每一轮都有一个干净的工作树；loop 结束时分支自动合并或丢弃，被 Ctrl-C 打断则保留分支供之后手工合并。
- **`luo daemon start`** — 启动常驻后台进程，监管本机所有 `luo run`，聚合 `tianluo/state|logs|calls|issues` 状态，并可选地拨入一个中心服务器。让你从任何地方查看 flow 进度。
- **`tianluo-server`** — FastAPI + WebSocket 中心服务器（自带静态网页控制台挂在 `/`），把多台 daemon 汇聚到同一张多机视图上。适合 fleet、远程发布任务、用浏览器盯长跑 flow。默认监听 `127.0.0.1:8080`。

#### 网页控制台鉴权

中心服务器是一个多租户控制面——网页控制台与 REST API 都需要登录，每台机器 / 每个
flow 都按其所属 owner 隔离。首次启用的动线是：

1. **铸发 break-glass admin token** — 跑一次 `tianluo-server bootstrap-token`，它会把
   一次性 admin token 打印到控制台。
2. **登录** — 打开网页控制台，用该 token 换取 break-glass admin 会话
   （`POST /api/auth/breakglass`）。
3. **建本地用户** — 以 admin 身份邀请 / 创建账号（`POST /api/users`）。v1 不开放
   公开自助注册。
4. **签发 daemon key** — 每个 owner 在 UI 中自助铸发一把 daemon key
   （`POST /api/daemon-keys`），再用 `luo daemon start --daemon-key <key>` 把工作机
   绑到自己名下。owner 只能看到自己名下的机器与 flow。

完整的端到端鉴权操作指引与配置键见
[docs/daemon-and-server.zh.md](docs/daemon-and-server.zh.md#鉴权与多租户访问)。

---

## 命令清单

下表中的所有命令在 12.0.0 版本下均存在于 `src/tianluo/cli.py` 或其注册的 sub-typer 中。

### 顶层命令

| 命令 | 用途 |
|------|------|
| `luo run [TASK]` | 统一入口。驱动 flow engine 状态机（analyze → plan → implement → test → self_check → invariant_check → charter_freshness → version_analyze → commit → summarize）。支持 `--resume` / `--flow-id` / `--loop` / `--max-iterations` / `--no-worktree` / `--merge` / `--list-loops` / `--discover` / `--from-issue` / `--change` / `--type` / `--preset` / `--output-format`。 |
| `luo init` | 初始化新项目：写 `tianluo.yaml`、`tianluo/charter.md`、`.gitignore`，按需 `git init`。参数：`--project-root` / `--name` / `--force`。 |
| `luo code-index` | 从 `tianluo/code-index.md` 渲染**自适应根地图**：一棵按字节预算缩放的目录树（顶层始终显示；代码目录在预算内展开几层）。这正是注入每个 flow step 的那张地图。读取已提交的地图（未构建时提示先 `rebuild`）；flow step 会按需懒增量保持其新鲜。 |
| `luo code-index index [PATH]` | 渲染 `PATH` 处**恰好一个字面层级**：目录的直接子项（子目录 + 文件），或文件的函数/方法。无参 → 字面根层级。与裸命令不同，它从不自动展开。 |
| `luo code-index show <path>` | 从结构地图打印某个文件的完整函数/方法详情（及任何降级 chunk）。 |
| `luo code-index rebuild [--force]` | 重建 code-index，构建途中周期性 flush md 作为 checkpoint。默认增量（只对指纹变更的节点重跑摘要）；`--force` 全量重跑。 |
| `luo code-index inspect` | 展示 code-index 统计（文件 / 符号 / 降级 chunk 计数）。 |
| `luo migrate run <id>` / `luo migrate list` | 运行一个已注册的版本/格式迁移（`run <id>`），或列出可用迁移器（`list`）。它是可复用的注册式骨架；首个迁移器（`spec-to-new-system`）把旧的 `tianluo/specs/` 项目一次性迁到 code-index + charter + why-注释 体系，落成单个可 review、可 `git revert` 的变更。 |
| `luo guardrails <spec-file>` | 对文件跑 tianluo guardrails（检测被删除的行 / 被弱化的语言）；`--sizes` 跑项目级的尺寸检查。供 `luo merge` 共用。参数：`--original` / `-o <baseline-file>`。 |
| `luo merge <branch> [<branch> ...]` | 按序把多个分支合并到当前 HEAD，冲突由 LLM 驱动解决。参数：`--strategy fast\|safe\|strict` / `--delete-merged` / `--no-delete-merged`。`tianluo/` 下的运行时数据按分层策略同步。 |
| `luo merge-respond <call-file>` | 处理 `luo merge` 在冲突或 guardrail 违规升级为人工 MCP call 时写出的响应文件。 |
| `luo salvage` | 对异常终止的 session 做尽力抢救：宽容地加载 state、commit 残留 diff、为未完成工作补 issue、归档 session。参数：`--project-root` / `-p <path>`。 |

### `luo history` — flow 历史

| 子命令 | 用途 |
|--------|------|
| `luo history` / `luo history list` | 列出活跃 / 归档 / history-only 三处的 flow。参数：`--active-only` / `--archived-only` / `--json`。 |
| `luo history show <flow_id>` | 展示某个 flow 的逐 step 结构化详情。参数：`--detailed`（LLM 调用细节）/ `--verbose`（完整 tool-call 流）/ `--json`。 |
| `luo history restore <flow_id>` | 按 ID 续跑某个 flow（委托给 `luo run --resume --flow-id`）。`--dry-run` 仅打印命令不执行。 |
| `luo history archived` | 仅列出归档 flow。`--json` 输出机读 JSON。 |

### `luo issue` — 项目 issue

| 子命令 | 用途 |
|--------|------|
| `luo issue` / `luo issue list` | 默认列出 open issue。`--all` 含已关闭；`--type <t>` 按类型过滤。 |
| `luo issue show <id>` | 展示某条 issue 的全部细节。 |
| `luo issue create` | 交互式创建新 issue（title / description / type / priority / tags）。 |
| `luo issue reset <id>` | 把 in-progress 的 issue 重置回 `open`。 |

### `luo daemon` — 常驻控制面

| 子命令 | 用途 |
|--------|------|
| `luo daemon start` | 启动 daemon。`--foreground` 不脱离终端；`--server-url <ws://…>` 向中心服务器注册；`--daemon-key <key>` 在多租户服务器上把本机绑定到某个 owner。 |
| `luo daemon stop` | 停止运行中的 daemon。 |
| `luo daemon status` | 报告运行状态、machine id、server URL、真实连接状态与已跟踪 flow。`--json` 输出机读 JSON。 |

---

## 目录布局

`tianluo/` 下所有内容默认 gitignored，*除了*下表中显式 whitelist 的子路径（code-index 地图、charter、issues、scripts、prompts、`version-rules.md` 入库；运行时 state 与 log 不入库）。

```
your-project/
├── tianluo.yaml                       # 项目配置（入库）
├── tianluo.local.yaml                 # 本地覆盖配置（gitignored）
├── pyproject.toml                 # 项目版本号的单一事实源
├── VERSIONS.md                    # 更新日志（由 documentation-updater 维护）
├── scripts/                       # 辅助脚本
├── .gitignore                     # 由 `luo init` 创建 / 追加
└── tianluo/                           # tianluo 运行时根目录
    ├── code-index.md             # ✅ 入库 — 权威结构地图（注入 LLM，可供人 review）
    ├── charter.md                # ✅ 入库 — 项目身份 / 架构 / 不变量，每个 step 全量注入
    ├── issues/                   # ✅ 入库 — open/ 与 closed/ YAML 记录
    ├── prompts/                  # ✅ 入库 — 项目级 preset prompt 正文（luo run --preset）
    ├── version-rules.md          # ✅ 入库 — 可选，默认不存在
    ├── state/                    # ❌ runtime — engine.json / …
    │   └── archive/              #   归档的 engine 快照
    ├── history/                  # ❌ runtime — per-flow per-step 的 jsonl 对话
    ├── logs/                     # ❌ runtime — 执行日志（含 logs/llm/ 调用 trace）
    ├── calls/                    # ❌ runtime — 待处理的人工 MCP call 文件
    ├── cache/                    # ❌ runtime — 衍生缓存（构建锁等）
    ├── tmp/                      # ❌ runtime — 临时 prompt / response 快照
    └── worktrees/                # ❌ runtime — loop / DAG 隔离用的 worktree
```

---

## 在代码库里导航

code-index *就是*进入这个代码库的索引。从根视图开始往下钻——先读地图上那几行，
只有在需要某个符号背后的实现细节时才打开源码文件：

```bash
luo code-index                           # 自适应根地图（按预算缩放的目录树）
luo code-index index src/tianluo/engine      # 一个层级：engine 包的直接子项
luo code-index show src/tianluo/engine/code_index.py   # 该文件的完整符号树
```

同一张根视图地图会自动注入每个 flow step，所以 agent 永远带着一张项目级的
orientation map；更深的函数级细节按需拉取。charter（`tianluo/charter.md`）与它一起
全量注入，承载每个 step 都需要全量看到的高层事实——项目身份、顶层架构、项目级
不变量。

---

## 版本与许可证

- 版本号由 `pyproject.toml`（`12.0.0`）独家持有，引擎在 `version_analyze` + `commit` step 自动 bump，请勿手动修改。
- License：Apache-2.0。
- 完整更新日志见 [VERSIONS.md](VERSIONS.md)。
