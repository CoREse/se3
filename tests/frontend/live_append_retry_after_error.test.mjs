/*
 * Live-append retry-after-error tests (Group G1, second freeze scenario).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes
 * `registerLiveAppendRetryAfterErrorTests({app, check, findOne, findAll})`.
 *
 * Regression context — the second face of the running-flow freeze: a later step
 * (e.g. `update_spec`) FAILS, the operator chooses to retry, and the engine
 * re-runs that SAME step REUSING its step_id and per-step jsonl file. The daemon
 * then pushes, as `mode: append` increments:
 *
 *   step_failed (terminal)   →   step_status=retrying   →
 *   step_started=running     →   fresh assistant turns (often with content
 *                                 SIMILAR to the failed attempt's output)
 *
 * The live view must keep streaming this through the lifecycle-anchor supersede
 * machinery without the resumed records being dropped:
 *
 *   * `recordKey` must distinguish the `retrying` step_status from the resumed
 *     `running` step_started of the SAME step at the SAME wall-clock second
 *     (status is part of the key) — otherwise the resumed running anchor is
 *     deduped away and the region freezes on 重试中.
 *   * `removeSupersededStatusRows` must segment by the terminal report: the
 *     post-failure retry execution's anchors are preserved (a fresh execution),
 *     and the region settles on the retry's CURRENT anchor.
 *   * the retry's freshly-streamed assistant output (similar but timestamped
 *     later than the failed attempt) must NOT be mistaken for a duplicate.
 *
 * The contract: the incremental (append) path and a one-shot `mode: full`
 * reload converge on the SAME conversation — no loss, no dup, no freeze.
 */
import assert from "node:assert/strict";

export function registerLiveAppendRetryAfterErrorTests(ctx) {
  const { app, check } = ctx;

  const asst = (content, ts, stepId, stepType) => ({
    step_id: stepId,
    step_type: stepType,
    message: { role: "assistant", content, timestamp: ts },
  });
  const startedRow = (stepId, stepType, ts) => ({
    type: "step_started", step_id: stepId, step_type: stepType,
    status: "running", timestamp: ts,
  });
  const retryingRow = (stepId, stepType, ts) => ({
    type: "step_status", step_id: stepId, step_type: stepType,
    status: "retrying", timestamp: ts,
  });
  const failedRow = (stepId, stepType, ts, err = "spec gate failed") => ({
    type: "step_failed", step_id: stepId, step_type: stepType,
    data: {
      step: {
        step_id: stepId, step_type: stepType, status: "failed", error_message: err,
      },
    },
    timestamp: ts,
  });

  const keys = (recs) => recs.map(app.recordKey);
  const allUnique = (recs) => new Set(keys(recs)).size === recs.length;
  const bodies = (recs) => recs.map(app.normalizeRecord).map((n) => n.content);
  const asstBodies = (recs) =>
    recs.map(app.normalizeRecord).filter((n) => n.role === "assistant").map((n) => n.content);

  function freshFlow(flowId, initial = []) {
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = initial.slice();
    app.state.flowConversationProgress = null;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    if (initial.length) app.renderConversation(c, app.state.flowConversationRecords, false);
    return c;
  }
  const bubbleNodes = (c) => c.children.filter((x) => x.__convIdx !== undefined);
  const statusRows = (c) => c.children.filter((x) => x.__convStatusRow);
  const terminalRows = (c) => c.children.filter((x) => x.__convTerminalRow);

  const STEP = "06_update_spec_9f3a";
  // The canonical retry-after-error stream: update_spec runs, produces a first
  // (doomed) attempt, FAILS, then on retry RE-RUNS the same step (reused step_id)
  // and produces a similar-but-fresh attempt. The retry's running anchor lands at
  // the SAME wall-clock second as the retrying status (the daemon-resume window).
  function retrySequence() {
    return [
      startedRow(STEP, "update_spec", 1),                       // 0 RUNNING
      asst("Drafting the spec update…", 2, STEP, "update_spec"), // 1 (doomed attempt)
      failedRow(STEP, "update_spec", 3),                        // 2 FAILED (terminal)
      retryingRow(STEP, "update_spec", 4),                      // 3 operator retries
      startedRow(STEP, "update_spec", 4),                       // 4 RE-RUN RUNNING (same second!)
      asst("Drafting the spec update…", 5, STEP, "update_spec"), // 5 similar content, later ts
      asst("Spec update applied.", 6, STEP, "update_spec"),     // 6 fresh success turn
    ];
  }
  function retryBatches(seq) {
    return [
      seq.slice(0, 2),   // running + first attempt
      seq.slice(2, 3),   // FAILED
      seq.slice(3, 5),   // retrying + re-run running (same-second collision pair)
      seq.slice(5, 6),   // similar-content retry turn
      seq.slice(6),      // success turn
    ];
  }

  // ----------------------------------------------------------------------- //
  // 1. recordKey distinguishes retrying vs the resumed running anchor.       //
  // ----------------------------------------------------------------------- //

  check("G1 retry: recordKey distinguishes retrying vs running anchors at the same second", () => {
    const retrying = retryingRow(STEP, "update_spec", 4);
    const running = startedRow(STEP, "update_spec", 4);
    assert.notEqual(app.recordKey(retrying), app.recordKey(running),
      "retrying step_status and the resumed running step_started must not collide");
    // True duplicates still collapse, so genuine dedup is preserved.
    assert.equal(app.recordKey(retrying), app.recordKey(retryingRow(STEP, "update_spec", 4)),
      "two identical retrying anchors still share one key");
  });

  check("G1 retry: dedupeAppendRecords keeps the resumed running anchor next to the retrying row", () => {
    const retrying = retryingRow(STEP, "update_spec", 4);
    const running = startedRow(STEP, "update_spec", 4);
    const fresh = app.dedupeAppendRecords([retrying], [running]);
    assert.equal(fresh.length, 1, "the resumed running anchor survives the dedup");
    assert.equal(app.normalizeRecord(fresh[0]).status, "running");
  });

  // ----------------------------------------------------------------------- //
  // 2. The live retry keeps streaming and settles correctly.                 //
  // ----------------------------------------------------------------------- //

  check("G1 retry: live append keeps streaming the retry; region settles on the re-run running anchor", () => {
    const flowId = "flow-retry-1";
    const c = freshFlow(flowId, []);
    const seq = retrySequence();
    for (const batch of retryBatches(seq)) {
      app.applyHistoryData({ flow_id: flowId, mode: "append", records: batch });
    }
    const recs = app.state.flowConversationRecords;
    // Both the doomed and the retry assistant turns are present (the similar
    // content of the retry turn was NOT mistaken for a duplicate of the first).
    assert.deepEqual(asstBodies(recs), [
      "Drafting the spec update…",   // doomed attempt
      "Drafting the spec update…",   // retry attempt (same text, later ts → distinct)
      "Spec update applied.",        // retry success
    ], "doomed + retry assistant turns all streamed, similar retry text not deduped");
    assert.ok(allUnique(recs), "no duplicate recordKey across the retry");

    // The terminal step_failed report card stays (the failed execution's record),
    // and exactly one live status anchor remains — the retry's 进行中 anchor.
    assert.equal(terminalRows(c).length, 1, "the step_failed report card is retained");
    const rows = statusRows(c);
    assert.equal(rows.length, 1, "exactly one live status anchor after the retry");
    assert.ok(rows[0].classList.contains("step-status-running"),
      "the surviving anchor reads 进行中 (the re-run), not frozen on 重试中");
  });

  check("G1 retry: incremental append converges on the same result as a full reload", () => {
    const seq = retrySequence();

    const full = freshFlow("flow-retry-full", []);
    app.applyHistoryData({ flow_id: "flow-retry-full", mode: "full", records: seq.slice() });
    const fullBodies = bodies(app.state.flowConversationRecords);
    const fullStatus = statusRows(full).length;
    const fullTerminal = terminalRows(full).length;
    const fullBubbles = bubbleNodes(full).length;

    const live = freshFlow("flow-retry-live", []);
    app.state.selectedFlowId = "flow-retry-live";
    for (const batch of retryBatches(seq)) {
      app.applyHistoryData({ flow_id: "flow-retry-live", mode: "append", records: batch });
    }
    const liveBodies = bodies(app.state.flowConversationRecords);

    assert.deepEqual(liveBodies, fullBodies,
      "live-append content equals full-reload content (no loss, no dup)");
    assert.equal(statusRows(live).length, fullStatus,
      "live-append settles on the same status-anchor count as a full reload");
    assert.equal(terminalRows(live).length, fullTerminal,
      "live-append settles on the same terminal-report count as a full reload");
    assert.equal(bubbleNodes(live).length, fullBubbles,
      "live-append DOM bubble count equals the full-reload bubble count");
  });

  // ----------------------------------------------------------------------- //
  // 3. A retrying-only batch (the daemon's first post-retry tick may carry    //
  //    only the retrying anchor or only the running anchor) must apply.       //
  // ----------------------------------------------------------------------- //

  check("G1 retry: a running-anchor-only post-retry batch un-freezes the retrying row", () => {
    const flowId = "flow-retry-2";
    const c = freshFlow(flowId, [
      asst("Drafting the spec update…", 2, STEP, "update_spec"),
      failedRow(STEP, "update_spec", 3),
      retryingRow(STEP, "update_spec", 4),
    ]);
    // After the failure the region shows the failed report + the 重试中 anchor.
    assert.equal(terminalRows(c).length, 1);
    let rows = statusRows(c);
    assert.equal(rows.length, 1);
    assert.ok(rows[0].classList.contains("step-status-retrying"),
      "starts on the 重试中 anchor");

    // The first post-retry tick carries ONLY the resumed running anchor (the
    // re-run's output is still streaming). Pre-fix (status-blind key) this would
    // be a recordKey duplicate of the retrying row → applyHistoryData
    // short-circuits → never un-freezes. It must apply and flip 重试中 → 进行中.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [startedRow(STEP, "update_spec", 4)],
    });
    rows = statusRows(c);
    assert.equal(rows.length, 1, "still one live status anchor");
    assert.ok(rows[0].classList.contains("step-status-running"),
      "the running anchor alone un-freezes the 重试中 row on this very tick");

    // And the retry output keeps streaming afterward.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [asst("Spec update applied.", 6, STEP, "update_spec")],
    });
    assert.ok(asstBodies(app.state.flowConversationRecords).includes("Spec update applied."),
      "the retry's output keeps streaming after un-freezing");
  });

  // ----------------------------------------------------------------------- //
  // 4. The post-retry batch re-delivered (REST snapshot ∩ WS broadcast) is    //
  //    rendered once, and streaming continues after the overlap.             //
  // ----------------------------------------------------------------------- //

  check("G1 retry: a re-delivered retry batch is deduped to a single render and keeps streaming", () => {
    const flowId = "flow-retry-3";
    const seq = retrySequence();
    const c = freshFlow(flowId, []);
    const batches = retryBatches(seq);
    // Stream up to the retry (retrying + running) batch.
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[0] });
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[1] });
    const before = app.state.flowConversationRecords.length;

    // retrying + running arrives twice (REST pull, then WS rebroadcast).
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[2] });
    const afterFirst = app.state.flowConversationRecords.length;
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[2] });
    assert.equal(app.state.flowConversationRecords.length, afterFirst,
      "the re-delivered retry batch must not append duplicates");
    assert.ok(afterFirst > before, "the retry batch genuinely appended once");
    assert.ok(allUnique(app.state.flowConversationRecords));

    // Streaming continues after the overlap dedup.
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[3] });
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[4] });
    assert.ok(asstBodies(app.state.flowConversationRecords).includes("Spec update applied."),
      "post-retry output keeps streaming after the overlap dedup");
    assert.ok(bubbleNodes(c).length > 0);
  });
}
