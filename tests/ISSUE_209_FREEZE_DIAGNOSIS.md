# issue #209 — WebUI console freeze on discovery→analyze / retry — G1 diagnosis

**Status:** root cause坐实ed (grounded in real reproduction, not static analysis).
**Layer:** the **daemon push side** (`src/se3/daemon/client.py::_push_loop` /
`_push_history`, and the synchronous `src/se3/daemon/history.py::active_flow_signature`
it calls each tick) **under realistic project load** — NOT the frontend, NOT the
server cache, NOT `read_flow` correctness, NOT `dedupeAppendRecords`.

This is why the regression survived ~10 previous fixes: every prior attempt
targeted the frontend (`recordKey` / `dedupeAppendRecords` / render) or the
server cache, but those layers are **already correct** — the live `history_data`
append simply **never leaves the daemon** when the daemon is busy.

## Symptom (recap)

After the user confirms the discovery plan and the flow steps into `analyze`
(or after a step errors and is manually retried), the WebUI conversation stops
appending. The **left status bar keeps advancing** (it is driven by the 3 s
`refreshFlowDetail()` poll — a request/response path) but the conversation
(driven by live WS `history_data` appends — a push path) freezes until you exit
and re-enter the session (which issues a fresh REST `GET /api/history/{flow}` —
again a request/response path).

> The split is the tell: **request-driven paths work, the push-driven path is
> dead.** The user's own observation — "左边的状态明明已经转到了 analyze" — is
> exactly this.

## How it was坐实ed (reproductions, in order)

All artifacts are real; nothing below is inferred from reading code alone.

1. **Real flow captured.** A real `se3 run --discover` (deterministic fake
   agent) ran `discovery → analyze → plan` (plan failed → `retry_decision` →
   paused), exercising both #209 triggers in one flow. The real on-disk records
   are in `tests/frontend/fixtures/issue_209/*.jsonl`.

2. **Daemon read path is correct.** Replaying those real records through the
   real `DaemonHistoryReader.read_active_flows` produces the real frame sequence
   (`tests/frontend/fixtures/issue_209/daemon_frames.json`): the discovery→analyze
   resume burst is delivered as one `mode:append` frame of **14 records**
   carrying discovery's resume `step_started`/`step_completed` **and all of
   analyze and plan**. The daemon does NOT drop the transition batch.

3. **Frontend frame handling is correct.** Feeding those real frames through the
   production `app.js`:
   * `dedupeAppendRecords` keeps **all 14** transition records (no recordKey
     collision — `status`/`kind` already discriminate the step-event anchors);
   * `applyHistoryData` + `renderConversation` render the incremental-append
     path **byte-identical** to the full-rebuild (exit/re-enter) path — analyze
     and plan both appear.
   So neither the dedupe short-circuit nor an incremental-vs-full render
   divergence explains the freeze.

4. **Server accepts + always broadcasts the append.** `append_history` extends
   the existing bundle and `ws.py` only suppresses a `mode:full` reply that
   resolved a REST pull — a `mode:append` increment is **always** broadcast.

5. **Full e2e — the freeze is load-driven.** A real `se3-server` + real
   `se3 daemon` + real `se3 run` + a real `/ws/ui` WebSocket client (browser
   stand-in), driving the discovery→analyze transition
   (`tests/repro_issue_209_freeze.py`):
   * **`--clean`** (daemon tracks only the tiny temp project): the analyze
     `mode:append` frame **is delivered over `/ws/ui`** — no freeze.
   * **`--loaded`** (daemon also tracks a heavy root — a ~1 MB `engine.json`,
     a busy multi-MB active flow, ~300 history dirs): the analyze frame is
     **never delivered over `/ws/ui`** within 25–30 s, while a REST
     `GET /api/history/{flow}` (exit/re-enter) returns it. **Freeze reproduced.**

6. **Instrumentation pinned the mechanism** (gated on `SE3_HISTORY_DIAG`).
   In the loaded run the daemon's `_push_loop` logged only **2 ticks in ~30 s**
   (vs ~75 expected at the 0.4 s cadence), every tick spent **21–40 ms
   synchronously on the event loop** inside `_history_changed()` →
   `active_flow_signature()` parsing the heavy root's ~1 MB `engine.json`, and
   **no** `read_active_flows` / `SEND history_data` / `_push_history returned`
   line ever fired for the temp flow's analyze. The server-side instrumentation
   confirmed it **never received** an analyze `history_data` frame. The push
   simply never happened.

## Root cause

The daemon's `_push_loop` is **starved** when a tracked project root is large
and busy:

* **Per-tick synchronous cost on the event loop.** `_history_changed()` calls
  `active_flow_signature()`, which does a full `_read_json()` (read + `json.loads`)
  of **every active root's `engine.json`** on the event loop every tick. The
  spec assumes this is "cheap to call per push", but a real long-running flow's
  `engine.json` grows to ~1 MB, so each tick burns tens of ms on the loop.
* **Per-iteration awaited heavy work.** When a push does fire, `_push_history`
  awaits `build_index` (walks the whole `se3/history` tree) and
  `read_active_flows` (reads **every** active flow's jsonl — multi-MB for a busy
  flow, and a full read after each reconnect). A continuously-appending active
  flow also invalidates the `BUILD_INDEX_TTL` cache every tick, defeating it.
* **Result.** Under load the loop completes only a couple of iterations in tens
  of seconds, so the incremental `history_data` push carrying the
  discovery→analyze (or retry) transition append is never produced/sent. The
  request-driven REST pull (`MSG_HISTORY_REQUEST`, served on its own offloaded
  path) keeps working — hence "exit/re-enter fixes it".

### Why specifically discovery→analyze and retry

Both are **pause→resume boundaries** the user actively watches. During the pause
(plan confirmation / retry decision) the loaded daemon has fallen behind; the one
incremental push that must carry the post-resume burst never lands. Earlier
discovery streaming may have arrived before the daemon got loaded (or via the
open's REST pull), and later steps run in quick succession — but the watched
transition stalls. The trigger is **load**, the manifestation is the transition.

## Trigger boundaries (the two the task asks to distinguish)

* **discovery→analyze:** a **new** `02_analyze_*.jsonl` file (read as a `full`
  read of that file inside an overall `mode:append` FlowRead). `read_flow`
  handles this correctly (verified); the frame just never gets pushed.
* **retry-after-error:** the step re-runs under the **same** `step_id` and
  **appends** to the same jsonl (engine writes are append-only — verified, no
  truncate/rewrite happens). Also delivered correctly by `read_flow`; again the
  push is what is starved.
  * (Aside: `read_flow`'s full-read branch *does* have a latent
    truncate/rewrite bug — if a jsonl ever shrinks, `start = cursor_lines` skips
    all rewritten lines and emits zero records. It is **not** triggered by #209
    because the engine never truncates a per-step jsonl, but G2 may wish to
    harden it defensively.)

## Implications for G2 (fix) — recorded, not implemented here

The fix must make the **incremental push reliable under load** (or fall back to
the user-sanctioned workaround). Candidate directions for G2:

1. **Take the per-tick change detection off the event loop / make it cheap.**
   `active_flow_signature` should not `json.loads` a ~1 MB `engine.json` on the
   loop each tick — read only the needed `flow_id`/`status` cheaply (or offload),
   so `_history_changed` stays sub-millisecond.
2. **Decouple per-flow pushes.** A slow/large active flow's read should not block
   another flow's delta (today `read_active_flows` reads all flows in one awaited
   call).
3. **Workaround (user-sanctioned secondary path, see the issue).** Since the
   reliable `refreshFlowDetail()` poll already detects the step advance
   (`current_step` / `current_step_index` / `step_history` status change), have
   the frontend trigger a `loadFlowConversation(flowId, {incremental:true})`
   on a detected advance, falling back to a token-cleared full reload — this is
   request-driven and immune to the push starvation.

The G2 group must pick the path and state which was used and why.

## Reproduction / regression hooks for G3

* `tests/repro_issue_209_freeze.py --loaded` — fails (freeze) before the fix,
  passes (no freeze) after. `--clean` is the always-passing control.
* `tests/frontend/fixtures/issue_209/` — real records + real daemon frames for
  daemon-`read_flow` and frontend-`applyHistoryData` replay guards.

## Temporary instrumentation added in G1 (remove / demote in G4)

All gated and inert unless enabled, so normal operation is unaffected:

* Python (`SE3_HISTORY_DIAG=1`): `daemon/history.py`, `daemon/client.py`,
  `server/state.py`, `server/ws.py` — `[G1-DIAG …]` WARNING lines.
* Frontend (`window.__SE3_HISTORY_DIAG = true` in devtools):
  `server/static/app.js` — `[G1-DIAG …]` console lines.
