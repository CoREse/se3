/*
 * Progression-refresh fallback tests (Group G2).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerProgressionRefreshTests({app, check, checkAsync, findOne, findAll})`
 * so the parent harness drives the same check() reporter and the same `app`
 * module export (mirrors live_append_*, reply_send_error_handling, …).
 *
 * Context: the long-standing "flow advances (step switch / in-step retry) but
 * the main conversation freezes until you exit and re-enter the session" bug.
 * This group adds a CAUSE-IMMUNE fallback, NOT a root-cause fix: it watches the
 * authoritative /api/flows/{id} snapshot (which reliably advances current_step /
 * current_step_index on a step switch, and flips status FAILED/PAUSED→RUNNING on
 * an in-step retry) and, on a detected advance of the open flow, fires exactly
 * one SILENT full /api/history rebuild (the G1 silent path, equivalent to
 * exit-and-re-enter but without the blank flash or scroll jump).
 *
 * IMPORTANT: the daemon's FlowSnapshot.to_dict() NEVER emits a `step_history`
 * field (the server back-fills it to an empty list), so these tests deliberately
 * use the real /api/flows shape (no step_history) and exercise the in-step-retry
 * path through the `status` signal — not a synthetic growing step_history that
 * never occurs in production.
 *
 * These tests pin:
 *   (1) first snapshot only establishes a baseline — no refresh;
 *   (2) a current_step change triggers exactly one refresh, and the same
 *       snapshot delivered again does NOT re-trigger;
 *   (3) a status flip with an unchanged current_step (in-step retry: the engine
 *       reuses the step_id so current_step stays put; the flow flips
 *       FAILED/PAUSED→RUNNING) triggers one refresh;
 *   (4) a progression on a flow that is NOT the open one does not trigger;
 *   (5) the silent refresh never pre-clears the container nor shows a Loading
 *       placeholder — the DOM is rebuilt only once the new data arrives;
 *   (6) the silent refresh preserves the reader's scroll position unless they
 *       were already near the bottom;
 *   (7) the silent refresh never touches the reply-region state.
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

  function resetProgressionState(flowId) {
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];
    app.state.flowConversationProgress = null;
    app.state.flowProgressionMarker = null;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    c.scrollTop = 0;
    c.scrollHeight = 0;
    c.clientHeight = 0;
    return c;
  }

  // -- (1) first observation only establishes a baseline ---------------------
  await checkAsync("progression: first snapshot only sets baseline, no refresh", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "discovery", current_step_index: 0, status: "running",
      });
      await flush();
      assert.equal(calls.length, 0, "the first snapshot must not trigger a refresh");
      assert.ok(app.state.flowProgressionMarker, "baseline marker must be set");
      assert.equal(app.state.flowProgressionMarker.flowId, "F1");
      assert.equal(app.state.flowProgressionMarker.currentStep, "discovery");
      assert.equal(app.state.flowProgressionMarker.currentStepIndex, 0);
      assert.equal(app.state.flowProgressionMarker.status, "running");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (2) current_step change triggers exactly once -------------------------
  await checkAsync("progression: current_step change triggers exactly one refresh", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({
      records: [asstRecord("A", 1, "s2", "analyze")], progress: "p2", delivery: "full",
    });
    try {
      // Baseline at discovery.
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "discovery", current_step_index: 0, status: "running",
      });
      await flush();
      assert.equal(calls.length, 0);
      // Advance discovery → analyze: one refresh.
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "analyze", current_step_index: 1, status: "running",
      });
      await flush();
      assert.equal(calls.length, 1, "an advance must fire exactly one refresh");
      assert.ok(calls[0].includes("/api/history/"), calls[0]);
      assert.ok(!calls[0].includes("after="), "silent refresh must be a full (no-after) pull");
      assert.equal(app.state.flowProgressionMarker.currentStep, "analyze");
      // The same snapshot delivered again (3s poll re-delivering the WS push)
      // must NOT re-trigger.
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "analyze", current_step_index: 1, status: "running",
      });
      await flush();
      assert.equal(calls.length, 1, "a duplicate snapshot of the same advance must not re-trigger");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (3) in-step retry (status flips, current_step / index unchanged) -------
  // Uses the REAL /api/flows shape (no step_history): when update_spec errors
  // the flow snapshot keeps current_step == "update_spec" and the same
  // current_step_index, but status flips RUNNING → FAILED (the failure) and then
  // FAILED → RUNNING when the operator chooses Retry and the step re-runs. Only
  // the FORWARD-MOTION transition (FAILED/PAUSED → RUNNING) is an advance: the
  // failure itself (RUNNING → FAILED) is the flow STOPPING, not advancing, and
  // must NOT fire a refresh on the open flow. Only the retry that re-runs the
  // step is a genuine advance → exactly one silent refresh.
  await checkAsync("progression: only a retry/resume status flip triggers a refresh (not a halt)", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      // Baseline: update_spec running.
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "update_spec", current_step_index: 5, status: "running",
      });
      await flush();
      assert.equal(calls.length, 0);
      // The step errors out: same current_step / index, status → FAILED. This is
      // the flow halting, not advancing — it must NOT trigger a refresh.
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "update_spec", current_step_index: 5, status: "failed",
      });
      await flush();
      assert.equal(calls.length, 0, "a RUNNING→FAILED halt must NOT trigger a refresh");
      assert.equal(app.state.flowProgressionMarker.status, "failed");
      // Operator chooses Retry: the engine reuses the same step_id, so
      // current_step / index stay put while status flips back to RUNNING. This
      // forward-motion FAILED→RUNNING transition IS an advance.
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "update_spec", current_step_index: 5, status: "running",
      });
      await flush();
      assert.equal(calls.length, 1, "the retry (FAILED→RUNNING) must trigger a refresh");
      assert.equal(app.state.flowProgressionMarker.status, "running");
      // A duplicate of the retry snapshot (3s poll re-delivering it) must NOT
      // re-trigger.
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "update_spec", current_step_index: 5, status: "running",
      });
      await flush();
      assert.equal(calls.length, 1, "a duplicate retry snapshot must not re-trigger");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (3b) a halt-only transition (RUNNING → PAUSED / FAILED) never refreshes -
  // The flow stopping on the open step is not progression. Neither a pause nor a
  // failure that keeps current_step / index put may fire a silent reload.
  await checkAsync("progression: a halt-only status change (RUNNING→PAUSED) never refreshes", async () => {
    resetProgressionState("F1");
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "update_spec", current_step_index: 5, status: "running",
      });
      await flush();
      assert.equal(calls.length, 0);
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "update_spec", current_step_index: 5, status: "paused",
      });
      await flush();
      assert.equal(calls.length, 0, "a RUNNING→PAUSED halt must NOT trigger a refresh");
      assert.equal(app.state.flowProgressionMarker.status, "paused");
      // And resuming from the pause (PAUSED → RUNNING) IS forward motion.
      app.maybeRefreshConversationOnProgression({
        flow_id: "F1", current_step: "update_spec", current_step_index: 5, status: "running",
      });
      await flush();
      assert.equal(calls.length, 1, "resuming from PAUSED→RUNNING must trigger a refresh");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (4) progression on a non-open flow does not trigger -------------------
  await checkAsync("progression: only the open flow triggers a refresh", async () => {
    resetProgressionState("OPEN");          // the open flow is OPEN, not OTHER
    const saved = globalThis.fetch;
    const calls = installCountingFetch({ records: [], progress: "p", delivery: "full" });
    try {
      // Baseline + advance for a DIFFERENT flow while OPEN stays selected.
      app.maybeRefreshConversationOnProgression({
        flow_id: "OTHER", current_step: "discovery", current_step_index: 0, status: "running",
      });
      app.maybeRefreshConversationOnProgression({
        flow_id: "OTHER", current_step: "analyze", current_step_index: 1, status: "running",
      });
      await flush();
      assert.equal(calls.length, 0, "a non-open flow's advance must not trigger a refresh");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (5) silent refresh: no pre-clear, no Loading placeholder --------------
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

  // -- (6) silent refresh: scroll position preserved unless near bottom ------
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

  // -- (7) silent refresh never touches the reply-region state ---------------
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

  // -- (8) refreshFlowDetail: stale out-of-order response must not regress -----
  // Detail fetches run concurrently (3s poll vs STATUS_UPDATE refresh) and can
  // resolve out of order. A late OLDER response carrying a stale current_step
  // must not overwrite the fresher snapshot, rewind the progression marker, or
  // re-trigger the silent refresh.
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
      assert.equal(historyCalls.length, 1, "the real advance fires exactly one silent refresh");
      // Now the OLDER request (seq=1) resolves LATE with the stale snapshot.
      flowResolvers[0]({
        flow: { flow_id: "F1", current_step: "discovery", current_step_index: 0, status: "running" },
        machine_id: "m1",
      });
      await p1;
      await flush();
      assert.equal(app.state.flowProgressionMarker.currentStep, "analyze",
        "a stale older response must not rewind the progression marker");
      assert.equal(historyCalls.length, 1,
        "a stale older response must not fire a second silent refresh");
      assert.equal(app.state.flowDetail && app.state.flowDetail.current_step, "analyze",
        "a stale older response must not overwrite the fresher flow detail");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (9) cross-lifecycle stale response (close/reopen the SAME flow) ---------
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
      globalThis.fetch = saved;
    }
  });

  // -- (10) a failing silent refresh must not strand an in-flight first open ---
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
