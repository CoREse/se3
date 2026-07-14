/*
 * Cursor completeness self-check + numbered backfill (WebUI head-loss repair).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerHistoryCursorBackfillTests({app, check, checkAsync})`.
 *
 * Live defect these pin (flow 20260714-122542_d4e052c5, discovery, paused): the
 * console held the TAIL of the bundle and never the head, while the server's
 * progress receipt already read "everything delivered" (token o=2, cursor
 * {01_discovery_9ed2a95c.jsonl: 2}). Every 3s poll was therefore answered
 * `delivery:"not_modified"` with zero records, the client no-op'd it
 * unconditionally, and the first message stayed invisible forever — an absorbing
 * state no amount of polling could leave.
 *
 * The repair: the bundle's own `cursor` (per-step-file record counts) is the
 * authority on what the client SHOULD hold; the client checks its held
 * `stepId#ordinal` set against it on every reply and every pushed frame, and
 * asks for exactly the numbers it lacks (`?missing=step:0`), which the server
 * answers from the SAME bundle as `delivery:"backfill"`. A full re-pull remains
 * only for the cases where the numbering itself cannot be trusted.
 */
import assert from "node:assert/strict";

export async function registerHistoryCursorBackfillTests(ctx) {
  const { app, check, checkAsync, findOne } = ctx;

  const STEP = "01_discovery_9ed2a95c";
  const FILE = `${STEP}.jsonl`;

  // Read the bubbles the user would actually SEE, in rendered order. The whole
  // defect is a rendering absence, so the acceptance assertions are made against
  // the DOM — matching state.*Records only proves the data arrived, not that it
  // reached the screen.
  const renderedBodies = (container) => container.children
    .filter((c) => c.__convIdx !== undefined)
    .map((c) => { const b = findOne(c, "conv-bubble"); return b ? b.textContent : c.textContent; });

  const rec = (ordinal, content, ts, stepId) => ({
    step_id: stepId || STEP,
    step_type: "discovery",
    ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });
  const bodies = (records) => records.map(app.normalizeRecord).map((n) => n.content);

  function resetBackfillState() {
    app.state.backfillInFlight = {};
    app.state.backfillAttempts = {};
    app.state.backfillUnfillable = {};
  }

  // Seed the running-flow view in the LIVE shape: the tail record only, plus the
  // progress token + signature the server minted for the whole bundle.
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

  // Route each request by its query string so one stub can serve the poll and
  // the backfill it provokes.
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
  // 1. stepIdFromCursorKey — mirrors the daemon's _display_step_id.
  // ---------------------------------------------------------------- //
  check("stepIdFromCursorKey: plain file, sidecar file, bare id", () => {
    assert.equal(app.stepIdFromCursorKey(FILE), STEP);
    assert.equal(
      app.stepIdFromCursorKey("01_discovery_ab12.jsonl.from-worktree__b"),
      "01_discovery_ab12.from-worktree__b",
      "a .from-<branch> sidecar keeps its marker — it is a DISTINCT frontend stream whose ordinals restart at 0");
    assert.equal(app.stepIdFromCursorKey("01_discovery_ab12"), "01_discovery_ab12");
  });

  // ---------------------------------------------------------------- //
  // 2. findMissingOrdinals — the completeness probe.
  // ---------------------------------------------------------------- //
  check("findMissingOrdinals: the live head-loss shape reports exactly ordinal 0", () => {
    const probe = app.findMissingOrdinals([rec(1, "tail", 2)], { [FILE]: 2 });
    assert.deepEqual(probe.missing, { [STEP]: [0] });
    assert.equal(probe.surplus, false);
    assert.equal(probe.unkeyable, false);
  });

  check("findMissingOrdinals: a step the client holds NOTHING of yields 0..n-1", () => {
    const probe = app.findMissingOrdinals([], { [FILE]: 3 });
    assert.deepEqual(probe.missing, { [STEP]: [0, 1, 2] });
  });

  check("findMissingOrdinals: a complete view reports no missing numbers", () => {
    const probe = app.findMissingOrdinals(
      [rec(0, "head", 1), rec(1, "tail", 2)], { [FILE]: 2 });
    assert.deepEqual(probe.missing, {});
    assert.equal(probe.surplus, false);
  });

  check("findMissingOrdinals: holding MORE than the cursor declares is surplus", () => {
    const probe = app.findMissingOrdinals(
      [rec(0, "a", 1), rec(1, "b", 2), rec(2, "c", 3)], { [FILE]: 2 });
    assert.equal(probe.surplus, true,
      "the numbering no longer describes this bundle — only a full re-pull is sound");
  });

  check("findMissingOrdinals: a legacy record with no ordinal makes the step unkeyable", () => {
    const legacy = {
      step_id: STEP, step_type: "discovery",
      message: { role: "assistant", content: "legacy", timestamp: 1 },
    };
    const probe = app.findMissingOrdinals([legacy, rec(1, "tail", 2)], { [FILE]: 2 });
    assert.equal(probe.unkeyable, true,
      "an un-numbered record is not addressable by number on either side");
  });

  check("findMissingOrdinals: a local echo (step absent from the cursor) is not surplus", () => {
    const echo = {
      step_id: "reply_pending", __localEcho: true,
      message: { role: "user", content: "hi", timestamp: 9 },
    };
    const probe = app.findMissingOrdinals(
      [rec(0, "head", 1), rec(1, "tail", 2), echo], { [FILE]: 2 });
    assert.deepEqual(probe.missing, {});
    assert.equal(probe.surplus, false, "client-only echoes belong to no server bundle");
  });

  // ---------------------------------------------------------------- //
  // 3. encodeMissingParam + historySnapshotUrl.
  // ---------------------------------------------------------------- //
  check("encodeMissingParam: wire form, empty → null, over-cap → null", () => {
    assert.equal(
      app.encodeMissingParam({ a: [0, 2], b: [1] }), "a:0,2;b:1");
    assert.equal(app.encodeMissingParam({}), null);
    assert.equal(app.encodeMissingParam({ a: [] }), null);
    const huge = Array.from({ length: 201 }, (_, i) => i);
    assert.equal(app.encodeMissingParam({ a: huge }), null,
      "over the server's ordinal cap the caller must re-pull in full, not send a request the server rejects");
  });

  check("historySnapshotUrl: missing rides alongside a live token, never alone", () => {
    const url = app.historySnapshotUrl("F1", "tok", "sig", `${STEP}:0`);
    assert.ok(url.includes("after=tok") && url.includes("sig=sig"));
    assert.ok(url.includes(`missing=${encodeURIComponent(STEP + ":0")}`)
      || url.includes(`missing=${STEP}%3A0`), `missing param present: ${url}`);
    assert.equal(app.historySnapshotUrl("F1", null, null, `${STEP}:0`),
      "/api/history/F1",
      "with no token there is no generation to number against — a bare full URL");
  });

  // ---------------------------------------------------------------- //
  // 4. mergeHistoryResponse: cursor on every branch + the backfill delivery.
  // ---------------------------------------------------------------- //
  check("mergeHistoryResponse: not_modified now returns the cursor (no longer a blind no-op)", () => {
    const held = [rec(1, "tail", 2)];
    const out = app.mergeHistoryResponse(
      { delivery: "not_modified", records: [], progress: "tok", signature: "sig",
        cursor: { [FILE]: 2 } },
      held, held);
    assert.equal(out.render, "noop");
    assert.deepEqual(out.cursor, { [FILE]: 2 },
      "the caller needs the cursor to discover it is missing the head");
  });

  check("mergeHistoryResponse: a backfill folds the head back into its ORDERED position", () => {
    const held = [rec(1, "tail", 2)];
    const out = app.mergeHistoryResponse(
      { delivery: "backfill", records: [rec(0, "head", 1)], progress: "tok2",
        signature: "sig", cursor: { [FILE]: 2 } },
      held, held);
    assert.equal(out.render, "full", "a head/middle record cannot be tail-appended");
    assert.deepEqual(bodies(out.records), ["head", "tail"]);
    assert.equal(out.progress, "tok2");
  });

  check("mergeHistoryResponse: re-backfilling the same number is idempotent", () => {
    const held = [rec(0, "head", 1), rec(1, "tail", 2)];
    const out = app.mergeHistoryResponse(
      { delivery: "backfill", records: [rec(0, "head", 1)], progress: "tok",
        signature: "sig", cursor: { [FILE]: 2 } },
      held, held);
    assert.equal(out.render, "noop");
    assert.deepEqual(bodies(out.records), ["head", "tail"]);
  });

  check("mergeHistoryResponse: the #287 empty-full rejection withholds its cursor too", () => {
    const held = [rec(0, "head", 1), rec(1, "tail", 2)];
    const out = app.mergeHistoryResponse(
      { delivery: "full", records: [], progress: "tok", signature: "sig",
        cursor: {} },
      held, held);
    assert.equal(out.preserveTokens, true);
    assert.equal(out.cursor, null,
      "checking against a rejected empty bundle's cursor would read every held record as surplus");
  });

  // ---------------------------------------------------------------- //
  // 5. THE LIVE DEFECT, end to end: tail held + not_modified receipt.
  // ---------------------------------------------------------------- //
  await checkAsync(
    "live shape: tail-only + not_modified → self-check finds ordinal 0 → backfill restores the head",
    async () => {
      const flowId = "20260714-122542_d4e052c5";
      const c = seedHeadlessFlow(flowId, [rec(1, "tail answer", 2)]);
      const stub = withRouter((url) => {
        if (url.includes("missing=")) {
          return { delivery: "backfill", records: [rec(0, "head question", 1)],
                   progress: "tok-gen1", signature: "sig-gen1", cursor: { [FILE]: 2 } };
        }
        // What the live server answered every 3s: "you have everything".
        return { delivery: "not_modified", records: [], progress: "tok-gen1",
                 signature: "sig-gen1", cursor: { [FILE]: 2 } };
      });
      try {
        await app.loadFlowConversation(flowId, { silent: true });
        await flush();
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 2, "the not_modified poll provoked exactly one repair");
      // The exact wire form, not merely "some missing param": the ordinal asked
      // for, the generation it is numbered against, and the signature that pins
      // it are each load-bearing — a backfill sent against the wrong generation
      // would splice records from another bundle into this one.
      assert.equal(
        stub.calls[1],
        `/api/history/${flowId}?after=tok-gen1&sig=sig-gen1`
          + `&missing=${encodeURIComponent(`${STEP}:0`)}`,
        `backfill asked for exactly ordinal 0 of the live generation: ${stub.calls[1]}`);
      assert.deepEqual(bodies(app.state.flowConversationRecords),
        ["head question", "tail answer"]);
      // THE acceptance assertion: the first message is on screen again, ahead of
      // the tail the console had been stuck showing alone.
      const shown = renderedBodies(c);
      assert.equal(shown.length, 2, "both bubbles are rendered");
      assert.ok(shown[0].includes("head question"),
        "the record the console could never see is now the FIRST bubble in the DOM");
      assert.ok(shown[1].includes("tail answer"));
    });

  await checkAsync("healthy path: a complete view issues no repair request and no render", async () => {
    const flowId = "healthy-flow";
    seedHeadlessFlow(flowId, [rec(0, "head", 1), rec(1, "tail", 2)]);
    const stub = withRouter(() => ({
      delivery: "not_modified", records: [], progress: "tok-gen1",
      signature: "sig-gen1", cursor: { [FILE]: 2 },
    }));
    try {
      await app.loadFlowConversation(flowId, { silent: true });
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(stub.calls.length, 1, "the idle poll stays exactly as cheap as before");
  });

  // ---------------------------------------------------------------- //
  // 6. The push path: a WS frame's cursor triggers the same self-check.
  // ---------------------------------------------------------------- //
  await checkAsync("WS append frame carrying a cursor triggers the numbered backfill", async () => {
    const flowId = "ws-cursor-flow";
    const c = seedHeadlessFlow(flowId, []);
    const stub = withRouter(() => ({
      delivery: "backfill", records: [rec(0, "head question", 1)],
      progress: "tok-gen1", signature: "sig-gen1", cursor: { [FILE]: 2 },
    }));
    try {
      // A console that joined late (the /api/auth/me 401 login gate held its
      // WebSocket shut) sees only this tail append — but the frame's cursor says
      // the bundle holds 2 records.
      app.applyHistoryData({
        flow_id: flowId, mode: "append", records: [rec(1, "tail answer", 2)],
        cursor: { [FILE]: 2 }, signature: "sig-gen1",
      });
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(stub.calls.length, 1, "the pushed frame is itself the cue to pull the head");
    assert.equal(
      stub.calls[0],
      `/api/history/${flowId}?after=tok-gen1&sig=sig-gen1`
        + `&missing=${encodeURIComponent(`${STEP}:0`)}`);
    assert.deepEqual(bodies(app.state.flowConversationRecords),
      ["head question", "tail answer"]);
    const shown = renderedBodies(c);
    assert.equal(shown.length, 2);
    assert.ok(shown[0].includes("head question"),
      "a console that joined late renders the head it was never pushed");
  });

  await checkAsync("records-less history_cursor advisory also triggers the self-check", async () => {
    const flowId = "ws-advisory-flow";
    seedHeadlessFlow(flowId, [rec(1, "tail answer", 2)]);
    const stub = withRouter(() => ({
      delivery: "backfill", records: [rec(0, "head question", 1)],
      progress: "tok-gen1", signature: "sig-gen1", cursor: { [FILE]: 2 },
    }));
    try {
      app.applyHistoryCursor({ flow_id: flowId, cursor: { [FILE]: 2 }, signature: "sig-gen1" });
      await flush();
    } finally {
      stub.restore();
    }
    assert.deepEqual(bodies(app.state.flowConversationRecords),
      ["head question", "tail answer"],
      "the frame that REPAIRS a bundle but carries no records still reaches the console");
  });

  // ---------------------------------------------------------------- //
  // 7. Fallbacks: unusable numbering → one full re-pull, not a backfill.
  // ---------------------------------------------------------------- //
  await checkAsync(
    "generation/machine/signature mismatch: the server answers the backfill with a full bundle and the stale receipt is discarded",
    async () => {
      const flowId = "regen-mismatch-flow";
      // The console holds the tail of a bundle the server has since REPLACED
      // (daemon restart / cross-machine takeover): its token and signature name a
      // generation that no longer exists, so its ordinals name positions in a
      // bundle nobody can serve.
      const c = seedHeadlessFlow(flowId, [rec(1, "stale tail", 2)]);
      const stub = withRouter(() => ({
        // The server validates the token, finds the generation/machine mismatched,
        // and ignores `missing=` entirely — the only sound answer is the whole new
        // bundle. (state.py pins this server-side; here we pin that the CLIENT
        // adopts it rather than trying to splice the reply into the dead one.)
        delivery: "full",
        records: [rec(0, "new head", 10), rec(1, "new tail", 11)],
        progress: "tok-gen2", signature: "sig-gen2", cursor: { [FILE]: 2 },
      }));
      try {
        await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 2 });
        await flush();
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 1);
      assert.ok(stub.calls[0].includes("missing="),
        "the client cannot know its generation is dead until the server says so — it asks by number first");
      // The dead generation's records are GONE, not merged with the new bundle:
      // splicing ordinals across generations is exactly the corruption the
      // signature check exists to prevent.
      assert.deepEqual(bodies(app.state.flowConversationRecords), ["new head", "new tail"]);
      assert.ok(!bodies(app.state.flowConversationRecords).includes("stale tail"));
      assert.equal(app.state.flowConversationProgress, "tok-gen2",
        "the stale receipt is discarded — the client now speaks for the new generation");
      assert.equal(app.state.flowConversationSignature, "sig-gen2");
      const shown = renderedBodies(c);
      assert.deepEqual(shown.length, 2);
      assert.ok(shown[0].includes("new head") && shown[1].includes("new tail"),
        "the rebuilt conversation renders the new bundle head-first");
    });

  await checkAsync("surplus (held > cursor) falls back to a token-less full re-pull", async () => {
    const flowId = "surplus-flow";
    seedHeadlessFlow(flowId, [rec(0, "a", 1), rec(1, "b", 2), rec(2, "c", 3)]);
    const stub = withRouter(() => ({
      delivery: "full", records: [rec(0, "a", 1)], progress: "tok-gen2",
      signature: "sig-gen2", cursor: { [FILE]: 1 },
    }));
    try {
      await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 1 });
      await flush();
    } finally {
      stub.restore();
    }
    assert.equal(stub.calls.length, 1);
    assert.ok(!stub.calls[0].includes("missing="), "numbering is untrustworthy — never backfill");
    assert.ok(!stub.calls[0].includes("after="),
      "the stale token is DISCARDED so the server must answer with the complete bundle");
    assert.deepEqual(bodies(app.state.flowConversationRecords), ["a"],
      "the new generation is adopted wholesale");
  });

  const legacyRec = (content, ts) => ({
    step_id: STEP, step_type: "discovery",
    message: { role: "assistant", content, timestamp: ts },
  });

  await checkAsync(
    "an un-numbered record escalates ONCE to a full re-pull, which heals the hole",
    async () => {
      const flowId = "legacy-heals-flow";
      // The view holds one un-numbered record while the cursor says the file has
      // two: a genuine hole, but one no NUMBERED backfill can express (the held
      // record answers to no number, so the probe cannot say which are absent).
      // Skipping the check here — the old behaviour — left this flow on the
      // token-only path forever: every poll answered `not_modified`, the missing
      // record invisible for the life of the page. A single token-less full
      // serves every record the bundle holds, numbered or not, and closes it.
      const c = seedHeadlessFlow(flowId, [legacyRec("legacy tail", 2)]);
      const stub = withRouter(() => ({
        delivery: "full", records: [rec(0, "head question", 1), rec(1, "tail", 2)],
        progress: "tok-gen1", signature: "sig-gen1", cursor: { [FILE]: 2 },
        generation: 1,
      }));
      try {
        for (let frame = 0; frame < 4; frame++) {
          await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 2 }, 1);
          await flush();
        }
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 1,
        "exactly one full re-pull — the healed view self-checks clean thereafter");
      assert.ok(!stub.calls[0].includes("after="),
        "the token is discarded so the server must answer with the complete bundle");
      assert.deepEqual(bodies(app.state.flowConversationRecords), ["head question", "tail"]);
      const shown = renderedBodies(c);
      assert.ok(shown[0].includes("head question"),
        "the record no number could name is on screen after one request");
    });

  await checkAsync(
    "still un-numbered after the full: the numbered check retires for this generation — no re-pull per frame",
    async () => {
      const flowId = "legacy-flow";
      seedHeadlessFlow(flowId, [legacyRec("legacy", 2)]);
      // A pre-ordinal daemon: the full re-pull hands back un-numbered records too,
      // so the numbering is a property of the DAEMON, not a transient hole. One
      // full proves that; after it the flow falls back to its token-only path.
      // Re-pulling per frame would swap the cheap delta poll for a whole-bundle
      // download once per streamed record, forever.
      const stub = withRouter(() => ({
        delivery: "full",
        records: [legacyRec("legacy", 2), legacyRec("legacy 2", 3)],
        progress: "tok-gen1", signature: "sig-gen1", cursor: { [FILE]: 2 },
        generation: 1,
      }));
      try {
        for (let frame = 0; frame < 5; frame++) {
          app.state.flowConversationSignature = `sig-append-${frame}`;
          await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 2 }, 1);
          await flush();
        }
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 1,
        "one full escalation for the whole generation, then silence — an append is not evidence the daemon started numbering");
      assert.deepEqual(bodies(app.state.flowConversationRecords), ["legacy", "legacy 2"]);
    });

  // ---------------------------------------------------------------- //
  // 7b. A number the bundle legitimately holds no record for.
  //
  // The cursor counts PHYSICAL LINES, so a blank / unparseable line (or a read
  // resumed at a non-zero base) advances it without producing a record: a number
  // under the cursor need not name a record at all. The server says so once
  // (`unfillable`), and the client must retire that number — not re-ask on every
  // signal and not re-pull the whole bundle on every append.
  // ---------------------------------------------------------------- //
  await checkAsync(
    "a number the server declares unfillable is retired, not re-requested on every append",
    async () => {
      const flowId = "blank-line-flow";
      // cursor says 3 lines; ordinal 1 is a blank line the daemon skipped.
      seedHeadlessFlow(flowId, [rec(0, "head", 1), rec(2, "tail", 3)]);
      const stub = withRouter(() => ({
        delivery: "backfill", records: [],
        unfillable: { [STEP]: [1] },
        progress: "tok-gen1", signature: "sig-gen1", cursor: { [FILE]: 3 },
      }));
      try {
        await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 3 });
        await flush();
        assert.equal(stub.calls.length, 1);
        assert.ok(stub.calls[0].includes(`missing=${STEP}%3A1`)
          || stub.calls[0].includes(`missing=${STEP}:1`),
          "the client asks by number once");
        // Now the daemon keeps appending. Each append mints a NEW signature; the
        // retired number must stay retired regardless.
        for (let frame = 0; frame < 5; frame++) {
          app.state.flowConversationSignature = `sig-append-${frame}`;
          await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 3 });
          await flush();
        }
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 1,
        "one round-trip for the life of the flow — no per-append backfill, no per-append full re-pull");
      assert.deepEqual(bodies(app.state.flowConversationRecords), ["head", "tail"]);
    });

  // ---------------------------------------------------------------- //
  // 7c. A new bundle generation voids every per-bundle verdict.
  //
  // `unfillable` and the repair budget are claims about ONE cached bundle. When
  // the daemon replaces it (a restart rewrites the step file), the same number can
  // name a real, servable record — carrying the old verdict across would keep that
  // record invisible for the life of the page, which is the very defect this
  // machinery exists to close.
  // ---------------------------------------------------------------- //
  await checkAsync(
    "a number retired as unfillable in gen 1 is asked for again in gen 2",
    async () => {
      const flowId = "regen-unretire-flow";
      seedHeadlessFlow(flowId, [rec(0, "head", 1), rec(2, "tail", 3)]);
      let reply = {
        delivery: "backfill", records: [], unfillable: { [STEP]: [1] },
        progress: "tok-gen1", signature: "sig-gen1", cursor: { [FILE]: 3 },
        generation: 1,
      };
      const stub = withRouter(() => reply);
      try {
        // gen 1: ordinal 1 is a blank line the daemon skipped — legitimately
        // unfillable, retired, and not re-requested while that bundle stands.
        await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 3 }, 1);
        await flush();
        await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 3 }, 1);
        await flush();
        assert.equal(stub.calls.length, 1, "retired within its own generation");

        // The daemon restarts and rewrites the file: gen 2's ordinal 1 IS a
        // record. The gen-1 verdict must not survive the roll.
        reply = {
          delivery: "backfill", records: [rec(1, "middle", 2)],
          progress: "tok-gen2", signature: "sig-gen2", cursor: { [FILE]: 3 },
          generation: 2,
        };
        app.state.flowConversationProgress = "tok-gen2";
        app.state.flowConversationSignature = "sig-gen2";
        await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 3 }, 2);
        await flush();
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 2, "the new bundle is checked afresh");
      assert.ok(stub.calls[1].includes(`missing=${STEP}%3A1`)
        || stub.calls[1].includes(`missing=${STEP}:1`));
      assert.deepEqual(bodies(app.state.flowConversationRecords),
        ["head", "middle", "tail"],
        "the record gen 1 could not serve is rendered once gen 2 holds it");
    });

  await checkAsync(
    "a repair budget spent against gen 1 is re-armed by gen 2",
    async () => {
      const flowId = "regen-budget-flow";
      seedHeadlessFlow(flowId, [rec(1, "tail", 2)]);
      let reply = {
        delivery: "backfill", records: [], progress: "tok-gen1",
        signature: "sig-gen1", cursor: { [FILE]: 2 }, generation: 1,
      };
      const stub = withRouter(() => reply);
      try {
        // gen 1 never hands over ordinal 0: 2 backfills + 1 full, then silence.
        for (let poll = 0; poll < 5; poll++) {
          await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 2 }, 1);
          await flush();
        }
        assert.equal(stub.calls.length, 3, "gen 1's budget is spent and stays spent");

        // A fresh bundle is a fresh hole: a budget exhausted against a gap the OLD
        // bundle could not fill says nothing about this one, and refusing to spend
        // for it would leave a genuinely new head-loss unrepaired forever.
        reply = {
          delivery: "backfill", records: [rec(0, "head", 1)],
          progress: "tok-gen2", signature: "sig-gen2", cursor: { [FILE]: 2 },
          generation: 2,
        };
        app.state.flowConversationProgress = "tok-gen2";
        app.state.flowConversationSignature = "sig-gen2";
        await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 2 }, 2);
        await flush();
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 4, "gen 2 gets its own repair attempts");
      assert.deepEqual(bodies(app.state.flowConversationRecords), ["head", "tail"]);
    });

  check("findMissingOrdinals: a retired (server-declared unfillable) number is not missing", () => {
    const probe = app.findMissingOrdinals(
      [rec(0, "head", 1), rec(2, "tail", 3)], { [FILE]: 3 }, { [STEP]: [1] });
    assert.deepEqual(probe.missing, {},
      "the bundle's own cursor counts physical lines — a skipped line is not a hole");
    assert.equal(probe.surplus, false);
  });

  // ---------------------------------------------------------------- //
  // 8. Storm guard: a bundle that genuinely lacks the number.
  // ---------------------------------------------------------------- //
  await checkAsync(
    "a server bundle missing the record itself is retried at most 2 backfills + 1 full, then stops",
    async () => {
      const flowId = "gap-forever-flow";
      seedHeadlessFlow(flowId, [rec(1, "tail", 2)]);
      // The server keeps CLAIMING 2 records but never hands over ordinal 0 (a
      // record the daemon never reported) — the self-check can never close.
      const stub = withRouter(() => ({
        delivery: "backfill", records: [], progress: "tok-gen1",
        signature: "sig-gen1", cursor: { [FILE]: 2 },
      }));
      try {
        for (let poll = 0; poll < 6; poll++) {
          await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 2 });
          await flush();
        }
      } finally {
        stub.restore();
      }
      assert.equal(stub.calls.length, 3,
        "2 numbered backfills + 1 full escalation, then silence — a server-side gap must not become a client-driven request storm");
      const missingCalls = stub.calls.filter((u) => u.includes("missing="));
      assert.equal(missingCalls.length, 2);
    });

  await checkAsync(
    "a spent budget is NOT handed back by a mere append (a new signature), only by a clean self-check",
    async () => {
      const flowId = "regen-flow";
      seedHeadlessFlow(flowId, [rec(1, "tail", 2)]);
      const stub = withRouter(() => ({
        delivery: "backfill", records: [], progress: "tok-gen1",
        signature: "sig-gen1", cursor: { [FILE]: 2 },
      }));
      try {
        for (let poll = 0; poll < 4; poll++) {
          await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 2 });
          await flush();
        }
        assert.equal(stub.calls.length, 3, "budget spent: 2 backfills + 1 full");
        // The signature is minted from (generation, record count), so EVERY
        // appended record changes it. Re-arming the budget on that would re-spend
        // it per record — 2 backfills + a whole-bundle re-pull each time — which
        // is precisely the request storm the budget exists to prevent.
        for (let append = 0; append < 5; append++) {
          app.state.flowConversationSignature = `sig-append-${append}`;
          await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 2 });
          await flush();
        }
        assert.equal(stub.calls.length, 3, "an append is not evidence the gap became fillable");

        // Recovery: the flow actually becomes whole (the head arrives), the
        // self-check comes back clean, and the budget is released — so a LATER,
        // genuinely new hole in the same flow is repairable again.
        app.state.flowConversationRecords = [rec(0, "head", 1), rec(1, "tail", 2)];
        await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 2 });
        await flush();
        assert.equal(stub.calls.length, 3, "a clean check costs zero requests");
        app.state.flowConversationRecords = [rec(0, "head", 1), rec(2, "tail3", 3)];
        await app.reconcileCursorCompleteness("flow", flowId, { [FILE]: 3 });
        await flush();
        assert.equal(stub.calls.length, 4, "the healthy flow is repairable again");
      } finally {
        stub.restore();
      }
    });

  // ---------------------------------------------------------------- //
  // 9. The history-detail view runs the same self-check.
  // ---------------------------------------------------------------- //
  await checkAsync("history-detail view: tail-only + cursor → numbered backfill restores the head", async () => {
    resetBackfillState();
    const flowId = "hist-detail-flow";
    app.state.selectedFlowId = null;
    app.state.selectedHistoryId = flowId;
    app.state.historyRecords = [rec(1, "tail answer", 2)];
    app.state.historyProgress = "tok-gen1";
    app.state.historySignature = "sig-gen1";
    const stub = withRouter(() => ({
      delivery: "backfill", records: [rec(0, "head question", 1)],
      progress: "tok-gen1", signature: "sig-gen1", cursor: { [FILE]: 2 },
    }));
    try {
      await app.reconcileCursorCompleteness("history", flowId, { [FILE]: 2 });
      await flush();
    } finally {
      stub.restore();
      app.state.selectedHistoryId = null;
    }
    assert.deepEqual(bodies(app.state.historyRecords), ["head question", "tail answer"]);
  });
}
