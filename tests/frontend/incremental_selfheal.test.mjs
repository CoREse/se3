/*
 * Incremental-drop self-heal tests (Group G2 / bundle root cause).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerIncrementalSelfHealTests({app, check, ...})`.
 *
 * The right side keeps the low-latency WS `mode:append` path, but it is no
 * longer the source of correctness: a periodic full history re-pull (the same
 * cadence the left status panel already uses) re-delivers the WHOLE current
 * flow as `mode:full`, and the idempotent reconcile makes that snapshot the
 * authority. So ANY increment that is lost, dropped, or mis-judged along the
 * WS path self-heals at the next full snapshot — correctness stops depending on
 * every increment arriving.
 *
 * These tests simulate a DROPPED append frame (the daemon push-loop starvation
 * that was misdiagnosed as a frontend freeze) and prove the next full snapshot
 * converges the view to the complete, correct history — with a stale in-place
 * marker corrected and a rewritten line healed too.
 */
import assert from "node:assert/strict";

export function registerIncrementalSelfHealTests(ctx) {
  const { app, check, findAll } = ctx;

  const asst = (stepId, stepType, ordinal, content, ts) => ({
    step_id: stepId,
    step_type: stepType,
    ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });
  const ipMarker = (ordinal, path, done, total, ts) => ({
    step_id: "09_commit_c0ffee00",
    step_type: "commit",
    ordinal,
    message: { type: "index_progress", role: "system", step_type: "commit", path, kind: "file", done, total, timestamp: ts },
  });

  function freshFlow(flowId) {
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];
    app.state.flowConversationProgress = null;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    return c;
  }
  const bodies = (recs) => recs.map(app.normalizeRecord).map((n) => n.content);

  check("G2 self-heal: a dropped append frame is recovered by the next full snapshot", () => {
    const flowId = "g2-selfheal-drop";
    freshFlow(flowId);
    // Full open: discovery lines 0-1.
    app.applyHistoryData({ flow_id: flowId, mode: "full", records: [
      asst("01_discovery_a", "discovery", 0, "d0", 1),
      asst("01_discovery_a", "discovery", 1, "d1", 2),
    ]});
    // A WS append frame carrying analyze line 0 is DROPPED (never delivered) —
    // only the later analyze line 1 arrives, leaving a gap.
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: [
      asst("02_analyze_b", "analyze", 1, "a1", 4),
    ]});
    // The gap is visible: a0 is missing on the WS-only path.
    assert.ok(!bodies(app.state.flowConversationRecords).includes("a0"),
      "the dropped frame's record is absent before the heal (the freeze symptom)");

    // The periodic full re-pull delivers the COMPLETE current history.
    app.applyHistoryData({ flow_id: flowId, mode: "full", records: [
      asst("01_discovery_a", "discovery", 0, "d0", 1),
      asst("01_discovery_a", "discovery", 1, "d1", 2),
      asst("02_analyze_b", "analyze", 0, "a0", 3),
      asst("02_analyze_b", "analyze", 1, "a1", 4),
    ]});
    assert.deepEqual(bodies(app.state.flowConversationRecords), ["d0", "d1", "a0", "a1"],
      "the full snapshot self-heals the dropped frame — no view re-entry needed");
    const keys = app.state.flowConversationRecords.map(app.recordKey);
    assert.equal(new Set(keys).size, keys.length, "no duplicates after the heal");
  });

  check("G2 self-heal: a full snapshot deletes a stale bubble no longer in the history", () => {
    const flowId = "g2-selfheal-stale";
    freshFlow(flowId);
    // The WS path left an extra/stale line 2 that the authoritative history does
    // not contain (e.g. a superseded draft the server pruned).
    app.applyHistoryData({ flow_id: flowId, mode: "full", records: [
      asst("01_discovery_a", "discovery", 0, "keep0", 1),
      asst("01_discovery_a", "discovery", 1, "keep1", 2),
      asst("01_discovery_a", "discovery", 2, "STALE", 3),
    ]});
    // The next full snapshot no longer has line 2 → it must be dropped.
    app.applyHistoryData({ flow_id: flowId, mode: "full", records: [
      asst("01_discovery_a", "discovery", 0, "keep0", 1),
      asst("01_discovery_a", "discovery", 1, "keep1", 2),
    ]});
    assert.deepEqual(bodies(app.state.flowConversationRecords), ["keep0", "keep1"],
      "the stale bubble was deleted by the authoritative full snapshot");
  });

  check("G2 self-heal: an index_progress card mis-tracked live converges on the full snapshot", () => {
    const flowId = "g2-selfheal-index";
    freshFlow(flowId);
    // Live path shows the rebuild at (1/5) via its per-line markers.
    app.applyHistoryData({ flow_id: flowId, mode: "full", records: [
      ipMarker(0, "a.py", 1, 5, 1),
    ]});
    // Suppose the intermediate progress frames were dropped; the full re-pull
    // carries every marker line, so the card converges on the completed count.
    app.applyHistoryData({ flow_id: flowId, mode: "full", records: [
      ipMarker(0, "a.py", 1, 5, 1),
      ipMarker(1, "b.py", 2, 5, 2),
      ipMarker(2, "c.py", 3, 5, 3),
      ipMarker(3, "d.py", 4, 5, 4),
      ipMarker(4, "e.py", 5, 5, 5),
    ]});
    const recs = app.state.flowConversationRecords;
    assert.equal(recs.length, 5, "every marker line is present after the heal");
    const last = app.normalizeRecord(recs[recs.length - 1]);
    assert.equal(last.done, 5);
    assert.equal(last.total, 5);
  });

  check("G2 self-heal: re-delivering the SAME full snapshot renders no duplicate bubbles (render idempotent)", () => {
    const flowId = "g2-selfheal-repeat";
    const c = freshFlow(flowId);
    const snap = {
      flow_id: flowId,
      mode: "full",
      records: [
        asst("01_discovery_a", "discovery", 0, "d0", 1),
        asst("01_discovery_a", "discovery", 1, "d1", 2),
        asst("02_analyze_b", "analyze", 0, "a0", 3),
      ],
    };
    app.applyHistoryData(snap);
    const bubblesAfterFirst = findAll(c, "conv-bubble").length;
    const lenAfterFirst = app.state.flowConversationRecords.length;
    assert.equal(bubblesAfterFirst, 3, "three assistant bubbles rendered on the first full snapshot");

    // The periodic 3s backstop re-pulls the identical full snapshot repeatedly.
    // An idempotent reconcile must converge to the SAME view — no stacking.
    app.applyHistoryData({ ...snap, records: snap.records.map((r) => ({ ...r })) });
    app.applyHistoryData({ ...snap, records: snap.records.map((r) => ({ ...r })) });

    assert.equal(app.state.flowConversationRecords.length, lenAfterFirst,
      "record count is unchanged after repeated identical full snapshots");
    assert.equal(findAll(c, "conv-bubble").length, bubblesAfterFirst,
      "no duplicate bubbles accumulate when the same full snapshot re-arrives");
    assert.deepEqual(bodies(app.state.flowConversationRecords), ["d0", "d1", "a0"],
      "the view stays exactly the authoritative sequence");
    const keys = app.state.flowConversationRecords.map(app.recordKey);
    assert.equal(new Set(keys).size, keys.length, "still no duplicate keys after the repeats");
  });
}
