/*
 * Progression-refresh FAILURE-SAFETY-NET tests (issue #209 wind-down).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerProgressionRefreshTests({app, check, checkAsync, findOne, findAll})`
 * so the parent harness drives the same check() reporter and the same `app`
 * module export (mirrors live_append_*, reply_send_error_handling, …).
 *
 * Context: the "flow advances (step switch / in-step retry) but the main
 * conversation freezes until you exit and re-enter the session" bug (#209). Its
 * root cause — the daemon _push_loop starved under a heavy root so incremental
 * history_data never went out — is FIXED by #243/#244 (the push side now reads
 * engine headers off the event loop, so the WS delta arrives on its own within
 * ~2s). The former "rebuild on every advance" workaround is therefore DEMOTED to
 * a failure safety net: on a detected advance of the open flow we start a grace
 * window and fire the silent /api/history full rebuild ONLY IF the WS push path
 * failed to deliver an increment for that flow before the window elapsed. On the
 * healthy path (WS delivers) the fallback never fires — zero silent rebuilds.
 *
 * The "WS delivered an increment" signal is `state.flowConversationAppendSeq`, a
 * monotonic counter bumped ONLY by applyHistoryData's running-flow branch when it
 * actually lands new records. The grace timer snapshots it at schedule time and,
 * when it fires, rebuilds only if the counter has not moved past the snapshot.
 *
 * IMPORTANT: the daemon's FlowSnapshot.to_dict() NEVER emits a `step_history`
 * field (the server back-fills it to an empty list), so these tests deliberately
 * use the real /api/flows shape (no step_history) and exercise the in-step-retry
 * path through the `status` signal — not a synthetic growing step_history that
 * never occurs in production.
 *
 * The tests use REAL setTimeout (the harness installs no fake timer), so each
 * shrinks `app.state.progressionGraceMs` to a few ms and awaits just past it.
 *
 * These tests pin:
 *   (1)  first snapshot only establishes a baseline — never arms a timer;
 *   (2)  HEALTHY: advance + a WS increment within the grace window → zero
 *        silent rebuilds (the live WS append is the only update);
 *   (3)  FALLBACK: advance + NO WS increment within the window → exactly one
 *        silent full (no-after) rebuild once the window elapses;
 *   (4)  the same advance re-delivered (marker already updated ⇒ not advanced)
 *        neither re-arms nor re-fires — at most one rebuild per advance;
 *   (5)  a RUNNING→FAILED / RUNNING→PAUSED halt arms nothing and never rebuilds,
 *        while the forward-motion FAILED/PAUSED→RUNNING retry/resume does arm;
 *   (6)  a progression on a flow that is NOT the open one arms nothing;
 *   (7)  cancelling the grace (flow switch/close) drops a pending fallback;
 *   (8)  the silent refresh never pre-clears the container nor shows a Loading
 *        placeholder — the DOM is rebuilt only once the new data arrives;
 *   (9)  the silent refresh preserves the reader's scroll position unless they
 *        were already near the bottom;
 *   (10) the silent refresh never touches the reply-region state;
 *   (11) refreshFlowDetail drops a stale out-of-order detail response;
 *   (12) a prior-lifecycle detail response is dropped after close/reopen;
 *   (13) a failing silent refresh lets an in-flight first-open complete.
 */
import assert from "node:assert/strict";

export async function registerProgressionRefreshTests(ctx) {
  const { app, check, checkAsync } = ctx;

  const asstRecord = (content, ts, stepId, stepType) => ({
    step_id: stepId,
    step_type: stepType,
    message: { role: "assistant", content, timestamp: ts },
  });

  // A counting fetch spy that always answers with the same full snapshot. The
  // silent loadFlowConversation issues its GET synchronously (no `await` before
  // the fetch call), so `calls` is populated by the time the synchronous part
  // of the fire-and-forget refresh returns.
  function installCountingFetch(payload) {
    const calls = [];
    globalThis.fetch = (url) => {
      calls.push(String(url));
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(payload),
      });
    };
    return calls;
  }

  // A deferred fetch that suspends loadFlowConversation at its `await` so the
  // mid-flight container state can be inspected before the data lands.
  function installDeferredFetch() {
    const calls = [];
    let resolveFn = null;
    globalThis.fetch = (url) => {
      calls.push(String(url));
      return new Promise((resolve) => {
        resolveFn = (payload, ok = true, status = 200) =>
          resolve({ ok, status, json: () => Promise.resolve(payload) });
      });
    };
    return { calls, resolve: (...a) => resolveFn(...a) };
  }

  const flush = () => new Promise((r) => setTimeout(r, 0));
  // Await just past the (shrunk) grace window so a pending fallback fires.
  const waitGrace = () =>
    new Promise((r) => setTimeout(r, app.state.progressionGraceMs + 15));

  // Small grace window (real timers, no fake clock) so the two paths resolve in
  // ~ms rather than the production 5s.
  const TEST_GRACE_MS = 5;

  function resetProgressionState(flowId) {
    // Cancel any timer a prior test left pending BEFORE swapping the flow, so it
    // can never fire against this one.
    app.cancelProgressionGrace();
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];
    app.state.flowConversationProgress = null;
    app.state.flowProgressionMarker = null;
    app.state.flowConversationAppendSeq = 0;
    app.state.progressionGraceMs = TEST_GRACE_MS;
    // A fresh open is a bottom-follower (openFlowView forces a scroll to bottom);
    // individual cases that simulate the reader scrolling up flip this to false,
    // mirroring the scroll handler that maintains it in production (#260).
    app.state.flowConversationFollowingBottom = true;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    c.scrollTop = 0;
    c.scrollHeight = 0;
    c.clientHeight = 0;
    return c;
  }

  const snap = (flowId, step, index, status) => ({
    flow_id: flowId, current_step: step, current_step_index: index, status,
  });

  // -- (1) first observation only establishes a baseline ---------------------
  await checkAsync("progression: first snapshot only sets baseline, no timer", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      app.maybeRefreshConversationOnProgression(snap("F1", "discovery", 0, "running"));
      assert.equal(app.state.progressionGraceTimer, null, "first snapshot must not arm a timer");
      assert.ok(app.state.flowProgressionMarker, "baseline marker must be set");
      assert.equal(app.state.flowProgressionMarker.flowId, "F1");
      assert.equal(app.state.flowProgressionMarker.currentStep, "discovery");
      await waitGrace();
      assert.equal(calls.length, 0, "the first snapshot must never trigger a refresh");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (2) HEALTHY path: WS increment within the grace window → zero rebuilds -
  await checkAsync("progression: advance + WS increment within grace → zero silent rebuilds", async () => {
    const c = resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p2", delivery: "full" });
    try {
      // Baseline at discovery.
      app.maybeRefreshConversationOnProgression(snap("F1", "discovery", 0, "running"));
      // Advance discovery → analyze: arms the grace timer (does NOT rebuild now).
      app.maybeRefreshConversationOnProgression(snap("F1", "analyze", 1, "running"));
      assert.notEqual(app.state.progressionGraceTimer, null, "an advance must arm the grace timer");
      assert.equal(calls.length, 0, "an advance must NOT rebuild immediately");
      const seq0 = app.state.flowConversationAppendSeq;
      // A real WS increment for this flow arrives through applyHistoryData's
      // running-flow branch — this is the healthy push path recovering on its own.
      app.applyHistoryData({
        flow_id: "F1", mode: "append", records: [asstRecord("A", 1, "s2", "analyze")],
      });
      assert.equal(app.state.flowConversationAppendSeq, seq0 + 1,
        "a landed WS append must bump flowConversationAppendSeq");
      assert.equal(ctx.findAll(c, "conv-bubble").length, 1,
        "the WS append renders the new record live");
      // Let the grace window elapse: the fallback must observe the bumped counter
      // and skip — zero silent rebuilds on the healthy path.
      await waitGrace();
      assert.equal(calls.length, 0, "a delivered WS increment must suppress the fallback rebuild");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (3) FALLBACK path: no WS increment within the window → one rebuild -----
  await checkAsync("progression: advance + no WS increment → exactly one silent rebuild after grace", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({
      records: [asstRecord("A", 1, "s2", "analyze")], progress: "p2", delivery: "full",
    });
    try {
      app.maybeRefreshConversationOnProgression(snap("F1", "discovery", 0, "running"));
      app.maybeRefreshConversationOnProgression(snap("F1", "analyze", 1, "running"));
      assert.equal(calls.length, 0, "no rebuild before the grace window elapses");
      // No WS increment arrives; the window elapses → exactly one fallback rebuild.
      await waitGrace();
      assert.equal(calls.length, 1, "a silent WS path must trigger exactly one fallback rebuild");
      assert.ok(calls[0].includes("/api/history/"), calls[0]);
      assert.ok(!calls[0].includes("after="), "the fallback must be a full (no-after) pull");
      // The timer reference is cleared after firing so a later advance re-arms cleanly.
      assert.equal(app.state.progressionGraceTimer, null, "the fired timer reference is cleared");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (4) same-advance duplicate snapshot still at most one rebuild ----------
  await checkAsync("progression: a duplicate snapshot of the same advance re-fires nothing", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      app.maybeRefreshConversationOnProgression(snap("F1", "discovery", 0, "running"));
      // Advance → arm.
      app.maybeRefreshConversationOnProgression(snap("F1", "analyze", 1, "running"));
      const armed = app.state.progressionGraceTimer;
      // Same snapshot re-delivered (3s poll re-carrying the WS advance): the
      // marker already reads analyze, so it is NOT advanced → no re-arm.
      app.maybeRefreshConversationOnProgression(snap("F1", "analyze", 1, "running"));
      assert.equal(app.state.progressionGraceTimer, armed,
        "a duplicate snapshot must not re-arm (cancel+reschedule) the grace timer");
      await waitGrace();
      assert.equal(calls.length, 1, "a duplicate snapshot of the same advance must not fire a second rebuild");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (5) in-step retry arms; a halt (RUNNING→FAILED / →PAUSED) never does ----
  // Uses the REAL /api/flows shape (no step_history): when update_spec errors the
  // flow keeps current_step == "update_spec" / same index, status flips
  // RUNNING→FAILED (halt) then FAILED→RUNNING when the operator retries. Only the
  // forward-motion transition is an advance; the halt is the flow STOPPING and
  // must arm nothing.
  await checkAsync("progression: only a retry/resume status flip arms a fallback (not a halt)", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      app.maybeRefreshConversationOnProgression(snap("F1", "update_spec", 5, "running"));
      // The step errors: same step/index, status → FAILED. A halt — arms nothing.
      app.maybeRefreshConversationOnProgression(snap("F1", "update_spec", 5, "failed"));
      assert.equal(app.state.progressionGraceTimer, null, "a RUNNING→FAILED halt must not arm a timer");
      await waitGrace();
      assert.equal(calls.length, 0, "a RUNNING→FAILED halt must never rebuild");
      // Operator retries: current_step / index unchanged, status FAILED→RUNNING.
      // This forward-motion transition IS an advance → arms the fallback.
      app.maybeRefreshConversationOnProgression(snap("F1", "update_spec", 5, "running"));
      assert.notEqual(app.state.progressionGraceTimer, null, "a FAILED→RUNNING retry must arm a timer");
      await waitGrace();
      assert.equal(calls.length, 1, "the retry, with no WS increment, fires exactly one fallback rebuild");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (5b) a RUNNING→PAUSED halt never rebuilds; PAUSED→RUNNING resumes -------
  await checkAsync("progression: a halt-only status change (RUNNING→PAUSED) arms nothing", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      app.maybeRefreshConversationOnProgression(snap("F1", "update_spec", 5, "running"));
      app.maybeRefreshConversationOnProgression(snap("F1", "update_spec", 5, "paused"));
      assert.equal(app.state.progressionGraceTimer, null, "a RUNNING→PAUSED halt must not arm a timer");
      await waitGrace();
      assert.equal(calls.length, 0, "a RUNNING→PAUSED halt must never rebuild");
      // Resuming (PAUSED → RUNNING) is forward motion.
      app.maybeRefreshConversationOnProgression(snap("F1", "update_spec", 5, "running"));
      await waitGrace();
      assert.equal(calls.length, 1, "resuming from PAUSED→RUNNING fires exactly one fallback rebuild");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (6) progression on a non-open flow arms nothing -----------------------
  await checkAsync("progression: only the open flow arms a fallback", async () => {
    resetProgressionState("OPEN");          // the open flow is OPEN, not OTHER
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      app.maybeRefreshConversationOnProgression(snap("OTHER", "discovery", 0, "running"));
      app.maybeRefreshConversationOnProgression(snap("OTHER", "analyze", 1, "running"));
      assert.equal(app.state.progressionGraceTimer, null, "a non-open flow's advance must not arm a timer");
      await waitGrace();
      assert.equal(calls.length, 0, "a non-open flow's advance must never rebuild");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (7) cancelling the grace (flow switch/close) drops a pending fallback ---
  // openFlowView / doCloseFlowView call cancelProgressionGrace() in their reset
  // section; this pins that a pending fallback is dropped so it can never rebuild
  // against a flow that is no longer open.
  await checkAsync("progression: cancelling the grace drops a pending fallback", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      app.maybeRefreshConversationOnProgression(snap("F1", "discovery", 0, "running"));
      app.maybeRefreshConversationOnProgression(snap("F1", "analyze", 1, "running"));
      assert.notEqual(app.state.progressionGraceTimer, null, "the advance armed a timer");
      // Simulate the openFlowView / doCloseFlowView reset path.
      app.cancelProgressionGrace();
      assert.equal(app.state.progressionGraceTimer, null, "cancel clears the pending timer");
      assert.equal(app.state.progressionGraceFlowId, null, "cancel clears the target flow id");
      await waitGrace();
      assert.equal(calls.length, 0, "a cancelled grace must never rebuild");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (8) silent refresh: no pre-clear, no Loading placeholder --------------
  await checkAsync("progression: silent refresh keeps the DOM until new data arrives", async () => {
    const c = resetProgressionState("F1");
    const saved = globalThis.fetch;
    try {
      // First, a normal full open populates the conversation.
      installCountingFetch({
        records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
        progress: "t0", delivery: "full",
      });
      await app.loadFlowConversation("F1");
      const before = ctx.findAll(c, "conv-bubble").length;
      assert.equal(before, 2, "the initial open must render two bubbles");

      // Now drive a SILENT refresh against a deferred fetch and inspect the
      // mid-flight container: it must NOT be cleared and must show no Loading
      // placeholder.
      const def = installDeferredFetch();
      const p = app.loadFlowConversation("F1", { silent: true });
      assert.equal(ctx.findAll(c, "conv-bubble").length, 2,
        "silent refresh must not clear the existing bubbles while fetching");
      assert.equal(ctx.findAll(c, "empty").length, 0,
        "silent refresh must not show a Loading placeholder");

      def.resolve({
        records: [
          asstRecord("A", 1, "s1", "discovery"),
          asstRecord("B", 2, "s1", "discovery"),
          asstRecord("C", 3, "s2", "analyze"),
        ],
        progress: "t1", delivery: "full",
      });
      await p;
      assert.equal(ctx.findAll(c, "conv-bubble").length, 3,
        "after the data lands the DOM is rebuilt with the new record");
      assert.equal(app.state.flowConversationRecords.length, 3);
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (9) silent refresh: scroll position preserved unless near bottom ------
  await checkAsync("progression: silent refresh preserves scroll unless near bottom", async () => {
    const saved = globalThis.fetch;
    try {
      // Case A: reader scrolled up (NOT near bottom) → the *exact* reading
      // offset is preserved. A nonzero offset is used deliberately so the
      // assertion proves the position is restored after the append=false
      // rebuild rather than merely left at the top: a bare "don't scroll to
      // bottom" would pass at scrollTop 0 even if the rebuild reset it.
      const cA = resetProgressionState("F1");
      installCountingFetch({
        records: [asstRecord("A", 1, "s1", "discovery")], progress: "t0", delivery: "full",
      });
      await app.loadFlowConversation("F1");
      cA.scrollHeight = 1000; cA.clientHeight = 100; cA.scrollTop = 600;  // 300 from bottom
      // The reader deliberately scrolled up: in production the scroll handler
      // drops the follow-bottom intent, which the silent rebuild consults (#260).
      app.state.flowConversationFollowingBottom = false;
      installCountingFetch({
        records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
        progress: "t1", delivery: "full",
      });
      await app.loadFlowConversation("F1", { silent: true });
      assert.equal(cA.scrollTop, 600,
        "a reader scrolled up keeps their exact offset, neither yanked to the bottom nor reset to the top");

      // Case B: reader already near the bottom → follow to the bottom.
      const cB = resetProgressionState("F2");
      installCountingFetch({
        records: [asstRecord("A", 1, "s1", "discovery")], progress: "t0", delivery: "full",
      });
      await app.loadFlowConversation("F2");
      cB.scrollHeight = 200; cB.clientHeight = 100; cB.scrollTop = 120;  // -20 → near bottom
      installCountingFetch({
        records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
        progress: "t1", delivery: "full",
      });
      await app.loadFlowConversation("F2", { silent: true });
      assert.equal(cB.scrollTop, cB.scrollHeight,
        "a reader already near the bottom follows to the new bottom");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (10) silent refresh never touches the reply-region state ---------------
  await checkAsync("progression: silent refresh leaves reply-region state untouched", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    try {
      installCountingFetch({
        records: [asstRecord("A", 1, "s1", "discovery")], progress: "t0", delivery: "full",
      });
      await app.loadFlowConversation("F1");
      // Seed reply-region state as if the user were mid-draft against a chip.
      app.state.flowReplyTargetId = "call-xyz";
      app.state.flowInterjectRequested = true;
      app.state.flowInterjectFlowId = "F1";
      installCountingFetch({
        records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
        progress: "t1", delivery: "full",
      });
      await app.loadFlowConversation("F1", { silent: true });
      assert.equal(app.state.flowReplyTargetId, "call-xyz",
        "silent refresh must not clear the reply target");
      assert.equal(app.state.flowInterjectRequested, true,
        "silent refresh must not clear the interject opt-in");
      assert.equal(app.state.flowInterjectFlowId, "F1");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (11) refreshFlowDetail: stale out-of-order response must not regress -----
  // Detail fetches run concurrently (3s poll vs STATUS_UPDATE refresh) and can
  // resolve out of order. A late OLDER response carrying a stale current_step
  // must not overwrite the fresher snapshot, rewind the progression marker, or
  // re-arm the fallback.
  await checkAsync("progression: a stale out-of-order detail response does not regress the marker", async () => {
    resetProgressionState("F1");
    app.state.flowDetailReqSeq = 0;
    app.state.flowDetailAppliedSeq = 0;
    // Baseline established at discovery (as the first observation would).
    app.state.flowProgressionMarker = {
      flowId: "F1", currentStep: "discovery", currentStepIndex: 0, status: "running",
    };
    const saved = globalThis.fetch;
    // Detail (`/api/flows/`) fetches resolve via collected resolvers so we can
    // control ordering; silent-refresh (`/api/history/`) pulls are just counted.
    const flowResolvers = [];
    const historyCalls = [];
    globalThis.fetch = (url) => {
      const u = String(url);
      if (u.includes("/api/flows/")) {
        return new Promise((resolve) => {
          flowResolvers.push((payload) =>
            resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) }));
        });
      }
      historyCalls.push(u);
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ records: [], progress: "p", delivery: "full" }),
      });
    };
    try {
      const p1 = app.refreshFlowDetail();   // reqSeq=1 → will resolve discovery (stale)
      const p2 = app.refreshFlowDetail();   // reqSeq=2 → will resolve analyze (fresh)
      await flush();
      assert.equal(flowResolvers.length, 2, "both detail fetches are in flight");
      // Resolve the NEWER request (seq=2) first with the advanced snapshot.
      flowResolvers[1]({
        flow: { flow_id: "F1", current_step: "analyze", current_step_index: 1, status: "running" },
        machine_id: "m1",
      });
      await p2;
      await flush();
      assert.equal(app.state.flowProgressionMarker.currentStep, "analyze");
      // The advance armed the fallback; with no WS increment it fires once.
      await waitGrace();
      assert.equal(historyCalls.length, 1, "the real advance fires exactly one fallback rebuild");
      // Now the OLDER request (seq=1) resolves LATE with the stale snapshot. The
      // seq guard drops it before the detector, so the marker holds and nothing re-arms.
      flowResolvers[0]({
        flow: { flow_id: "F1", current_step: "discovery", current_step_index: 0, status: "running" },
        machine_id: "m1",
      });
      await p1;
      await flush();
      await waitGrace();
      assert.equal(app.state.flowProgressionMarker.currentStep, "analyze",
        "a stale older response must not rewind the progression marker");
      assert.equal(historyCalls.length, 1,
        "a stale older response must not fire a second fallback rebuild");
      assert.equal(app.state.flowDetail && app.state.flowDetail.current_step, "analyze",
        "a stale older response must not overwrite the fresher flow detail");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (12) cross-lifecycle stale response (close/reopen the SAME flow) ---------
  // openFlowView/doCloseFlowView reset the seq counters to 0 each lifecycle. A
  // high-seq fetch still in flight from a PRIOR open of the same flow would pass
  // the selectedFlowId check on resolution (same flowId), apply its stale
  // snapshot, and bump flowDetailAppliedSeq to a high value that suppresses this
  // lifecycle's fresh low-seq responses. The flowDetailViewGen guard must drop
  // that prior-lifecycle response so it neither applies nor suppresses.
  await checkAsync("progression: a prior-lifecycle stale detail response is dropped after reopening the same flow", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const flowResolvers = [];
    const historyCalls = [];
    globalThis.fetch = (url) => {
      const u = String(url);
      if (u.includes("/api/flows/")) {
        return new Promise((resolve) => {
          flowResolvers.push((payload) =>
            resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) }));
        });
      }
      historyCalls.push(u);
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ records: [], progress: "p", delivery: "full" }),
      });
    };
    try {
      // --- prior lifecycle: a fetch reaches a high seq and stays in flight. ---
      app.state.flowDetailViewGen = 5;
      app.state.flowDetailReqSeq = 74;
      app.state.flowDetailAppliedSeq = 74;
      app.state.flowProgressionMarker = {
        flowId: "F1", currentStep: "discovery", currentStepIndex: 0, status: "running",
      };
      const stale = app.refreshFlowDetail(); // claims seq=75, reqGen=5
      await flush();
      assert.equal(flowResolvers.length, 1, "the prior-lifecycle fetch is in flight");

      // --- close + reopen the SAME flow: counters reset, generation bumps. ---
      app.state.flowDetailReqSeq = 0;
      app.state.flowDetailAppliedSeq = 0;
      app.state.flowDetailViewGen += 1; // -> 6, mirrors openFlowView
      app.state.flowProgressionMarker = null;
      app.state.selectedFlowId = "F1";

      const fresh = app.refreshFlowDetail(); // claims seq=1, reqGen=6
      await flush();
      assert.equal(flowResolvers.length, 2, "the post-reopen fetch is in flight");
      flowResolvers[1]({
        flow: { flow_id: "F1", current_step: "analyze", current_step_index: 1, status: "running" },
        machine_id: "m1",
      });
      await fresh;
      await flush();
      assert.equal(app.state.flowDetail && app.state.flowDetail.current_step, "analyze",
        "the fresh post-reopen response applies");
      assert.equal(app.state.flowDetailAppliedSeq, 1, "applied seq tracks the new lifecycle");

      // --- the prior-lifecycle high-seq fetch resolves LATE with stale data. -
      flowResolvers[0]({
        flow: { flow_id: "F1", current_step: "discovery", current_step_index: 0, status: "running" },
        machine_id: "m1",
      });
      await stale;
      await flush();
      assert.equal(app.state.flowDetail && app.state.flowDetail.current_step, "analyze",
        "the prior-lifecycle response must not overwrite the fresh snapshot");
      assert.equal(app.state.flowDetailAppliedSeq, 1,
        "the prior-lifecycle response must not bump the applied seq and suppress future polls");
    } finally {
      // Drop any fallback the fresh advance (marker null → analyze was a first
      // observation, so none should be armed, but be defensive) may have left.
      app.cancelProgressionGrace();
      globalThis.fetch = saved;
    }
  });

  // -- (13) a failing silent refresh must not strand an in-flight first open ---
  // openFlowView fires a normal full load (shows the Loading placeholder) and a
  // step advance can fire a SILENT refresh before that first-open resolves. The
  // silent path must NOT bump the shared conversation epoch up-front: if it did,
  // the in-flight first-open would be invalidated, and a transient silent-fetch
  // failure would then leave the conversation stuck on the Loading/empty DOM
  // (the silent path returns early on failure, and the superseded first-open
  // also returns early on the epoch mismatch). The deferred epoch bump lets the
  // first-open complete and render whenever the silent refresh fails.
  await checkAsync("progression: a failing silent refresh lets the in-flight first-open complete", async () => {
    const c = resetProgressionState("F1");
    const saved = globalThis.fetch;
    try {
      // Two deferred fetches: [0] is the first-open full load, [1] is the silent
      // refresh, both held at their await so we can control resolution order.
      const resolvers = [];
      globalThis.fetch = (url) =>
        new Promise((resolve) => {
          resolvers.push((payload, ok = true, status = 200) =>
            resolve({ ok, status, json: () => Promise.resolve(payload) }));
        });

      // First-open begins and parks on its fetch, showing the Loading placeholder.
      const firstOpen = app.loadFlowConversation("F1");
      await flush();
      assert.equal(resolvers.length, 1, "the first-open fetch is in flight");
      assert.equal(ctx.findAll(c, "empty").length, 1,
        "first-open shows the Loading placeholder while fetching");

      // A step advance fires a silent refresh while first-open is still pending.
      const silent = app.loadFlowConversation("F1", { silent: true });
      await flush();
      assert.equal(resolvers.length, 2, "the silent refresh fetch is in flight");

      // The silent fetch fails transiently (non-OK). It must return early WITHOUT
      // having superseded the first-open.
      resolvers[1]({}, false, 503);
      await silent;
      await flush();

      // The first-open now resolves successfully — it must still be live and
      // render the conversation rather than being discarded by a stale epoch.
      resolvers[0]({
        records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
        progress: "t0", delivery: "full",
      });
      await firstOpen;
      await flush();
      assert.equal(ctx.findAll(c, "empty").length, 0,
        "the Loading placeholder must be gone — the first-open was not stranded");
      assert.equal(ctx.findAll(c, "conv-bubble").length, 2,
        "the first-open load rendered the conversation despite the silent failure");
      assert.equal(app.state.flowConversationRecords.length, 2);
    } finally {
      globalThis.fetch = saved;
    }
  });
}
