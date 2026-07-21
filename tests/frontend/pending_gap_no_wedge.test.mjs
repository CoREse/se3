/*
 * Pending-gap self-check: the panel must NOT wedge on a cursor gap the server
 * declares is still streaming from the daemon, and a stale/rotated signed cursor
 * must resync rather than bare-retry.
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerPendingGapNoWedgeTests({app, check, checkAsync,
 * findOne})`.
 *
 * Live defect these pin (flow 20260720-163316_2df2d504, discovery, frozen at the
 * implement step): the daemon→server history push was all-or-nothing, so on a
 * host doing ~40s clean reconnects the multi-MB implement backlog never fit a
 * single connection window — every window's batch was voided, the server cursor
 * DECLARED records it had not received, and the frozen frontend, finding that
 * cursor gap, drained its backfill+full budget and printed
 * "gap persists … giving up" while the same frame carried `unfillable={}`.
 *
 * The server now names that trailing declared-but-undelivered window `pending`
 * (still coming) as opposed to `unfillable` (a proven hole). Defect B here is the
 * frontend consumer: a pending gap must neither backfill (nothing to slice) nor
 * enter the giving-up terminal state nor wedge — the already-rendered records
 * stand, the self-check stays armed, and the daemon's later delivery heals it
 * with no user action. Defect C: a `resync:true` reply (a stale cursor the server
 * could not bind — which per the G4 forensic finding can NEVER 401, since
 * require_owner is cookie-only) makes the client adopt the authoritative token
 * and shed the dead generation's repair state, once, without a bare-retry loop.
 */
import assert from "node:assert/strict";

export async function registerPendingGapNoWedgeTests(ctx) {
  const { app, check, checkAsync, findOne } = ctx;

  const STEP = "06_implement_398863d6";
  const FILE = `${STEP}.jsonl`;

  const rec = (ordinal, content, ts, stepId) => ({
    step_id: stepId || STEP,
    step_type: "implement",
    ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });
  const bodies = (records) => records.map(app.normalizeRecord).map((n) => n.content);
  const renderedBodies = (container) => container.children
    .filter((c) => c.__convIdx !== undefined)
    .map((c) => { const b = findOne(c, "conv-bubble"); return b ? b.textContent : c.textContent; });

  function resetBackfillState() {
    app.state.backfillInFlight = {};
    app.state.backfillAttempts = {};
    app.state.backfillUnfillable = {};
  }

  function seedHeadlessFlow(flowId, records) {
    resetBackfillState();
    app.state.selectedFlowId = flowId;
    app.state.selectedHistoryId = null;
    app.state.flowConversationRecords = records;
    app.state.flowConversationProgress = "tok-gen1";
    app.state.flowConversationSignature = "sig-gen1";
    app.state.flowConversationEpoch = 0;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    app.renderConversation(c, records, false);
    return c;
  }

  function withRouter(route) {
    const saved = globalThis.fetch;
    const calls = [];
    globalThis.fetch = (url) => {
      const u = String(url);
      calls.push(u);
      const payload = route(u);
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve(payload),
      });
    };
    return { calls, restore: () => { globalThis.fetch = saved; } };
  }

  const flush = async () => {
    for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
  };

  // ---------------------------------------------------------------- //
  // 1. findMissingOrdinals — pending numbers drop out of `missing`.
  // ---------------------------------------------------------------- //
  check("findMissingOrdinals: a pending trailing gap is NOT missing (pendingGap set)", () => {
    // Cursor declares 3, client holds 0..1, and the server says ordinal 2 is
    // still streaming from the daemon — so it is neither a hole to backfill nor
    // a reason to give up.
    const probe = app.findMissingOrdinals(
      [rec(0, "a", 1), rec(1, "b", 2)], { [FILE]: 3 }, undefined, { [STEP]: [2] });
    assert.deepEqual(probe.missing, {},
      "a pending number must not appear in `missing` — asking for it serves nothing");
    assert.equal(probe.pendingGap, true, "the caller must be told a pending gap remains");
    assert.equal(probe.surplus, false);
  });

  check("findMissingOrdinals: a REAL interior hole survives while a pending tail is excluded", () => {
    // Holds only ordinal 1: ordinal 0 is a genuine interior hole (backfill it),
    // ordinal 2 is pending (leave it to the daemon).
    const probe = app.findMissingOrdinals(
      [rec(1, "b", 2)], { [FILE]: 3 }, undefined, { [STEP]: [2] });
    assert.deepEqual(probe.missing, { [STEP]: [0] },
      "only the real hole is named; the pending number is filtered out");
    assert.equal(probe.pendingGap, true);
  });

  check("findMissingOrdinals: with no pending map the pre-existing gap set is unchanged", () => {
    const probe = app.findMissingOrdinals([rec(1, "b", 2)], { [FILE]: 3 });
    assert.deepEqual(probe.missing, { [STEP]: [0, 2] },
      "omitting `pending` keeps every unheld number a candidate (backward compatible)");
    assert.equal(probe.pendingGap, false);
  });

  // ---------------------------------------------------------------- //
  // 2. mergeHistoryResponse surfaces `pending` (and `resync`) to the caller.
  // ---------------------------------------------------------------- //
  check("mergeHistoryResponse: the pending window rides on a not_modified reply", () => {
    const held = [rec(0, "a", 1), rec(1, "b", 2)];
    const out = app.mergeHistoryResponse(
      { delivery: "not_modified", records: [], progress: "tok", signature: "sig",
        cursor: { [FILE]: 3 }, pending: { [STEP]: [2] } },
      held, held);
    assert.equal(out.render, "noop");
    assert.deepEqual(out.pending, { [STEP]: [2] },
      "the caller needs pending to tell 'still coming' apart from a hole");
    assert.equal(out.resync, false, "a normal not_modified is not a resync");
  });

  check("mergeHistoryResponse: a resync full is flagged; a #287-rejected empty full is NOT", () => {
    const held = [rec(0, "a", 1), rec(1, "b", 2)];
    const good = app.mergeHistoryResponse(
      { delivery: "full", records: [rec(0, "a", 1), rec(1, "b", 2)],
        progress: "tok-fresh", signature: "sig-fresh", cursor: { [FILE]: 2 },
        resync: true, generation: 7 },
      held, held);
    assert.equal(good.resync, true);
    assert.equal(good.progress, "tok-fresh", "the authoritative token is adopted");
    // An empty full carrying resync must not resync onto null tokens.
    const rejected = app.mergeHistoryResponse(
      { delivery: "full", records: [], progress: "tok", signature: "sig",
        cursor: {}, resync: true },
      held, held);
    assert.equal(rejected.preserveTokens, true);
    assert.equal(rejected.resync, false,
      "a wholesale-rejected frame must never drive a resync onto its null token");
  });

  // ---------------------------------------------------------------- //
  // 3. THE DEFECT B PANEL, end to end: a pending gap does not wedge.
  // ---------------------------------------------------------------- //
  await checkAsync(
    "pending gap: no backfill, no giving-up, already-rendered records stand",
    async () => {
      const flowId = "20260720-163316_2df2d504";
      const c = seedHeadlessFlow(flowId, [rec(0, "impl start", 1), rec(1, "impl mid", 2)]);
      // The frozen live shape: the server's cursor declares a record (ordinal 2)
      // it has NOT yet received from the daemon, so every idle poll answers
      // not_modified while naming that trailing window `pending`.
      const stub = withRouter(() => ({
        delivery: "not_modified", records: [], progress: "tok-gen1",
        signature: "sig-gen1", cursor: { [FILE]: 3 }, pending: { [STEP]: [2] },
      }));
      try {
        await app.loadFlowConversation(flowId, { silent: true });
        await flush();
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 1,
        "a pending gap provokes NO repair request — no backfill, no full re-pull, no request storm");
      // The panel keeps rendering what it already holds — it is NOT wedged blank.
      const shown = renderedBodies(c);
      assert.equal(shown.length, 2, "the already-rendered records still show");
      assert.ok(shown[0].includes("impl start") && shown[1].includes("impl mid"));
      // The self-check stays armed: nothing was retired, and the budget is clean
      // so a later REAL hole is still repairable.
      assert.deepEqual(app.state.backfillUnfillable, {},
        "a pending number is never retired as unfillable");
    });

  await checkAsync(
    "pending gap heals automatically once the daemon delivers the tail — no user action",
    async () => {
      const flowId = "pending-heal-flow";
      const c = seedHeadlessFlow(flowId, [rec(0, "impl start", 1), rec(1, "impl mid", 2)]);
      // First: the pending poll — panel holds 2, cursor declares 3, ord 2 pending.
      const idle = withRouter(() => ({
        delivery: "not_modified", records: [], progress: "tok-gen1",
        signature: "sig-gen1", cursor: { [FILE]: 3 }, pending: { [STEP]: [2] },
      }));
      try {
        await app.loadFlowConversation(flowId, { silent: true });
        await flush();
      } finally {
        idle.restore();
      }
      assert.equal(idle.calls.length, 1, "no repair while the tail is still pending");

      // Then: the daemon's next window delivers ordinal 2 as a WS append, and the
      // frame's cursor now DECLARES nothing pending (records caught up). A healthy
      // self-check must fire no request.
      const healed = withRouter(() => {
        throw new Error("a healed pending gap must issue NO repair request");
      });
      try {
        app.applyHistoryData({
          flow_id: flowId, mode: "append", records: [rec(2, "impl tail", 3)],
          cursor: { [FILE]: 3 }, signature: "sig-gen1", pending: {},
        });
        await flush();
      } finally {
        healed.restore();
      }
      assert.equal(healed.calls.length, 0,
        "the pending window closed by a plain append — no backfill needed");
      const shown = renderedBodies(c);
      assert.equal(shown.length, 3, "the tail the daemon finally delivered is now on screen");
      assert.ok(shown[2].includes("impl tail"));
      assert.deepEqual(bodies(app.state.flowConversationRecords),
        ["impl start", "impl mid", "impl tail"]);
    });

  await checkAsync(
    "mixed gap: the real interior hole is backfilled, the pending tail is left alone",
    async () => {
      const flowId = "mixed-gap-flow";
      // Holds ordinal 1 only: ord 0 is a real hole, ord 2 is pending.
      seedHeadlessFlow(flowId, [rec(1, "impl mid", 2)]);
      const stub = withRouter((url) => {
        if (url.includes("missing=")) {
          return { delivery: "backfill", records: [rec(0, "impl start", 1)],
                   progress: "tok-gen1", signature: "sig-gen1",
                   cursor: { [FILE]: 3 }, pending: { [STEP]: [2] }, unfillable: {} };
        }
        return { delivery: "not_modified", records: [], progress: "tok-gen1",
                 signature: "sig-gen1", cursor: { [FILE]: 3 }, pending: { [STEP]: [2] } };
      });
      try {
        await app.loadFlowConversation(flowId, { silent: true });
        await flush();
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 2, "exactly one backfill for the real hole");
      assert.equal(
        stub.calls[1],
        `/api/history/${flowId}?after=tok-gen1&sig=sig-gen1`
          + `&missing=${encodeURIComponent(`${STEP}:0`)}`,
        `the backfill names ONLY ordinal 0 — the pending ordinal 2 is excluded: ${stub.calls[1]}`);
    });

  // ---------------------------------------------------------------- //
  // 4. DEFECT C: a resync reply re-syncs the cursor rather than bare-retrying.
  // ---------------------------------------------------------------- //
  await checkAsync(
    "resync: a stale signed cursor adopts the authoritative token and sheds dead repair state",
    async () => {
      const flowId = "resync-flow";
      seedHeadlessFlow(flowId, [rec(0, "impl start", 1), rec(1, "impl mid", 2)]);
      // Pretend the held cursor is a now-stale one (daemon reconnected, bundle
      // rotated) and there is spent repair budget bound to the DEAD generation.
      app.state.flowConversationProgress = "tok-stale";
      app.state.flowConversationSignature = "sig-stale";
      app.state.backfillAttempts["flow|" + flowId] =
        { generation: 1, backfills: 2, full: 1, unkeyableFull: true };
      app.state.backfillUnfillable["flow|" + flowId] =
        { generation: 1, map: { [STEP]: [0] } };
      // The server could not bind tok-stale, so it answers a recoverable full
      // tagged resync:true with the CURRENT bundle's authoritative token. This is
      // a 200, never a 401 (require_owner is cookie-only — the G4 finding).
      const stub = withRouter(() => ({
        delivery: "full",
        records: [rec(0, "impl start", 1), rec(1, "impl mid", 2)],
        progress: "tok-fresh", signature: "sig-fresh", cursor: { [FILE]: 2 },
        generation: 9, resync: true, pending: {},
      }));
      try {
        await app.loadFlowConversation(flowId, { silent: true });
        await flush();
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 1,
        "one recoverable full — NOT a bare-retry loop of the dead cursor");
      assert.equal(app.state.flowConversationProgress, "tok-fresh",
        "the authoritative progress token is adopted");
      assert.equal(app.state.flowConversationSignature, "sig-fresh",
        "the authoritative signature is adopted");
      // The repair state bound to the superseded generation is gone, so the fresh
      // bundle's self-check starts clean.
      assert.equal(app.state.backfillAttempts["flow|" + flowId], undefined,
        "the dead generation's spent budget is shed on resync");
      assert.equal(app.state.backfillUnfillable["flow|" + flowId], undefined,
        "the dead generation's retired set is shed on resync");
    });

  await checkAsync(
    "resync: the very next poll echoes the fresh cursor and does NOT resync again",
    async () => {
      const flowId = "resync-once-flow";
      seedHeadlessFlow(flowId, [rec(0, "impl start", 1), rec(1, "impl mid", 2)]);
      app.state.flowConversationProgress = "tok-stale";
      app.state.flowConversationSignature = "sig-stale";
      let served = 0;
      const stub = withRouter(() => {
        served += 1;
        if (served === 1) {
          return { delivery: "full",
                   records: [rec(0, "impl start", 1), rec(1, "impl mid", 2)],
                   progress: "tok-fresh", signature: "sig-fresh",
                   cursor: { [FILE]: 2 }, generation: 9, resync: true, pending: {} };
        }
        // The fresh cursor binds now → an ordinary in-sync reply, resync:false.
        return { delivery: "not_modified", records: [], progress: "tok-fresh",
                 signature: "sig-fresh", cursor: { [FILE]: 2 }, resync: false, pending: {} };
      });
      try {
        await app.loadFlowConversation(flowId, { silent: true });
        await flush();
        // Second poll now echoes the adopted fresh token.
        await app.loadFlowConversation(flowId, { silent: true });
        await flush();
      } finally {
        stub.restore();
      }
      assert.equal(served, 2, "two polls, no extra bare-retry round trips");
      assert.ok(stub.calls[1].includes("after=tok-fresh"),
        `the second poll echoes the resynced cursor: ${stub.calls[1]}`);
      assert.equal(app.state.flowConversationProgress, "tok-fresh",
        "the cursor stays synced — no thrash back to a stale token");
    });
}
