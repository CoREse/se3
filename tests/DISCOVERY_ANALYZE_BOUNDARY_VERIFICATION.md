# Discovery→Analyze WS-Freeze — Root Cause, Fix & Verification (issue #260)

Follow-up to the `20260705-122709_377bfbb7` session. **Status: FIXED** — this
document records the reproduced boundary, the confirmed root cause, the fixes
that landed (daemon + server + frontend), the script-level acceptance results,
and the manual browser check.

## Symptom (as reported)

Frontend v11.11.1+, daemon `~/.se3-stable` v11.12.0: once a running flow crossed
the **discovery→analyze** boundary, an already-open WebUI chat stopped receiving
any new content over the live `/ws/ui` WebSocket. ~5s later the frontend
progression-grace fallback (commit `8a128eb3`) fired a one-shot silent rebuild,
which only surfaced the lone `analyze` step label and (a) scrolled the view up;
thereafter nothing appeared for the rest of analyze until the user exited and
re-entered the chat. The 5s timer elapsing at all proved
`state.flowConversationAppendSeq` did not grow — the WS increment genuinely
stopped at that boundary. `8a128eb3` failed because it assumed the push side was
already fixed and never reproduced end-to-end; this task's mandate was
**reproduce-then-locate first**.

## Harness & five-hop instrumentation

`tests/test_discovery_analyze_ws_delivery.py` drives the REAL daemon
`DaemonHistoryReader` over a REAL on-disk `engine.json`/`jsonl` evolution that
mirrors the boundary timing (discovery with an **empty** steps table → confirm →
steps table **first-write** + **PAUSED→RUNNING** → a freshly-created `02_analyze`
jsonl → analyze continuous appends), feeds every increment through the REAL
server `_handle_message` (cache + `/ws/ui` broadcast), and asserts on what a
subscribed `_UiWS` client receives.

* **Fidelity vs the pre-existing harness:** the older
  `test_server_history_live_append_broadcast.py::_drive_scenario` calls
  `read_active_flows` *unconditionally* every mutation. The new
  `_GatedBoundaryDriver` reproduces the daemon push loop's **signature gate** — it
  reads a delta ONLY when `active_flow_signature` (via `client._history_changed`)
  reports a change. That gate is where a boundary-specific freeze could hide, and
  is the missing piece `8a128eb3` never exercised.
* **Five-hop DEBUG observability** (all at `logging.DEBUG`, tagged `hist-diag`
  for grepping; zero cost at the default level) — retained in the code so a live
  run can be traced end to end:
  1. **daemon change-detection** — `history.active_flow_signature`,
     `history._is_still_active` (`DROP confirmed` / `RESCUED`),
     `disk_json_cache.read_json_cached` (active engine.json REUSE-cached vs
     RE-PARSE), `client._history_changed`.
  2. **daemon incremental read** — `history.read_flow` (per-file
     `cursor_lines`/`prev_consumed`/`rewritten`/`can_incremental` + result
     `mode`/`records`), `history.read_active_flows` (the active set).
  3. **daemon→server send** — `client._push_history` (the `MSG_HISTORY_DATA`
     frame leaving the daemon).
  4. **server→UI fanout** — `ws._handle_message`, `state.append_history`
     (APPLIED vs DISCARD with reason: `first-sighting-append` /
     `requires_full-set` / `machine-change`), recovery-pull dispatch.
  5. **frontend apply** — `app.js applyHistoryData` (opt-in via
     `localStorage.SE3_WS_DEBUG = "1"` / `window.SE3_WS_DEBUG = true`): `mode` /
     `records` / `selectedFlowId` / `flowConversationAppendSeq` and whether the
     append applied or was all-duplicates.

## Final root cause — the freeze hop

The boundary is the flow's **only structural-deformation edge**: discovery runs
with `steps: []` / `current_step: None`, and the steps list is written for the
first time only when discovery ends, inside a dense `engine.json` rewrite window
(discovery-confirm gate → PAUSED→RUNNING flip → steps first-write → analyze
launch). The freeze is a **daemon-side stale read** in that window, with two
reinforcing layers, plus a **server relay stuck-state** that made it persist:

1. **`disk_json_cache` served a STALE parse for the LIVE `engine.json`
   (primary).** `read_engine_header(active=True)` reused the cached parse while a
   `(mtime, size)` stat key *and* a hash of only a bounded **head+tail 64 KiB
   window** both still matched. In the dense boundary window a rewrite that
   changed only the **true middle** of the `state.steps` table — head (`flow_id`/
   `status`) and tail (`current_step_index` + worktree keys) byte-identical — was
   invisible to that window; when two writes shared an `st_mtime_ns` (coarse-mtime
   FS, or two writes in one jiffy) the stat key matched too, so the superseded
   parse came back. The daemon's activity/signature machinery, built on that read,
   could then miss the transition.
2. **`history._is_still_active` had no forced-fresh fallback (reinforcing).** It
   read `read_engine_header(active=True)` and, unlike `active_flow_signature`
   (self-healing via raw `_safe_stat`), had no raw-stat / re-parse fallback — so a
   same-`(mtime, size)` PAUSED→RUNNING (or flow-identity) read on a stale parse
   could transiently move a still-active flow **out** of the active set, dropping
   its whole live stream for the step.
3. **`server.state.append_history` `requires_full` stuck-state (why it
   persisted).** Once a live `append` was discarded (first-sighting after a
   restart / cross-machine desync) the flow was flagged `requires_full` and EVERY
   later append was dropped until a `full` frame replaced the bundle — exactly the
   "must exit and re-enter the chat to recover" persistence the user saw.

## Fixes that landed

**Daemon (G2).**
* `disk_json_cache` now hashes the **whole content** of the active `engine.json`
  (bounded by the `MAX_PARSE_BYTES` / `SIZE_GUARD_BYTES` = 5 MiB size guard),
  re-read every poll, instead of a head+tail window. The cached parse is reused
  only while BOTH the `(mtime, size)` key and that whole-content `blake2b` digest
  are unchanged; a middle-only same-stat rewrite now moves the digest and forces a
  re-parse. Per-poll cost is a bounded read + one C-speed hash — NOT the full
  file read + `json.loads` (that would reintroduce the #209/#243 starvation). A
  new `force_fresh` flag bypasses the cache entirely for a controlled re-parse.
* `history._is_still_active` gained a forced-fresh true-value fallback: on the
  DROP path only, it re-confirms with `read_engine_header(active=True,
  force_fresh=True)` before excluding a flow, so a transient stale/racing read
  cannot freeze a live flow (`RESCUED` log). The healthy keep path pays no extra
  read.

**Server relay (G3).** When a live `mode:append` is discarded (first-sighting /
`requires_full`-set / machine-change), the server now dispatches exactly one
cursorless `MSG_HISTORY_REQUEST` (full pull) back to the owning daemon over the
same socket, resolving the authoritative `project_root` the same way the REST
path does (so worktree flows don't regress). The full reply repopulates the
bundle, clears the flag, and broadcasts — so subsequent appends flow to
already-open `/ws/ui` views **without the user re-entering the chat**.
`take_recovery_pull`/`clear_recovery_pull` fire once per stuck flow (no storm).

**Frontend fallback (G4).** The one-shot 5s silent rebuild became a
self-re-arming **periodic retry** (`armProgressionGrace`): while the WS stays
silent it re-pulls a silent full `/api/history` rebuild on the
`progressionGraceMs` cadence until a genuine WS increment (`flowConversationAppendSeq`
growth past the frozen snapshot) lands or the flow closes. A WS that never
recovers still surfaces mid-step content without exit/re-enter; the healthy path
stays zero-rebuild; flow switch/close cancels.

**Frontend scroll (G5).** Added a persistent `state.flowConversationFollowingBottom`
intent, maintained by the conversation scroll handler / `scrollFlowConversationToBottom`
and reset on `openFlowView`. The silent-rebuild stick decision in
`loadFlowConversation` switched from the unreliable frozen-DOM `isNearBottom` to
`(flowConversationFollowingBottom || isNearBottom)`: a bottom-follower drifted off
the bottom by a stalled boundary increment now sticks to the new bottom (no
up-jump, symptom (a)); a genuinely scrolled-up reader still takes the
`captureScrollAnchor`/`restoreScrollAnchor` branch (no #217 regression).

## Script-level acceptance results

All acceptance-criteria suites pass. Key cases:

* **Boundary e2e** (`tests/test_discovery_analyze_ws_delivery.py`, real daemon
  reader + real server relay + subscribed `/ws/ui` client):
  * `test_boundary_each_disk_append_delivered_via_gated_push` — every disk append
    reaches the live client within its push cycle.
  * `test_boundary_streamed_records_equal_full_snapshot_no_loss_no_dup` — streamed
    records equal the authoritative full snapshot, no loss / no dup, no mid-stream
    `mode: full` reload.
  * `test_normal_step_boundary_not_regressed` — analyze→plan control still clean.
  * `test_active_engine_json_middle_rewrite_returns_fresh_parse` — the stale-parse
    repro (shipped by G1 as a strict `xfail`) now XPASSes / PASSes after the cache
    hardening.
  * `test_worktree_engine_middle_rewrite_fresh_and_tail_keys_preserved` —
    `--worktree` root-swap non-regression.
* **Daemon cache/transition** (`test_disk_json_cache.py`,
  `test_daemon_history_readpath_cache.py`, `test_daemon_history_step_transition.py`):
  same-`(mtime, size)` middle rewrite is re-parsed (`test_active_same_size_same_mtime_swap_detected`,
  `test_active_engine_middle_rewrite_same_stat_returns_fresh_parse`); the active
  set is stable across the PAUSED→RUNNING flip and the transient-drop rescue
  (`test_is_still_active_stable_across_paused_running_flip`,
  `test_is_still_active_rescues_flow_on_transient_cache_drop`,
  `test_is_still_active_genuine_terminal_drop_pays_no_extra_read`); cursor advances
  on steps first-write + new jsonl (`test_read_active_flows_advances_cursor_on_steps_first_write_and_new_jsonl`).
* **Server relay self-heal** (`test_server_history_live_append_broadcast.py`):
  `test_restart_at_discovery_analyze_boundary_self_heals_no_reenter`,
  `test_first_sighting_append_dispatches_one_recovery_pull_then_heals`,
  `test_take_recovery_pull_fires_once_per_stuck_flow`,
  `test_recovery_marker_cleared_when_full_frame_heals_bundle`, and the
  `test_healthy_boundary_never_triggers_recovery_pull` zero-recovery control.
* **Frontend fallback** (`tests/frontend/progression_refresh.test.mjs` + pytest
  bridge): healthy WS increment → zero silent rebuilds; sustained WS silence →
  periodic silent rebuild that stays armed and keeps pulling mid-step content;
  WS recovery stops the loop; flow switch/close cancels a pending fallback.
* **Frontend scroll** (`tests/frontend/test_app_pure.mjs` DOM-stub + pytest
  bridge): bottom-follower sticks to the new bottom after a silent rebuild;
  scrolled-up reader's viewport-anchored content does not move (no #217
  regression).

**Full-suite integration run** (`python -m pytest tests src/se3/engine`, this
branch with G2–G5 merged):

```
7327 passed, 1 skipped, 1 deselected, 9 warnings in 278.59s
```

* Exit code 0, zero failures. The 1 deselected case is the headless-Chromium e2e
  (`test_console_real_daemon_e2e.py::test_render_paradigm_in_headless_browser`,
  deselected in `pyproject.toml` for the missing `libnspr4.so`); the 1 skipped is
  its node-stub sibling's guarded path.
* The 4 environment-conditional `test_steps.py` failures noted historically
  (codex-runner env + discovery token-usage) did **not** manifest in this clean
  run and are not regressions; no boundary/daemon/server/frontend case failed.
* No concurrency-contention `ERROR`s (mass `test_worktree_*` / tmpfile ERRORs)
  appeared; the run was clean on the first pass, so no clean re-run was needed.

## Manual browser verification (not an automated gate)

This host cannot run the Chromium e2e — Playwright/headless Chromium needs
`libnspr4.so`, which is absent here (the browser e2e
`test_console_real_daemon_e2e.py::test_render_paradigm_in_headless_browser` is
deselected in `pyproject.toml`). By decision, real-browser observation is a
**manual** check and NOT an automated acceptance gate; the scripted e2e above is
the automated proof. To confirm once by hand after these fixes:

1. Start a real `se3-server` + `se3 daemon`, open the WebUI, and start a real
   `se3 run --discover "<task>"` so the flow enters **discovery**.
2. Open the flow's chat and answer the discovery clarification/confirm prompts so
   the flow crosses into **analyze**. Optionally set
   `localStorage.SE3_WS_DEBUG = "1"` in the browser console first to see the hop-5
   `hist-diag applyHistoryData …` lines (and grep the daemon/server logs for
   `hist-diag` to watch hops 1–4).
3. Confirm, WITHOUT exiting/re-entering the chat:
   * analyze content appears **incrementally** as it is produced (not just the
     lone `analyze` label), and keeps appearing for the rest of the step;
   * the view does **not** jump/scroll up when content lands or when the
     progression fallback rebuilds — a reader at the bottom keeps following the
     bottom; a reader scrolled up stays anchored on the same content;
   * `flowConversationAppendSeq` keeps growing (hop-5 log) on the healthy path,
     i.e. the fallback rebuild does not fire when the WS is live.
4. Optionally sever the WS mid-analyze (e.g. block `/ws/ui`) and confirm the
   periodic fallback still surfaces new mid-step content without exit/re-enter,
   then stops re-pulling once the WS recovers.
5. Repeat step 1–3 for a normal boundary (analyze→plan) to confirm no regression,
   and for a `se3 run --worktree` flow to confirm the worktree root-swap is
   unaffected.

---

# Worktree Multi-Round Discovery — Live Chat Loss After Round 1 (issue #278)

Follow-up to the #260 boundary fix above. **Status: FIXED** — the
discovery→analyze track loss is closed, but a *distinct* worktree-only blind
spot remained: in a `se3 run --worktree` flow the discovery step's chat records
**after the first round** vanished from the live web console. Only round 1
rendered; everything the agent said in later rounds was reachable ONLY through
the adjudication "显示详情 / show details" affordance, never as live chat.

## Symptom (as reported)

> 在 worktree 的任务里，discovery 步骤第一轮之后的聊天记录消失，只能通过回复机制里的
> 显示详情来查看它给出了什么需要判决的结果，而中间的聊天记录完全无法看到。

## Root cause — an identity collision in the daemon read path

`run_worktree_mode` forks the worktree first and runs discovery ENTIRELY in the
worktree (`run_flow(project_root=<worktree>)`), so a single worktree discovery
`.jsonl` file holds every round (append-only) — the engine WRITE side was
already correct. The loss was in the **daemon read path**, where one logical
step was surfaced from several physical files and the identity the frontend
reconciles by (`stepId#ordinal`) collided:

1. **Folded step ids (primary).** `_merge_flow_jsonl` can surface a step's
   primary file plus its `.from-<branch>` sidecar (and, transiently, a
   cross-root main copy). The old reader folded every physical file under ONE
   logical `step_id` while each file numbered its own lines from 0, so a
   sidecar's ordinal-0 record shared `stepId#ordinal` with the primary's — and
   the frontend's idempotent reconcile dropped (or in-place-overwrote) the 2nd+
   round as a "duplicate".
2. **Copy-selection thrash + cursor desync (reinforcing).** The `largest-copy-wins`
   selection could flip mid-flow from the (initially larger) static main clone
   to the growing worktree copy. The wire cursor is keyed by bare filename but
   the offset table by absolute path, so the flip made the by-name cursor look
   already-consumed and the later rounds were skipped (an empty `append`).
3. **Server cache never self-healed (why it persisted).** The 3 s self-heal only
   returned `not_modified` from the server cache and never re-questioned the
   daemon, so a round the live push dropped/collided stayed missing until
   exit/re-enter.

## Fixes that landed (G1 daemon · G2 server · G3 frontend)

**Daemon read path (G1, `daemon/history.py`).**
* `_display_step_id` gives each physical file its OWN frontend-facing step id
  (keeping the `.from-<branch>` sidecar marker) while step *type* is still parsed
  from the folded logical id — so `stepId#ordinal` is globally unique and stable
  across full/append reads, and `step_type` is never corrupted by the marker.
* `_merge_flow_jsonl` prefers the worktree write-root copy (structurally detected
  by `_is_worktree_copy`) as a stable, size-independent selection, so a live
  worktree flow's growing copy is chosen from the start and never flips.
* `read_flow` detects a physical-copy switch for a bare filename
  (`_cursor_source`) and reads the new copy cleanly from line 0 rather than
  trusting the other copy's by-name cursor — the re-emitted early lines reconcile
  idempotently by `stepId#ordinal`.

**Server relay (G2, `server/state.py`, `server/app.py`).** Confirmed the relay
passes G1's disambiguated `step_id`/`ordinal` through `append_history` /
`get_history` verbatim (records are opaque, no dedup). Added
`ServerState.is_active_worktree_flow` and a running-worktree self-heal reconcile
to `GET /api/history/{flow_id}`: when the cache says `not_modified` but the flow
is a live worktree run and not throttled, it re-pulls the daemon (via the
authoritative worktree root) so a dropped/collided round is filled in; the
identical-full-replace branch keeps the bundle generation so a no-op reconcile
does not churn tokens.

**Frontend reconcile (G3, `server/static/app.js`).** `recordKey` consumes G1's
disambiguated `step_id` verbatim (documented the invariant now spanning
cross-source physical files). `dedupeSnapshotDiscovery` compares CONTENT (the
`legacyKeyFromNorm` signature) before dropping a discovery record — only a
byte-identical clone is de-duped, so a legitimately-different 2nd+ round that
happens to reuse a physical ordinal is never silently deleted.

## End-to-end acceptance (G4)

The seam is verified per-hop AND end-to-end:

* **Daemon read path** (`tests/test_daemon_worktree_discovery_multiround.py`,
  `tests/test_daemon_history_worktree_merge.py`,
  `tests/test_worktree_history_sidecar.py`): a live worktree discovery file's
  rounds all read with unique, stable `(step_id, ordinal)` pairs across
  full+append; the merge selection sticks to the worktree copy across snapshots
  (no thrash, no re-emit churn); a late-appearing worktree copy re-reads cleanly;
  primary + sidecar surface as DISTINCT streams whose ordinals never collide.
* **Server relay** (`tests/test_server_history_authoritative_root.py`,
  `tests/test_interjection_fast_push.py`): the disambiguated identity passes
  through the relay verbatim; the running-worktree self-heal reconciles a
  cache stuck at round 1 by re-questioning the daemon (respecting the throttle;
  non-worktree/completed flows unaffected); an identical full re-pull keeps the
  generation.
* **Frontend reconcile** (`tests/frontend/worktree_discovery_multiround.test.mjs`,
  registered in `tests/frontend/test_app_pure.mjs`): cross-source records that
  share a physical ordinal keep distinct `recordKey`s and both render; a 2nd+
  round appends (is not dropped); `dedupeSnapshotDiscovery` de-dups only a
  byte-identical clone.
* **Full seam end-to-end** (`tests/test_worktree_seam_regression.py`, **new in
  G4**): `test_e2e_worktree_multiround_discovery_all_rounds_reach_frontend`
  chains the REAL `DaemonHistoryReader.read_flow` → REAL
  `ServerState.append_history` relay → a faithful `_FrontendConsole` port of the
  `app.js` reconcile (`recordKey` / `reconcileAppendRecords` /
  `dedupeSnapshotDiscovery`), driving a four-round live worktree discovery whose
  single append-only file grows each round. It asserts every intermediate round's
  chat records reach the frontend (not just round 1), the final adjudication
  verdict is a live chat bubble, no round is dropped/duplicated (all
  `stepId#ordinal` keys unique), and the server's authoritative bundle matches
  the frontend exactly (lossless relay). The append frames are asserted non-empty
  so a copy-selection/cursor regression that emitted an empty round-2 append
  fails here. `test_e2e_worktree_discovery_cross_source_rounds_all_render` runs
  the same seam over the primary + `.from-<branch>` sidecar split-root topology
  and asserts both sources render with distinct step ids.

## Full-suite regression run

`python -m pytest tests src/se3/engine` on this branch (G1–G3 merged + G4 e2e):

```
7923 passed, 1 deselected, 9 warnings in 355.29s (0:05:55)
```

Exit code 0, zero failures, on a single clean pass (no concurrency-contention
ERRORs, so no clean re-run was needed).

* The discovery→analyze boundary chain (issue #260, section above) is
  **not regressed** — its `test_discovery_analyze_ws_delivery.py` boundary e2e,
  the daemon cache/transition suites, and the server relay self-heal suite all
  pass alongside the new #278 worktree coverage.
* The 1 deselected case is the headless-Chromium e2e
  (`test_console_real_daemon_e2e.py::test_render_paradigm_in_headless_browser`,
  deselected in `pyproject.toml` for the missing `libnspr4.so`); its node-stub
  sibling covers the same logic.
* Known environment-conditional failures (the 4 `test_steps.py` codex-runner /
  discovery token-usage cases, and mass `test_worktree_*` git-contention ERRORs
  under concurrent-session load) are NOT #278 regressions — they pre-exist on a
  clean HEAD and clear on a clean re-run.

## Manual browser verification (not an automated gate)

As with #260, real-browser observation is a manual check (this host lacks
`libnspr4.so` for the Chromium e2e). To confirm once by hand:

1. Start a real `se3-server` + `se3 daemon`, open the WebUI, and start a real
   `se3 run --worktree --discover "<task>"` so the flow enters **discovery**
   inside its worktree.
2. Open the flow's chat and answer the discovery clarification prompts across
   **several rounds**.
3. Confirm, WITHOUT using "显示详情 / show details" and without exiting/re-entering
   the chat: every round's intermediate chat (round 2, 3, …), not only round 1,
   appears as live chat; the adjudication verdict shows as a normal chat bubble;
   no round is doubled.
