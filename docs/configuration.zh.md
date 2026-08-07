# tianluo 配置参考

本文是 tianluo 所读取的每一个配置块的**完整权威参考**。它以
`src/tianluo/config.py` 为准编写 —— 下文每一个 key 都对应一个真实存在的 dataclass
字段,每一个默认值都取自该模块中对应的 `DEFAULT_*` 常量(或 dataclass 的字段
默认值)。

`tianluo.example.yaml` 是一份*起步样例*,不是 schema:它只展示了一个小而有主张的
子集,并且还残留着若干引擎早已不再读取的历史配置块。两者不一致时,以本文档
(以及它所镜像的代码)为准。

## 目录

1. [配置文件与解析规则](#配置文件与解析规则)
   - [读取哪个文件:四级查找](#读取哪个文件四级查找)
   - [坑 1 —— `tianluo.local.yaml` 替换整个文件](#坑-1--tianluolocalyaml-替换整个文件)
   - [全局层(`~/.se3/config.yaml`)](#全局层se3configyaml)
   - [遗留的 `se3.yaml` / `se3.local.yaml`](#遗留的-se3yaml--se3localyaml)
   - [如何阅读本文档](#如何阅读本文档)
2. [配置块](#配置块)
   - [`agents`](#agents)
   - [`llm_caller`](#llm_caller)
   - [`confirmation`](#confirmation)
   - [`language`](#language)
   - [`workflow`](#workflow)
   - [`investigation`](#investigation)
   - [`test`](#test)
   - [`e2e`](#e2e)
     - [前置条件:一个你无需 sudo 就能跑的容器 runtime](#前置条件一个你无需-sudo-就能跑的容器-runtime)
     - [Runtime 选择](#runtime-选择)
     - [另一半:`tianluo/e2e/` 内容配置](#另一半tianluoe2e-内容配置)
     - [E2E step 在流程中的位置,以及失败如何路由](#e2e-step-在流程中的位置以及失败如何路由)
   - [`implement`](#implement)
   - [`steps`](#steps)
   - [`version`](#version)
   - [`documentation`](#documentation)
   - [`code_index`](#code_index)
   - [`merge`](#merge)
   - [`conflict_resolver`](#conflict_resolver)
   - [`claude_subprocess`](#claude_subprocess)
   - [`spec_write_protection`](#spec_write_protection)
   - [`server`](#server)
   - [`presets`](#presets)
3. [Legacy / 历史遗留配置](#legacy--历史遗留配置)
   - [`spec_governance`](#spec_governance)
   - [`spec_loading`](#spec_loading)
   - [引擎已不再读取的配置块](#引擎已不再读取的配置块)
4. [排错:改了配置却没有任何变化](#排错改了配置却没有任何变化)

---

## 配置文件与解析规则

### 读取哪个文件:四级查找

项目配置存放在项目根目录下的单个 YAML 文件中。最终只会选中**一个**文件 ——
`get_project_config_path()` 按顺序探测候选路径,遇到第一个是普通文件的即停止:

| # | 候选路径 | 说明 |
|---|----------|------|
| 1 | `<worktree>/tianluo.local.yaml` | 优先级最高。被 gitignore 的本地覆盖文件。 |
| 2 | `<main_repo>/tianluo.local.yaml` | 仅当项目根是一个 linked git worktree 时才探测。 |
| 3 | `<worktree>/tianluo.yaml` | 进版本库的项目配置。 |
| 4 | `<main_repo>/tianluo.yaml` | 优先级最低。 |

第 2、4 级只为 **git worktree** 而存在。`luo run --worktree` 在 linked worktree 中
执行,而被 gitignore 的 `tianluo.local.yaml` 并不会跟着过去 —— 因此主仓库的本地
覆盖文件会先于 worktree 中已提交的 `tianluo.yaml` 被采用。对于普通(非 worktree)
的 checkout,查找退化为经典的两级:`tianluo.local.yaml` > `tianluo.yaml`。

若一个都不存在,则返回 `<project_root>/tianluo.yaml` 作为名义上的目标路径,各个
loader 一律回落到内置默认值。

有两个细节值得知道:

- 探测使用 `is_file()`,它会**跟随符号链接**。诸如
  `tianluo.local.yaml -> ../shared-overrides.yaml` 这样的布局会被识别为生效的
  覆盖文件(这是有意为之 —— 用户正是靠它在多个 clone 之间共享覆盖配置)。而该
  路径上若是一个游离的*目录*或断链的符号链接,则不算普通文件,因而不会遮蔽
  `tianluo.yaml`。
- 选中的配置里出现的相对路径(如 `version.version_file`、`test.command`、
  `test.phases[].cwd`)由下游调用方按**当前运行进程的项目根**解析,而不是按配置
  文件所在目录解析。写在主仓库 `tianluo.local.yaml` 里的相对路径,会被读取它的
  那个 worktree 按自己的根来解释。

### 坑 1 —— `tianluo.local.yaml` 替换整个文件

**本地覆盖是整文件择一,不是按 key 合并。**一旦 `tianluo.local.yaml` 存在,
`tianluo.yaml` 就*根本不会被打开*。只写在 `tianluo.yaml` 里的一切都会悄无声息地
回落到内置默认值。

```yaml
# tianluo.yaml(已提交)
workflow:
  max_fix_iterations: 100
agents:
  primary: { type: claude-code, cmd: claude }
llm_caller:
  defaults: [primary]
```

```yaml
# tianluo.local.yaml —— 『我只想调一个旋钮』
workflow:
  self_check_passes_required: 2
```

结果:`max_fix_iterations` 回落到默认值 `100`(此处恰好相同),**而项目级的
`agents` 注册表与 `llm_caller.defaults` 也一并没了** —— 这两个块的项目侧此刻
为空。这次运行实际用什么,取决于全局层,顺序是:先看 `~/.se3/config.yaml` 里的
`agents` 条目与 `llm_caller.defaults`,再看 legacy `claude_commands:` 块隐含的
链路,以上都拿不出链路时才轮到内置的 PATH 探测链路。所以在 `~/.se3/config.yaml`
配了 agents 的机器上,这次运行是从项目链路悄悄切到了**全局**链路,而不是切到内置
探测链路。想保住某个值,就得在本地文件里把它重写一遍。这也正是
`tianluo.example.yaml` 给 `workflow.max_fix_iterations` 加注『与
`tianluo.local.yaml` 中的值刻意保持一致,以免本地覆盖悄悄遮蔽掉一个不同的值』的
原因。

经验法则:**`tianluo.local.yaml` 是一份完整的替换配置,不是补丁。**请以
`tianluo.yaml` 的副本为起点,再在其上修改。

还有一个相关的失效模式:一个*无法解析*的 `tianluo.local.yaml`(YAML 语法错误,
或顶层不是 mapping)会被当作空文件处理 —— 它照样遮蔽 `tianluo.yaml`,于是所有
loader 全部回落到内置默认值。发生这种情况时 loader 会打印一条一次性告警并点名
出错的文件;如果你的项目配置『突然不起作用了』,先去日志里找它。

### 全局层(`~/.se3/config.yaml`)

每用户的全局配置从 `~/.se3/config.yaml` 读取。该路径来自
`_GLOBAL_CONFIG_PATH_SUFFIX = (".se3", "config.yaml")`,在 se3 → tianluo 更名时
**并未**改变 —— 目录仍然是 `~/.se3/`,与 `~/.se3/server.db`、`~/.se3/daemon.pid`
以及其余的每用户运行时状态放在一起。它是一个普通的 YAML mapping,块名与项目
配置文件完全相同。

**只有部分配置块参与项目层↔全局层的合并。**其余的块只从项目文件读取:

| 配置块 | 参与全局层? | 合并粒度 |
|--------|--------------|----------|
| `agents` | 是 | **条目级。**项目中的条目覆盖同名的全局条目;两侧互不冲突的条目共存。 |
| `llm_caller.defaults` | 是 | **整列表。**项目的 `defaults` 整体替换全局的 `defaults`;仅当项目未写该 key 时才使用全局值。 |
| `llm_caller.steps.<step>` | 是 | **按 step 整值替换。**项目中声明了某个 step,就*针对该 step*替换全局声明;只在全局声明过的 step 依然生效。 |
| `confirmation.steps` | 是 | **条目级**,规则同 `agents`。 |
| `language` | 是 | **字段级。**`language` / `spec_language` 各自独立:项目设了就取项目的,否则取全局的。 |
| `server` | 是 | **整块。**项目的 `server:` 段整体替换全局的(不做深度合并)。 |
| 其余全部 | 否 | 只读项目文件(`workflow`、`test`、`implement`、`steps`、`version`、`documentation`、`code_index`、`merge`、`conflict_resolver`、`claude_subprocess`、`spec_write_protection`、`investigation`、`presets` ……)。 |

### 遗留的 `se3.yaml` / `se3.local.yaml`

更名前的文件名在查找链的**每一级**上都仍被认可,同一级内规范的 `tianluo.*` 名
永远胜过 `se3.*` 名。于是实际顺序为:

```
worktree/tianluo.local.yaml → worktree/se3.local.yaml
  → main/tianluo.local.yaml → main/se3.local.yaml
  → worktree/tianluo.yaml   → worktree/se3.yaml
  → main/tianluo.yaml       → main/se3.yaml
```

`se3.*` 回落在整个 12.x 期间持续有效,并在 **13.0.0 中移除**。更古老的
`se3.config.yaml` 只被当作项目根的*标记文件*(供 CLI 向上遍历父目录时识别),
永远不会被当作配置加载。

### 如何阅读本文档

- **默认值**列写的是代码实际使用的字面值。代码中以常量命名的,以该常量为准
  (`DEFAULT_MAX_FIX_ITERATIONS = 100`、`DEFAULT_CODE_INDEX_CHUNK_BYTES = 16 * 1024`
  ……);此处的数字就是这些常量的当前取值。
- **类型**指可接受的 YAML 类型,而非 Python 注解。
- 除非某个 key 的条目另有说明,加载行为一律是**钳制并告警**(clamp-and-warn):
  非法值只记一条告警并回落到默认值,而不中断本次运行。会**快速失败**的例外均已
  显式标出(`ConfigError` / `ValueError`),因为它们会在加载时就中断流程。
- 标注为*空转*(inert)的 key 指:`config.py` 会解析它,但引擎里没有任何消费方。
  列出它只为完备(免得你去追一个它从未有过的行为),而不是建议你去设置它。

---

## 配置块

### `agents`

顶层的身份层。每一个 `llm_caller.*` 条目、每一个
`confirmation.steps.<step>.reviewer`,都是**按名字引用、并在此注册表中解析**的。
它是一个以 agent 名为 key 的 mapping。

```yaml
agents:
  primary: { type: claude-code, cmd: claude }
  cheap:   { type: claude-code, cmd: hclaude }
  gpt:     { type: codex,       cmd: codex }
  tty:     { type: claude-interactive, cmd: claude }
```

每个条目对应一个 `AgentDef`:

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| *(mapping 的 key)* | string | —— | 该 agent 的 `name`。必须是非空字符串;否则该条目被跳过并告警。 |
| `type` | string | `claude-code` | 由哪个 `AgentRunner` 适配器驱动这个 agent。见下表。 |
| `cmd` | string | —— | 要调用的 CLI 命令。**必填** —— 没有可用 `cmd` 的条目会被跳过并告警。 |
| `priority` | int | `0` | **已废弃且被忽略。**轮换顺序取决于 `llm_caller` 中名字列表的*书写顺序*,与这个数字无关。设置它会按来源各打印一次废弃告警。 |

支持一种简写形式:`primary: claude` 等价于
`primary: { type: claude-code, cmd: claude, priority: 0 }`。

合法的 `type` 取值(在 `LLMCaller` 中分派;无法识别的类型会在**首次调用**时抛出
`ValueError: Unknown agent type: …`,而不是在配置加载时):

| `type` | 适配器 | 说明 |
|--------|--------|------|
| `claude-code` | `claude_runner.ClaudeRunner` | 一次性的 `claude -p` 子进程。默认值。 |
| `codex` | `codex_runner` | OpenAI Codex CLI。 |
| `claude-interactive` | `claude_interactive_runner` | 由 pexpect 驱动的交互式 PTY 会话。仅可显式选用 —— 它需要终端和 `pexpect`,因此永远不会被自动选中。 |

**未知的*名字*快速失败。**引用一个不在合并后注册表里的 agent,会在配置加载时抛出
`ValueError`(并列出已注册的名字),因此 `llm_caller.defaults` 或某个 reviewer 名
里的笔误,会在任何 LLM 调用发生之前就中断本次运行。

**项目层与全局层按条目合并**:`~/.se3/config.yaml` 可以放你这台机器全量的 agent
清单,项目里只需增补或覆盖它真正关心的条目。

**完全不写 `agents` 时**,默认链路按此顺序从 `PATH` 探测:先 `claude`(类型
`claude-code`),再 `codex`(类型 `codex`)。所有探测到的候选共同组成链路;若
`PATH` 上一个都没有,加载抛出 `ValueError`。`claude-interactive` 被刻意排除在这条
自动探测之外。

已移除的列表写法(`agents: [ … ]`)与遗留的 `claude_commands:` key 仍会被检测到:
列表写法只告警并整体忽略;`claude_commands` 会被自动迁移成一份合成的注册表外加一份
隐式的 `llm_caller.defaults`(并打印一次性告警,附上等价的新 schema YAML)。若同一
份配置里同时写了 `agents` 与 `claude_commands`,则忽略 `claude_commands`。

### `llm_caller`

哪条 agent 链路跑哪个 step。

```yaml
llm_caller:
  defaults: [primary, cheap]
  steps:
    implement: [primary]
    self_check: [[primary], [gpt]]
```

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `defaults` | agent 名列表 | 内置的 PATH 探测 | 所有没有单独覆盖的 step 所用的默认轮换链路。书写顺序**就是**轮换顺序。 |
| `steps` | mapping `<step> → agent 名列表` | `{}` | 按 step 的硬覆盖。key 必须是 `StepType` 的取值;未知 key 会打印一次性的『很可能是笔误』告警并被忽略。 |

轮换语义:在一条链路内部,`LLMCaller` 遇到*基础设施*类错误时轮换到下一个 agent。
轮换**严格发生在所选列表内部** —— 单个 runner 永不自行轮换,step 覆盖也永远不会
溢出到 `defaults`。

#### 坑 2 —— `llm_caller.steps.<step>` 是无 fallback 的硬覆盖

在这里声明一个 step,就是**替换**该 step 的链路。既不会隐式追加 `defaults`,也不会
回落到 `defaults`。想把默认 agent 作为尾巴,就必须自己列出来。

```yaml
llm_caller:
  defaults: [primary, cheap, backup]

  steps:
    # 如果你的本意是『先用 opus,再走常规链路』,这样写就错了。
    # implement 现在只有一个 agent;基础设施故障时无处可轮换。
    implement: [opus]

    # 正确 —— 默认尾巴被显式写出。
    plan: [opus, primary, cheap, backup]
```

不同来源之间的优先级同样是整值替换:若某个 step 在项目配置里声明过,全局
`~/.se3/config.yaml` 中对同一 step 的声明会被完全忽略(不拼接、不去重)。

以下退化写法一律视为*没有覆盖*(告警后回落到 `defaults`):非列表的值、空列表、
所有条目都非法的列表,以及已移除的内联 dict 写法(`- cmd: claude-opus`)。只有
未知的 agent *名字*才是致命的。

#### `self_check`:按 pass 的嵌套链路

`llm_caller.steps.self_check` 额外接受**列表的列表** —— 每个 self_check pass 一条
链路:

```yaml
llm_caller:
  steps:
    self_check:
      - [primary]        # 第 1 遍
      - [gpt]            # 第 2 遍 —— 换一家厂商来审同一份 diff
      - [primary, cheap] # 第 3 遍
```

- **扁平写法**(`self_check: [a, b]`)—— 一条链路复用于每一遍。完全向后兼容。
- **嵌套写法** —— 第 *i* 条链路驱动第 *i* 遍(从 1 开始计)。超出已声明链路数的
  那些遍,复用**最后一条**链路。
- **混合写法**(同一个列表里既有裸名字又有子列表)是配置错误:告警并回落到
  `llm_caller.defaults`。

嵌套写法同时也决定 pass 的遍数。当 `workflow.self_check_passes_required` *未*被
显式设置时,生效的遍数就是已声明链路的条数 —— 链路列表本身已经表达了意图。两者
都设置时,显式的遍数胜出(多出或不足时按上文所述复用或跳过链路)。

### `confirmation`

哪些 step 在流程继续之前要先过一道 review 闸口。

```yaml
confirmation:
  steps:
    plan:
      max_iterations: 3
    adjudicate:
      reviewer: human
```

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `steps` | mapping `<step> → 条目` | `{}` | **当且仅当**某个 step 作为 key 出现在这里时,它才被确认(外加下文永远开启的 `plan`)。 |
| `steps.<step>.reviewer` | string 或 null | `null` | `human` → 走 `tianluo/calls/` 的 MCP call 文件 + 交互式批准。填**某个 agent 名** → 由该 agent 做单 agent 的 LLM review。省略 / `null` → 由 `llm_caller.defaults` 做 LLM review。 |
| `steps.<step>.max_iterations` | 正 int | `3`(`_CONFIRM_DEFAULT_MAX_ITERATIONS`) | review→修改→再 review 循环的上限。非整数或 `<= 0` 会告警并回落到默认值。 |

**没有全局的开关。**只有出现在 `confirmation.steps` 里的 key 才会被确认。step 条目
内部的未知字段(除 `reviewer` / `max_iterations` 之外的一切)会被告警一次并忽略。

未知的 `reviewer` agent 名会在加载时抛出 `ValueError` —— 而且这项检查会走遍**每一个**
条目,而不只是即将运行的那个 step,因此写在本次流程序列之外的 step 下的笔误,同样
会在启动时暴露出来。

**plan-confirm 永远开启。**每个 `plan` step 之后都会插入一个执行专门的需求覆盖度
review 的 `CONFIRM` step,与本配置块无关 —— 即使删掉 `plan` 条目、甚至删掉整个
`confirmation.steps` 也照样插入。因此 `confirmation.steps.plan` 条目不再决定 plan
*是否*被确认;它剩下的唯一作用是定制 `reviewer` 与 `max_iterations`。没有该条目时,
plan-confirm 解析为由 `llm_caller.defaults` 做 LLM review、`max_iterations: 3`。

与之相对,`adjudicate` **默认不确认**:这里没有条目时,裁决自动通过、没有任何闸口
—— 包括那种会改写*任务描述*的裁决。如果一次无人值守的运行绝不能悄悄改写自己的任务,
请用 `adjudicate: { reviewer: human }` 显式开启。

以下废弃 key 会被检测到、按来源各打印一次告警后忽略:`confirmation.enabled`、
顶层的 `confirmation.reviewer`、`confirmation.llm_reviewer`,以及
`confirmation.steps` 的列表写法。

### `language`

两个互相独立的语言设置,与 `~/.se3/config.yaml` **逐字段**合并。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `language` | string 或 null | `null` | 统一的**人类语言**。它同时决定 CLI / 控制台的 UI 文案语言(经 i18n 语言包),以及注入到面向人类的 LLM step 输出(summarize / discovery / 已确认的各 step)中的语言。如 `zh-CN`、`en-US`。`null` = 不限制 LLM 输出的语言(UI 文案仍按下面的链路解析)。 |
| `spec_language` | string 或 null | `null` | **知识资产语言** —— `tianluo/charter.md` 与 code-index 的书写语言,注入到 `charter_freshness` 与 code-index 摘要 prompt 中。`null` = 不限制。 |

CLI UI 文案的解析顺序:`SE3_LANG` 环境变量 > 本 key(先项目、后全局)> 系统 locale
(`LC_ALL` / `LC_MESSAGES` / `LANG`)> `en-US`。`en-US` 是持有 key 全集的基准语言包;
缺失某个 key 或语言码不受支持时回落到它。

更改任一设置只影响此后新生成的内容,不会回溯翻译既有的知识资产。

中心 WebUI 控制台的界面语言是每用户的浏览器 / localStorage 偏好,**不**跟随这个
项目设置。

### `workflow`

fix loop 与 self_check 的行为。加载进 `WorkflowConfig`。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `max_fix_iterations` | int `>= 0` | `100` | test→verify→fix 循环的上限。**`0`(或 `null`)表示无限。**负数 → `ConfigError`。浮点数(哪怕是 `0.0`)或 bool 会告警并回落到 `100` —— 表示无限的哨兵值必须是字面量 int `0` 或 `null`。 |
| `self_check_passes_required` | int `>= 1` | `1` | 必须跑几遍 self_check。`< 1` → `ConfigError`。bool / 浮点 / 非整数会告警并回落到 `1`。与嵌套链路的相互作用见 [`llm_caller`](#self_check按-pass-的嵌套链路)。 |
| `self_check_convergence_enabled` | bool | `false` | 除了满足遍数之外,self_check 的各遍是否还必须收敛(不再发现新问题)。接受 `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off`;其余值告警并回落。 |
| `baseline_fix_max_attempts` | int `>= 0` | `3` | 每个 flow 针对*继承而来*的(implement 之前的 baseline)测试失败所做循环的上限。刻意与 `max_fix_iterations` 独立 —— 后者可能是表示无限的哨兵值,而继承来的失败必须自己有界。**`0` 完全禁用 baseline 循环**(继承来的失败只上报,不循环)。负数 → `ConfigError`。 |
| `self_check_defer_fix_threshold` | int `>= 0` | `3` | 用于嵌套 self_check 链路:当某个非最后一遍发现的问题*少于*此数量、且其中没有 critical / high 严重级时,推迟其修复,让剩余各遍先跑完;随后把各遍的发现去重合并进一次统一的 fix loop。**`0`(或 `null`)禁用推迟** —— 每一遍只要发现问题就立刻修(历史行为)。负数 → `ConfigError`。 |
| `adjudicate_period` | int `>= 0` | `10` | adjudicate step 那张兜底安全网的周期,单位是 fix 迭代次数:每 N 次 fix 迭代,即使没有任何结构性震荡信号触发,也强制跑一次 adjudicate。**`0`(或 `null`)禁用这张周期性的网**(adjudicate 此后只在结构性触发条件下运行:候选震荡 / 相互矛盾 / 反复复发)。与它的同类不同,这个 key 在**类型错误时快速失败**:bool、浮点或非数字字符串会抛 `ConfigError` 而非回落默认值 —— 因为悄悄回落等于启用了一个用户从未要求过的周期。能干净取整的字符串(`"7"`)仍会被强制转换。负数 → `ConfigError`。 |

`WorkflowConfig` 还带着一个名为 `self_check_passes_required_explicit` 的字段。它
**不是配置 key** —— 它在加载时派生(记录 `self_check_passes_required` 是否在 YAML
中出现过),供嵌套链路的遍数解析使用。在 YAML 里写它没有任何效果。

请注意本项目通用的哨兵约定:对每一个*迭代上限*,`0` 与 `null` 都表示『无限 / 禁用』,
负值则是快速失败的错误。这条规则同样适用于 `workflow.max_fix_iterations`、
`workflow.self_check_defer_fix_threshold`、`workflow.adjudicate_period` 以及
[`investigation.max_iterations`](#investigation)。

加载时,解析出的 `max_fix_iterations` 及其胜出的来源文件会按配置路径各记录一次
(`workflow config: max_fix_iterations=… (effective source: …)`),正是为了让
`tianluo.local.yaml` 遮蔽掉已提交值这件事可见,而不是变成一桩悬案。

### `investigation`

`investigate` step 自己那个有界的循环。`investigate` 处理『症状已知、原因未明』的
工作:它可以临时加日志或验证补丁,但必须在 step 结束前全部还原(净零 diff,由引擎
快照校验)。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `max_iterations` | int `>= 0` | `3` | 当一轮调查以无定论收尾(`conclusive=false`)时,最多还能再跑几轮。**`0`(或 `null`)= 无限。**负数 → `ConfigError`。bool / 浮点会告警并回落到 `3`。 |

预算耗尽**不会**让流程失败:它会带着目前最好的那个假设进入 `plan`,并标记为低置信度。

这个循环刻意与 fix loop(`workflow.max_fix_iterations`)分开:一轮调查是*探索*
预算,不是一次修复尝试;共用一个计数器会让漫长的修复史饿死调查(或者反过来)。

### `test`

test step 的命令、超时、附加阶段,以及『skip 不算通过』的闸口。加载进 `TestConfig`。
全部七个字段:

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `command` | string 或 null | `null` | 主测试命令,用 `shlex` 切分。`null` → 按项目布局自动探测:有 `pytest.ini` 或 `pyproject.toml` → `<python> -m pytest -v`;有 `package.json` → `npm test`;有 `Cargo.toml` → `cargo test`;有 `go.mod` → `go test ./...`;其余情况 → `<python> -m pytest -v`。 |
| `timeout` | int(秒) | `1800` | 主命令的兜底超时,同时也是任何未自带超时的 phase 的默认超时。与同块的其他 key 不同,这个 key **不是**逐项钳制并告警 —— 见下方提示。 |
| `phases` | map 的列表 | `[]` | 在主命令**之后**运行的附加命令。条目 schema 见下。 |
| `timeout_multiplier` | float `>= 1.0` | `2.0` | 计算动态超时时,施加在 LLM 估算的测试时长上的倍数。会带告警地上钳到 `1.0`,因此像 `0` 或 `0.1` 这样的笔误不会悄悄把该特性关掉。非数字会告警并回落到 `2.0`。 |
| `min_dynamic_timeout` | int `>= 1`(秒) | `30` | 计算出的动态超时的下限。会带告警地上钳到 `1`。 |
| `max_dynamic_timeout` | int(秒) | `14400`(4 小时) | 计算出的动态超时的上限,以免 fix loop 中反复超时把估算无限放大、把一个卡死的测试掩饰成『只是有点慢』。**实际默认值是 `max(14400, timeout)`** —— 一个刻意把 `test.timeout` 设得更大的项目,不会被压到低于它自己的明示意图。若配置值低于 `min_dynamic_timeout`,会带告警地提升到与之相等。 |
| `critical_tests` | string 列表 | `[]` | 必须真正跑起来的验收测试。见下文。 |

> **坑 —— 一个坏的 `timeout` 会丢掉整块配置。**`TestConfig.load()` 用一个笼统的
> `try/except` 包住了它的解析过程,出错时回落到一个**全默认的 `TestConfig`**。多数
> key 都逐项校验、钳制并告警,但 `timeout` 是用裸的 `int(...)` 转换的,因此像
> `timeout: "30m"` 这样的值会在该块内抛异常,并连带悄悄丢掉你的 `command`、
> `phases` 与 `critical_tests`。唯一的症状是一行日志:
> `Failed to load TestConfig from …, using defaults: …`。请写朴素的整数秒。

`phases[]` 条目的 key(由 test step 直接读取):

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `name` | string | —— | 阶段标签,用于结果与日志。实践上必填。 |
| `command` | string | —— | 命令,用 `shlex` 切分。必填。 |
| `cwd` | string 或 null | 项目根 | 工作目录,按项目根解析。 |
| `timeout` | int(秒) | `test.timeout` | 该阶段的超时。 |
| `required` | bool | `true` | 为 `true` 时,该阶段失败会让整次运行失败。`false` = 仅供参考。 |
| `in_fix_loop` | bool | `true` | 为 `false` 时,该阶段在 fix 迭代中被跳过(`get_phases_for_run(is_fix_iteration=True)` 据此过滤)—— 适合那种只想在第一遍跑的慢速冒烟套件。 |

主命令与每个 required 阶段在超时后都会原地重试一次,然后才被判为失败,这样一次
偶发的变慢不至于把流程推进 fix loop。

**`critical_tests` —— 『skip 不得冒充通过』的守卫。**pytest 对 SKIPPED 的测试退出码
仍是 `0`;而一个被改名、或被悄悄漏收集(import 错误、模式写错)的测试,会干脆从输出
里消失,运行却照样以 `0` 退出。这两种情况原本都会让 `tests_passed`(以及下游的
`verified`)变成假绿。每个条目都按 pytest 的单测 id(`path/to/file.py::test_name`)
做**子串**匹配,因此写全限定 id 就是精确锚点,而只写文件路径则匹配该文件内的每个
测试。对每个模式:

- 匹配到一个或多个**被 skip** 的测试 → 这些 id 上报为 `critical_skipped`,本次运行
  **不算 verified**;
- 否则,匹配到一个或多个真正跑过的测试(通过*或*失败)→ 认为该模式确实被执行到了
  (真正的失败会经正常的失败路径暴露);
- 否则 → 上报为 `critical_missing`,本次运行**不算 verified** —— 但仅限于本次运行
  确实产出了可解析的单测结果的情况。

最后那条限定很关键:在非 verbose 的测试命令下什么都解析不出来,于是缺失检测被跳过
(并告警),以免把每个模式都标成缺失。**`critical_tests` 需要一个输出逐条测试结果的
verbose 命令**,例如 `python -m pytest -v`。

该列表默认为空 —— 这是一个显式的 opt-in,因此普通的平台 / 可选依赖类 skip 永远不会
被惩罚。非列表的值只告警并禁用该闸口,不抛错。

### `e2e`

端到端测试:搭起一个真实的隔离环境(一个容器网络 + 一个或多个 service),在其中驱动
被测项目,并对实际发生的事情做断言。加载进 `E2EConfig`。**默认关闭** —— `e2e.enabled`
为 false 或整个块缺省时,状态机永远不会插入 `E2E` step,流程行为与该子系统存在之前
完全一致。

本块只承载**运行时设置**。e2e 的*内容* —— services、构建步骤、测试场景、基线截图 ——
放在独立的 [`tianluo/e2e/` 目录](#另一半tianluoe2e-内容配置)。这个切分是刻意的:
`enabled` 是**用户**的承诺(容器 runtime 已装好,且允许 fix loop 花时间跑场景),
flow 永远不会替你翻转它 —— 至多在输出里提示这个项目看起来适合上 e2e。而内容恰恰相反,
由 flow 像编写测试代码一样生成并持续演进。正因为两者物理分文件,『flow 从不写
`tianluo.yaml`』这条规则只需看改动路径就能机械核实。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `enabled` | bool | `false` | 总开关。为 false 时 `E2E` step 永远不会进入任何步骤序列。接受常见的布尔写法;无法识别的值只告警并保持 `false`。 |
| `runtime` | `auto` \| `docker` \| `podman` | `auto` | 使用哪个容器 runtime。见 [Runtime 选择](#runtime-选择)。非法值告警并回落 `auto`。 |
| `oci_runtime` | string 或 null | `null` | 透传给 runtime 的 `--runtime` 参数。指向 VM 级 OCI runtime(Kata Containers 之类)即可**仅凭配置**获得 VM 边界隔离,无需独立后端。`null` = 容器 runtime 自身的默认值。 |
| `build_timeout` | int `>= 1`(秒) | `1800` | 构建一个 service 镜像的预算。与 `scenario_timeout` 分开,是因为镜像构建是慢的那一半(冷层缓存下装依赖),场景执行是快的那一半。 |
| `scenario_timeout` | int `>= 1`(秒) | `300` | 每个场景的默认预算。单个场景可用自己的 `timeout:` 覆盖。 |
| `estimated_e2e_duration` | int `>= 1`(秒)或 null | `null` | 对应 [`test`](#test) 中 `estimated_test_duration` 所起的作用:让监管方分得清『还在跑』与『卡死了』。 |
| `scenarios` | list of strings | `[]` | 按名字做场景选择。**空表示『全部跑』**,而非『一个都不跑』。收窄它,使 fix loop 每轮不必重放整套 —— 与 `test.critical_tests` 是同一个先例。 |
| `critical_scenarios` | list of strings | `[]` | 必须真正跑到、结果才算数的场景。 |
| `keep_environment` | bool | `false` | 运行结束后保留容器与网络,便于 attach 进去查看。调试用;运行会打印出清理所需的 `rm -f` / `network rm` 命令原文。 |

每个字段都遵循 [`test`](#test) 的 clamp-and-warn 策略:非法值只记日志并回落为默认值,
不抛错 —— 一个旋钮上的拼写错误绝不会让整个项目加载不了。

#### 前置条件:一个你无需 sudo 就能跑的容器 runtime

e2e 需要 Docker 或 Podman,而这个**由你自行安装** —— 与你自行安装 `claude` / `codex`
CLI 完全一样。pip 提供不了容器 runtime。

tianluo 及其 e2e 子系统全程以你的普通用户身份运行,**任何代码路径都不调用 `sudo`、
不要求 root**。因此前置条件精确表述为:*当前用户可以不带 sudo 直接执行 `docker` 或
`podman`*。以下任意一条满足即可:

- 你的用户属于 `docker` 用户组;
- **rootless Docker**(Docker 自带 `dockerd-rootless-setuptool.sh install` 脚本);
- **Podman**,它经 user namespaces 原生 rootless,无特权用户开箱可用。

在 rootless runtime 下,bind mount 进去的源码目录会做 UID 映射(Podman 的
`--userns=keep-id`),使容器写进你源码目录的产物文件归属**你自己** —— 绝不会留下
root 所有、你自己清不掉的残留文件。

启用之前先检查宿主机:

```bash
luo e2e doctor
```

第二层(基线截图 diff)的图像对比需要一个第三方 Python 包,经 optional extra 隔离:

```bash
pip install 'tianluo[e2e]'
```

框架代码与 Dockerfile 模板随**每一次**安装分发 —— extra 隔离的是*依赖*,不是 tianluo
自己的代码。只装 core 时包仍可正常 import;项目启用了 e2e 却没装 extra、并执行到第二层
断言时,得到的是可操作的『请安装 `tianluo[e2e]`』提示,而不是 `ModuleNotFoundError`。

#### Runtime 选择

`auto` 的探测方式是**执行** `docker info`、再执行 `podman info`,取第一个成功的。这里
刻意不是查 `PATH`:最常见的故障恰恰是 runtime *装了、但当前用户用不了* —— 没加进
`docker` 组、daemon 没起 —— 而查 PATH 会欣然选中它。执行一次 `info` 则一举验明:二进制
存在、daemon/环境正常、当前用户有权限。同一份代码同时充当 preflight 检查,因此探测与
preflight 不可能给出互相矛盾的结论。

- **两者都可用 → 选 `docker`**,确定性优先序。BuildKit/buildx 生态更成熟,且在双装的
  机器上 Docker 通常是用户刻意安装、日常使用的那一个。想优先 podman?显式写出来即可。
- **显式指定 runtime 即关闭回退。** 配了 `runtime: docker` 而 Docker 不可用时,报错并给
  修复指引 —— tianluo *不会*悄悄改用 podman。静默切换会在你背后改变镜像缓存、存储位置与
  UID 映射行为,恰好制造出那种最难排查的『昨天还好好的』故障。
- **探测结果在一次会话内固定。** 同一次 run 的全部容器操作使用同一 runtime,不中途混用。

探测失败按**环境**问题上报,并附逐条修复指引(加入 `docker` 组 / 安装 podman / 配置
rootless Docker),绝不当作代码缺陷 —— 见下文的失败路由。

#### 另一半:`tianluo/e2e/` 内容配置

内容配置有自己的目录,与 `charter.md`、`code-index.md`、`issues/` 同级,并**进 git**:

```
tianluo/e2e/
├── environment.yaml     # services 拓扑:基底镜像、构建步骤、就绪探测
├── scenarios/
│   ├── cli-smoke.yaml   # 一文件一场景:driver + 操作序列 + 断言
│   └── api-smoke.yaml
└── baselines/           # 进 git 的基线截图,供第二层 diff 使用
```

镜像**不**进 git —— 它是可再生缓存,凭这份配置可从零重建。被测项目的源码是 *bind
mount* 进容器的,而不是 `COPY` 进镜像,因此 fix loop 每轮迭代只需重启容器;仅当构建
步骤本身变更时才触发镜像重建。

开关已开但目录尚不存在时,flow 会在首次使用时生成它,此后按增量演进维护 —— 只新增与
修订,不覆盖你的手工改动。你也可以手动驱动:

```bash
luo e2e bootstrap          # 生成 / 演进内容目录
luo e2e list               # 列出已声明的场景
luo e2e run                # 全部跑一遍
luo e2e run -s api-smoke   # 只跑一个
luo e2e run --keep         # 跑完保留环境供查看
```

`luo e2e run` 与流程内的 `E2E` step 共用同一套执行逻辑,因此手动调试与 flow 内行为完全
一致。退出码:场景失败 `1`,环境问题 `3`,配置缺失/不合法 `4`。

一个最小的单 service 示例 —— `tianluo/e2e/environment.yaml`:

```yaml
network: tianluo-e2e
services:
  - name: app
    image: python:3.12-slim
    base_kind: base            # base | playwright | gui-xvfb
    build:
      - pip install --no-cache-dir -e .
    readiness:
      kind: command            # command | http | tcp | log
      command: ["python", "-c", "import myapp"]
      timeout: 60
```

……以及 `tianluo/e2e/scenarios/cli-smoke.yaml`:

```yaml
name: cli-smoke
driver: app                    # 必须指向上面已声明的某个 service
actions:
  - action: exec
    command: ["python", "-m", "myapp", "--version"]
assertions:
  - kind: exit_code
    equals: 0
  - kind: stdout
    contains: "myapp "
```

`base_kind` 决定在 `image` 之上叠哪一份内置 Dockerfile 模板:`base`(纯 CLI / web /
API)、`playwright`(浏览器 driver —— 官方镜像固定了浏览器、系统依赖*与字体*,这正是
第二层基线可复现的前提)、`gui-xvfb`(Xvfb + 轻量窗口管理器 + scrot + xdotool,使桌面
应用无需物理显示器即可运行并被截图)。

**断言升级阶梯由 schema 强制,而非仅仅建议。** 第一层是确定性断言(`exit_code`、
`stdout`、`stderr`、`http_status`、`http_body`、`file_exists`、`file_content`、`dom`),
是默认层,无需声明。第二层是与已提交基线做 `screenshot_diff`,断言上必须写
`visual_regression: true`。第三层是 `visual_semantic` —— LLM 看图 —— 必须同时声明
`semantic_visual: true` 与 `require_evidence: true`,因为 LLM 的结论只有连同可复核的
证据描述一起才可采信。越过本可胜任的低层而升级到高层是**校验错误**:DOM 查询就能解决的
地方改用截图对比,等于把确定性验证退化成概率性验证。驱动侧同理 —— 点击屏幕坐标
(`visual_click`)只保留给没有任何程序化入口的 GUI。

#### E2E step 在流程中的位置,以及失败如何路由

`enabled` 为真时,`E2E` step 被插在 **`test` 紧后面**,因而位于 `self_check` 之前:e2e
是单测套件的粗粒度对偶,所以它跑在已通过细粒度检查的代码上,而 review 层随后读到的是
一份行为已被实际执行过的 diff。没有 `test` step 的序列(`review`、`survey`)原样返回 ——
它们不产生代码变更,没有什么可供场景去执行。

失败路由刻意一分为二:

- **场景失败是代码缺陷。** 返回 `REVISION_NEEDED` 并进入常规 fix loop,受
  [`workflow.max_fix_iterations`](#workflow) 约束,与单测失败完全同构;预算耗尽后经同一
  条 fix-loop-exhaustion 通道建 issue。不存在丢弃、豁免或按 severity 分级放行。
- **环境问题不是。** runtime 不可用、权限不足、preflight 失败 —— 这些让 step 失败并附上
  修复指引,且**不**消耗 fix 迭代次数。派 LLM 去『修』一台没装 Docker 的宿主机,只会
  白白烧光整个 fix 预算。

### `implement`

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `group_loc_threshold` | int | `300` | 估算 LOC 总量在此值及以下时,多 group 的任务计划会被合并成**单次** LLM 调用,而不是逐 group 分发。超过阈值时,它还参与『顺序 vs DAG 并行』的判定。**属于 fail-fast,而非 clamp-and-warn**(见下)。 |
| `use_worktree` | bool | `true` | implement 的各 group 是否在隔离的 git worktree 中运行。接受常见的布尔写法;无法识别的字符串回落到默认值,而不是反转行为。**可在运行时被 `SE3_IMPLEMENT_USE_WORKTREE` 环境变量覆盖**,该变量胜过 YAML 中的值。 |

`group_loc_threshold` 是 clamp-and-warn 规则的显式例外之一:`ImplementConfig.from_dict`
对该值直接调用裸的 `int(...)`,既没有 `try`/`except`,implement step 里的调用方也没有
兜底。`int()` 解析不了的值 —— 例如 `group_loc_threshold: "300 LOC"` —— 会从
`ImplementConfig.load` 抛出未捕获的 `ValueError`,直接**让 implement step 失败**,而
不是回落到 `300`。float 与 bool 可以被接受,但会被 `int()` 静默截断(`300.9` → `300`,
`true` → `1`)。

### `steps`

| YAML key | dataclass 字段 | 类型 | 默认值 | 含义 |
|----------|----------------|------|--------|------|
| `steps.append` | `StepConfig.append_steps` | string 列表 | `[]` | 追加到默认 step 序列末尾的 step 类型名。 |

**注意名字不一致**:YAML 的 key 是 `append`,dataclass 的字段是 `append_steps`。在
YAML 里写 `steps.append_steps:` 不起任何作用。

```yaml
steps:
  append:
    - summarize
```

条目会按 `StepType` 枚举校验:未知的名字**被静默忽略**(不告警),已在序列中的 step
不会重复添加。追加是唯一受支持的改动方式 —— 没有 `steps.remove`,也没有
`steps.replace`。

### `version`

`commit` step 的版本号递增行为。加载进 `config.VersionConfig`。

> **命名陷阱。**显式指定版本文件路径的 YAML key 是 **`version_file`**,不是
> `file_path`。`VersionConfig.from_dict` 读的是 `version_data.get("version_file")`;
> `.file_path` 只作为 dataclass 上的只读*属性别名*存在(为了与
> `version_bumper.VersionConfig` 鸭子类型兼容),永远不会从 YAML 读取。你配置里的
> `version: { file_path: … }` —— 包括 `tianluo.example.yaml` 里的那个 —— 都会被
> 静默忽略。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `enabled` | bool | `true` | commit step 中自动递增版本号的总开关。 |
| `version_file` | string 或 null | `null` | 版本文件的显式路径。`null` = 自动探测(`pyproject.toml`、`package.json` ……)。 |
| `include_in_commit_message` | bool | `true` | 提交信息里是否带 `Version: X.Y.Z` 尾行。 |
| `script_path` | string 或 null | `null` | 项目版本脚本的路径。`null` = 默认的 `tianluo/scripts/version.py`。 |
| `auto_generate_script` | bool | `true` | 找不到该脚本时,是否用 LLM 生成一份。 |
| `auto_bump` | bool | `true` | *空转。*引擎中没有消费方 —— 版本递增由 `version_analyze` 的 `suggested_version` 驱动、由 `confirmation` 把关,与这个开关无关。 |
| `confidence_threshold` | string 或 null | `null` | *空转。*历史上用 `"medium"` / `"high"` 选择哪些置信度需要人工确认。 |
| `prerelease_prefix` | string | `""` | *空转。* |
| `prerelease_number` | int | `0` | *空转。* |
| `templates` | map | `{readme_badge: …, versions_entry: …}` | *空转 / 遗留。*已被 [`documentation`](#documentation) 块取代,后者才是 `DocumentationUpdater` 真正读取的。保留它只为让旧配置仍能解析。 |
| `readme_enabled` | bool | `true` | *空转。* |
| `readme_marker` | string | `"<!-- SE3-VERSION -->"` | *空转。* |
| `versions_enabled` | bool | `true` | *空转。* |
| `versions_file` | string | `"VERSIONS.md"` | *空转。* |
| `versions_header` | string | `"# Version History\n\n"` | *空转。* |

以下废弃 key **被接受但会告警后忽略**:`bump_rules` 与 `smart_version_analysis`。
两者都在版本决策模型收敛为『由 `version_analyze` 产出的唯一权威 `suggested_version`』
时被移除。想定制递增规则,请改为把自然语言规则写进 `tianluo/version-rules.md` ——
该文件会被注入 `version_analyze` 的 prompt;文件不存在时回落到默认的 SemVer 2.0.0。

注意 worktree 流程在这里的分工:worktree 会话的 commit **不是**它的发布点,因此
不写版本文件;写版本文件的是 merge 侧的 `version_reconcile` step。

### `documentation`

commit step 机械地更新 `README.md` 与 `VERSIONS.md` 时,`DocumentationUpdater` 所用
的模板。这才是真正驱动 updater 的配置块,已取代遗留的 `version.templates`。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `readme_badge_template` | string | `![Version](https://img.shields.io/badge/version-{{version}}-blue)` | 写到 README 版本徽章位置上的 markdown。 |
| `versions_entry_template` | string | 打包内置的 `versions_md.md` 模板里的第一个 `##` 块;没有则为 `## {{version}} - {{date}}\n\n{{changes}}\n` | 每次发布时前插到 VERSIONS.md 的条目。显式配置的值会被**原样采用、不做任何校验** —— 漏写 `{{changes}}` 就会静默产出没有变更正文的发布条目。`{{version}}` + `{{changes}}` 这个要求只作用于打包文件回落路径:`versions_md.md` 的第一个 `##` 块只有同时带上两者才会被当作模板接受,否则改用内置默认值。 |
| `readme_header_template` | string | *(未设置)* | 可选。设置后,README 中的版本头行也会被一并替换。未设置意味着整个 header 环节被跳过。 |

非字符串的值(以及非 mapping 的 `documentation:` 段)会被丢弃,于是 updater 保留
它的内置默认值。

**占位符**采用 `{{双大括号}}` 形式,通过朴素的字符串替换完成。commit step 提供的
上下文是:

| 占位符 | 值 |
|--------|-----|
| `{{version}}` | 新的版本号字符串。 |
| `{{date}}` | 渲染时刻的 `YYYY-MM-DD`。 |
| `{{year}}` | 渲染时刻的四位年份。 |
| `{{changes}}` | *(仅 VERSIONS 条目)*渲染好的变更条目。 |

**徽章的匹配与插入。**`update_readme` 按以下顺序寻找已有徽章:静态的 shields 风格
`![Version](…version-X-…)` 链接、任意 `![version](…)` 链接(大小写不敏感)、
`<img …version…>` 标签。**第一个**匹配上的模式会被替换(`count=1`)为渲染后的模板。
若**一个都没匹配上**,渲染后的徽章会被*插入*到标题行之后。只有内容真的变了才会写盘。

**幂等 no-op 技巧(本仓库正在用)。**由于渲染后的模板是逐字替换匹配到的徽章,一个
**不含 `{{version}}` 占位符**的模板渲染出来就是一个常量字符串。一旦 README 里已经是
这个字符串,第二条模式会匹配到它,`re.sub` 把它替换成它自己,
`content == original_content`,于是什么都不写 —— 徽章改写变成一次又一次逐字节的
no-op。

本仓库根目录的 [`tianluo.yaml`](../tianluo.yaml) 正是用这一招钉死了一个 shields.io
的*动态*徽章,让它直接从 `pyproject.toml` 读版本号,从而 README 里不留任何硬编码
版本号,commit step 也永远不会把它改回去:

```yaml
documentation:
  readme_badge_template: "![Version](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2FCoREse%2Ftianluo%2Fmaster%2Fpyproject.toml&query=%24.project.version&label=version&color=blue)"
```

有两个细节是这招成立的关键,照抄时务必保留:alt 文本必须保持 `![Version]`(这样
`![version](…)` 那条模式才仍能找到它 —— 否则 updater 找**不到**徽章,会**插入第二个**),
以及模板里不能含 `{{version}}`(否则每次发布渲染出的字符串都不同,文件就会被重写)。

### `code_index`

构建与渲染 `tianluo/code-index.md` 的旋钮。全部八个字段:

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `degrade_trigger_lines` | 正 int | `2000` | 一个无结构、非二进制的文本文件行数达到此值时,即有资格进入按行 / 按字节切块的**降级**模式(AST / 结构边界才是正常的切分粒度,切块是最后手段)。这是两个体积触发条件之一 —— 谁先命中谁生效。 |
| `degrade_trigger_bytes` | 正 int | `262144`(256 KiB) | `degrade_trigger_lines` 的字节版对应项。 |
| `chunk_lines` | 正 int | `200` | 文件降级为切块后,每块最多跨越这么多行。 |
| `chunk_bytes` | 正 int | `16384`(16 KiB) | `chunk_lines` 的字节版对应项 —— 先命中的那个限制切断当前块。 |
| `exclude` | string 列表 | `[]` | 枚举时排除的项目相对路径模式。它为基于 gitignore 的遍历兜底,处理那些已被 git 跟踪、但并不想要的噪音(vendored 二进制、超大生成文件)。非字符串 / 空白条目会被丢弃并告警;非列表的值告警并得到 `[]`。 |
| `view_budget_bytes` | 正 int | `8192`(8 KiB) | 注入到每个流程 step 的自适应**根视图地图**的字节预算。刻意设得小:它约束的是一份每步都注入的定位地图,并自然地停在目录粒度上 —— 这正是合适的高度,函数级细节按需用 `luo code-index show` 拉取。 |
| `primary_roots` | string 列表 | `[]` | 自适应根视图会钻进其子树的顶层目录名;其余保持折叠。`[]` = 自动探测承载代码的顶层目录。条目写不写结尾斜杠都行(`src` 或 `src/`),会被统一规范为带结尾斜杠。 |
| `max_concurrency` | 正 int | `4` | (重)构建期间并发执行的逐文件 LLM 摘要调用数。默认保守,因为天花板由 LLM 配额 / 限流决定(这些调用是 I/O 密集而非 CPU 密集);可按你后端的限额调高。 |

每个整数字段都是钳制并告警:bool、浮点、非整数或非正值都只记一条告警并回落到默认值,
因此一份写坏的 `code_index:` 绝不会让索引重建失败。

### `merge`

`luo merge` 编排器的行为。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `strategy` | `fast` \| `safe` \| `strict` | `fast` | 冲突解决的档位。`fast`:由 LLM 解决冲突;失败时 merge 直接退出,不叫人,也绝不回落到 take-theirs(它继承了旧 robust 策略对脏工作区的 stash 行为)。`safe`:由 LLM 解决,收敛不了时升级为人工 MCP call。`strict`:每个冲突都直接交给人工 call,不走 LLM。 |
| `delete_merged_default` | bool | `true` | `luo merge` 是否默认删除已合并的分支(并归档其 worktree)。单次调用可用 `--no-delete-merged` 退出该行为。 |
| `strict_runtime_sync` | bool | `false` | merge 期间对运行时状态做更严格的对账。 |
| `max_conflict_resolve_iterations` | int `>= 1` | `10` | LLM 解决冲突的轮数上限。`< 1` → `ConfigError`;非整数告警并回落到 `10`。 |

已移除的策略名 `default` 与 `robust` **不会被静默地当作别名** —— 它们会抛出带迁移
提示的 `ConfigError`,以免一份过时的配置悄悄改变 merge 语义。请把 `default` 迁移为
`safe`,`robust` 迁移为 `fast`。

### `conflict_resolver`

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `strategy` | `human` \| `llm` | `human` | **loop 分支**合并的冲突处理方式。`human`:保留冲突现场,写一个 call 文件,等人来处理。`llm`:尝试逐文件的 LLM 解决,失败时回落到人工。 |

无法识别的值会被静默地强制为 `human`(fail-safe,不报错)。

本块与 [`merge`](#merge) 不同:`merge.strategy` 管的是 `luo merge` 编排器,
`conflict_resolver.strategy` 管的是 loop 分支的合并路径。

### `claude_subprocess`

tianluo 作为 worker 拉起的 Claude CLI 子进程的设置。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `setting_sources` | 由 `user` \| `project` \| `local` 组成的非空列表 | `["user"]` | 被拉起的 CLI 加载哪些 Claude settings 文件,原样透传为 `--setting-sources <csv>`。 |

`["user"]` 这个默认值把 **tianluo 的 worker 与目标项目自己的
`.claude/settings.json` 隔离开**,使那些针对该项目自身子 LLM 的 `permissions.deny`
规则,无法把 tianluo 的 plan / implement / review 子进程锁在门外。要重新纳入,请
显式写 `["user", "project"]`。

这个 key **快速失败**:非列表、空列表、非字符串元素,或任何不在
`{user, project, local}` 内的值,都会在加载时抛 `ValueError`,而不是告警后回落默认值。
非 mapping 的 `claude_subprocess:` 段同样抛错。

> **坑 —— 重复的 `--settings` 是后者胜出。**Claude CLI 的 `--settings` 参数*不会*
> 累加:argv 中后出现的第二个会整体覆盖第一个,连它选定的 `model` 一起覆盖。本项目
> 曾被这一点咬过:一个守卫把自己的 `--settings` 追加在某个 agent 包装脚本的
> `--settings` 之后,包装脚本指定的 model 被静默丢弃,实际跑的是 user settings 里的
> model。引擎现在改用 `--plugin-dir` 安装它的 spec 写保护守卫 —— 该参数是会话级的、
> 可重复、且**叠加式**加载,因此不会覆盖 agent 的 `--settings`。如果你自己的
> `agents.<name>.cmd` 是一个会传 `--settings` 的包装脚本,请确保最终 argv 里只有它
> 这一个。注意 `--setting-sources`(即本配置 key)是另一个参数、语义不同,不受影响。

### `spec_write_protection`

两层互相独立的硬防护,阻止不该写 spec 的 step 写入 `tianluo/specs/**`。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `hook_enabled` | bool | `true` | 安装 `PreToolUse` 的 spec 写入 hook —— 主要的实时拦截层。 |
| `diff_fallback_enabled` | bool | `true` | 运行 step 之后的 spec diff 兜底检查 —— 捕捉 hook 看不见的 Bash 重定向写入。 |

两个 key 都**快速失败**:非布尔值(或非 mapping 的段)会抛 `ConfigError`,因此像
`hook_enabled: "false"` 这样的笔误无法悄悄关掉守卫。整段缺失则两者都取默认值
(全部开启)。

### `server`

> 仅适用于可选的中心控制面(`pip install 'tianluo[server]'`,经 `tianluo-server`
> 入口启动)。只装 core 的安装在运行时永远不会读它。部署、TLS 反向代理、引导首个
> 管理员与 daemon key 签发,请见
> [docs/daemon-and-server.zh.md](daemon-and-server.zh.md)。

`server:` 是一个普通的顶层 key:项目的 `server:` 段会**整体替换**全局的那个
(不做深度合并)。每个字段都有默认值,因此一台没有 `server:` 段的服务器照样能正常
起来。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `db_path` | string | `~/.se3/server.db` | 承载身份 / 鉴权的嵌入式 sqlite 存储路径。`~` 在使用时展开。`tianluo-server --db-path` 参数可对单次启动覆盖它。 |
| `auth` | map | *(全部默认)* | 见下文。 |

#### `server.auth`

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `providers` | 由 `local` \| `oidc` \| `proxy_header` 组成的列表 | `["local"]` | 要装配的鉴权 provider 有序链。未知 / 空白 / 非字符串的条目会被丢弃并告警;若最终没有任何合法项(或该值根本不是列表),回落到 `["local"]`,因此服务器绝不会带着空 provider 链起来。重复项会被折叠。 |

#### `server.auth.session`

UI 会话 cookie 的属性。默认值面向『部署在 TLS 终止的反向代理之后的公网服务』做了
fail-safe 取舍。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `cookie_name` | 非空 string | `se3_session` | 会话 cookie 名。 |
| `cookie_secure` | bool | `true` | 设置 `Secure` 属性。除非你在 localhost 上跑明文 HTTP,否则请保持开启。 |
| `cookie_httponly` | bool | `true` | 设置 `HttpOnly` 属性。 |
| `cookie_samesite` | `lax` \| `strict` \| `none` | `lax` | `SameSite` 属性(比较时转小写)。非法值告警并回落。 |
| `max_age_seconds` | 正 int | `86400`(24 小时) | 会话有效期。 |

#### `server.auth.local`

内置的用户名 + 密码 provider 的暴力破解防护。两种互相独立的机制:连续失败锁定,
以及滑动窗口限流。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `max_failed_attempts` | 正 int | `5` | 连续失败多少次即锁定账号。 |
| `lockout_seconds` | 正 int | `300`(5 分钟) | 锁定持续多久。 |
| `ratelimit_window_seconds` | 正 int | `60` | 滑动限流窗口。 |
| `ratelimit_max_attempts` | 正 int | `10` | 每个窗口内接受的登录尝试次数。 |

这几项遇到 bool、非整数或非正值都是钳制并告警 —— 一个笔误不会悄悄让锁定窗口失效。

#### `server.auth.oidc`

为未来的 OIDC 社交登录 provider 预留的配置**接缝**;默认禁用,v1 未实现。`enabled`
为 false 时,其余字段都是空转的。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `enabled` | bool | `false` | 打开该 provider。 |
| `issuer` | string 或 null | `null` | OIDC issuer URL。 |
| `client_id` | string 或 null | `null` | |
| `client_secret` | string 或 null | `null` | |
| `redirect_url` | string 或 null | `null` | |
| `scopes` | 非空 string 列表 | `["openid", "email", "profile"]` | 空列表或格式错误会告警并回落到默认值。 |

#### `server.auth.proxy_header`

为『信任反向代理注入的身份 header』预留的配置**接缝**;默认禁用,v1 只提供接缝。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `enabled` | bool | `false` | 打开该 provider。 |
| `trust_proxy` | bool | `false` | 信任上游代理给出的身份断言。 |
| `header` | 非空 string | `X-Forwarded-Email` | 承载身份的 header。 |

> **启用它的安全前提**:反向代理必须剥掉客户端自带的同名 `header`,且服务器必须
> 无法被绕过代理直接访问。否则注入的身份即可伪造,这就是一个授权漏洞。

### `presets`

面向重复性任务的具名 prompt 模板,用 `luo run --preset <name>` 运行(用
`luo run --preset list` 列出)。由 `preset_loader` 读取,不经 `config.py`。

```yaml
presets:
  doc-sync:
    type: feature
    prompt_file: tianluo/prompts/doc-sync.md
```

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| *(mapping 的 key)* | string | —— | preset 名。 |
| `type` | 任务类型 | 内置默认值,或已有条目的类型 | 该 preset 以哪种任务类型运行(`feature`、`bugfix`、`small`、`review`、`survey`)。 |
| `prompt_file` | string | `tianluo/prompts/<name>.md` | prompt Markdown 的路径,相对项目根。 |

解析分两层。内置 preset 随包发布、运行时读取(`luo init` 从不复制它们),因此升级
tianluo 就会自动获得最新的那批。项目级 preset 按名字覆盖内置的,而它自身又由两个来源
构成:先是对 `tianluo/prompts/*.md` 的零配置扫描(文件名主干 = preset 名,类型取
默认值),然后才是这个 `presets:` 块 —— 它把元数据叠加到扫描结果上,并且可以把某个
preset 重定向到任意 `prompt_file`。在这里声明一个 preset、却既不给 `prompt_file`
也没有对应的 `tianluo/prompts/<name>.md`,会解析到那个约定位置,并在它确实不存在时
报错。

---

## Legacy / 历史遗留配置

下面这些块名来自已退役的 **spec 镜像**时代 —— 那时 `tianluo/specs/**` 是一份受治理
的代码镜像。该镜像已经退役 —— 代码是唯一真相源,经由 code-index、
`tianluo/charter.md` 以及同位的 why-comments 对外暴露。保留下面这些块,是因为它们
仍能被解析(其中一个甚至仍在驱动一项真实检查),而不是因为它们还描述着当前的知识
模型。这里没有任何东西值得在新项目里配置。

### `spec_governance`

spec 文件体积的字节预算与一个执法档位。容错:非法值告警并回落,永不抛错。

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `base_max_bytes` | 正 int | `32768`(32 KiB) | `base` spec(历史上每个 step 都全量注入的那一份)的预算。 |
| `index_render_threshold` | 正 int | `16384`(16 KiB) | 超过此阈值时,`luo spec index` 的输出会被折叠为分组句柄。**空转** —— spec 索引渲染器已随 spec 体系一同退役;保留该字段只为向后兼容,它不驱动任何渲染器。 |
| `spec_file_warn_bytes` | 正 int | `65536`(64 KiB) | 单个 spec 文件达到此体积时,guardrails 报出 `SIZE_SPEC_FILE` 违规。 |
| `requirement_warn_bytes` | 正 int | `8192`(8 KiB) | 单个 Requirement 达到此体积时,guardrails 报出 `SIZE_REQUIREMENT` 违规。 |
| `guardrails_size_tier` | `warn` \| `enforce` | `warn` | `warn` 打印违规并以 `0` 退出;`enforce` 打印违规并以 `1` 退出,而且会让超预算的 spec 像内容违规那样**阻断一次 merge**。 |

**它们如今还被什么用着(已对照代码核实):**三个字节阈值由
`engine/merge/guardrails.py` 中的 `check_spec_sizes()` 读取,而该函数由
`luo guardrails` 命令调用 —— 档位为 `enforce` 时,merge 的 guardrail 检查也会调用它。
`guardrails_size_tier` 还额外决定 `luo guardrails` 的退出码。但 `check_spec_sizes`
遍历的是 `tianluo/specs/<name>/spec.md`;在一个后 spec 时代的项目里该目录并不存在,
于是这项检查无物可量,这些阈值也就没有任何可观测的效果。它们**不**被 code-index 的
降级逻辑使用 —— 那是 [`code_index.degrade_trigger_*`](#code_index),另一个块。

### `spec_loading`

| Key | 类型 | 默认值 | 含义 |
|-----|------|--------|------|
| `steps` | mapping `<step> → items` \| `full_spec` | `{}` | 按 step 的 spec 内容加载模式:`items`(头部 + 选中的若干 requirement)对 `full_spec`(整个 spec 文件)。非法值会被跳过并告警,于是内置默认值生效。 |

**完全空转。**`SpecLoadingConfig` / `load_spec_loading_config()` 在 `config.py` 之外
没有任何调用方(已用全仓 grep 核实,覆盖 `src/` 与 `tests/`)。历史上 `update_spec`
默认取 `full_spec`;它已转向 index-first 协议、完全不再消费 spec 文本,而那份
默认 full_spec 的集合如今是空的。`mode_for()` 对每个 step 都返回 `items`。设置这个块
不会改变任何事情。

### 引擎已不再读取的配置块

`tianluo.example.yaml` 里仍然带着下面两个块。**引擎两个都不读** —— 已通过在 `src/` 中
grep 每一个 key 名、以及 grep `get("human_call")` / 顶层的 `get("session")` 核实;
`session` 唯一的命中是*嵌套的* `server.auth.session` 块,以及 `engine/chat_history.py`
中一个无关的 JSON 字段。它们留在样例里只为历史延续性,配置它们没有任何效果。

| 配置块 | Key | 状态 |
|--------|-----|------|
| `human_call` | `timeout_days`、`directory` | 无读取方。human call 文件写到 `tianluo/calls/`,路径由运行时布局固定,与这个 key 无关。 |
| `session` | `progress_file`、`max_progress_entries` | 无读取方。 |

> 这张表里原本还有一个 `e2e` 块,带着 `baseline_dir`、`diff_threshold`、
> `default_viewport`、`test_paths` 四个从来没有读取方的 key。这个名字此后被一个真实的
> 子系统收回,其 schema 完全不同:见 [`e2e`](#e2e)。那四个遗留 key 不被它识别,会被忽略。

---

## 排错:改了配置却没有任何变化

**头号嫌疑人永远是[坑 1](#坑-1--tianluolocalyaml-替换整个文件)** —— 一个
`tianluo.local.yaml` 遮蔽了你刚编辑的文件。先确认到底哪个文件在生效:

```bash
python -c "
from pathlib import Path
from tianluo.config import get_project_config_path
print(get_project_config_path(Path('.')))
"
```

如果它打印出来的路径不是你编辑的那个(注意:当你身处 worktree 中时,它可能指向
**主仓库**),那就找到原因了。

然后把你关心的那个块解析后的值打印出来 —— 每个 loader 都是可以直接调用的普通函数:

```bash
python -c "
from pathlib import Path
from tianluo.config import (
    load_workflow_config, load_investigation_config, load_docs_config,
    load_code_index_config, load_merge_config, load_agents,
    load_confirmation_config, load_language_config,
)
p = Path('.')
print(load_workflow_config(p))
print(load_investigation_config(p))
print(load_docs_config(p))
print(load_code_index_config(p))
print(load_merge_config(p))
print(load_agents(p))
print(load_confirmation_config(p))
print(load_language_config(p))
"
```

其余 loader 的形状相同:`TestConfig.load(p)`、`ImplementConfig.load(p)`、
`StepConfig.load(p)`、`load_version_config(p)`、`load_server_config(p)`、
`load_claude_subprocess_config(p)`、`load_spec_write_protection_config(p)`、
`load_conflict_resolver_config(p)`、`load_step_agents(p, "implement")`、
`load_self_check_resolution(p)`。

剩余情况的排查清单:

1. **`tianluo.local.yaml` 里有 YAML 语法错误。**它照样遮蔽 `tianluo.yaml`,于是一切
   回落到内置默认值。loader 会打印一条点名该文件的一次性告警 —— 把日志打开跑一次,
   或者干脆 `python -c "import yaml; yaml.safe_load(open('tianluo.local.yaml'))"`。
2. **某条按 step 的链路吞掉了你对 `defaults` 的修改。**见
   [坑 2](#坑-2--llm_callerstepsstep-是无-fallback-的硬覆盖):
   `llm_caller.steps.<step>` 永不回落。用 `load_step_agents(p, "<step>")` 确认。
3. **key 名写错了。**本代码库里真实存在的名字不一致有两处:`steps.append`
   (不是 `append_steps`),以及 `version.version_file`(不是 `file_path`)。未知 key
   通常会被静默忽略。
4. **这个块是空转或遗留的。**对照
   [Legacy / 历史遗留配置](#legacy--历史遗留配置)以及 [`version`](#version) 表里的
   *空转*标记核查。
5. **值被钳制或拒绝了。**多数字段是告警并回落而不抛错;loader 打印出来的解析值才是
   ground truth。`workflow` 还会按配置路径各记录一次它生效的来源:
   `workflow config: max_fix_iterations=… (effective source: …)`。
6. **某个环境变量胜出了。**`SE3_IMPLEMENT_USE_WORKTREE` 覆盖
   `implement.use_worktree`;在 CLI UI 文案上,`SE3_LANG` 压过 `language.language`。
7. **你改的是 `tianluo.example.yaml`。**它是随包发布的样例,永远不会被读取。请先把它
   复制成 `tianluo.yaml`。

