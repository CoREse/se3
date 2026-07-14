# WebUI 聊天历史「头部记录永久丢失」根因诊断（G1）

现场：flow `20260714-122542_d4e052c5`，discovery 步，worktree 模式，status=paused。
症状：WebUI 第一条消息完全不可见，且永不自愈。

本文件是 G1（只读分析）的产物，供 G6 汇入验证报告。**本组不改生产代码。**

---

## 0. 结论摘要

| 问题 | 结论 |
|---|---|
| 是「旧代码未生效」还是「新缺陷」？ | **新缺陷。** 现场三个投递面（daemon / se3-server / 浏览器实际加载的 app.js）都已包含 e41a6a31。 |
| issue 288（`tagStepType` InvalidCharacterError） | **已由 e41a6a31 修复**，且在真实记录上复验通过。与本次故障无因果关系。 |
| 头部记录 r0 在服务器侧存在吗？ | **存在。** 服务器 bundle 含 2 条记录；头部可被 full / backfill 取回。**daemon 侧无需改动。** |
| 头部为何永不回来？ | **`not_modified` 是一个吸收态（absorbing state）**：进度回执 `o` 是服务器「我已下发多少条」的自签声明，从不与客户端「实际留存多少条」核对；客户端对 `not_modified` 无条件 no-op，且**全文从不读取 `cursor`**。一旦客户端在任何一处丢掉一条记录，回执照常推进到 `o == total`，此后服务器永远回 `not_modified`、客户端永远不重查——空洞被永久焊死。 |
| `/api/auth/me` 401 的影响 | **401 时 WS 根本不建连**（`connect()` 只在 `onAuthenticated()` 里调用）。故**轮询路径必须能独立自愈**——这是硬需求，不是可选项。 |

---

## 1. 部署面排查（任务 1）

e41a6a31 提交时间 `2026-07-14T11:51:32+08:00`。三个投递面逐一核实：

| 投递面 | 实测 | 是否含 e41a6a31 |
|---|---|---|
| daemon 进程 | PID 921577，启动 `Jul 14 12:19:38`，解释器 `/home/cre/.se3-stable/bin/python`；该 venv 内 `se3` 为 **11.22.2**（非 editable 快照，安装时间 12:19，晚于提交 11:51） | ✅ |
| daemon 携带的 app.js | `/home/cre/.se3-stable/.../se3/server/static/app.js` 与 worktree 源码 **byte-identical**，含 `sanitizeDomToken` ×3 | ✅ |
| se3-server 实际下发的 app.js | 服务器在 **另一台主机** `192.168.1.10:4573`（本机为 `192.168.1.15`）。`curl http://192.168.1.10:4573/app.js` → HTTP 200，660800 字节，与 worktree 源码 **byte-identical**，含 `sanitizeDomToken` | ✅ |
| 浏览器缓存 | 静态资源带 `ETag: "0736e3b02782111e20f2baa51d48629b"` 与 `Last-Modified: Tue, 14 Jul 2026 04:19:38 GMT`（= 本地 12:19:38，即 11.22.2 的安装时刻）。带 ETag 即会重校验，且当前 ETag 对应的正是含修复的文件 | ✅ 无陈旧缓存风险 |

> 注意：本机 pixi 环境里另有一个 **11.15.3** 的旧 `se3`（`/home/cre/.pixi/envs/pip/...`），它会遮蔽 worktree src。但现场 daemon **不**走这个环境（走 `.se3-stable`），故与本次故障无关。做 CLI/import 核验时仍需 `PYTHONPATH=src`。

**结论：现场故障不是 e41a6a31 未生效导致。三个面都是新代码。这是一个新缺陷。**

### issue 288 的修复状态证据

- `sanitizeDomToken`（app.js:7654）存在；`tagStepType`（app.js:7672）第一行即 `const key = sanitizeDomToken(stepType);`——消毒路径已就位，含空格的 `step_type` 不再进入 `classList.add`。
- 回归测试：`tests/frontend/step_type_token_safety.test.mjs`。
- 本次额外复验：把现场 flow 的 **5 条真实记录**（真实 daemon 信封 `{step_id, step_type, ordinal, message}`）喂进生产渲染器 `renderConversation`，渲染**完整跑通、无抛错**，并正确产出 `step-type-discovery` class。
- 该报错栈与本次故障**无因果关系**：288 是渲染期抛错，本次是记录根本没送到客户端（见 §2）。

*（issue 288 的状态处置由用户决定，本任务不做任何 issue 状态变更。）*

---

## 2. 投递路径根因诊断（任务 2）

### 2.1 先排除「渲染丢失」

把现场 5 条真实记录喂进 `renderConversation`（node-stub），头部记录 **正常渲染**成用户提示气泡，内容为用户真实输入「这是测试一下worktree模式webui显示问题有没有解决。」（模板前缀已按设计折叠）。

⇒ **渲染器无罪。头部不可见 ⟺ 客户端的记录数组里根本没有 r0。** 这是一次真正的投递丢失，不是显示问题。

### 2.2 服务器侧确有头部

`apply_history_frame`（state.py:1027+）的 append 分支在检测到 cursor gap 时 **records 和 cursor 一起丢弃**（state.py:1155-1165 提前 return，不执行 1169 的 cursor 更新）。因此 `len(bundle.records)` 与 `cursor` 不会脱节。

现场 `cursor = {"01_discovery_9ed2a95c.jsonl": 2}` 且 token `o=2`，而 `o` 的铸造正是 `total = len(records)`（state.py:1663）。

⇒ **服务器 bundle 确实持有 2 条记录，头部在服务器侧存在**，可被 full / backfill 取回。**daemon 侧无需改动**（G1 判定：daemon 组件本轮不动）。

### 2.3 协议的语义错位（核心）

进度回执 `o` 的语义是**「服务器已下发的记录总数」**，它是服务器对自己行为的自签声明，**无法感知客户端是否真的把这些记录收下并留存**。而：

- **客户端从不校验**：`app.js` 全文**从不读取任何响应里的 `cursor`**（grep 确认）。回执 `o` 就是客户端持有量的唯一「证明」，而这个证明不是客户端出具的。
- **回执与持有量双向脱钩**：
  - WS append 路径**只改记录、不改 token**（`applyHistoryData` 在 append 分支从不写 `flowConversationProgress`）；
  - REST 路径**只要收到响应就吞下 token**（app.js:2579），但在它之前/之后有 **两个 early-return**（2588 `render === "noop"`、2610 `sameRenderedConversation`）会在 **不写入 `state.flowConversationRecords`（2612）** 的情况下让 token 落袋。

### 2.4 两个已确认的投递面缺陷（服务器侧）

读 `ws.py` 的 fan-out 判据（ws.py:1417）：

```python
suppress_broadcast = (resolved_pull and mode == protocol.HISTORY_MODE_FULL) or outcome.rejected_full
```

这条规则是**反的**，造成一对镜像缺陷：

- **(a) 被丢弃的 append 照样广播。** `mode == APPEND and existing is None` → 首见 append 被 **DISCARD**、armed `requires_full`、`resolves_pull=False`（state.py:1073-1090）。但 `suppress_broadcast` 对 append 恒为 False ⇒ **服务器 bundle 里没有的记录，却被推给了 UI**。客户端与 bundle 从此可以合法地不一致。
- **(b) 修好 bundle 的那一帧不广播。** 上面的 DISCARD 会触发 `take_recovery_pull` → 服务器向 daemon 要一次 cursorless **full** → daemon 回 `mode=full, records=[r0, r1]` → bundle 被修复（2 条、generation 1、cursor 2）。但这一帧 `resolved_pull=True and mode==FULL` ⇒ **`suppress_broadcast=True`，UI 永远不会被告知头部的存在。**

- **(c) `history_data` 帧不含 cursor。** `_push_history_data`（ws.py:919-940）广播的帧只有 `{type, flow_id, mode, records}`——**没有 cursor、没有 signature**。即使客户端想自查，推送路径上也无从下手。（这条直接证伪了任务书前提里「cursor 已随 WS 帧送达客户端」的假设。）

### 2.5 单一因果链（可解释现场全部证据）

1. **头部离开投递面。** 头部 r0 在某一处从客户端的持有集合中缺席——由 §2.4(a)（服务器丢弃却广播的 append，此时 UI 尚未选中该 flow / 或还卡在登录门无 WS，帧被丢在地上）、或 §2.4(b)（修复帧被抑制）产生。两条都是**已确认的代码事实**，都会留下「服务器有、客户端没有」的空洞。
2. **服务器 bundle 补齐到 2 条**（recovery full），但 UI 未被告知（§2.4(b)）。
3. **客户端的数组只从 WS append 长出尾部**：`reconcileAppendRecords` 把增量并进当前数组，**不做任何锚定校验**——客户端乐呵呵地建起一段无头的对话。
4. **回执推进到 `o == total`**：客户端下一次 REST 读拿到回执并**无条件吞下**（app.js:2579），而 2588/2610 两个 early-return 允许它在**不落记录**的情况下吞 token。
5. **门锁死。** 此后 `token.offset == total` 且 signature 相符 ⇒ `get_history_snapshot` 恒答 `delivery: "not_modified", records: []` ⇒ `mergeHistoryResponse` 恒返回 `render: "noop"` ⇒ 客户端永不重查。`cursor` 每次都送到了，但**没有任何代码读它**。**头部丢失成为永久状态。**

### 2.6 用生产代码实证的吸收态

直接驱动生产 `mergeHistoryResponse`（node，见 §5 复现脚本）：

| 场景 | 输入 | 结果 |
|---|---|---|
| **A** 服务器发 offset=0 的 delta（含头部） | held=`[#1]`，records=`[#0, #1]` | `render=full`，持有 **2** 条，顺序正确 `#0,#1` ✅ **能自愈** |
| **B** 现场形态：回执声称同步 | held=`[#1]`，`delivery=not_modified`，`records=[]`，`cursor={file:2}` | `render=noop`，持有 **仍是 1** 条 `#1`，**token `o=2` 照单全收**，cursor 被忽略 ❌ **永久锁死** |

场景 B 就是现场证据（`records: []` + `not_modified` + `cursor: 2` + `token o=2` + 第一条消息不可见）的**完整复现**。
场景 A 同时证明：**合并机制本身有能力把头部按时间戳插回正确位置**——所以「按编号补取」的修复方向是成立的，缺的只是「发现缺失」这一步。

---

## 3. `/api/auth/me` 401 的影响（任务 3）

- **时机**：`bootstrapAuth()`（app.js:13266）在页面启动时无条件 `fetch("/api/auth/me")`。无会话 cookie 时服务器返回 401（实测 `curl` 亦为 401）——这是**引导期探测**，本身正常。
- **后果（关键）**：401 → `nextAuthState(…, "me_401")` → `AUTH_STATES.LOGIN` → 只 `applyAuthState()` 亮出登录门，**不调用 `connect()`**。而 `connect()`（开 `/ws/ui`）**只在 `onAuthenticated()` 里被调用**（app.js:13288）。
  ⇒ **401 期间 WS 根本不建连**，客户端只剩轮询路径。登录成功后 `onAuthenticated()` 才 `connect()`。
- **它不是本次头部丢失的根因**（登录后 WS 会连上），但它**扩大了丢失窗口**：登录门期间到达的 `history_data` 广播帧无人接收，直接落地——正是 §2.5 步骤 1 的一个现实触发点。

**对 G4 的硬约束：完整性自查入口必须同时覆盖轮询响应与 WS 帧，且在 WS 从未建连的情况下也能仅靠轮询自愈。** 不得把自愈能力挂在 WS 上。

---

## 4. 待修项清单

| # | 缺陷 | 位置 | 归属 |
|---|---|---|---|
| D1 | `not_modified` 无条件 no-op，客户端不校验自身持有量 | `mergeHistoryResponse` app.js:7285 | **G4** |
| D2 | app.js 全文从不读取响应/推送里的 `cursor` | app.js 全局 | **G4** |
| D3 | token 在不落记录的路径上也被吞下（2588 / 2610 两处 early-return 之上的 2579） | `loadFlowConversation` app.js:2579 | **G4** |
| D4 | WS `history_data` 帧不含 `cursor` / `signature`，推送路径无法自查 | `_push_history_data` ws.py:919-940 | **G3** |
| D5 | 修复 bundle 的 full 帧被 `suppress_broadcast` 抑制，UI 永不被告知头部 | ws.py:1417 | **G3** |
| D6 | 被 DISCARD 的 append 仍被广播 → 客户端持有 bundle 里没有的记录 | ws.py:1417 + state.py:1073 | **G3** |
| D7 | `reconcileAppendRecords` 合并 WS 增量时不做锚定校验（无头也照并） | app.js:7051 | **G4**（由 cursor 自查覆盖） |
| — | daemon 侧 | — | **无需改动**（§2.2：bundle 内头部齐全） |

**副带发现（非阻塞）：** `tests/frontend/render_real_records.mjs` 断言「真实 daemon 信封的 `message` 不含 `step_type`」，但现场真实记录的 `message` **确实带 `step_type`**，导致该 harness 对真实数据直接判失败。这是 harness 的 fixture 假设与真实数据脱节，建议由 G5/G6 顺手校正（不影响本次根因）。

---

## 5. 复现与取证脚本

```bash
# 部署面
ps -eo pid,lstart,args | grep se3            # daemon PID 921577, 12:19:38, /home/cre/.se3-stable
diff /home/cre/.se3-stable/lib/python3.14/site-packages/se3/server/static/app.js \
     src/se3/server/static/app.js            # identical
curl -s http://192.168.1.10:4573/app.js | grep -c sanitizeDomToken   # 3
curl -s -o /dev/null -w '%{http_code}' http://192.168.1.10:4573/api/auth/me   # 401

# 吸收态实证（生产 app.js，无浏览器）
node -e '
const app = require("./src/se3/server/static/app.js");
const mk = (o, role, ts, txt) => ({step_id:"01_discovery_9ed2a95c", step_type:"discovery",
  ordinal:o, message:{role, timestamp:ts, content:txt}});
const r0 = mk(0,"user","2026-07-14T12:25:43.005664","HEAD prompt");
const r1 = mk(1,"assistant","2026-07-14T12:25:43.018766","tail progress");
// A: delta from offset 0 heals
console.log(app.mergeHistoryResponse({delivery:"delta",records:[r0,r1],progress:"t",signature:"s"},[r1],[r1]));
// B: not_modified locks the hole in forever
console.log(app.mergeHistoryResponse({delivery:"not_modified",records:[],progress:"t",signature:"s",
  cursor:{"01_discovery_9ed2a95c.jsonl":2}},[r1],[r1]));
'
```

---

## 6. 对修复设计的确认

Decided 方案（客户端按 `recordKey = stepId#ordinal` 用 cursor 自查、缺哪条按编号补取）**正面命中 D1/D2/D3/D7**：它把「客户端持有量」的判定权从服务器的自签回执转移到客户端对 cursor 的逐编号核对，这是唯一能发现 §2.3 那种语义错位的信号。

同时 **D4/D5/D6 必须实修，不能只靠补取掩盖**——否则每一轮轮询都要补一次，把一个持续发生的投递缺陷压成隐性流量与延迟。

场景 A 已证明合并层能正确安放补回的头部；场景 B 已证明缺的正是「发现缺失」这一步。

---

## 7. G3 实修记录（投递路径）

G1 定位的三个服务器侧投递缺陷（D4/D5/D6）已在 G3 实修；daemon 侧经复核**确认无需改动**。

### 7.1 D6 — 被 DISCARD 的 append 仍被广播（**头部丢失的产生点**）

`ws.py` 的 fan-out 判据只审视 `full` 帧，`append` 恒不抑制。于是被 `apply_history_frame`
丢弃的 append（首见 append / `requires_full` 已置位 / 跨 machine delta / cursor gap）**照样推给
UI**：客户端把一段「不是任何 bundle 的后缀」的无锚记录并进空面板 —— 现场那个「持尾缺头」的
数组正是这样长出来的。

修复：`suppress_broadcast` 增加 `or not applied`。**服务器自己都不认的记录，不再发给任何人。**
bundle 由紧随其后的 recovery pull 修复，客户端从那一帧（或 §7.2 的通告）得知真相。
回归：`tests/server/test_history_push_cursor.py::test_first_sighting_append_is_not_broadcast`、
`::test_gapped_append_is_not_broadcast`。

### 7.2 D5 — 修好 bundle 的 full 帧被抑制，UI 无人知晓

`mode: full` 的抑制本身是对的（重播它会清掉 REST 刚下发的 progress token，触发整面重建）。
错的是**连「bundle 变了」这件事也一并瞒下**——而 recovery pull 的修复帧恰恰落在这一支。

修复：抑制**记录**，但补发一帧无记录的通告 `{type: "history_cursor", flow_id, cursor, signature}`
（`_push_history_cursor`）。零重建、零 token 重置，客户端据此自查并按编号补取。
回归：`::test_suppressed_full_pull_reply_still_pushes_a_cursor_advisory`。

### 7.3 D4 — `history_data` 帧不含 cursor

`_push_history_data` 现在附带**帧应用后**的权威 `cursor` 与 `signature`，取自新增的
`ServerState.get_history_bundle_meta`（与 `get_history_snapshot` 同源同锁，故推送面与轮询面
**不可能对「客户端应持有什么」给出不同答案**）。旧前端忽略新字段即可，行为不变。
回归：`::test_full_frame_carries_cursor_and_signature`、`::test_append_frame_carries_cursor_after_the_append`
（断言帧内 cursor/signature 与同一时刻 REST 快照逐字段相等）。

### 7.4 daemon 侧：已核实无需改动

依据（非「补取机制会兜住」）：

- `apply_history_frame` 的 append 分支在 cursor gap 上**记录与 cursor 一并丢弃**（state.py:1155-1165
  提前 return，不执行 1169 的 cursor 写入），故 `len(bundle.records)` 与 `cursor` 不会脱节。
- 现场 `cursor = {"01_discovery_9ed2a95c.jsonl": 2}`，且 token 的 `o` 铸造自 `total = len(records)`
  而现场 `o=2` —— 两个独立信号一致指向：**服务器 bundle 内实有 2 条记录，头部在服务器侧存在**。
- `tests/server/test_history_push_cursor.py::test_head_loss_shape_end_to_end_the_head_is_announced`
  以现场形态（首见 append 携尾 → 丢弃 → recovery full）驱动真实 `_handle_message`，
  paused worktree flow 的 full 回复确实返回 `[HEAD, TAIL]` 完整 bundle，且到达 UI 的唯一一帧
  就是这份含头部的 full。

⇒ 头部从未在 daemon 的读取/组装侧丢失；它丢在**服务器→UI 的投递面**（D6），并被 D5/D4 变成永久态。
