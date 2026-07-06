/*
 * Periodic progression-fallback RETRY tests (issue #260).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerProgressionFallbackRetryTests({app, check, checkAsync, findOne, findAll})`
 * so the parent harness drives the same check() reporter and the same `app`
 * module export (mirrors progression_refresh, live_append_*, …).
 *
 * Context: the discovery→analyze boundary can leave the WS push path silent for
 * a whole step (#260). The prior fallback (8a128eb3) was a ONE-SHOT silent
 * rebuild: it painted only the disk state at the moment it fired (the lone
 * analyze step label), and any mid-step content the still-broken WS never pushed
 * stayed invisible until the reader exited and re-entered the session. This
 * suite pins the fix — the grace timer now RE-ARMS itself after each silent
 * rebuild and keeps pulling on the same cadence (state.progressionGraceMs) until
 * a genuine WS increment lands (state.flowConversationAppendSeq moves past the
 * value frozen when the loop was first armed), so a WS that never recovers still
 * surfaces freshly-written mid-step content without an exit/re-enter.
 *
 * A silent full rebuild deliberately does NOT bump flowConversationAppendSeq
 * (only applyHistoryData's running-flow branch — a real /ws/ui landing — does),
 * so the frozen snapshot is the sole "the WS itself recovered" gate. That is
 * what makes the loop both persist under total silence AND terminate the instant
 * the healthy push path returns.
 *
 * Tests (REAL setTimeout, no fake clock — each shrinks progressionGraceMs to a
 * few ms and awaits a handful of windows):
 *   (A) continuous WS silence → multiple periodic FULL pulls, the view
 *       accumulating progressively more mid-step content across windows;
 *   (B) a genuine WS increment (appendSeq bumps) STOPS the loop — no further
 *       pulls, timer not re-armed;
 *   (C) HEALTHY: a WS increment within the first window → zero fallback pulls;
 *   (D) a flow switch / close (cancelProgressionGrace) cancels the loop — no
 *       pull ever fires against a flow that is no longer open.
 */
import assert from "node:assert/strict";

export async function registerProgressionFallbackRetryTests(ctx) {
  const { app, checkAsync } = ctx;

  const asstRecord = (content, ts, stepId, stepType) => ({
    step_id: stepId,
    step_type: stepType,
    message: { role: "assistant", content, timestamp: ts },
  });

  const snap = (flowId, step, index, status) => ({
    flow_id: flowId, current_step: step, current_step_index: index, status,
  });

  // Counting fetch that always answers with the same full snapshot.
  function installCountingFetch(payload) {
    const calls = [];
    globalThis.fetch = (url) => {
      calls.push(String(url));
      return Promise.resolve({
        ok: true, status: 200, json: () => Promise.resolve(payload),
      });
    };
    return calls;
  }

  const TEST_GRACE_MS = 5;
  // Await k grace windows plus a small slack so the periodic loop has had time to
  // fire ~k times (real timers, no fake clock).
  const waitWindows = (k) =>
    new Promise((r) => setTimeout(r, app.state.progressionGraceMs * k + 12));

  function resetProgressionState(flowId) {
    // Cancel any loop a prior test left armed BEFORE swapping the flow, so it can
    // never fire against this one.
    app.cancelProgressionGrace();
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];
    app.state.flowConversationProgress = null;
    app.state.flowProgressionMarker = null;
    app.state.flowConversationAppendSeq = 0;
    app.state.progressionGraceMs = TEST_GRACE_MS;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    c.scrollTop = 0;
    c.scrollHeight = 0;
    c.clientHeight = 0;
    return c;
  }

  // -- (A) continuous silence → periodic full pulls of growing mid-step content -
  await checkAsync("periodic fallback: continuous WS silence keeps pulling growing mid-step content", async () => {
    const c = resetProgressionState("F1");
    const saved = globalThis.fetch;
    // Model the daemon still writing analyze records to disk while the WS stays
    // dead: the Nth full pull returns N records (delivery:"full" replaces the
    // view's records each time), so a working periodic loop shows the view grow.
    let disk = 1;
    const calls = [];
    globalThis.fetch = (url) => {
      calls.push(String(url));
      const records = Array.from({ length: disk }, (_, i) =>
        asstRecord("analyze-" + i, i + 1, "s" + i, "analyze"));
      disk += 1;
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ records, progress: "p" + disk, delivery: "full" }),
      });
    };
    try {
      app.maybeRefreshConversationOnProgression(snap("F1", "discovery", 0, "running"));
      app.maybeRefreshConversationOnProgression(snap("F1", "analyze", 1, "running"));
      assert.equal(calls.length, 0, "no pull before the first grace window elapses");
      await waitWindows(5);
      assert.ok(calls.length >= 2,
        "continuous WS silence must drive multiple periodic pulls, got " + calls.length);
      assert.ok(calls.every((u) => u.includes("/api/history/") && !u.includes("after=")),
        "every periodic pull is a full (no-after) history pull");
      // The mid-step content accumulated across windows with NO WS increment at all.
      assert.equal(app.state.flowConversationAppendSeq, 0,
        "no WS increment landed — the growth came purely from the periodic fallback");
      assert.ok(app.state.flowConversationRecords.length >= 2,
        "the open view accumulated mid-step content across windows without exit/re-enter");
      assert.ok(ctx.findAll(c, "conv-bubble").length >= 2,
        "the accumulated mid-step content is rendered into the open conversation");
      // The loop is still armed — a persistently dead WS keeps it going.
      assert.notEqual(app.state.progressionGraceTimer, null,
        "the periodic loop stays armed while the WS remains silent");
    } finally {
      app.cancelProgressionGrace();
      globalThis.fetch = saved;
    }
  });

  // -- (B) a genuine WS increment stops the loop -----------------------------
  await checkAsync("periodic fallback: a real WS increment stops the retry loop", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({
      records: [asstRecord("A", 1, "s2", "analyze")], progress: "p", delivery: "full",
    });
    try {
      app.maybeRefreshConversationOnProgression(snap("F1", "discovery", 0, "running"));
      app.maybeRefreshConversationOnProgression(snap("F1", "analyze", 1, "running"));
      await waitWindows(3);
      assert.ok(calls.length >= 1, "the silent WS drives at least one fallback pull");
      // The WS push path recovers: a genuine append lands and bumps the counter
      // past the frozen (0) snapshot the loop gates on.
      app.applyHistoryData({
        flow_id: "F1", mode: "append", records: [asstRecord("live", 99, "s9", "analyze")],
      });
      assert.ok(app.state.flowConversationAppendSeq >= 1, "the WS append bumped the counter");
      const pullsAtRecovery = calls.length;
      // After recovery the loop must terminate: no further pulls, no re-arm.
      await waitWindows(4);
      assert.equal(calls.length, pullsAtRecovery,
        "a recovered WS suppresses all further fallback pulls");
      assert.equal(app.state.progressionGraceTimer, null,
        "the loop is not re-armed once the WS recovers");
    } finally {
      app.cancelProgressionGrace();
      globalThis.fetch = saved;
    }
  });

  // -- (C) HEALTHY path: WS delivers within the first window → zero pulls ------
  await checkAsync("periodic fallback: a healthy WS within the first window → zero pulls", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      app.maybeRefreshConversationOnProgression(snap("F1", "discovery", 0, "running"));
      app.maybeRefreshConversationOnProgression(snap("F1", "analyze", 1, "running"));
      // The WS delivers the analyze increment before the first window elapses.
      app.applyHistoryData({
        flow_id: "F1", mode: "append", records: [asstRecord("live", 1, "s2", "analyze")],
      });
      await waitWindows(4);
      assert.equal(calls.length, 0, "a healthy WS suppresses every fallback pull — zero rebuilds");
      assert.equal(app.state.progressionGraceTimer, null,
        "the loop terminated on the first window without a single rebuild");
    } finally {
      app.cancelProgressionGrace();
      globalThis.fetch = saved;
    }
  });

  // -- (D) a flow switch / close cancels the loop ----------------------------
  await checkAsync("periodic fallback: switching flows cancels the retry loop", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({
      records: [asstRecord("A", 1, "s2", "analyze")], progress: "p", delivery: "full",
    });
    try {
      app.maybeRefreshConversationOnProgression(snap("F1", "discovery", 0, "running"));
      app.maybeRefreshConversationOnProgression(snap("F1", "analyze", 1, "running"));
      await waitWindows(3);
      assert.ok(calls.length >= 1, "the loop pulled while F1 was open and the WS silent");
      const pullsBeforeSwitch = calls.length;
      // The operator opens another flow: openFlowView / doCloseFlowView cancel the
      // grace, then the selected flow changes.
      app.cancelProgressionGrace();
      app.state.selectedFlowId = "F2";
      await waitWindows(4);
      assert.equal(calls.length, pullsBeforeSwitch,
        "no pull fires after the flow switch cancelled the loop");
      assert.equal(app.state.progressionGraceTimer, null, "the loop stays cancelled");
    } finally {
      app.cancelProgressionGrace();
      globalThis.fetch = saved;
    }
  });
}
