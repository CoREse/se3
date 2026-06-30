/*
 * History flow_id display tests (Parts 1 & 2).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes the async
 * `registerHistoryFlowIdTests({app, check, checkAsync, findOne, findAll})` so
 * the parent harness drives the same check() reporter and the same `app`
 * module export.
 *
 * Coverage:
 *   (1) renderHistoryList — each .history-item-meta row carries a
 *       .history-item-flow-id span showing the session's flow_id, with the full
 *       value mirrored into the span's title for readability/copy.
 *   (2) openHistorySession — the dedicated #history-detail-flow-id line shows
 *       the COMPLETE flow_id even when a task_description is present (i.e. it is
 *       NOT routed through the title's task_description→flow_id fallback);
 *       closeHistory clears that line.
 */
import assert from "node:assert/strict";

export async function registerHistoryFlowIdTests(ctx) {
  const { app, check, checkAsync, findOne } = ctx;

  // ---- (1) list card meta-row flow_id span --------------------------------
  check("history list meta row shows the session flow_id with a full-value title", () => {
    app.state.historySessions = [
      {
        flow_id: "20260630-120006_4ad8d7e2",
        machine_id: "m1",
        task_description: "Do a thing",
        project_root: "/proj/a",
        updated_at: 200,
      },
    ];
    app.state.historyIndexLoading = false;
    app.state.historyIndexConfirmed = true;
    app.state.machines = [{ machine_id: "m1", online: true }];
    app.state.historySelectedProjectRoot = null;
    app.state.selectedHistoryId = null;

    app.renderHistoryList();
    const list = document.getElementById("history-list");
    const item = findOne(list, "history-item");
    assert.ok(item, "expected a history card");

    const meta = findOne(item, "history-item-meta");
    assert.ok(meta, "expected a .history-item-meta row");
    const flowSpan = findOne(meta, "history-item-flow-id");
    assert.ok(flowSpan, "flow_id span must sit inside .history-item-meta");
    assert.equal(flowSpan.textContent, "20260630-120006_4ad8d7e2",
      "the span must show the session flow_id");
    assert.equal(flowSpan.title, "20260630-120006_4ad8d7e2",
      "the span title must carry the full flow_id for readability/copy");

    // The original machine_id span is untouched.
    assert.ok(item.textContent.includes("m1"),
      "the machine_id span must still render alongside the flow_id");
  });

  // ---- (2) detail header dedicated flow_id line ---------------------------
  await checkAsync("history detail shows the FULL flow_id even when a task_description exists", async () => {
    app.state.historySessions = [
      { flow_id: "FLOW-XYZ-123", machine_id: "m1",
        task_description: "Some descriptive task" },
    ];
    app.state.selectedHistoryId = null;
    app.state.historyRecords = [];
    app.state.historyProgress = null;
    const d = document.getElementById("history-detail");
    d.innerHTML = ""; d.__convState = null;
    globalThis.fetch = () => Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve({ records: [], progress: "p0", delivery: "full" }),
    });

    await app.openHistorySession("FLOW-XYZ-123");
    // The title keeps its existing behaviour (falls back to task_description)…
    assert.equal(
      document.getElementById("history-detail-title").textContent,
      "Some descriptive task",
      "title behaviour is unchanged (task_description still shown)");
    // …but the dedicated flow_id line shows the COMPLETE flow_id regardless of
    // whether a task_description is present.
    assert.equal(
      document.getElementById("history-detail-flow-id").textContent,
      "FLOW-XYZ-123",
      "the flow_id line must show the full flow_id, independent of the title");
  });

  check("closeHistory clears the flow_id line and hides the usage badge", () => {
    document.getElementById("history-detail-flow-id").textContent = "STALE-ID";
    const badge = document.getElementById("history-usage-badge");
    badge.classList.remove("hidden");

    app.closeHistory();

    assert.equal(
      document.getElementById("history-detail-flow-id").textContent, "",
      "the flow_id line must be cleared on close so it can't bleed into the next session");
    assert.ok(badge.classList.contains("hidden"),
      "the usage badge must be hidden on close");

    // closeHistory hides the shared #history-view element; the later harness
    // body tests (run after this registration) assume the history view is open
    // (isHistoryOpen() → true), so restore visibility to avoid cross-test leak.
    document.getElementById("history-view").classList.remove("hidden");
  });
}
