# SE3 — Software Engineering 3.0 框架

![Version](https://img.shields.io/badge/version-8.0.0-blue)
![Python](https://img.shields.io/badge/python-3.8+-green)
![License](https://img.shields.io/badge/license-Apache--2.0-lightgrey)

[English](README.md) | **中文**

> **一个项目级、跨 session 的流程框架——由程序而非人作监工来监管 AI agent。你只 prompt 一次，走开，回来时交付物已经躺在 commit 里。**

SE3 不是单次会话的 prompting 工具，不是 skill、subagent，也不是 dynamic workflow。后者是*单 session*内的增强手段，作用是放大一次"人在回路"的对话。SE3 在它们上一层：一个 CLI 引擎 + 持久化状态机 + code-first 的 code↔spec 治理，监管 AI coding agent 跨多个 session、跨多台机器，直到项目级任务真正做完。

---

## 设计哲学

### 1. 全新范式：程序作监工，人 out-of-the-loop

skill、subagent、dynamic workflow 让*一次 AI 对话*更聪明、或在内部更好地并行。它们是好工具，但前提是有人盯着读输出、每步介入引导。

SE3 押的是另一边。工作的最小单位不是一次对话，而是一项**项目级任务**。在 `se3 run "…"` 与最终 commit 之间，可能跨过 plan / implement / test / verify / commit 等数十次 LLM 调用、多次 agent 轮换、fix loop、spec guardrails 回滚，甚至通过 daemon + 中心服务器在多台机器之间协作。所有这些的*监工*是 SE3 引擎——一段跑在 Python 里的确定性状态机——而不是一个盯着终端的人。

| 工具类别 | 作用范围 | 谁作监工 | 状态在哪里 |
|---------|---------|---------|-----------|
| skill / subagent / dynamic workflow | 单 session 内一次对话（或一次对话内的 fan-out） | 人在回路、读输出 | 对话上下文 |
| **SE3** | 一项跨多 session、多机器的项目任务 | 程序（引擎 + daemon） | 持久化文件（`se3/state/` / `se3/history/` / `se3/issues/`） |

### 2. 真正的痛点：attention is all you need

LLM 不是瓶颈，*人的注意力*才是。任何 agentic 系统的成本，最终都以"逼一个人去读、去判、去决策"的次数为单位。SE3 的北极星是**节省人的 attention**。

理想的 SE3 session 长这样：

1. **Prompt** — 你打一句 `se3 run "…"`（或开一次 discovery）。
2. **Discover** — 引擎用少量精准的澄清问题逼近真实需求。
3. **发射后不管** — 你走开。引擎自动 plan / implement / test / self-check / verify_spec / update_spec / version bump / commit。
4. **拿走交付物** — 回来时，分支上是干净的 commit，spec、版本、history 已经对齐。

只有第 1、2 步真正需要 attention，其它都是程序应该自己干完的活。

### 3. 撑起这种范式的四件套护城河

只有在框架本身具备以下四件套时，"程序作监工"的范式才立得住——这正是 session 内工具做不到的事：

- **跨 session 状态机** — `se3/state/engine.json` 持久化每个 flow 的精确 step、attempt、上下文与 fix-loop 历史；`se3 daemon` 提供一个常驻进程监管本机所有 `se3 run` flow；`se3-server` 把多台 daemon 聚合成一个网页视图；`se3 run --loop` 在隔离的 git worktree 上自动串起多次任务。flow 跨终端退出、机器重启、跨机器接管依然存活。*这一范式为何离不开它：* 没有持久化状态，"走开再回来"就等于丢工作。
- **spec ↔ code 双向治理（非对称）** — `se3/specs/*/spec.md` 是项目代码的文档化快照，两个方向并不对等。**code → spec 是第一性方向：** `se3 sync` 以当前代码为准重新生成 spec，二者不一致时以代码为准、更新 spec，绝不反向。**spec → code 仅是 flow 内有界的防漂移护栏：** 在单次 flow 期间，`se3 guardrails` 把已记录的 SHALL/MUST requirement 视为*该 flow 期间*的实现契约，拦下进行中实现对它的悄悄弱化或删除；这并不使 spec 在一般意义上凌驾于代码之上。*这一范式为何离不开它：* 一个长时间无人盯着的 agent 必然会漂移；spec 是这一次 flow 的实现契约。
- **失败回收内建** — `se3 salvage` 在 session 异常崩溃后做尽力抢救：commit 未提交改动、为遗留问题补 issue、归档 session；`se3/state/known_test_failures.json` 区分"新引入的回归"与"历史既存的红测试"；issue discovery 把任何未解决的隐患落成 `se3/issues/` 记录。*这一范式为何离不开它：* 没人盯着的时候，框架必须自己接住自己的失败，而不是把脏状态留给下一次。
- **底座可移植性** — 引擎本身是纯 Python + 文件系统；LLM 调用层是一层薄薄的 `AgentRunner` 适配；当前的具体 runner 是 Claude Code CLI，但抽象（`AgentRunner` / `RunResult` / `InfraErrorType`）是 provider-neutral 的。*这一范式为何离不开它：* 押在一种范式上，不应同时押在单一供应商上。

### se3 vs Claude Code Dynamic Workflows（互补，而非竞争）

Dynamic Workflows 解决的是*单 session 内*的并行编排：确定性 fan-out、judge panel、pipeline，都在一次对话里完成。它把"一次对话"做得更全、更可信。

SE3 解决的是*跨 session*的项目治理：持久化状态、code↔spec 治理、失败回收，以及一个能跨越任何单次对话的可移植底座。

两者可以叠加。未来某个 SE3 step 可以把它在 step 内部的并行工作委托给一次 Dynamic Workflow 调用，外层的状态机不变。我们故意不在 README 里钉死 DW 的具体 API 名字——DW 仍在 research preview，API 还会演进。

---

## 安装

```bash
# 核心 CLI（Python 3.8+）
pip install se3

# 含中心服务器 / 网页控制台
pip install 'se3[server]'

# 含 headless-browser e2e 测试依赖（之后需 `playwright install chromium`）
pip install 'se3[browser]'
```

当前版本：**8.0.0**。安装后会注册两个 console script：

| 脚本 | 用途 |
|------|------|
| `se3` | 核心 CLI（永远可用） |
| `se3-server` | 中心网页服务器（仅在装了 `server` extra 时可用） |

核心 CLI 永远不会 import web 依赖栈，所以不装 `[server]` 也能保持依赖面最小。

---

## 快速上手

```bash
# 1. 初始化项目（写 se3.yaml、se3/specs/base/spec.md、.gitignore，并按需 git init）
cd your-project
se3 init

# 2. 可选：先通过多轮 discovery 厘清模糊需求
se3 run --discover "我想要一个能做 X 的 CLI 工具"

# 3. 端到端跑一次任务（analyze → plan → implement → test → self-check →
#    verify_spec → update_spec → version_analyze → commit）
se3 run "Add JWT authentication"

# 4. 从中断的 flow 处精确续跑
se3 run --resume
```

### 三种运行形态

- **`--loop`** — 在隔离的 git worktree 分支（`loop/<slug>-<n>`）上连续跑多轮任务。每一轮都有一个干净的工作树；loop 结束时分支自动合并或丢弃，被 Ctrl-C 打断则保留分支供之后手工合并。
- **`se3 daemon start`** — 启动常驻后台进程，监管本机所有 `se3 run`，聚合 `se3/state|logs|calls|issues` 状态，并可选地拨入一个中心服务器。让你从任何地方查看 flow 进度。
- **`se3-server`** — FastAPI + WebSocket 中心服务器（自带静态网页控制台挂在 `/`），把多台 daemon 汇聚到同一张多机视图上。适合 fleet、远程发布任务、用浏览器盯长跑 flow。默认监听 `127.0.0.1:8080`。

#### 网页控制台鉴权（自 8.0.0 起）

中心服务器是一个多租户控制面——网页控制台与 REST API 都需要登录，每台机器 / 每个
flow 都按其所属 owner 隔离。首次启用的动线是：

1. **铸发 break-glass admin token** — 跑一次 `se3-server bootstrap-token`，它会把
   一次性 admin token 打印到控制台。
2. **登录** — 打开网页控制台，用该 token 换取 break-glass admin 会话
   （`POST /api/auth/breakglass`）。
3. **建本地用户** — 以 admin 身份邀请 / 创建账号（`POST /api/users`）。v1 不开放
   公开自助注册。
4. **签发 daemon key** — 每个 owner 在 UI 中自助铸发一把 daemon key
   （`POST /api/daemon-keys`），再用 `se3 daemon start --daemon-key <key>` 把工作机
   绑到自己名下。owner 只能看到自己名下的机器与 flow。

完整的端到端鉴权操作指引与配置键见
[docs/daemon-and-server.zh.md](docs/daemon-and-server.zh.md#鉴权与多租户访问)。

---

## 命令清单

下表中的所有命令在 8.0.0 版本下均存在于 `src/se3/cli.py` 或其注册的 sub-typer 中。

### 顶层命令

| 命令 | 用途 |
|------|------|
| `se3 run [TASK]` | 统一入口。驱动 flow engine 状态机（analyze → plan → implement → test → self_check → verify_spec → update_spec → version_analyze → commit）。支持 `--resume` / `--flow-id` / `--loop` / `--max-iterations` / `--no-worktree` / `--merge` / `--list-loops` / `--discover` / `--from-issue` / `--change` / `--type` / `--preset` / `--output-format`。 |
| `se3 init` | 初始化新项目：写 `se3.yaml`、base spec、`.gitignore`，按需 `git init`。参数：`--project-root` / `--name` / `--force`。 |
| `se3 guardrails <spec-file>` | 对 spec 文件跑 SE3 spec guardrails（检测被删除的 requirement、被弱化的语言）。供 CI 与 `se3 merge` 共用。参数：`--original` / `-o <original-file>`，指定对比的基线文件。 |
| `se3 sync` | 单向 code → spec 同步，按轮迭代直至收敛。参数包括 `--once` / `--max-rounds` / `--stable-rounds` / `--interactive` / `--show-diff` / `--validate-only` / `--resume` / `--force` / `--confirm-cleanup`。 |
| `se3 sync-respond <call-file>` | 处理 `se3 sync --interactive` 在高影响 requirement 删除时写出的人工决策响应文件。 |
| `se3 merge <branch> [<branch> ...]` | 按序把多个分支合并到当前 HEAD，冲突由 LLM 驱动解决。参数：`--strategy fast\|safe\|strict` / `--delete-merged` / `--no-delete-merged`。`se3/` 下的运行时数据按分层策略同步。 |
| `se3 merge-respond <call-file>` | 处理 `se3 merge` 在冲突或 guardrail 违规升级为人工 MCP call 时写出的响应文件。 |
| `se3 salvage` | 对异常终止的 session 做尽力抢救：宽容地加载 state、commit 残留 diff、为未完成工作补 issue、归档 session。参数：`--project-root` / `-p <path>`。 |

### `se3 history` — flow 历史

| 子命令 | 用途 |
|--------|------|
| `se3 history` / `se3 history list` | 列出活跃 / 归档 / history-only 三处的 flow。参数：`--active-only` / `--archived-only` / `--json`。 |
| `se3 history show <flow_id>` | 展示某个 flow 的逐 step 结构化详情。参数：`--detailed`（LLM 调用细节）/ `--verbose`（完整 tool-call 流）/ `--json`。 |
| `se3 history restore <flow_id>` | 按 ID 续跑某个 flow（委托给 `se3 run --resume --flow-id`）。`--dry-run` 仅打印命令不执行。 |
| `se3 history archived` | 仅列出归档 flow。`--json` 输出机读 JSON。 |

### `se3 issue` — 项目 issue

| 子命令 | 用途 |
|--------|------|
| `se3 issue` / `se3 issue list` | 默认列出 open issue。`--all` 含已关闭；`--type <t>` 按类型过滤。 |
| `se3 issue show <id>` | 展示某条 issue 的全部细节。 |
| `se3 issue create` | 交互式创建新 issue（title / description / type / priority / tags）。 |
| `se3 issue reset <id>` | 把 in-progress 的 issue 重置回 `open`。 |

### `se3 daemon` — 常驻控制面

| 子命令 | 用途 |
|--------|------|
| `se3 daemon start` | 启动 daemon。`--foreground` 不脱离终端；`--server-url <ws://…>` 向中心服务器注册；`--daemon-key <key>` 在多租户服务器上把本机绑定到某个 owner。 |
| `se3 daemon stop` | 停止运行中的 daemon。 |
| `se3 daemon status` | 报告运行状态、machine id、server URL、真实连接状态与已跟踪 flow。`--json` 输出机读 JSON。 |

---

## 目录布局

`se3/` 下所有内容默认 gitignored，*除了*下表中显式 whitelist 的子路径（specs、issues、scripts、prompts、`version-rules.md` 入库；运行时 state 与 log 不入库）。

```
your-project/
├── se3.yaml                       # 项目配置（入库）
├── se3.local.yaml                 # 本地覆盖配置（gitignored）
├── pyproject.toml                 # 项目版本号的单一事实源
├── VERSIONS.md                    # 更新日志（由 documentation-updater 维护）
├── scripts/                       # 辅助脚本
├── .gitignore                     # 由 `se3 init` 创建 / 追加
└── se3/                           # SE3 运行时根目录
    ├── specs/                     # ✅ 入库 — 代码的文档化快照
    │   ├── base/spec.md           # base spec，每个 flow 自动加载
    │   └── <capability>/spec.md
    ├── issues/                    # ✅ 入库 — open/ 与 closed/ YAML 记录
    ├── prompts/                   # ✅ 入库 — 项目级 preset prompt 正文（se3 run --preset）
    ├── version-rules.md           # ✅ 入库 — 可选，默认不存在
    ├── state/                     # ❌ runtime — engine.json / sync_state.json / …
    │   └── archive/               #   归档的 engine 快照
    ├── history/                   # ❌ runtime — per-flow per-step 的 jsonl 对话
    ├── logs/                      # ❌ runtime — 执行日志（含 logs/llm/ 调用 trace）
    ├── calls/                     # ❌ runtime — 待处理的人工 MCP call 文件
    ├── collab/                    # ❌ runtime — 多智能体协作状态
    ├── cache/                     # ❌ runtime — 衍生缓存（例如 spec-index）
    ├── tmp/                       # ❌ runtime — 临时 prompt / response 快照
    └── worktrees/                 # ❌ runtime — loop / DAG 隔离用的 worktree
```

---

## Specs 索引

SE3 在 `se3/specs/` 下自带 24 份 spec，它们是项目的活文档——代码当前行为的 code-first 快照——并在 flow 内被 `se3 guardrails` 守护、防止被悄悄弱化。可以作为深入代码的索引。

| Spec | 一句话用途 |
|------|-----------|
| `base` | 项目身份、目录布局、代码与流程约定；每个 flow 自动加载。 |
| `se3-commands` | 所有顶层 `se3 *` 命令与参数的 CLI 契约。 |
| `se3-config` | `se3.yaml` / `se3.local.yaml` 的 schema 与加载 / 覆盖语义。 |
| `se3-scaffold` | 标准项目结构与 `se3 init` 生成物。 |
| `se3-workflows` | 五种 workflow 类型（feature / bugfix / review / small / directive）加上 discovery，及各自的 step 序列。 |
| `se3-versioning` | SemVer 2.0.0 规则、单一版本源、自动 bump 契约。 |
| `session-protocol` | session 启动、resume、loop 模式生命周期、分支隔离、回合并规则。 |
| `flow-engine` | 核心状态机——step 池、转移、事件流、sink、prompt markers、fix loop。 |
| `agent-runner-infrastructure` | `AgentRunner` ABC 与 `ClaudeCodeRunner` 适配器：子进程、hang 检测、超大 prompt 重路由。 |
| `llm-caller` | agent 轮换、retry context 注入、JSON 抽取模式、NDJSON 流式输出。 |
| `dag-scheduler` | implement step 的并行 DAG 执行器（relay worktree、传递闭包削减）。 |
| `worktree-management` | loop / merge worktree 生命周期、分支命名、孤儿 worktree 清理。 |
| `requirement-intake` | 新需求通过 `se3 run` 进入 SE3 的 intake 契约。 |
| `preset-prompts` | 内置 + 项目两层 preset prompt 注册表，供 `se3 run --preset` 复用标准化的常见任务。 |
| `spec-format` | spec-format v1 文法：marker、标题、`### Requirement:` 与 scenario。 |
| `spec-guardrails` | 拦截既有 requirement 被悄悄弱化 / 删除的规则。 |
| `spec-role` | spec 作为代码文档化快照（spec-assistant）的角色：以 code → spec 为主，无常规手工编辑入口。 |
| `issue-management` | `se3 issue` CLI 与 `IssueManager` 存储 API（YAML on disk + 状态机）。 |
| `issue-discovery` | 从 flow 执行与未解决隐患中自动发现 issue。 |
| `documentation-updater` | `README.md` 徽章更新与 `VERSIONS.md` 更新日志生成。 |
| `salvage-command` | 五步式尽力抢救 pipeline。 |
| `user-interjection-handling` | Ctrl-C 插话生命周期与 call 文件在 CLI / daemon / web 间的路由。 |
| `running-flow-console` | 运行中 flow 全屏视图的网页控制台行为。 |
| `test-project` | 用来跑通 `se3 run` 工作流的端到端测试项目。 |

---

## 版本与许可证

- 版本号由 `pyproject.toml`（`8.0.0`）独家持有，引擎在 `version_analyze` + `commit` step 自动 bump，请勿手动修改。
- License：Apache-2.0。
- 完整更新日志见 [VERSIONS.md](VERSIONS.md)。
