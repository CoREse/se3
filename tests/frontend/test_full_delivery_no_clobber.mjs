/*
 * Empty-full no-clobber guard (issue #287, Group G4).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerFullDeliveryNoClobberTests({app, check, checkAsync})`
 * so the parent harness drives the same check() reporter and `app` export.
 *
 * Regression context ("worktree session 的 discovery 聊天记录在 WebUI 中完全不
 * 显示"): the worktree self-heal re-pull (widened to cover a `paused` flow) can
 * make the daemon answer with a `mode:full` snapshot carrying ZERO records — its
 * history directory failed to resolve, and "no dir" was indistinguishable from
 * "no records". Both the REST merge path (`mergeHistoryResponse`, delivery
 * "full") and the WS push path (`applyHistoryData`, mode "full") adopt a full
 * frame WHOLESALE, so that empty snapshot replaced the rendered conversation and
 * blanked the chat pane — even the first discovery round that had displayed
 * fine.
 *
 * The daemon (G3) and the server cache (G2) each refuse to emit such a frame;
 * these tests pin the frontend's last layer of that defence: an empty full frame
 * against a NON-empty view is a no-op (DOM, records, progress token and
 * signature all stand), a GROWN full frame still rebuilds authoritatively (so
 * the multi-round self-heal is not blunted), and a first load with genuinely no
 * records still renders the empty state.
 */
import assert from "node:assert/strict";

export async function registerFullDeliveryNoClobberTests(ctx) {
  const { app, check, checkAsync } = ctx;

  const rec = (content, ts, stepId) => ({
    step_id: stepId || "01_discovery",
    step_type: "discovery",
    message: { role: "assistant", content, timestamp: ts },
  });

  // Two rendered discovery rounds — the state #287 wiped.
  const twoRounds = () => [
    rec("round-1 question", 1),
    rec("round-1 answer", 2),
    rec("round-2 question", 3),
    rec("round-2 answer", 4),
  ];

  function seedOpenFlow(records, progress, signature) {
    app.state.selectedFlowId = "WT1";
    app.state.selectedHistoryId = null;
    app.state.flowConversationRecords = records;
    app.state.flowConversationProgress = progress || null;
    app.state.flowConversationSignature = signature || null;
    app.state.flowConversationEpoch = 0;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    app.renderConversation(c, records, false);
    return c;
  }

  const bodies = (records) =>
    records.map(app.normalizeRecord).map((n) => n.content);
  const bubbleCount = (c) =>
    c.children.filter((x) => x.__convIdx !== undefined).length;

  function withFetch(payload) {
    const saved = globalThis.fetch;
    const calls = [];
    globalThis.fetch = (url) => {
      calls.push(String(url));
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve(payload),
      });
    };
    return { calls, restore: () => { globalThis.fetch = saved; } };
  }

  // ------------------------------------------------------------------- //
  // Scenario 1: two rendered rounds + a zero-record full → DOM unchanged.
  // ------------------------------------------------------------------- //
  check("mergeHistoryResponse: an empty full frame does NOT clobber held records", () => {
    const held = twoRounds();
    const out = app.mergeHistoryResponse(
      { delivery: "full", records: [], progress: "tokEMPTY", signature: "sigEMPTY" },
      held,
      held,
    );
    assert.equal(out.render, "noop", "the rejected frame repaints nothing");
    assert.equal(out.records, held, "the held array survives by identity");
    assert.equal(out.preserveTokens, true,
      "the caller is told to keep its own token/signature");
  });

  await checkAsync("silent self-heal: a zero-record full leaves the two rendered rounds intact", async () => {
    const held = twoRounds();
    const c = seedOpenFlow(held, "tok0", "sig0");
    const before = bubbleCount(c);
    const state0 = c.__convState;
    assert.equal(before, 4, "the two discovery rounds rendered before the poll");
    const f = withFetch({
      delivery: "full", records: [], progress: "tokEMPTY", signature: "sigEMPTY",
    });
    try {
      await app.loadFlowConversation("WT1", { silent: true });
      assert.equal(bubbleCount(c), before, "the chat pane was NOT blanked");
      assert.equal(app.state.flowConversationRecords, held,
        "the held records array is untouched");
      assert.equal(c.__convState, state0, "no rebuild of the incremental state");
      // The rejected frame's cursor pins an EMPTY bundle — adopting it would
      // force the next poll into a needless full re-pull.
      assert.equal(app.state.flowConversationProgress, "tok0",
        "the held progress token stands");
      assert.equal(app.state.flowConversationSignature, "sig0",
        "the held signature stands");
    } finally {
      f.restore();
    }
  });

  check("WS mode:full with zero records leaves the rendered conversation alone", () => {
    const held = twoRounds();
    const c = seedOpenFlow(held, "tokWS", "sigWS");
    const before = bubbleCount(c);
    app.applyHistoryData({ type: "history_data", flow_id: "WT1", mode: "full", records: [] });
    assert.equal(bubbleCount(c), before, "the empty WS full push repainted nothing");
    assert.equal(app.state.flowConversationRecords, held, "records survive by identity");
    assert.equal(app.state.flowConversationProgress, "tokWS",
      "the empty push does not invalidate the held token");
    assert.equal(app.state.flowConversationSignature, "sigWS",
      "…nor the held signature");
  });

  // ------------------------------------------------------------------- //
  // Scenario 2: a full carrying MORE records still rebuilds — the original
  // multi-round self-heal (the 2nd round arriving late) must not be blunted.
  // ------------------------------------------------------------------- //
  await checkAsync("silent self-heal: a GROWN full still rebuilds and shows the 2nd round", async () => {
    // The view holds only round 1 (round 2 never reached it live).
    const held = [rec("round-1 question", 1), rec("round-1 answer", 2)];
    const c = seedOpenFlow(held, "tok1", "sig1");
    const f = withFetch({
      delivery: "full",
      records: twoRounds(),
      progress: "tok2", signature: "sig2",
    });
    try {
      await app.loadFlowConversation("WT1", { silent: true });
      assert.deepEqual(bodies(app.state.flowConversationRecords), [
        "round-1 question", "round-1 answer",
        "round-2 question", "round-2 answer",
      ], "the authoritative full snapshot healed the missing 2nd round");
      assert.equal(bubbleCount(c), 4, "all four bubbles are rendered");
      assert.equal(app.state.flowConversationProgress, "tok2",
        "a genuine full adopts its fresh token");
      assert.equal(app.state.flowConversationSignature, "sig2",
        "…and its fresh signature");
    } finally {
      f.restore();
    }
  });

  check("WS mode:full with MORE records still replaces the bundle", () => {
    const held = [rec("round-1 question", 1), rec("round-1 answer", 2)];
    const c = seedOpenFlow(held, "tokWS", "sigWS");
    app.applyHistoryData({
      type: "history_data", flow_id: "WT1", mode: "full", records: twoRounds(),
    });
    assert.equal(bubbleCount(c), 4, "the grown full push rebuilt all four bubbles");
    assert.equal(app.state.flowConversationProgress, null,
      "a real full push still invalidates the held delta cursor");
  });

  // ------------------------------------------------------------------- //
  // Scenario 3: a genuinely empty flow (nothing held) still renders empty —
  // only a REGRESSION to zero is refused, never a first paint.
  // ------------------------------------------------------------------- //
  await checkAsync("first load of a genuinely empty flow still renders the empty state", async () => {
    const c = seedOpenFlow([], null, null);
    const f = withFetch({ delivery: "full", records: [], progress: "tok0", signature: "sig0" });
    try {
      await app.loadFlowConversation("WT1", {});
      assert.deepEqual(app.state.flowConversationRecords, [],
        "the empty snapshot is adopted when nothing was held");
      assert.equal(bubbleCount(c), 0, "no bubbles — the empty state");
      assert.equal(app.state.flowConversationProgress, "tok0",
        "the first-load token is adopted normally");
    } finally {
      f.restore();
    }
  });

  check("WS mode:full with zero records against an EMPTY view is applied normally", () => {
    seedOpenFlow([], null, null);
    app.applyHistoryData({ type: "history_data", flow_id: "WT1", mode: "full", records: [] });
    assert.deepEqual(app.state.flowConversationRecords, [],
      "the empty full push is adopted when nothing was held");
    assert.equal(app.state.flowConversationEpoch, 1,
      "…and it takes a fresh epoch, unlike a rejected frame");
  });

  // ------------------------------------------------------------------- //
  // Scenario 4: the HISTORY DETAIL view has its own twin of each guard —
  // `applyHistoryData`'s history-view branch and `openHistorySession`'s
  // preserveTokens adoption guard. They are separate call sites from the
  // running-flow ones above, so a refactor could blank an open history pane
  // while every flow-conversation assertion above still passed. Drive them.
  // ------------------------------------------------------------------- //
  function seedOpenHistory(records, progress, signature) {
    // `isHistoryOpen()` gates the history-view consumer of applyHistoryData; a
    // sibling test may have left the view hidden.
    document.getElementById("history-view").classList.remove("hidden");
    // Null so the running-flow consumer stays out of these assertions — only
    // the history-detail twin of the guard may act on the frame.
    app.state.selectedFlowId = null;
    app.state.selectedHistoryId = "WT1";
    app.state.historyRecords = records;
    app.state.historyProgress = progress || null;
    app.state.historySignature = signature || null;
    app.state.historyEpoch = 0;
    app.state.historySessions = [{ flow_id: "WT1", machine_id: "m1" }];
    const d = document.getElementById("history-detail");
    d.innerHTML = "";
    d.__convState = null;
    app.renderConversation(d, records, false);
    return d;
  }

  check("history detail: WS mode:full with zero records leaves the rendered pane alone", () => {
    const held = twoRounds();
    const d = seedOpenHistory(held, "tokH", "sigH");
    const before = bubbleCount(d);
    const epoch0 = app.state.historyEpoch;
    assert.equal(before, 4, "the two discovery rounds rendered into the detail pane");

    app.applyHistoryData({ type: "history_data", flow_id: "WT1", mode: "full", records: [] });

    assert.equal(bubbleCount(d), before, "the empty WS full push did NOT blank the pane");
    assert.equal(app.state.historyRecords, held, "records survive by identity");
    assert.equal(app.state.historyEpoch, epoch0,
      "a rejected frame takes no fresh epoch");
    assert.equal(app.state.historyProgress, "tokH",
      "the held progress token still pins the held records");
    assert.equal(app.state.historySignature, "sigH", "…as does the signature");
  });

  check("history detail: WS mode:full with MORE records still rebuilds the pane", () => {
    const held = [rec("round-1 question", 1), rec("round-1 answer", 2)];
    const d = seedOpenHistory(held, "tokH", "sigH");
    app.applyHistoryData({
      type: "history_data", flow_id: "WT1", mode: "full", records: twoRounds(),
    });
    assert.equal(bubbleCount(d), 4, "the grown full push rebuilt all four bubbles");
    assert.equal(app.state.historyProgress, null,
      "a real full push invalidates the held delta cursor");
  });

  await checkAsync("history detail: a zero-record REST full keeps the pane and its tokens", async () => {
    const held = twoRounds();
    const d = seedOpenHistory(held, "tokH", "sigH");
    const before = bubbleCount(d);
    const f = withFetch({
      delivery: "full", records: [], progress: "tokEMPTY", signature: "sigEMPTY",
    });
    try {
      // `incremental` is the reconnect refresh — the only open path that holds
      // records across the fetch (a fresh open clears them first, so its full is
      // a legitimate first paint, not a regression to zero).
      await app.openHistorySession("WT1", { incremental: true });
      assert.equal(bubbleCount(d), before, "the detail pane was NOT blanked");
      assert.equal(app.state.historyRecords, held, "the held records array is untouched");
      assert.equal(app.state.historyProgress, "tokH",
        "the rejected frame's cursor — which pins an EMPTY bundle — is not adopted");
      assert.equal(app.state.historySignature, "sigH", "…nor its signature");
    } finally {
      f.restore();
    }
  });

  await checkAsync("history detail: a GROWN REST full still heals the missing 2nd round", async () => {
    const held = [rec("round-1 question", 1), rec("round-1 answer", 2)];
    const d = seedOpenHistory(held, "tokH", "sigH");
    const f = withFetch({
      delivery: "full", records: twoRounds(), progress: "tok2", signature: "sig2",
    });
    try {
      await app.openHistorySession("WT1", { incremental: true });
      assert.deepEqual(bodies(app.state.historyRecords), [
        "round-1 question", "round-1 answer",
        "round-2 question", "round-2 answer",
      ], "the authoritative full snapshot healed the missing 2nd round");
      assert.equal(bubbleCount(d), 4, "all four bubbles are rendered");
      assert.equal(app.state.historyProgress, "tok2",
        "a genuine full adopts its fresh token");
      assert.equal(app.state.historySignature, "sig2", "…and its fresh signature");
    } finally {
      f.restore();
    }
  });

  // ------------------------------------------------------------------- //
  // The guard is scoped to full delivery: delta / not_modified are untouched.
  // ------------------------------------------------------------------- //
  check("the empty-full guard does not touch the delta / not_modified branches", () => {
    const held = twoRounds();
    const nm = app.mergeHistoryResponse(
      { delivery: "not_modified", records: [], progress: "tokN", signature: "sigN" },
      held, held,
    );
    assert.equal(nm.render, "noop");
    assert.ok(!nm.preserveTokens, "not_modified still adopts the refreshed token");
    assert.equal(nm.progress, "tokN");

    const delta = app.mergeHistoryResponse(
      { delivery: "delta", records: [rec("round-3 question", 5)], progress: "tokD", signature: "sigD" },
      held, held,
    );
    assert.equal(delta.render, "delta");
    assert.ok(!delta.preserveTokens);
    assert.equal(delta.records.length, 5, "the delta tail still appends");

    // An empty DELTA (server had nothing new) is a plain no-op that still
    // refreshes the token — it never went through the empty-full guard.
    const emptyDelta = app.mergeHistoryResponse(
      { delivery: "delta", records: [], progress: "tokD2", signature: "sigD2" },
      held, held,
    );
    assert.equal(emptyDelta.render, "noop");
    assert.ok(!emptyDelta.preserveTokens);
    assert.equal(emptyDelta.progress, "tokD2");
  });

  // Later modules in the shared harness render against the same singleton state;
  // drop our selections so neither consumer of `applyHistoryData` fires on their
  // frames. The DOM stub's `history-view` is left as we found it — CLASSLESS,
  // i.e. open — because later history tests drive the history-view consumer and
  // rely on `isHistoryOpen()` defaulting to true.
  app.state.selectedFlowId = null;
  app.state.selectedHistoryId = null;
}
