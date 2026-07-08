/*
 * Ordinal-identity reconcile tests (Group G2).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerMarkerDedupOrdinalTests({app, check, findOne, findAll})`.
 *
 * The right-side console's correctness no longer rides on a content+timestamp
 * dedup key. Each record now carries a stable `stepId#ordinal` identity (the
 * daemon injects `ordinal`, the record's 0-based line position in its step
 * .jsonl — see daemon/history.py). These tests pin the two properties that make
 * "the chat stops advancing" impossible:
 *
 *   (a) marker records (discovery / commit index_progress step markers) carry
 *       EMPTY content and no status, so under the old content key several of
 *       them collided and a whole batch was dropped. Keyed by ordinal they stay
 *       distinct and every marker survives.
 *   (b) the SAME line re-delivered (REST∩WS overlap) converges to one record;
 *       the SAME line REWRITTEN by a retry (same ordinal, new content) updates
 *       IN PLACE to the newest content instead of being dropped or duplicated.
 */
import assert from "node:assert/strict";

export function registerMarkerDedupOrdinalTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  // A commit-step index_progress marker (empty content, no status) at a given
  // physical line `ordinal`.
  const ipMarker = (ordinal, path, done, total, ts) => ({
    step_id: "09_commit_abcd1234",
    step_type: "commit",
    ordinal,
    message: {
      type: "index_progress",
      role: "system",
      step_type: "commit",
      path,
      kind: "file",
      done,
      total,
      timestamp: ts,
    },
  });

  // A discovery step_output round marker (empty content, role step-event).
  const discOut = (ordinal, ts) => ({
    step_id: "01_discovery_ab12",
    step_type: "discovery",
    ordinal,
    message: { type: "step_output", step_id: "01_discovery_ab12", data: {}, timestamp: ts },
  });

  // A generic discovery assistant turn — the shape a retry rewrites in place.
  const asst = (ordinal, content, ts) => ({
    step_id: "01_discovery_ab12",
    step_type: "discovery",
    ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });

  // ---- (a) recordKey is the ordinal identity, content-independent ---------- //

  check("G2 recordKey uses stepId#ordinal when the envelope carries an ordinal", () => {
    assert.equal(app.recordKey(ipMarker(0, "a.py", 1, 3, 5)), "09_commit_abcd1234#0");
    assert.equal(app.recordKey(ipMarker(7, "z.py", 3, 3, 9)), "09_commit_abcd1234#7");
  });

  check("G2 recordKey is independent of content and timestamp for an ordinal record", () => {
    // Same line (ordinal 2), different content + timestamp → SAME key.
    assert.equal(
      app.recordKey(asst(2, "draft A", 100)),
      app.recordKey(asst(2, "a completely different, much longer draft B", 999)),
      "a retry rewrite of line N keeps the same stepId#ordinal identity");
  });

  check("G2 empty-content marker records stay distinct by ordinal (no false collision)", () => {
    // Three commit markers + two discovery step_output markers, ALL empty
    // content / no status — the exact family the old content key collapsed.
    const keys = [
      ipMarker(0, "a.py", 1, 3, 1),
      ipMarker(1, "b.py", 2, 3, 2),
      ipMarker(2, "c.py", 3, 3, 3),
      discOut(0, 4),
      discOut(1, 5),
    ].map(app.recordKey);
    assert.equal(new Set(keys).size, keys.length,
      "every empty-content marker has a distinct ordinal key");
  });

  check("G2 a record without an ordinal falls back to the legacy content key", () => {
    const legacy = { step_id: "01_discovery_ab12", message: { role: "user", content: "yes", timestamp: 1 } };
    const key = app.recordKey(legacy);
    assert.ok(!key.includes("#"), "legacy key is the coarse content key, not stepId#ordinal");
    assert.equal(app.recordOrdinal(legacy), null, "no ordinal on a local-echo-shaped record");
  });

  // ---- (b) reconcileAppendRecords: idempotent in-place merge --------------- //

  check("G2 reconcile: distinct-ordinal markers all append, none deduped away", () => {
    const rec = app.reconcileAppendRecords([], [
      ipMarker(0, "a.py", 1, 3, 1),
      ipMarker(1, "b.py", 2, 3, 2),
      ipMarker(2, "c.py", 3, 3, 3),
    ]);
    assert.equal(rec.records.length, 3, "all three markers survive as distinct records");
    assert.equal(rec.fresh.length, 3);
    assert.equal(rec.updatedInPlace, false);
    assert.ok(rec.changed);
  });

  check("G2 reconcile: a byte-identical re-delivery of a line is a no-op (changed=false)", () => {
    const base = [ipMarker(0, "a.py", 1, 3, 1), ipMarker(1, "b.py", 2, 3, 2)];
    const rec = app.reconcileAppendRecords(base, [
      ipMarker(0, "a.py", 1, 3, 1),
      ipMarker(1, "b.py", 2, 3, 2),
    ]);
    assert.equal(rec.changed, false, "re-delivering the same lines changes nothing");
    assert.equal(rec.records, base, "same array reference returned (cheap render skip)");
  });

  check("G2 reconcile: a retry rewrite of the same ordinal updates in place (not dropped/duped)", () => {
    const base = [asst(0, "draft A", 1), asst(1, "tail", 2)];
    // Line 0 is rewritten by the retry with new content, same ordinal.
    const rec = app.reconcileAppendRecords(base, [asst(0, "draft B (rewritten)", 3)]);
    assert.equal(rec.records.length, 2, "no new bubble — the rewrite replaced line 0 in place");
    assert.equal(rec.updatedInPlace, true, "an in-place update forces the caller to full-rebuild");
    assert.equal(app.normalizeRecord(rec.records[0]).content, "draft B (rewritten)",
      "line 0 converged to the newest content");
    assert.equal(app.normalizeRecord(rec.records[1]).content, "tail",
      "the untouched tail line is preserved");
  });

  check("G2 reconcile: same ordinal arriving many times converges to one record", () => {
    let recs = [];
    for (const [txt, ts] of [["v1", 1], ["v2", 2], ["v3", 3], ["v3", 3]]) {
      recs = app.reconcileAppendRecords(recs, [asst(4, txt, ts)]).records;
    }
    assert.equal(recs.length, 1, "one line, updated in place, no matter how many arrivals");
    assert.equal(app.normalizeRecord(recs[0]).content, "v3");
  });

  check("G2 reconcile: a new ordinal appends while an existing one updates in place", () => {
    const base = [asst(0, "A", 1)];
    const rec = app.reconcileAppendRecords(base, [asst(0, "A2", 2), asst(1, "B", 3)]);
    assert.equal(rec.records.length, 2);
    assert.equal(rec.updatedInPlace, true);
    assert.deepEqual(rec.records.map((r) => app.normalizeRecord(r).content), ["A2", "B"]);
  });

  // ---- (c) applyHistoryData: the discovery-freeze scenario end to end ------ //

  function freshFlow(flowId) {
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];
    app.state.flowConversationProgress = null;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    return c;
  }

  check("G2 applyHistoryData: a PAUSE→resume rewrite of a discovery line advances the view live", () => {
    const flowId = "g2-disc-rewrite";
    freshFlow(flowId);
    // Discovery round 1: a step_output marker + the round's assistant turn.
    app.applyHistoryData({ flow_id: flowId, mode: "full", records: [discOut(0, 1), asst(1, "round 1 draft", 2)] });
    assert.equal(app.state.flowConversationRecords.length, 2);
    // The operator answers; the step PAUSE→resumes and rewrites line 1 with the
    // round-2 draft (same ordinal), then appends the round-2 marker at line 2.
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: [asst(1, "round 2 draft", 3), discOut(2, 4)] });
    const recs = app.state.flowConversationRecords;
    // Line 1 converged to the new content (no freeze on the round-1 draft), the
    // round-2 marker appended, and no duplicate line-1 bubble was created.
    assert.equal(app.normalizeRecord(recs[1]).content, "round 2 draft",
      "the right side followed the rewrite — no left-pointer-advances-but-right-frozen");
    assert.equal(recs.filter((r) => app.recordKey(r) === "01_discovery_ab12#1").length, 1,
      "the rewritten line is present exactly once");
    const keys = recs.map(app.recordKey);
    assert.equal(new Set(keys).size, keys.length, "no duplicate recordKey after the rewrite append");
  });

  check("G2 applyHistoryData: re-delivered identical append leaves the render cursor untouched", () => {
    const flowId = "g2-disc-overlap";
    const c = freshFlow(flowId);
    app.applyHistoryData({ flow_id: flowId, mode: "full", records: [discOut(0, 1), asst(1, "draft", 2)] });
    const cursorBefore = c.__convState && c.__convState.count;
    const lenBefore = app.state.flowConversationRecords.length;
    // The SAME batch is re-broadcast (REST∩WS overlap).
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: [discOut(0, 1), asst(1, "draft", 2)] });
    assert.equal(app.state.flowConversationRecords.length, lenBefore, "no duplicate appended");
    assert.equal(c.__convState.count, cursorBefore, "the no-op short-circuit left the render cursor in lock-step");
  });

  // A commit-step assistant bubble — the "commit result" content the operator
  // must see once the code-index rebuild finishes.
  const commitResult = (ordinal, content, ts) => ({
    step_id: "09_commit_abcd1234",
    step_type: "commit",
    ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });

  check("G2 commit: the index_progress card updates in place across the whole rebuild, then the commit result content shows", () => {
    const flowId = "g2-commit-index";
    const c = freshFlow(flowId);

    // The rebuild opens: line 0 marker (1/5). Exactly one live progress card.
    app.applyHistoryData({ flow_id: flowId, mode: "full", records: [ipMarker(0, "a.py", 1, 5, 1)] });
    assert.equal(findAll(c, "index-progress-marker").length, 1, "one progress card at the start of the refresh");
    assert.equal(findOne(c, "index-progress-text").textContent, "更新 code-index：a.py (1/5)");

    // Each subsequent marker line arrives via the low-latency WS append path
    // (distinct ordinal → not deduped away), and the SINGLE card updates in
    // place to the latest count for the whole duration of the refresh.
    for (const [ord, path, done, ts] of [[1, "b.py", 2, 2], [2, "c.py", 3, 3], [3, "d.py", 4, 4]]) {
      app.applyHistoryData({ flow_id: flowId, mode: "append", records: [ipMarker(ord, path, done, 5, ts)] });
      assert.equal(findAll(c, "index-progress-marker").length, 1,
        `still one card mid-refresh at ${done}/5 (in-place update, not a new bubble)`);
      assert.equal(findOne(c, "index-progress-text").textContent, `更新 code-index：${path} (${done}/5)`,
        "the card tracks the latest count in place");
      assert.ok(findOne(c, "index-progress-marker").classList.contains("status-running"),
        "still running while done<total");
    }

    // The final marker (5/5) completes the rebuild.
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: [ipMarker(4, "e.py", 5, 5, 5)] });
    assert.equal(findAll(c, "index-progress-marker").length, 1, "one card at completion — never stacked");
    assert.equal(findOne(c, "index-progress-text").textContent, "更新 code-index：e.py (5/5)");
    assert.ok(findOne(c, "index-progress-marker").classList.contains("status-completed"),
      "the card flips to completed at 5/5");
    // No content bubbles were manufactured by the empty-content markers.
    assert.equal(findAll(c, "conv-bubble").length, 0, "empty-content markers created no chat bubbles");

    // Commit finishes and writes its result content — it MUST render after the
    // progress card (the "提交后无内容" symptom is that this bubble never showed).
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: [
      commitResult(5, "已提交：3 个文件，版本 12.0.1", 6),
    ]});
    const bubble = findOne(c, "conv-bubble");
    assert.ok(bubble && bubble.textContent.includes("已提交：3 个文件，版本 12.0.1"),
      "the commit result content is shown once the rebuild's markers are done");
    assert.equal(findAll(c, "index-progress-marker").length, 1,
      "the progress card survives alongside the commit result, still exactly one");
  });
}
