# Discovery→Analyze WS-Freeze — Boundary Diagnosis (issue #260)

Follow-up to the `20260705-122709_377bfbb7` session. Symptom (user-observed,
frontend v11.11.1+, daemon `~/.se3-stable` v11.12.0): once a running flow crosses
the **discovery→analyze** boundary, an already-open WebUI chat stops receiving
any new content over the live `/ws/ui` WebSocket. ~5s later the frontend
progression-grace fallback (commit `8a128eb3`) fires a silent rebuild, which only
surfaces the lone `analyze` step label and (a) scrolls the view up; thereafter
nothing appears for the rest of analyze until the user exits and re-enters the
chat. The 5s timer elapsing at all proves `state.flowConversationAppendSeq` did
not grow — the WS increment genuinely stopped at that boundary.

This document records what the **G1 boundary e2e + five-hop DEBUG instrumentation**
established, and hands G2/G3 concrete fix targets. `8a128eb3` failed because it
assumed the push side was already fixed and never reproduced end-to-end; G1's
mandate was **reproduce-then-locate first**.

## Harness & instrumentation

* `tests/test_discovery_analyze_ws_delivery.py` — drives the REAL daemon
  `DaemonHistoryReader` over a REAL on-disk `engine.json`/`jsonl` evolution that
  mirrors the boundary timing (discovery with an **empty** steps table → confirm
  → steps table **first-write** + **PAUSED→RUNNING** → a freshly-created
  `02_analyze` jsonl → analyze continuous appends), feeds every increment through
  the REAL server `_handle_message` (cache + `/ws/ui` broadcast), and asserts on
  what a subscribed `_UiWS` client receives.
  * **Fidelity vs the pre-existing harness:** the older
    `test_server_history_live_append_broadcast.py::_drive_scenario` calls
    `read_active_flows` *unconditionally* every mutation. The new
    `_GatedBoundaryDriver` reproduces the daemon push loop's **signature gate** —
    it reads a delta ONLY when `active_flow_signature` (via
    `client._history_changed`) reports a change. That gate is where a
    boundary-specific freeze could hide, and is the missing piece `8a128eb3`
    never exercised.
* **Five-hop DEBUG observability** (all at `logging.DEBUG`, tagged `hist-diag`
  for grepping; zero cost at the default level):
  1. **daemon change-detection** — `history.active_flow_signature`,
     `history._is_still_active`, `disk_json_cache.read_json_cached` (active
     engine.json REUSE-cached vs RE-PARSE), `client._history_changed`.
  2. **daemon incremental read** — `history.read_flow` (per-file
     `cursor_lines`/`prev_consumed`/`rewritten`/`can_incremental` + the result
     `mode`/`records`), `history.read_active_flows` (the active set).
  3. **daemon→server send** — `client._push_history` (the `MSG_HISTORY_DATA`
     frame leaving the daemon).
  4. **server→UI fanout** — `ws._handle_message` (`applied`/`resolved_pull`/
     `suppress_broadcast`), `state.append_history` (APPLIED vs DISCARD with
     reason: first-sighting-append / requires_full-set / machine-change).
  5. **frontend apply** — `app.js applyHistoryData` (opt-in via
     `localStorage.SE3_WS_DEBUG = "1"` / `window.SE3_WS_DEBUG = true`): logs
     `mode`/`records`/`selectedFlowId`/`flowConversationAppendSeq` and whether an
     append applied or was all-duplicates.

## What the boundary hop-trace shows (in-process, signature-gated)

Running the boundary scenario with DEBUG on, the increment survives every hop —
the analyze records are read, sent, applied, and broadcast:

```
history  read_flow file=02_analyze_cd34.jsonl cursor_lines=0 prev_consumed=None cur_size=193 rewritten=False can_incremental=False
history  read_flow RESULT flow=live mode=append records=3 cursor={'01_discovery_ab12.jsonl': 8, '02_analyze_cd34.jsonl': 2}
state    append_history APPLIED-append flow=live records=3 total=10
ws       ws HISTORY_DATA flow=live mode=append records=3 applied=True resolved_pull=False suppress_broadcast=False
...
history  read_flow file=02_analyze_cd34.jsonl cursor_lines=2 prev_consumed=2 cur_size=271 rewritten=False can_incremental=True
state    append_history APPLIED-append flow=live records=1 total=11
```

**Finding 1 — the signature-gated in-process path is CLEAN across the boundary.**
`test_boundary_each_disk_append_delivered_via_gated_push` and
`test_boundary_streamed_records_equal_full_snapshot_no_loss_no_dup` PASS: every
disk append reaches the live `/ws/ui` client within its push cycle, the streamed
records equal the authoritative full snapshot (no loss / no dup), and there is no
mid-stream `mode: full` reload. `test_normal_step_boundary_not_regressed`
(analyze→plan control) PASSES too.

The root reason: `active_flow_signature` keys the engine.json part on a **raw
`_safe_stat`** and each per-step jsonl on a **raw `_safe_stat`** — so a new
`02_analyze` jsonl and every subsequent append shift the signature, the gate
fires, and `read_flow` full-reads the new file (`prev_consumed=None`) as a live
`append`. **This exonerates, for the in-process path, exactly the layers a naive
fix would target:** `read_flow`/`read_active_flows` incremental reads, the
per-step cursor advance, `append_history`, the `requires_full` discard rule, and
the `/ws/ui` fanout. None of them drops the boundary increment when the daemon
actually reads it.

## The confirmed daemon-side latent hazard

**Finding 2 — `disk_json_cache` serves a STALE parse for the LIVE engine.json
under a same-`(mtime, size)` MIDDLE rewrite.** Deterministically reproduced by
`test_active_engine_json_middle_rewrite_returns_stale_parse` (xfail):

* `read_engine_header(path, active=True)` → `read_json_cached(verify_content=True)`
  reuses the cached parse while the `(mtime, size)` stat key AND a hash of a
  bounded **head+tail 64 KiB window** both still match.
* On a >128 KiB engine.json, a rewrite that changes **only the true middle** of
  the `state.steps` table (e.g. a deep-in-the-table step-status flip) — leaving
  the head (`flow_id`/`status`) and tail (`current_step_index` + worktree keys)
  byte-identical — is invisible to that window. When the two writes also share an
  `st_mtime_ns` (coarse-mtime FS, or two fast writes in one jiffy) the stat key
  matches too, so the just-superseded parse is returned. Confirmed:
  `read2` returns `MID0000` after `MID0001` was written.

**Finding 3 — why Finding 2 is masked in the common case (and where it is not).**
`test_active_flow_signature_masks_engine_middle_rewrite` shows the same
middle-only rewrite leaves the **raw-stat** signature unchanged, so
`client._history_changed` *debounces* that tick. In the healthy boundary the very
next jsonl append re-fires the gate, so a single stale tick is harmless. The
danger is a `_is_still_active` / `active_flow_signature` decision *taken on the
stale parse itself*: `_is_still_active` reads `read_engine_header(active=True)`
and has **no raw-stat / forced-reparse fallback** (unlike `active_flow_signature`,
whose engine part is self-healing via raw `_safe_stat`). A stale `flow_id`/status
read there can transiently exclude a still-active flow from the active set — the
blind spot the design's decision 3 names.

## Where the persistent freeze must originate (narrowed suspects for G2)

The in-process gated path being clean means the persistent, whole-analyze freeze
the user sees needs a condition the in-process harness cannot naturally hit.
Ranked for G2:

1. **`disk_json_cache` active-engine.json staleness (Finding 2) — primary.**
   Harden the `verify_content=True` path for the live engine.json: within the
   vulnerable band (`(mtime, size)` hit on a >128 KiB active file) do a bounded
   **middle** re-hash of the hot `state`/`steps` region, or drop the pure
   head+tail-window reuse for the active file and do a controlled re-parse. Keep
   it bounded (do NOT hash the whole consumed prefix — that reintroduces the #209
   starvation). `test_active_engine_json_middle_rewrite_returns_stale_parse`
   flips to XPASS when fixed.
2. **`history._is_still_active` blind spot — high.** Add a raw-`_safe_stat`
   fallback / forced true-value read so a same-`(mtime, size)` PAUSED→RUNNING (or
   flow-identity) rewrite cannot transiently move an active flow out of the active
   set. `active_flow_signature` already self-heals via raw stat; mirror that here.
3. **`server.state.append_history` `requires_full` stuck-state — medium (guard).**
   The DISCARD paths (`first-sighting-append`, `requires_full-set`,
   `machine-change`) now log their reason. If any boundary sequence ever lands an
   `append` while the server bundle is absent/flagged, EVERY later append is
   dropped until a full frame (exit/re-enter) — matching the "must re-enter"
   persistence exactly. Confirm the boundary can never desync the daemon cursor
   ahead of the server bundle, or add a bounded auto-`requires_full` recovery
   (request a full re-pull) rather than waiting on the user.

G3 owns the frontend halves (periodic-retry fallback replacing the one-shot
silent rebuild; stick/anchor scroll fix) — the app.js hop-5 DEBUG counter
(`flowConversationAppendSeq`) is the signal both rely on.

## Manual browser verification (placeholder — cannot run headless here)

This host cannot run the Chromium e2e (missing `libnspr4.so`), so browser
observation is manual and NOT an automated gate. After G2/G3 land, verify once:

1. Start a real `se3-server` + `se3 daemon`, open the WebUI, and start a real
   `se3 run --discover "<task>"` so the flow enters **discovery**.
2. Open the flow's chat and answer the discovery clarification/confirm prompts so
   the flow crosses into **analyze**. Optionally set
   `localStorage.SE3_WS_DEBUG = "1"` in the browser console first to see the
   hop-5 `hist-diag applyHistoryData …` lines.
3. Confirm, WITHOUT exiting/re-entering the chat:
   * analyze content appears **incrementally** as it is produced (not just the
     lone `analyze` label);
   * the view does **not** jump/scroll up when content lands (or when the
     progression fallback rebuilds);
   * `flowConversationAppendSeq` keeps growing (hop-5 log), i.e. the fallback
     rebuild does not fire on the healthy path.
4. Repeat for a normal boundary (analyze→plan) to confirm no regression.
