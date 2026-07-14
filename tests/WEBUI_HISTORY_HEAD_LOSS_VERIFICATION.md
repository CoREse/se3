# WebUI 聊天历史「头部记录永久丢失」修复验证报告（G6）

现场：flow `20260714-122542_d4e052c5`，discovery 步，worktree 模式，status=paused。
症状：WebUI 第一条消息完全不可见，且永不自愈。

本文件是**验证报告**：结论、证据索引、测试范围与结果。
逐行取证过程（部署面实测命令、生产代码复现脚本、缺陷清单）保留在同目录的
[`WEBUI_HISTORY_HEAD_LOSS_DIAGNOSIS.md`](./WEBUI_HISTORY_HEAD_LOSS_DIAGNOSIS.md)（G1 产出、G3 追记）。
**两份文件并存、互相引用**——诊断文件是原始证据，本文件是结论与验收。

> **本任务未做任何 issue 状态变更。** issue 288 的处置由用户决定（见 §2）。

---

## 0. 结论摘要

| 问题 | 结论 |
|---|---|
| 是「旧代码未生效」还是「新缺陷」？ | **新缺陷。** 三个投递面（daemon / se3-server / 浏览器实际加载的 app.js）都已含 e41a6a31。 |
| 头部记录在服务器侧存在吗？ | **存在。** 服务器 bundle 实有 2 条记录。**daemon 侧无缺陷、未改动。** |
| 头部为何丢？ | 服务器**丢弃**了一个首见 append，却仍把它**广播**给了 UI（`ws.py` fan-out 判据只审 `full`，`append` 恒不抑制）。UI 由此持有一段「不是任何 bundle 后缀」的无锚尾部。 |
| 头部为何**永不**回来？ | 进度回执 `o` 是服务器「我已下发多少条」的**自签声明**，从不与客户端「实际留存多少条」核对；客户端对 `not_modified` 无条件 no-op，且**全文从不读取 `cursor`**。空洞被永久焊死（吸收态）。 |
| `/api/auth/me` 401 的作用 | **不是根因**，但**扩大了丢失窗口**：401 期间 WS 根本不建连，广播帧无人接收。它把「轮询路径必须能独立自愈」变成硬需求。 |
| issue 288 | **已由 e41a6a31 修复**，与本次故障**无因果关系**。 |
| 修复状态 | 投递面缺陷（3 处）已实修；cursor 完整性自查 + 按编号补取已落地；新增回归测试全绿。 |
| WebUI 第一条消息是否恢复可见 | **待用户现场复测确认**（见 §5）。 |

---

## 1. 根因结论

### 1.1 因果链（可解释现场全部证据）

一条链，四步，每步都是**已核实的代码事实**（非推测）：

1. **头部离开投递面 —— 产生点。**
   `apply_history_frame` 对**首见 append**（`mode == APPEND and existing is None`）的处理是
   **DISCARD**：丢记录、armed `requires_full`、`resolves_pull=False`（state.py:1073-1090）。
   但 `ws.py:1417` 的 fan-out 判据

   ```python
   suppress_broadcast = (resolved_pull and mode == HISTORY_MODE_FULL) or outcome.rejected_full
   ```

   **只审视 `full` 帧，对 `append` 恒为 False** ⇒ **服务器自己都不认的记录，照样推给了 UI。**
   客户端于是持有一段无锚的尾部 `[#1]` —— 它不是任何 bundle 的后缀。**这是头部丢失的产生点。**

2. **服务器补齐了 bundle，却瞒着 UI。**
   上述 DISCARD 触发 `take_recovery_pull` → 服务器向 daemon 要一次 cursorless full → daemon 回
   `mode=full, records=[r0, r1]` → bundle 修复为 2 条、generation 1、cursor 2。
   但这一帧恰好命中 `resolved_pull and mode == FULL` ⇒ **`suppress_broadcast = True`**，
   **修好 bundle 的那一帧永远不会告诉 UI 头部的存在。**

3. **回执与持有量脱钩。**
   客户端下一次 REST 读**无条件吞下** progress token（app.js:2579），而它之前/之后的两个
   early-return（2588 `render === "noop"`、2610 `sameRenderedConversation`）允许它在
   **不写入记录数组**的情况下让 token 落袋。回执就此推进到 `o == total == 2`。

4. **吸收态 —— 门锁死。**
   此后 `token.offset == total` 且 signature 相符 ⇒ `get_history_snapshot` 恒答
   `delivery: "not_modified", records: []` ⇒ `mergeHistoryResponse` 恒返回 `render: "noop"`
   ⇒ 客户端永不重查。`cursor` 每一次都送到了，但**没有任何代码读它**（grep 全文确认）。
   **头部丢失成为永久状态。**

### 1.2 逐条对上现场证据

| 现场证据 | 因果链中的位置 |
|---|---|
| 第一条消息完全不可见 | 步骤 1：客户端的记录数组里根本没有 r0（**不是渲染问题**——见下） |
| `cursor: {"01_discovery_9ed2a95c.jsonl": 2}` | 步骤 2：服务器 bundle 已被 recovery full 修复为 2 条 |
| progress token `{g:1, o:2, v:1}` | 步骤 3：`o` 铸造自 `total = len(records) = 2`（state.py:1663），且已被客户端吞下 |
| `delivery: "not_modified"`、`records: []` | 步骤 4：`token.offset == total` ⇒ 服务器认定「已同步」 |
| **永不自愈** | 步骤 4：客户端从不读 cursor，`not_modified` 无条件 no-op |
| `/api/auth/me` 401 | 放大器：401 → 停在登录门 → **`connect()` 从不被调用** ⇒ WS 未建连 ⇒ 广播帧落地无人接收（步骤 1 的现实触发点之一） |

**渲染器已被排除。** 把现场 flow 的 **5 条真实记录**（真实 daemon 信封）喂进生产
`renderConversation`，头部**正常渲染**成用户提示气泡。头部不可见 ⟺ 客户端**根本没有** r0。
这是一次真正的**投递丢失**，不是显示问题。

**吸收态已用生产代码实证**（直接驱动生产 `mergeHistoryResponse`）：

| 场景 | 输入 | 结果 |
|---|---|---|
| A：服务器发 offset=0 的 delta（含头部） | held=`[#1]`，records=`[#0,#1]` | `render=full`，持有 2 条，顺序正确 ✅ 能自愈 |
| B：**现场形态** | held=`[#1]`，`not_modified`，`records=[]`，`cursor={file:2}` | `render=noop`，**仍持 1 条**，token `o=2` 照单全收，cursor 被忽略 ❌ 永久锁死 |

场景 B 是现场全部证据的完整复现；场景 A 证明合并层**有能力**把头部按时间戳插回正确位置——
缺的只是「发现缺失」这一步。

### 1.3 修复如何切断它

修复同时作用于**产生点**与**永久化机制**——两者都实修，不靠补取机制掩盖：

| 环节 | 修复 | 位置 |
|---|---|---|
| **产生点**（步骤 1） | fan-out 判据加 `or not applied`：**服务器自己都不认的记录，不再发给任何人。** | `ws.py` |
| **瞒报**（步骤 2） | 被抑制的 full 帧改为补发一帧无记录的通告 `{type:"history_cursor", flow_id, cursor, signature}`：抑制**记录**（重播会清掉 REST 刚下发的 token），但不再瞒下「bundle 变了」。 | `ws.py::_push_history_cursor` |
| **推送面无从自查**（步骤 4 的一半） | `history_data` 帧附带**帧应用后**的权威 `cursor` + `signature`，取自新增的 `get_history_bundle_meta`（与 `get_history_snapshot` **同源同锁** ⇒ 推送面与轮询面不可能对「客户端应持有什么」给出不同答案）。 | `ws.py` / `state.py` |
| **吸收态**（步骤 3+4） | 判定权从服务器的自签回执转移到客户端对 cursor 的**逐编号核对**：`not_modified` 的 noop 现在只意味着「无需重绘」，**再也不意味着「已同步」**。 | `app.js` |
| **补取** | 发现缺失 → `GET /api/history/{flow}?after=…&sig=…&missing=stepId:ord` → 服务器在**同一 bundle 内**按编号取记录 → `delivery:"backfill"` → 客户端按 `recordKey` 合并去重、按时间戳稳定排序、全量重建。 | `app.js` / `app.py` / `state.py` |

**progress token 与 `bundle_signature` 的铸造语义逐字节不变**（有断言）——delta / not_modified
的流量优化职责完整保留，健康路径仍是一次比对、零渲染、零额外请求。

**full 只作异常兜底**：持有量 > cursor（surplus）、generation/machine/signature 失配、
编号体系不成立（unkeyable，见下）、或补取预算耗尽（每 `(view, flow, generation)` 上限 2 次补取 +
1 次 full 升级）—— 防止服务器 bundle 真缺该编号时退化成每轮一次的请求风暴。

**编号并非每一个都对应记录；反过来，「索引里找不到」也不等于「bundle 里没有」。** 这条双向约束
决定了三处设计（后两处为自查迭代修正）：

- **cursor 数的是物理行，不是可寻址记录。** daemon 的 `consumed += 1` 发生在空行 / `json.loads`
  失败的 `continue` 之前，且 full 读取从 `cursor_base` 起算 —— 所以 bundle 在自己 cursor 之下
  **合法地**可以没有某个编号的记录。这类编号回落 full 毫无意义（full 返回同一个 bundle、依然没有
  该编号）⇒ 服务器对其**据实申报** `unfillable: {stepId:[ord]}`（`delivery` 仍为 `backfill`，能取到
  的记录照常返回），客户端**退休**该编号 —— 一个合法的永久空洞，全生命周期只花一次往返。
- **但「据实申报」只允许用于 bundle 确实没有的编号。** 若被请求的编号落在一个**同时含无 ordinal
  记录**的步骤里，索引找不到它**并不能**证明 bundle 没有这条记录 —— 它很可能就躺在那儿、只是没编号。
  此时申报 unfillable 会让客户端把一个**服务器实际持有**的记录永久退休、自查从此「干净」，正是本任务
  要消灭的头部丢失。故 `_locate_missing_positions` 在这种**歧义**下返回 `needs_full`，服务器改发
  `delivery:"full"`（整包含无编号记录）—— 混合 legacy bundle 的头部因此必定送达。
- **客户端持有无 ordinal 记录（unkeyable）⇒ 先做一次无 token 的 full，再按世代退休自查。** 编号自查
  对这类 bundle 判不了缺失，但「判不了」不等于「没缺」：直接跳过会把 flow 永久钉在 token-only 路径
  上（每次轮询 `not_modified`，真缺的那条永不可见）。故：**每个世代升级一次** full（它按定义携带
  bundle 全部记录、无论有无编号，一次请求即可自愈）；若 full 之后仍 unkeyable，说明无编号是该 **daemon
  的属性**而非瞬时空洞 ⇒ 在**本世代内**停用该 flow 的编号自查（回落 token-only、零额外请求），世代变更
  时重新武装 —— 绝不会退化成「每条流式追加一次整包下载」。

**每个 bundle 的判定必须随 bundle 作废。** `unfillable` 退休集与补取预算都是**关于某一个 bundle 的
论断**：daemon 重启改写步骤文件后，旧世代里「合法空洞」的那个编号，在新世代里可能是一条**真实可取**
的记录。故二者一律以 `(view, flow, generation)` 为键 —— 世代滚动即自然重置（新 bundle 拿到全新预算与
未退休的自查）。**不得以 signature 为键**：`bundle_signature(generation, total, machine)` 混入 `total`、
**每追加一条记录就变**，会令预算逐条重置 ⇒ 每条新记录 = 2 次补取 + 1 次整包重拉，活跃 flow 上是永久
请求风暴。为此 `get_history_snapshot` 响应与 WS `history_data` / `history_cursor` 帧一律附带
`generation`（bundle 唯一稳定、客户端可见的身份；token 不透明、signature 每次追加即变）。预算另在
**自查通过**（flow 真的健康了）时归还，绝不因「来了一条新记录」而归还。乐观本地 echo 已从 held 集合中
剔除，不会伪造 unkeyable / surplus。

### 1.4 daemon 侧：已核实无需改动

依据是**证据**，不是「补取机制会兜住」：

- `apply_history_frame` 的 append 分支在 cursor gap 上**记录与 cursor 一并丢弃**
  （state.py:1155-1165 提前 return，不执行 1169 的 cursor 写入）⇒ `len(records)` 与 `cursor` 不会脱节。
- 现场 `cursor = 2` 与 token `o = 2`（铸造自 `len(records)`）是**两个独立信号**，一致指向：
  **服务器 bundle 内实有 2 条记录，头部在服务器侧存在。**
- `tests/server/test_history_push_cursor.py::test_head_loss_shape_end_to_end_the_head_is_announced`
  以现场形态驱动真实 `_handle_message`：**paused worktree flow 的 full 回复确实返回
  `[HEAD, TAIL]` 完整 bundle**，且到达 UI 的唯一一帧就是这份含头部的 full（验收标准第 4 条）。

⇒ 头部从未在 daemon 的读取/组装侧丢失；它丢在**服务器 → UI 的投递面**。

---

## 2. issue 288 修复状态结论

**结论：issue 288 报告的报错栈已由 e41a6a31（本分支）修复，且与本次头部丢失故障无因果关系。**

issue 288 的症状是 `tagStepType` 对含空格的 `step_type` 执行 `classList.add`，抛
`InvalidCharacterError` 并中断 `renderConversation`。

证据：

- **消毒路径已就位**：`sanitizeDomToken`（app.js:7654）存在；`tagStepType`（app.js:7672）
  第一行即 `const key = sanitizeDomToken(stepType);` —— 含空格的 `step_type` 不再进入 `classList.add`。
- **回归测试**：`tests/frontend/step_type_token_safety.test.mjs`
  （经 `tests/test_frontend_step_type_token_safety.py` 桥接进 pytest，本轮实测通过）。
- **真实数据复验**：把现场 flow 的 5 条**真实**记录喂进生产 `renderConversation`，
  渲染完整跑通、无抛错，并正确产出 `step-type-discovery` class。
- **与本次故障无因果关系**：288 是**渲染期抛错**（记录到了、渲染炸了）；本次是**记录根本没送到客户端**
  （见 §1.2 的渲染器排除实验）。两者的失败面完全不同。

> **本任务不做任何 issue 状态变更**——是否关闭 issue 288 由用户自行决定。

---

## 3. 部署面排查结果（区分「旧代码未生效」与「新缺陷」）

e41a6a31 提交时间 `2026-07-14T11:51:32+08:00`。三个投递面逐一实测：

| 投递面 | 实测 | 含 e41a6a31 |
|---|---|---|
| daemon 进程 | PID 921577，启动 `Jul 14 12:19:38`，解释器 `/home/cre/.se3-stable/bin/python`；该 venv 内 `se3` 为 **11.22.2**（安装时刻 12:19，**晚于**提交 11:51） | ✅ |
| daemon 携带的 app.js | `~/.se3-stable/.../se3/server/static/app.js` 与 worktree 源码 **byte-identical**，含 `sanitizeDomToken` ×3 | ✅ |
| se3-server 实际下发的 app.js | 服务器在**另一台主机** `192.168.1.10:4573`（本机 `192.168.1.15`）。`curl .../app.js` → 200，660800 字节，与 worktree 源码 **byte-identical** | ✅ |
| 浏览器缓存 | 静态资源带 `ETag: "0736e3b02782111e20f2baa51d48629b"` + `Last-Modified: Tue, 14 Jul 2026 04:19:38 GMT`（= 11.22.2 的安装时刻）。带 ETag 即会重校验，且当前 ETag 对应的正是含修复的文件 | ✅ 无陈旧缓存风险 |

**结论：三个面都是新代码。现场故障不是 e41a6a31 未生效导致——这是一个新缺陷。**

> 旁注：本机 pixi 环境里另有一个 **11.15.3** 的旧 `se3`（`/home/cre/.pixi/envs/pip/...`），
> 它会遮蔽 worktree src。现场 daemon **不**走这个环境（走 `.se3-stable`），与本次故障无关；
> 但做 CLI/import 核验时仍需 `PYTHONPATH=src`。

---

## 4. 测试范围与结果

全部命令与原始结果如下（**非**「通过」二字了事）。

### 4.1 新增回归测试

| 命令 | 结果 |
|---|---|
| `python -m pytest tests/server/test_history_backfill.py tests/server/test_history_push_cursor.py -q` | **27 passed** in 2.80s |
| `node tests/frontend/test_app_pure.mjs`（含新增 `history_cursor_backfill.test.mjs`） | **914 checks passed** |

新增覆盖（对应验收标准第 5 条）：

- **前端**（`tests/frontend/history_cursor_backfill.test.mjs`，注册进 `test_app_pure.mjs`）：
  「持尾缺头 + `not_modified` 回执声称同步 → cursor 自查发现 `missing={step:[0]}` → 按编号补取 →
  backfill 合并后**头部记录是 DOM 里的第一个气泡**」（poll 与 WS 两条路径均断言真实气泡文本与顺序，
  非仅气泡计数）；精确 wire 形态断言
  `/api/history/<flow>?after=tok-gen1&sig=sig-gen1&missing=01_discovery_9ed2a95c%3A0`；
  **世代/machine/signature 失配回落 full**（客户端丢弃陈旧回执、丢弃死世代的记录，不跨代拼接 ordinal）；
  surplus 回落 full；**服务器申报 unfillable 的编号被退休 —— 其后每次追加（新 signature）零请求**；
  **持有无 ordinal 记录 ⇒ 恰好一次 full 升级：能治的一次治好（头部回到 DOM 首个气泡），治不好的（旧
  daemon 全程无编号）在本世代退休自查、连续 5 帧零请求**；**世代滚动使已退休编号重新受检、耗尽的预算
  重新武装**（gen1 判为 unfillable 的编号在 gen2 被再次补取并渲染；gen1 耗尽预算后 gen2 仍能修复）；
  预算不因追加归还、只因自查通过或世代变更归还；健康路径零请求；补取 in-flight 去重与上限（不成风暴）；
  WS 帧 + `history_cursor` 通告路径；history-detail 视图。
- **服务端**（`tests/server/test_history_backfill.py`）：`missing` 参数解析（含非法/超限
  一律降级为「无 missing」而非 500）、`backfill` delivery 按编号精确取记录、尾部并集与去重、
  **bundle 确实没有该编号 ⇒ 据实申报 `unfillable`（不回落 full，因为 full 返回同一个 bundle、
  救不了它）**、部分可定位时「能取的取、取不到的申报」、**步骤内含无 ordinal 记录时「找不到」是歧义
  ⇒ 改发 full（整包含无编号记录），legacy / 混合 bundle 的头部必定送达、绝不被误判退休**、
  陈旧 token 回落 full、**每种 delivery 均携带 `generation`**、
  **token 语义与 `bundle_signature` 铸造不变**。
- **推送面**（`tests/server/test_history_push_cursor.py` 6 例）：full/append 帧携带的 cursor+signature
  与同一时刻 REST 快照逐字段相等；被丢弃的 append 与 gapped append **不广播**；被抑制的 full pull
  改发 cursor 通告；现场头部丢失形态 end-to-end。

### 4.2 回归子集

| 命令 | 结果 |
|---|---|
| `python -m pytest tests/server tests/daemon -q` | **100 passed** in 10.86s |
| `python -m pytest tests/ -k "daemon_history or frontend or server_auth or test_server.py" -q` | **451 passed**, 7315 deselected, 131.43s |
| 历史/WS 投递相关既有模块（16 个，见下）`-q` | **416 passed** in 13.19s |
| `python -m pytest src/se3/engine/test_steps.py -q` | **69 passed** in 1.31s |

16 个历史/WS 模块：`test_server_history.py`、`test_server_history_live_append_broadcast.py`、
`test_server_history_authoritative_root.py`、`test_protocol_history.py`、
`test_protocol_traffic_reduction.py`、`test_chat_history.py`、`test_chat_history_group_status.py`、
`test_history_head_truncation_interlock.py`、`test_issue209_live_append_regression.py`、
`test_issue_209_push_starvation.py`、`test_ws_rejected_full_not_broadcast.py`、
`test_worktree_paused_history_reconcile.py`、`test_worktree_history_sidecar.py`、
`test_discovery_analyze_ws_delivery.py`、`test_running_flow_console_chain.py`、
`test_daemon_traffic_reduction.py`。

⇒ 既有 **#209 / #287 / #288** 防线均未被破坏。

### 4.3 全量套件

```
$ python -m pytest tests/ src/se3/engine/ -q
8169 passed, 1 skipped, 1 deselected, 9 warnings in 371.29s (0:06:11)
```

**零失败、零错误。** 那 1 个 deselected 即 §4.4 的 Chromium e2e；1 个 skipped 为既有跳过项。

### 4.4 已知环境性失败（不计为回归）——本轮实际情况

| 项 | 本轮实测 |
|---|---|
| Chromium e2e（缺 `libnspr4.so`） | 已在 `pyproject.toml:82` 的 `addopts` 中 deselect：`--deselect=tests/test_console_real_daemon_e2e.py::test_render_paradigm_in_headless_browser`。全量运行中体现为 **1 deselected**，从未进入收集。其逻辑由 node-stub 同胞用例覆盖。 |
| `test_steps.py` 中 4 个「干净 HEAD 即存在」的失败（codex runner 环境 + discovery token-usage） | **本轮未复现。** 单独运行 `src/se3/engine/test_steps.py` 为 **69 passed / 0 failed**；全量运行同样 0 失败。这 4 个失败依赖外部环境（codex CLI 可用性），本次环境下不触发。**如实记录——本轮无需动用「已知失败」豁免。** |

⇒ 本轮**没有任何失败需要归入环境性豁免**：全量套件是干净的全绿。

---

## 5. 待用户现场复测确认

代码侧的验收（投递路径根因定位并实修、cursor 自查 + 按编号补取落地、回归测试全绿）已完成。

**唯一未由本任务闭环的验收项：「WebUI 第一条消息恢复可见」需用户现场复测确认。**

复测要点：

1. **部署新代码**——现场 daemon 与 se3-server（`192.168.1.10:4573`）都需重新安装本分支的
   se3，否则跑的仍是 11.22.2（不含本次修复）。浏览器侧因静态资源带 ETag 会自动重校验，
   一般无需手动清缓存。
2. 打开 flow `20260714-122542_d4e052c5` 的对话面板，确认**第一条消息（用户提示气泡）可见且排在首位**。
3. 预期网络行为：首次打开时应看到一次
   `GET /api/history/<flow>?after=…&sig=…&missing=01_discovery_9ed2a95c:0` → `delivery: "backfill"`，
   之后回到零补取的稳态（后续轮询仍是 `not_modified` + 零渲染）。
4. 若仍不可见，请提供该请求的响应体——它会直接区分「服务器 bundle 真缺该编号」与「客户端合并出错」。

---

## 6. 变更清单

| 文件 | 性质 |
|---|---|
| `src/se3/server/ws.py` | 投递面实修：丢弃帧不再广播、被抑制的 full 改发 `history_cursor` 通告、`history_data` 帧携带 cursor+signature |
| `src/se3/server/state.py` | `(step_id, ordinal)` 索引、`get_history_snapshot(missing=…)` → `delivery: "backfill"`、`get_history_bundle_meta()`；**token/signature 铸造不变** |
| `src/se3/server/app.py` | `parse_missing_param`、`missing` 查询参数透传、带 missing 的请求不消耗 full-pull 节流预算 |
| `src/se3/server/static/app.js` | `stepIdFromCursorKey` / `findMissingOrdinals` / `encodeMissingParam` / `reconcileCursorCompleteness`；`mergeHistoryResponse` 每个分支都回传 cursor 并认识 `backfill` |
| `tests/frontend/history_cursor_backfill.test.mjs`（新） | 前端回归 |
| `tests/server/test_history_backfill.py`（新） | 服务端 backfill 回归 |
| `tests/server/test_history_push_cursor.py`（新） | 推送面回归 |
| `tests/frontend/test_app_pure.mjs`、`tests/test_server_history.py` | 注册新模块 / 加宽被新 `missing=` kwarg 打破的三个 test double |
| `tests/WEBUI_HISTORY_HEAD_LOSS_DIAGNOSIS.md`（新） | G1 诊断（原始证据） |
| `tests/WEBUI_HISTORY_HEAD_LOSS_VERIFICATION.md`（新） | 本报告 |

**daemon 侧（`src/se3/daemon/`）未改动**——依据见 §1.4。
**未做任何 issue 状态变更。**
