/*
 * Tail-first, step-block windowed history in the browser.
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerHistoryStepWindowTests({app, check, checkAsync,
 * findOne, findAll})`.
 *
 * The defect these pin (flow 20260829-224712_878b4fc9 — 222 MB of jsonl, 554 MiB
 * as a server bundle, against a 256 MiB cache budget): opening a flow fetched
 * the WHOLE conversation before rendering anything. The bundle did not fit, so
 * it was evicted, the next poll missed, the whole flow was pulled again, and the
 * console repeated `history delivery incomplete: re-reading flow=…` forever
 * while the pane showed only a prefix.
 *
 * The view now opens on the LAST HISTORY_WINDOW_STEP_BLOCKS step blocks and
 * pages backwards on demand. Coverage:
 *
 *   (W1) the first open asks for the tail window, not the whole flow
 *   (W2) a windowed reply renders only its blocks and offers the page-up control
 *   (W3) scrolling to the top (and clicking the control) loads the previous
 *        blocks ABOVE what is rendered, without moving the reader's viewport
 *   (W4) paging back reaches the first block, at which point the control is gone
 *   (W5) the completeness self-check is scoped to the LOADED blocks: an unloaded
 *        head is never reported missing and never triggers a repair request
 *   (W6) …while a real hole INSIDE the loaded window is still found
 *   (W7) live follow is unchanged: a delta appends, and the reader can page up
 *        and still return to the bottom to keep following
 *   (W8) a re-fetched tail window is idempotent — it never discards the earlier
 *        blocks the reader paged open
 *   (W9) a page-up must not adopt the reply's progress token
 *  (W10) a server that sends no `window` block leaves every path unwindowed
 *  (W11) a token-less window has a CHEAP steady-state poll: it echoes the
 *        window's `wsig`, an unchanged tail answers `not_modified` and repaints
 *        nothing, and the silent re-fetch is floored while the WS is live
 */
import assert from "node:assert/strict";

export async function registerHistoryStepWindowTests(ctx) {
  const { app, check, checkAsync, findOne } = ctx;

  // Six step blocks, three records each — the shape of a real flow, small
  // enough to assert on exhaustively.
  const BLOCKS = 6;
  const stepId = (i) => `0${i}_implement_h${i}`;
  const stepFile = (i) => `${stepId(i)}.jsonl`;
  const ALL_STEPS = Array.from({ length: BLOCKS }, (_, i) => stepId(i));
  const CURSOR = Object.fromEntries(ALL_STEPS.map((s) => [`${s}.jsonl`, 3]));

  const rec = (block, ordinal) => ({
    step_id: stepId(block),
    step_type: "implement",
    ordinal,
    // Timestamps ascend with (block, ordinal) so the ordinary renderer's
    // timestamp ordering agrees with the block ordering under test.
    message: {
      role: "assistant",
      content: `b${block}r${ordinal}`,
      timestamp: block * 10 + ordinal,
    },
  });
  const blockRecords = (block) => [0, 1, 2].map((o) => rec(block, o));
  const rangeRecords = (from, to) => {
    const out = [];
    for (let b = from; b < to; b++) out.push(...blockRecords(b));
    return out;
  };

  const windowMeta = (from, to, mode) => ({
    mode: mode || (from + (to - from) === BLOCKS ? "tail" : "before"),
    steps: ALL_STEPS,
    loaded: ALL_STEPS.slice(from, to),
    first_index: from,
    last_index: to - 1,
    has_earlier: from > 0,
    block_size: to - from,
    source: "cache",
  });

  const windowReply = (from, to, extra) => Object.assign({
    delivery: "window",
    records: rangeRecords(from, to),
    progress: null,
    signature: null,
    cursor: CURSOR,
    generation: null,
    pending: {},
    unfillable: {},
    incomplete: false,
    resync: false,
    window: windowMeta(from, to, to === BLOCKS ? "tail" : "before"),
  }, extra || {});

  const bodies = (records) => records.map(app.normalizeRecord).map((n) => n.content);
  // The bubble text with the default-collapsed "View raw" chip label trimmed
  // off, so these assertions are about the conversation content rather than
  // about the chip affordances the renderer also paints.
  const renderedBodies = (container) => container.children
    .filter((c) => c.__convIdx !== undefined)
    .map((c) => {
      const b = findOne(c, "conv-bubble");
      return (b ? b.textContent : c.textContent).replace(/View raw$/, "").trim();
    });

  const flush = async () => {
    for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
  };

  function withRouter(route) {
    const saved = globalThis.fetch;
    const calls = [];
    globalThis.fetch = (url) => {
      const u = String(url);
      calls.push(u);
      const payload = route(u);
      if (payload === undefined) {
        return Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) });
      }
      return Promise.resolve({
        ok: true, status: 200, json: () => Promise.resolve(payload),
      });
    };
    return { calls, restore: () => { globalThis.fetch = saved; } };
  }

  function resetFlowView(flowId) {
    app.state.selectedFlowId = flowId;
    app.state.selectedHistoryId = null;
    app.state.flowConversationRecords = [];
    app.state.flowConversationProgress = null;
    app.state.flowConversationSignature = null;
    app.state.flowConversationEpoch = 0;
    app.state.flowConversationInFlight = null;
    app.state.flowWindow = null;
    app.state.windowPageInFlight = {};
    app.state.backfillInFlight = {};
    app.state.backfillAttempts = {};
    app.state.backfillUnfillable = {};
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    c.__convLoadEarlier = null;
    return c;
  }

  const loadEarlierButton = () => {
    const row = document.getElementById("flow-conversation").__convLoadEarlier;
    return row ? row.__button : null;
  };

  // ------------------------------------------------------------------ //
  // pure helpers
  // ------------------------------------------------------------------ //

  check("(W0) historyWindowUrl builds the tail and the page-up forms", () => {
    assert.equal(app.historyWindowUrl("f1", 10), "/api/history/f1?window=10");
    assert.equal(
      app.historyWindowUrl("f1", 10, "03_implement_h3"),
      "/api/history/f1?window=10&before=03_implement_h3");
    // Flow ids are opaque; a URL-hostile one must not break the request.
    assert.ok(app.historyWindowUrl("a/b", 5).includes("a%2Fb"));
  });

  check("(W0) normalizeWindowMeta reads the server block, and null when absent", () => {
    assert.equal(app.normalizeWindowMeta(null), null);
    assert.equal(app.normalizeWindowMeta(undefined), null,
      "a reply with no window block must leave the client unwindowed");
    const m = app.normalizeWindowMeta(windowMeta(4, 6, "tail"));
    assert.equal(m.mode, "tail");
    assert.deepEqual(m.loaded, [stepId(4), stepId(5)]);
    assert.equal(m.firstIndex, 4);
    assert.equal(m.hasEarlier, true);
  });

  check("(W0) mergeWindowMeta only ever extends the loaded span backwards", () => {
    const opened = app.normalizeWindowMeta(windowMeta(4, 6, "tail"));
    const paged = app.normalizeWindowMeta(windowMeta(2, 4, "before"));
    const merged = app.mergeWindowMeta(opened, paged);
    assert.equal(merged.firstIndex, 2, "a page-up widens the span");
    // A later TAIL poll must not collapse the span the reader paged open.
    const polled = app.mergeWindowMeta(merged, app.normalizeWindowMeta(windowMeta(4, 6, "tail")));
    assert.equal(polled.firstIndex, 2,
      "a tail poll must not reset the reader back to the last blocks");
    assert.equal(polled.hasEarlier, true);
  });

  check("(W0) an EMPTY window reply cannot move the loaded boundary", () => {
    const held = app.normalizeWindowMeta(windowMeta(2, BLOCKS, "before"));
    // A page-up the server could not resolve: it loaded nothing and says so by
    // anchoring at the END of the block list it knows — the cached leg answers
    // from a bundle that may be a strict PREFIX of the flow mid-drain.
    const empty = app.normalizeWindowMeta({
      mode: "before", steps: ALL_STEPS.slice(0, 4), loaded: [],
      first_index: 4, last_index: 3, has_earlier: true,
      block_size: 2, source: "cache",
    });
    const merged = app.mergeWindowMeta(held, empty);
    assert.equal(merged.firstIndex, 2,
      "an empty reply must not claim blocks nobody loaded");
    assert.equal(merged.hasEarlier, true, "the reader's page-up must survive");
    assert.deepEqual(merged.steps, ALL_STEPS,
      "a partial block index must not shrink the reader's — blocks are append-only");
    assert.deepEqual(Array.from(app.windowLoadedStepIds(merged)),
      ALL_STEPS.slice(2), "the self-check stays scoped to what is loaded");

    // Defence in depth: even the pre-fix reply shape — empty, yet declaring
    // `first_index: 0` / `has_earlier: false` — cannot un-scope the view and
    // restart the backfill / full re-pull escalation.
    const legacy = app.normalizeWindowMeta({
      mode: "before", steps: ALL_STEPS, loaded: [],
      first_index: 0, last_index: -1, has_earlier: false, block_size: 2,
    });
    const survived = app.mergeWindowMeta(held, legacy);
    assert.equal(survived.firstIndex, 2);
    assert.notEqual(app.windowLoadedStepIds(survived), null,
      "an empty reply must never un-scope the completeness self-check");
  });

  check("(W0) windowLoadedStepIds scopes to the loaded blocks, null when whole", () => {
    const partial = app.normalizeWindowMeta(windowMeta(4, 6, "tail"));
    assert.deepEqual(
      Array.from(app.windowLoadedStepIds(partial)), [stepId(4), stepId(5)]);
    const whole = app.normalizeWindowMeta(windowMeta(0, 6, "tail"));
    assert.equal(app.windowLoadedStepIds(whole), null,
      "a view holding the whole flow checks everything, exactly as before");
    assert.equal(app.windowLoadedStepIds(null), null);
  });

  check("(W0) sortRecordsByBlock reproduces bundle (block, line) order", () => {
    const meta = app.normalizeWindowMeta(windowMeta(0, 6, "tail"));
    const scrambled = [rec(5, 1), rec(0, 2), rec(5, 0), rec(0, 0)];
    assert.deepEqual(
      bodies(app.sortRecordsByBlock(scrambled, meta)),
      ["b0r0", "b0r2", "b5r0", "b5r1"]);
    // A record whose block the index does not name sorts after the known ones
    // rather than being dropped or landing in the middle.
    const echo = { step_id: "not-a-block", message: { role: "user", content: "echo" } };
    assert.equal(
      bodies(app.sortRecordsByBlock([echo, rec(0, 0)], meta)).pop(), "echo");
  });

  // ------------------------------------------------------------------ //
  // (W1)(W2) the first open
  // ------------------------------------------------------------------ //

  await checkAsync("(W1) the first open asks for the TAIL WINDOW, not the whole flow", async () => {
    const flowId = "window-open-flow";
    resetFlowView(flowId);
    const stub = withRouter(() => windowReply(4, 6));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(stub.calls.length, 1);
    assert.equal(
      stub.calls[0], `/api/history/${flowId}?window=${app.HISTORY_WINDOW_STEP_BLOCKS}`,
      `the open must be windowed, not a whole-flow pull: ${stub.calls[0]}`);
  });

  await checkAsync("(W2) only the windowed blocks render, and the page-up control appears", async () => {
    const flowId = "window-render-flow";
    const c = resetFlowView(flowId);
    const stub = withRouter(() => windowReply(4, 6));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    assert.deepEqual(bodies(app.state.flowConversationRecords),
      ["b4r0", "b4r1", "b4r2", "b5r0", "b5r1", "b5r2"]);
    assert.deepEqual(renderedBodies(c),
      ["b4r0", "b4r1", "b4r2", "b5r0", "b5r1", "b5r2"]);
    // The reader lands on the END of the conversation — the last block is the
    // flow's last step, which is what "尾部起步" means.
    assert.equal(app.state.flowWindow.firstIndex, 4);
    const btn = loadEarlierButton();
    assert.ok(btn, "a window with earlier blocks must offer a way back");
    assert.equal(btn.textContent, "Load earlier steps");
    // Above every conversation node (the sticky-header float is an overlay
    // mounted at the scroller's literal first slot, not content).
    const kids = c.children;
    const firstBubble = kids.findIndex((k) => k.__convIdx !== undefined
      || (k.classList && k.classList.contains("history-step-header")));
    assert.ok(kids.indexOf(c.__convLoadEarlier) >= 0
      && kids.indexOf(c.__convLoadEarlier) < firstBubble,
      "the control sits above the earliest rendered step block");
  });

  // ------------------------------------------------------------------ //
  // (W3)(W4) paging backwards
  // ------------------------------------------------------------------ //

  await checkAsync("(W3) clicking the control prepends the previous blocks", async () => {
    const flowId = "window-page-flow";
    const c = resetFlowView(flowId);
    let stub = withRouter(() => windowReply(4, 6));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    stub = withRouter(() => windowReply(2, 4));
    try {
      loadEarlierButton().dispatch("click");
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(stub.calls.length, 1);
    assert.equal(
      stub.calls[0], `/api/history/${flowId}?window=2&before=${stepId(4)}`,
      `the page-up anchors on the EARLIEST loaded block: ${stub.calls[0]}`);
    // Prepended above what was already rendered, in flow order, nothing lost.
    assert.deepEqual(bodies(app.state.flowConversationRecords), [
      "b2r0", "b2r1", "b2r2", "b3r0", "b3r1", "b3r2",
      "b4r0", "b4r1", "b4r2", "b5r0", "b5r1", "b5r2",
    ]);
    assert.deepEqual(renderedBodies(c),
      bodies(app.state.flowConversationRecords));
    assert.equal(app.state.flowWindow.firstIndex, 2);
  });

  await checkAsync("(W3) a page-up in flight is never stacked by a scroll storm", async () => {
    const flowId = "window-stack-flow";
    resetFlowView(flowId);
    let stub = withRouter(() => windowReply(4, 6));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    stub = withRouter(() => windowReply(2, 4));
    try {
      // Ten scroll events at the top, as a real wheel gesture produces.
      const pending = [];
      for (let i = 0; i < 10; i++) pending.push(app.loadEarlierStepBlocks("flow", flowId));
      await Promise.all(pending);
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(stub.calls.length, 1,
      "a burst of scroll events must produce ONE page request, not ten");
  });

  await checkAsync("(W4) paging back reaches the first block and retires the control", async () => {
    const flowId = "window-walk-flow";
    const c = resetFlowView(flowId);
    let stub = withRouter(() => windowReply(4, 6));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    let pages = 0;
    while (app.state.flowWindow.hasEarlier) {
      const from = Math.max(0, app.state.flowWindow.firstIndex - 2);
      const to = app.state.flowWindow.firstIndex;
      stub = withRouter(() => windowReply(from, to));
      try {
        await app.loadEarlierStepBlocks("flow", flowId);
        await flush();
      } finally {
        stub.restore();
      }
      pages += 1;
      assert.ok(pages < 10, "paging did not converge on the first block");
    }
    // Every record of the flow, in flow order, exactly once.
    assert.deepEqual(bodies(app.state.flowConversationRecords),
      bodies(rangeRecords(0, BLOCKS)));
    assert.deepEqual(renderedBodies(c),
      bodies(rangeRecords(0, BLOCKS)));
    assert.equal(app.state.flowWindow.firstIndex, 0);
    assert.equal(loadEarlierButton(), null,
      "at the first block there is nothing earlier to offer");
    assert.equal(c.__convLoadEarlier, null);
  });

  // ------------------------------------------------------------------ //
  // (W5)(W6) the window-aware completeness self-check
  // ------------------------------------------------------------------ //

  check("(W5) findMissingOrdinals ignores cursor entries outside the loaded window", () => {
    const held = rangeRecords(4, 6);
    const loaded = new Set([stepId(4), stepId(5)]);
    // Unscoped, the whole unloaded head reads as missing — which is exactly the
    // false hole that drove the endless re-read loop.
    const unscoped = app.findMissingOrdinals(held, CURSOR);
    assert.ok(Object.keys(unscoped.missing).length >= 4,
      "without the scope the unloaded head looks like a hole");
    const scoped = app.findMissingOrdinals(held, CURSOR, undefined, undefined, loaded);
    assert.deepEqual(scoped.missing, {},
      "a block the reader has not paged to is NOT a hole");
    assert.equal(scoped.surplus, false);
    assert.equal(scoped.unkeyable, false);
  });

  check("(W6) a REAL hole inside the loaded window is still found", () => {
    const held = [rec(4, 0), rec(4, 2), ...blockRecords(5)];
    const loaded = new Set([stepId(4), stepId(5)]);
    const scoped = app.findMissingOrdinals(held, CURSOR, undefined, undefined, loaded);
    assert.deepEqual(scoped.missing, { [stepId(4)]: [1] },
      "windowing must not blind the self-check inside the window it does hold");
  });

  await checkAsync("(W5) an unloaded head provokes NO repair request", async () => {
    const flowId = "window-selfcheck-flow";
    resetFlowView(flowId);
    let stub = withRouter(() => windowReply(4, 6));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    // The open itself must not have chased the head: one request, no backfill,
    // no `missing=` and no token-less full re-pull.
    assert.equal(stub.calls.length, 1, `extra repair requests: ${stub.calls.join(" | ")}`);

    // …and neither does the next idle poll, whose reply still declares the whole
    // flow's cursor while the view holds only the tail.
    stub = withRouter((url) => {
      assert.ok(!url.includes("missing="),
        `the unloaded head must never be backfilled: ${url}`);
      return windowReply(4, 6);
    });
    try {
      await app.loadFlowConversation(flowId, { silent: true });
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(stub.calls.length, 1);
    assert.deepEqual(app.state.backfillUnfillable, {});
  });

  await checkAsync("(W5) a windowed reply declares the bundle settled — no incomplete loop", async () => {
    const flowId = "window-incomplete-flow";
    resetFlowView(flowId);
    const stub = withRouter(() => windowReply(4, 6));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    // `incomplete:false` on a windowed reply is a real statement, so the
    // interrupted-delivery repair — the source of the
    // `history delivery incomplete: re-reading flow=…` storm — is disarmed.
    assert.equal(app.declaredBundleCompleteness("flow", flowId), true);
    assert.equal(app.state.incompleteRecoveryTimers[`flow|${flowId}`], undefined);
  });

  // ------------------------------------------------------------------ //
  // (W7)(W8) live follow
  // ------------------------------------------------------------------ //

  await checkAsync("(W7) a delta still appends to a windowed view, and follows the bottom", async () => {
    const flowId = "window-live-flow";
    const c = resetFlowView(flowId);
    let stub = withRouter(() => windowReply(4, 6, {
      // A bundle-backed window DOES carry a token — its next poll is an ordinary
      // append-only delta, which is what keeps live follow unchanged.
      progress: "tok-1", signature: "sig-1", generation: 3,
    }));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(app.state.flowConversationProgress, "tok-1");

    stub = withRouter((url) => {
      assert.ok(url.includes("after=tok-1"),
        `a windowed view with a token polls for a DELTA, not a fresh window: ${url}`);
      return {
        delivery: "delta",
        records: [rec(5, 3)],
        progress: "tok-2", signature: "sig-2", cursor: CURSOR,
        generation: 3, pending: {}, incomplete: false,
      };
    });
    try {
      await app.loadFlowConversation(flowId, { silent: true });
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(bodies(app.state.flowConversationRecords).pop(), "b5r3",
      "a live append still lands at the tail of a windowed view");
    assert.equal(renderedBodies(c).pop(), "b5r3");
    assert.equal(app.state.flowWindow.firstIndex, 4,
      "a delta says nothing about the window, so the loaded span stands");
  });

  await checkAsync("(W7) after paging up the reader can return to the bottom and keep following", async () => {
    const flowId = "window-follow-flow";
    const c = resetFlowView(flowId);
    let stub = withRouter(() => windowReply(4, 6, {
      progress: "tok-1", signature: "sig-1", generation: 3,
    }));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    stub = withRouter(() => windowReply(2, 4));
    try {
      await app.loadEarlierStepBlocks("flow", flowId);
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(app.state.flowWindow.firstIndex, 2);
    // Back to the bottom: the follow intent is re-armed and the next append is
    // still placed at the tail of the (now longer) window.
    app.scrollFlowConversationToBottom();
    assert.equal(app.state.flowConversationFollowingBottom, true);
    app.applyHistoryData({
      flow_id: flowId, mode: "append", records: [rec(5, 3)],
      cursor: CURSOR, signature: "sig-2", pending: {},
    });
    await flush();
    assert.equal(bodies(app.state.flowConversationRecords).pop(), "b5r3");
    assert.equal(renderedBodies(c).pop(), "b5r3");
    // …and the earlier blocks the reader paged open are still there.
    assert.equal(bodies(app.state.flowConversationRecords)[0], "b2r0");
  });

  await checkAsync("(W8) a re-fetched tail window never discards the paged-open head", async () => {
    const flowId = "window-idempotent-flow";
    resetFlowView(flowId);
    let stub = withRouter(() => windowReply(4, 6));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    stub = withRouter(() => windowReply(2, 4));
    try {
      await app.loadEarlierStepBlocks("flow", flowId);
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(app.state.flowConversationRecords.length, 12);
    // A token-less (daemon-served) windowed view re-asks for the tail on every
    // poll. That must merge idempotently — the earlier blocks stay.
    stub = withRouter((url) => {
      assert.ok(url.includes("window="),
        `a token-less windowed view must re-ask for the WINDOW, never the whole flow: ${url}`);
      return windowReply(4, 6);
    });
    try {
      await app.loadFlowConversation(flowId, { silent: true });
      await flush();
    } finally {
      stub.restore();
    }
    assert.deepEqual(bodies(app.state.flowConversationRecords),
      bodies(rangeRecords(2, 6)),
      "the tail poll collapsed the reader back onto the last blocks");
    assert.equal(app.state.flowWindow.firstIndex, 2);
  });

  await checkAsync("(W3) a page-up the server cannot resolve leaves the view intact", async () => {
    // The server-side shape: the reader's anchor names no block of the CACHED
    // bundle (which mid-drain holds only the flow's leading blocks), so the
    // reply is empty — but NOT "you have reached the first block".
    const flowId = "window-unresolvable-flow";
    resetFlowView(flowId);
    let stub = withRouter(() => windowReply(4, BLOCKS));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    stub = withRouter(() => Object.assign(windowReply(4, BLOCKS), {
      records: [],
      window: {
        mode: "before", steps: ALL_STEPS.slice(0, 3), loaded: [],
        first_index: 3, last_index: 2, has_earlier: true,
        block_size: 2, source: "cache",
      },
    }));
    try {
      await app.loadEarlierStepBlocks("flow", flowId);
      await flush();
    } finally {
      stub.restore();
    }
    assert.deepEqual(bodies(app.state.flowConversationRecords),
      bodies(rangeRecords(4, BLOCKS)), "an empty page-up must not disturb the pane");
    assert.equal(app.state.flowWindow.firstIndex, 4);
    assert.ok(loadEarlierButton(),
      "the 'load earlier steps' affordance must survive an unresolvable page-up");
    // …and nothing escalated: no backfill, no full re-pull.
    assert.deepEqual(app.state.backfillUnfillable, {});
    assert.deepEqual(app.state.backfillInFlight, {});
  });

  await checkAsync("(W3) scrolling to the top of the pane pages the next block in", async () => {
    const flowId = "window-scroll-flow";
    const c = resetFlowView(flowId);
    let stub = withRouter(() => windowReply(4, 6));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    // Parked well below the trigger band: nothing loads.
    c.scrollTop = 5000;
    stub = withRouter(() => {
      throw new Error("a reader far from the top must not page");
    });
    try {
      app.maybeLoadEarlierOnScroll("flow");
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(stub.calls.length, 0);

    // …and at the top it does.
    c.scrollTop = 0;
    stub = withRouter(() => windowReply(2, 4));
    try {
      app.maybeLoadEarlierOnScroll("flow");
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(stub.calls.length, 1);
    assert.equal(app.state.flowWindow.firstIndex, 2);
    assert.equal(bodies(app.state.flowConversationRecords)[0], "b2r0");
  });

  // ------------------------------------------------------------------ //
  // the History view rides the same machinery
  // ------------------------------------------------------------------ //

  await checkAsync("(W2/W3) the History session view opens windowed and pages up too", async () => {
    const flowId = "window-history-flow";
    app.state.selectedFlowId = null;
    app.state.selectedHistoryId = null;
    app.state.historyRecords = [];
    app.state.historyProgress = null;
    app.state.historySignature = null;
    app.state.historyWindow = null;
    app.state.windowPageInFlight = {};
    app.state.historySessions = [];
    const detail = document.getElementById("history-detail");
    detail.innerHTML = "";
    detail.__convState = null;
    detail.__convLoadEarlier = null;

    let stub = withRouter(() => windowReply(4, 6));
    try {
      await app.openHistorySession(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    const opened = stub.calls.find((u) => u.startsWith(`/api/history/${flowId}`));
    assert.equal(opened, `/api/history/${flowId}?window=${app.HISTORY_WINDOW_STEP_BLOCKS}`,
      `the History view opens on the tail window too: ${opened}`);
    assert.equal(app.state.historyWindow.firstIndex, 4);
    assert.deepEqual(bodies(app.state.historyRecords),
      bodies(rangeRecords(4, 6)));
    assert.ok(detail.__convLoadEarlier, "the History pane offers the page-up control");

    stub = withRouter(() => windowReply(2, 4));
    try {
      await app.loadEarlierStepBlocks("history", flowId);
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(
      stub.calls[0], `/api/history/${flowId}?window=2&before=${stepId(4)}`);
    assert.deepEqual(bodies(app.state.historyRecords), bodies(rangeRecords(2, 6)));
    app.state.selectedHistoryId = null;
  });

  check("(W9) a page-up reply's token is NOT adopted", () => {
    const held = rangeRecords(4, 6);
    const out = app.mergeHistoryResponse(
      windowReply(2, 4, { progress: "tok-newer", signature: "sig-newer" }),
      held, held);
    // The page-up describes a bundle that may have grown since the held token
    // was minted; adopting its token would jump the cursor past records the view
    // never received.
    assert.equal(out.preserveTokens, true);
    assert.equal(out.progress, null);
    assert.equal(out.render, "full");
    assert.deepEqual(bodies(out.records), bodies(rangeRecords(2, 6)));
  });

  // ------------------------------------------------------------------ //
  // (W10) backward compatibility
  // ------------------------------------------------------------------ //

  await checkAsync("(W10) a server that sends no window block leaves the view unwindowed", async () => {
    const flowId = "window-legacy-flow";
    const c = resetFlowView(flowId);
    const stub = withRouter(() => ({
      // An older server ignores the `window` parameter and answers with the
      // whole flow, exactly as it always did.
      delivery: "full",
      records: rangeRecords(0, BLOCKS),
      progress: "tok-1", signature: "sig-1", cursor: CURSOR,
      generation: 1, pending: {}, incomplete: false,
    }));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(app.state.flowWindow, null,
      "no window block means windowing stays off — every path behaves as before");
    assert.equal(loadEarlierButton(), null);
    assert.deepEqual(bodies(app.state.flowConversationRecords),
      bodies(rangeRecords(0, BLOCKS)));
    assert.equal(renderedBodies(c).length, BLOCKS * 3);
    // …and the unscoped self-check still sees a complete view, so no repair.
    assert.deepEqual(app.state.backfillUnfillable, {});
  });

  check("(W10) findMissingOrdinals with no scope is byte-for-byte its old self", () => {
    const held = [rec(0, 0), rec(0, 2)];
    const cursor = { [stepFile(0)]: 3 };
    assert.deepEqual(app.findMissingOrdinals(held, cursor).missing,
      { [stepId(0)]: [1] });
    assert.deepEqual(
      app.findMissingOrdinals(held, cursor, undefined, undefined, null).missing,
      { [stepId(0)]: [1] });
  });

  // ------------------------------------------------------------------ //
  // (W11) the steady-state cost of following a token-less window
  // ------------------------------------------------------------------ //
  //
  // A window served straight from the daemon builds no bundle, so the view holds
  // NO progress token and its 3 s self-heal can only re-ask for the tail. Left
  // unconditional that is a full re-read on the daemon and a full re-transfer to
  // the browser on every tick, for as long as the flow is watched — which is the
  // very cost windowing was introduced to remove. Two things bound it: the
  // window's `signature`, echoed as `wsig` so an unchanged tail answers
  // `not_modified`, and a floor under how often the silent re-fetch may run at
  // all while the WS append stream is live.

  const daemonWindowMeta = (from, to, signature) => Object.assign(
    windowMeta(from, to, "tail"), { source: "daemon", signature });

  check("(W11) the window URL carries the probe only when one is held", () => {
    assert.equal(app.historyWindowUrl("f1", 10), "/api/history/f1?window=10");
    assert.equal(app.historyWindowUrl("f1", 10, "", "sig-1"),
      "/api/history/f1?window=10&wsig=sig-1");
    // A page-up is a one-shot read, not a poll, but the parameter must still
    // compose rather than replace `before`.
    assert.ok(app.historyWindowUrl("f1", 10, stepId(3), "sig-1")
      .includes("before=" + stepId(3)));
  });

  check("(W11) only a TAIL reply refreshes the probe", () => {
    const opened = app.normalizeWindowMeta(daemonWindowMeta(4, 6, "sig-1"));
    assert.equal(opened.signature, "sig-1");
    // The server folds the request shape into the signature, so a page-up's
    // would never match the tail read the poll issues; adopting it would
    // silently retire the conditional poll.
    const paged = app.normalizeWindowMeta(Object.assign(
      windowMeta(2, 4, "before"), { source: "daemon", signature: "sig-page" }));
    const merged = app.mergeWindowMeta(opened, paged);
    assert.equal(merged.signature, "sig-1",
      "a page-up must not overwrite the tail probe");
    const polled = app.mergeWindowMeta(
      merged, app.normalizeWindowMeta(daemonWindowMeta(4, 6, "sig-2")));
    assert.equal(polled.signature, "sig-2",
      "a fresh tail reply hands over the probe for the next poll");
    assert.equal(polled.firstIndex, 2, "…without collapsing the paged-open span");
  });

  check("(W11) a reply with no signature leaves the view polling unconditionally", () => {
    // An older server (or the cached leg, which hands back a real token instead)
    // carries none, and the client must simply keep its prior behaviour.
    const meta = app.normalizeWindowMeta(windowMeta(4, 6, "tail"));
    assert.equal(meta.signature, "");
    assert.equal(app.historyWindowUrl("f1", 2, "", meta.signature),
      "/api/history/f1?window=2");
  });

  await checkAsync("(W11) a silent poll of a token-less window echoes its probe", async () => {
    const flowId = "window-probe-flow";
    resetFlowView(flowId);
    const stub = withRouter((u) => (u.includes("wsig=")
      // The unchanged answer: not one record, and no window block — the view
      // keeps the window and the block index it already holds.
      ? { delivery: "not_modified", records: [], progress: null,
        signature: null, incomplete: false }
      : windowReply(4, BLOCKS, {
        window: daemonWindowMeta(4, BLOCKS, "sig-1"),
      })));
    try {
      await app.loadFlowConversation(flowId);
      await flush();
      assert.equal(app.state.flowWindow.signature, "sig-1");
      assert.equal(app.state.flowConversationProgress, null,
        "a relayed window mints no token — the probe is all the view has");
      const before = app.state.flowConversationRecords;
      // The self-heal path, with the floor cleared so this is purely about the
      // request it makes.
      app.state.flowWindowTailPolledAt = {};
      await app.loadFlowConversation(flowId, { silent: true });
      await flush();
      assert.ok(stub.calls[stub.calls.length - 1].includes("wsig=sig-1"),
        "the poll must be conditional: " + stub.calls[stub.calls.length - 1]);
      // `not_modified` repaints nothing and — critically — does not un-window
      // the view or retire the page-up the reader still needs.
      assert.equal(app.state.flowConversationRecords, before);
      assert.equal(app.state.flowWindow.firstIndex, 4);
      assert.equal(app.state.flowWindow.signature, "sig-1");
      assert.ok(loadEarlierButton(), "the page-up must survive an unchanged poll");
    } finally {
      stub.restore();
    }
  });

  check("(W11) the tail re-fetch floor applies only to a token-less window", () => {
    const flowId = "window-floor-flow";
    const savedStale = app.state.connStale;
    try {
      app.state.selectedFlowId = flowId;
      app.state.flowWindow = app.normalizeWindowMeta(
        daemonWindowMeta(4, BLOCKS, "sig-1"));
      app.state.flowConversationRecords = rangeRecords(4, BLOCKS);
      app.state.flowConversationProgress = null;
      app.state.flowWindowTailPolledAt = {};
      app.state.connStale = false;

      // The first tick is always allowed — and it arms the floor.
      assert.equal(app.windowTailRefetchThrottled(flowId), false);
      assert.equal(app.windowTailRefetchThrottled(flowId), true,
        "a second tick 3 s later must not re-ship the whole window");

      // A view that holds a token is answered `not_modified` for a few hundred
      // bytes, so it is never floored.
      app.state.flowConversationProgress = "tok-1";
      assert.equal(app.windowTailRefetchThrottled(flowId), false);
      app.state.flowConversationProgress = null;

      // With the WS down the poll is the only delivery path left; the re-fetch
      // cost is then the price of not freezing.
      app.state.connStale = true;
      assert.equal(app.windowTailRefetchThrottled(flowId), false);
      app.state.connStale = false;

      // An unwindowed view keeps the plain 3 s cadence it always had.
      app.state.flowWindowTailPolledAt = { [flowId]: Date.now() };
      app.state.flowWindow = null;
      assert.equal(app.windowTailRefetchThrottled(flowId), false);
      app.state.flowWindow = app.normalizeWindowMeta(
        daemonWindowMeta(4, BLOCKS, "sig-1"));

      // A view holding NO records must load, floor or not — otherwise a failed
      // open would sit empty for the whole interval.
      app.state.flowConversationRecords = [];
      assert.equal(app.windowTailRefetchThrottled(flowId), false);
      app.state.flowConversationRecords = rangeRecords(4, BLOCKS);

      // …and the floor really does expire.
      app.state.flowWindowTailPolledAt[flowId] =
        Date.now() - app.WINDOW_TAIL_REFETCH_MIN_MS - 1;
      assert.equal(app.windowTailRefetchThrottled(flowId), false);
    } finally {
      app.state.connStale = savedStale;
      app.state.flowWindowTailPolledAt = {};
    }
  });

  await checkAsync("(W11) the periodic self-heal honours the floor", async () => {
    const flowId = "window-selfheal-flow";
    const savedMachines = app.state.machines;
    const savedPeriodic = app.state.periodicSnapshotActive;
    const stub = withRouter(() => ({
      delivery: "not_modified", records: [], progress: null,
      signature: null, incomplete: false,
    }));
    try {
      app.state.machines = [{ flows: [{ flow_id: flowId, status: "running" }] }];
      app.state.periodicSnapshotActive = true;
      app.state.selectedFlowId = flowId;
      app.state.selectedHistoryId = null;
      app.state.flowConversationRecords = rangeRecords(4, BLOCKS);
      app.state.flowConversationProgress = null;
      app.state.flowWindow = app.normalizeWindowMeta(
        daemonWindowMeta(4, BLOCKS, "sig-1"));
      app.state.flowWindowTailPolledAt = {};
      app.state.connStale = false;
      app.cancelIncompleteRecoveryForView("flow");
      delete app.state.bundleCompleteness["flow|" + flowId];

      for (let i = 0; i < 5; i++) {
        app.selfHealFlowConversation();
        await flush();
      }
      // Five ticks of the 3 s poll, ONE window re-fetch: the WS append stream is
      // what follows a live flow, not a repeated whole-window transfer.
      assert.equal(stub.calls.length, 1,
        "the floor must bound the follow cost: " + stub.calls.length);
      assert.ok(stub.calls[0].includes("wsig=sig-1"));
    } finally {
      stub.restore();
      app.state.machines = savedMachines;
      app.state.periodicSnapshotActive = savedPeriodic;
      app.state.flowWindowTailPolledAt = {};
      app.cancelIncompleteRecoveryForView("flow");
    }
  });
}
