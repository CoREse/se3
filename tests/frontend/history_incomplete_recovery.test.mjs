/*
 * Interrupted-delivery recovery tests.
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerHistoryIncompleteRecoveryTests({app, check,
 * checkAsync, findOne, findAll})`.
 *
 * The defect these pin: opening a large COMPLETED session ships its history as
 * one `full` head plus ~146 chunk-bounded `append` tails, and a socket that dies
 * mid-drain leaves the server holding a self-consistent PREFIX — its cursor
 * names exactly the step files that landed, its pending window is empty — so the
 * client's `stepId#ordinal` self-check finds no hole to repair. The server does
 * detect it (`_OpenDelivery` → `incomplete: true`), but that statement reached
 * nobody: the WS frames did not carry it, the frontend read it from neither WS
 * nor REST, and the History view has no poll timer of its own. The conversation's
 * commit/summarize tail was therefore invisible for good.
 *
 * Coverage:
 *   (I1) a WS frame declaring `incomplete` arms exactly ONE bounded re-read, and
 *        that re-read is INCREMENTAL (it echoes the held token — it must not
 *        re-pull the whole multi-MB bundle)
 *   (I2) the loop keeps going while the server keeps saying `incomplete`, and
 *        stops as soon as a reply declares the bundle settled
 *   (I3) a LIVE flow is already covered by the running-flow view's 3s poll, so
 *        no second timer is armed there; a TERMINAL one, whose poll stops
 *        re-reading history, has the bounded loop armed instead
 *   (I6) a re-read that fails transiently (5xx, timeout, network) re-arms on the
 *        bounded backoff instead of ending the streak
 *   (I7) …and the streak itself is bounded, so an unrepairable bundle is not
 *        polled for the life of the tab
 *   (I8) a frame that carries NO completeness statement (the records-less
 *        advisory for a budget-evicted flow, an older server) is silence, not a
 *        settled declaration: it neither disarms an armed repair nor latches the
 *        bundle whole, and counts as no statement at all
 *   (I4) closing a view cancels what it armed
 *   (I5) `mergeHistoryResponse` surfaces `incomplete` on every delivery shape,
 *        `not_modified` included — the shape an interrupted bundle is answered
 *        with forever
 */
import assert from "node:assert/strict";

export async function registerHistoryIncompleteRecoveryTests(ctx) {
  const { app, check, checkAsync } = ctx;

  const flush = async (n = 8) => {
    for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 1));
  };

  // The production cadence is 5s→30s (above the server's own 5s repair floor).
  // Compress it for the tests; the array is the exported one the scheduler reads.
  const savedDelays = app.INCOMPLETE_RECOVERY_DELAYS_MS.slice();
  const fastDelays = () => {
    for (let i = 0; i < app.INCOMPLETE_RECOVERY_DELAYS_MS.length; i++) {
      app.INCOMPLETE_RECOVERY_DELAYS_MS[i] = 1;
    }
  };
  const restoreDelays = () => {
    savedDelays.forEach((v, i) => { app.INCOMPLETE_RECOVERY_DELAYS_MS[i] = v; });
  };

  // These cases open / close the History view, and the shared DOM stub is
  // shared with every later suite — so whatever visibility it had on entry is
  // restored on exit rather than left wherever the last case put it.
  const historyView = () => document.getElementById("history-view");
  const historyWasHidden = historyView().classList.contains("hidden");
  const restoreHistoryVisibility = () => {
    if (historyWasHidden) historyView().classList.add("hidden");
    else historyView().classList.remove("hidden");
  };

  const record = (ordinal) => ({
    step_id: "01_discovery_a",
    step_type: "discovery",
    ordinal,
    message: { role: "assistant", content: `r${ordinal}`, timestamp: ordinal },
  });

  // Put the History view in the state a large session's first load leaves: the
  // prefix is rendered, a progress token is held, and the bundle is a prefix.
  function openHistoryPrefix(flowId) {
    document.getElementById("history-view").classList.remove("hidden");
    app.state.selectedHistoryId = flowId;
    app.state.selectedFlowId = null;
    app.state.historyRecords = [record(0)];
    app.state.historyProgress = "tok-1";
    app.state.historySignature = "sig-1";
    app.state.historyEpoch = 0;
    app.state.historySessions = [{ flow_id: flowId, machine_id: "m1" }];
    app.cancelIncompleteRecoveryForView("history");
    const d = document.getElementById("history-detail");
    d.innerHTML = ""; d.__convState = null;
  }

  // -- (I1) + (I2) -----------------------------------------------------------
  await checkAsync(
    "(I1/I2) an `incomplete` bundle is re-read until the server says it is settled",
    async () => {
      const saved = globalThis.fetch;
      fastDelays();
      openHistoryPrefix("BIG");
      const urls = [];
      // Two more polls still find the bundle a prefix, the third is repaired.
      // A real `not_modified` reply re-mints the progress token / signature, so
      // the next recovery read still echoes one and stays incremental.
      const replies = [
        {
          delivery: "not_modified", records: [], progress: "tok-1",
          signature: "sig-1", incomplete: true,
        },
        {
          delivery: "not_modified", records: [], progress: "tok-1",
          signature: "sig-1", incomplete: true,
        },
        {
          delivery: "delta", records: [record(1)], progress: "tok-2",
          signature: "sig-2", incomplete: false,
        },
      ];
      globalThis.fetch = (url) => {
        urls.push(String(url));
        const payload = replies.shift()
          || { delivery: "not_modified", records: [], incomplete: false };
        return Promise.resolve({
          ok: true, status: 200, json: () => Promise.resolve(payload),
        });
      };
      try {
        // The WS frame is the ONLY signal this view gets: it has no poll timer.
        app.applyHistoryCursor({ flow_id: "BIG", incomplete: true });
        assert.equal(urls.length, 0, "the re-read is scheduled, not fired inline");
        // A burst of frames from the same drain must not stack re-reads.
        app.applyHistoryCursor({ flow_id: "BIG", incomplete: true });
        app.applyHistoryCursor({ flow_id: "BIG", incomplete: true });

        await flush(40);

        assert.ok(urls.length >= 3,
          "the re-read must keep firing while the bundle is a prefix: "
          + JSON.stringify(urls));
        assert.ok(
          urls.every((u) => u.includes("after=")),
          "every recovery read must be INCREMENTAL (held token echoed), never a "
          + "bare full re-pull of the whole bundle: " + JSON.stringify(urls));
        assert.equal(replies.length, 0, "all three replies must have been consumed");

        // The settled reply retires the loop: no timer is left armed for it.
        assert.equal(
          app.state.incompleteRecoveryTimers["history|BIG"], undefined,
          "a settled bundle must leave no armed re-read");
        const after = urls.length;
        await flush(20);
        assert.equal(urls.length, after,
          "and the loop must not keep polling once the bundle is whole");
      } finally {
        globalThis.fetch = saved;
        restoreDelays();
        app.cancelIncompleteRecoveryForView("history");
        restoreHistoryVisibility();
        app.state.selectedHistoryId = null;
      }
    });

  // -- (I3) ------------------------------------------------------------------
  check("(I3) a LIVE flow's own 3s poll owns the repair; a terminal one hands it over",
    () => {
      const savedMachines = app.state.machines;
      app.state.selectedFlowId = "LIVE";
      app.state.flowDetail = null;
      app.state.machines = [{ flows: [{ flow_id: "LIVE", status: "running" }] }];
      app.state.periodicSnapshotActive = true;
      app.cancelIncompleteRecoveryForView("flow");
      try {
        app.noteBundleCompleteness("flow", "LIVE", true);
        assert.equal(app.state.incompleteRecoveryTimers["flow|LIVE"], undefined,
          "the poll already re-reads this bundle every 3s; a second timer would "
          + "only double the request rate");
        // …but a view WITHOUT that poll (a detached/replayed flow pane) still
        // gets one, so the repair is never left to nothing.
        app.state.periodicSnapshotActive = false;
        app.noteBundleCompleteness("flow", "LIVE", true);
        assert.ok(app.state.incompleteRecoveryTimers["flow|LIVE"],
          "with no poll running the recovery timer is the only repair path");

        // Once the flow is terminal the 3s poll stops re-reading its history
        // (see selfHealFlowConversation), so the hand-over must go the other
        // way: the bounded loop is armed even though the poll is running.
        app.cancelIncompleteRecoveryForView("flow");
        app.state.periodicSnapshotActive = true;
        app.state.machines = [{ flows: [{ flow_id: "LIVE", status: "completed" }] }];
        app.noteBundleCompleteness("flow", "LIVE", true);
        assert.ok(app.state.incompleteRecoveryTimers["flow|LIVE"],
          "a terminal flow's incomplete bundle must never be left with no "
          + "repair path at all");
      } finally {
        app.cancelIncompleteRecoveryForView("flow");
        app.state.machines = savedMachines;
        app.state.selectedFlowId = null;
        app.state.periodicSnapshotActive = false;
      }
    });

  // -- (I6) ------------------------------------------------------------------
  await checkAsync(
    "(I6) a re-read that fails transiently is retried, never abandoned",
    async () => {
      const saved = globalThis.fetch;
      fastDelays();
      openHistoryPrefix("FLAKY");
      // The two failure shapes an incremental re-read swallows: a non-2xx reply
      // and a network/timeout rejection. Neither produces a completeness
      // statement, so neither may end the streak.
      const outcomes = ["500", "throw", "still-incomplete", "settled"];
      const urls = [];
      globalThis.fetch = (url) => {
        urls.push(String(url));
        const next = outcomes.shift() || "settled";
        if (next === "500") {
          return Promise.resolve({
            ok: false, status: 500,
            json: () => Promise.reject(new Error("no body")),
          });
        }
        if (next === "throw") return Promise.reject(new Error("network down"));
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve({
            delivery: "not_modified", records: [], progress: "tok-1",
            signature: "sig-1", incomplete: next === "still-incomplete",
          }),
        });
      };
      try {
        app.applyHistoryCursor({ flow_id: "FLAKY", incomplete: true });
        await flush(80);
        assert.equal(outcomes.length, 0,
          "the streak must have survived BOTH failures and gone on to the "
          + "settled reply: " + JSON.stringify(urls));
        assert.equal(
          app.state.incompleteRecoveryTimers["history|FLAKY"], undefined,
          "and stopped once the bundle was declared whole");
      } finally {
        globalThis.fetch = saved;
        restoreDelays();
        app.cancelIncompleteRecoveryForView("history");
        restoreHistoryVisibility();
        app.state.selectedHistoryId = null;
      }
    });

  // -- (I7) ------------------------------------------------------------------
  await checkAsync(
    "(I7) the streak is bounded: a bundle that never repairs stops being polled",
    async () => {
      const saved = globalThis.fetch;
      const savedWarn = console.warn;
      fastDelays();
      openHistoryPrefix("DEAD");
      let attempts = 0;
      globalThis.fetch = () => {
        attempts += 1;
        return Promise.reject(new Error("daemon is gone"));
      };
      console.warn = () => {};
      try {
        app.applyHistoryCursor({ flow_id: "DEAD", incomplete: true });
        await flush(300);
        assert.ok(attempts > 1, "the loop must have actually retried");
        assert.ok(attempts <= app.INCOMPLETE_RECOVERY_MAX_ATTEMPTS,
          "a repair budget of " + app.INCOMPLETE_RECOVERY_MAX_ATTEMPTS
          + " must not be exceeded, got " + attempts);
        assert.equal(app.state.incompleteRecoveryTimers["history|DEAD"], undefined,
          "a view left open on an unrepairable bundle must stop re-reading it");
        const spent = attempts;
        await flush(60);
        assert.equal(attempts, spent, "and must not resume on its own");
      } finally {
        globalThis.fetch = saved;
        console.warn = savedWarn;
        restoreDelays();
        app.cancelIncompleteRecoveryForView("history");
        restoreHistoryVisibility();
        app.state.selectedHistoryId = null;
      }
    });

  // -- (I8) ------------------------------------------------------------------
  await checkAsync(
    "(I8) a frame carrying NO completeness statement never settles a bundle",
    async () => {
      const saved = globalThis.fetch;
      fastDelays();
      openHistoryPrefix("MUTE");
      let reads = 0;
      globalThis.fetch = () => {
        reads += 1;
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            delivery: "not_modified", records: [], progress: "tok-1",
            signature: "sig-1", incomplete: true,
          }),
        });
      };
      try {
        // The drain was cut, so the server declared the bundle a prefix and the
        // bounded repair is armed.
        app.applyHistoryCursor({ flow_id: "MUTE", incomplete: true });
        assert.ok(app.state.incompleteRecoveryTimers["history|MUTE"],
          "an `incomplete` statement must arm the repair");

        // Now the history-cache budget evicts the flow and the next daemon frame
        // is suppressed, so what the console receives is the records-less
        // advisory. Even if that frame said nothing about completeness, silence
        // is not an answer: it must not disarm a repair the server asked for.
        // (the cursor it carries matches what this view holds, so the numbered
        // self-check finds no hole — the completeness statement is the only
        // thing that could still say the tail is missing)
        app.applyHistoryCursor({
          flow_id: "MUTE", cursor: { "01_discovery_a.jsonl": 1 }, pending: {},
        });
        assert.ok(app.state.incompleteRecoveryTimers["history|MUTE"],
          "a frame that makes no statement must leave the armed repair alone — "
          + "reading silence as `settled` retires the ONLY repair path this "
          + "view has, and the missing tail stays invisible for good");
        assert.equal(app.declaredBundleCompleteness("history", "MUTE"), false,
          "and must not latch the bundle as declared-complete");

        await flush(40);
        assert.ok(reads > 0,
          "the streak must go on re-reading across the statement-less frame");
      } finally {
        globalThis.fetch = saved;
        restoreDelays();
        app.cancelIncompleteRecoveryForView("history");
        restoreHistoryVisibility();
        app.state.selectedHistoryId = null;
      }
    });

  check("(I8b) silence with nothing said yet stays `not said yet`", () => {
    app.cancelIncompleteRecoveryForView("history");
    document.getElementById("history-view").classList.remove("hidden");
    app.state.selectedHistoryId = "QUIET";
    try {
      // Neither answer: it must not arm a repair against a bundle nothing is
      // known to be wrong with, and must not record a statement the server has
      // not made.
      app.applyHistoryCursor({ flow_id: "QUIET" });
      assert.equal(app.declaredBundleCompleteness("history", "QUIET"), undefined,
        "an absent statement is `undefined`, neither settled nor incomplete");
      assert.equal(app.state.incompleteRecoveryTimers["history|QUIET"], undefined,
        "and arms nothing");
      assert.equal(app.state.incompleteRecoverySignals["history|QUIET"], undefined,
        "a frame that says nothing is not a statement the recovery can count — "
        + "counting it would make a failed re-read look answered");
    } finally {
      app.cancelIncompleteRecoveryForView("history");
      restoreHistoryVisibility();
      app.state.selectedHistoryId = null;
    }
  });

  // -- (I4) ------------------------------------------------------------------
  check("(I4) closing a view cancels the re-read it armed", () => {
    app.state.selectedHistoryId = "GONE";
    app.noteBundleCompleteness("history", "GONE", true);
    assert.ok(app.state.incompleteRecoveryTimers["history|GONE"]);
    app.closeHistory();
    assert.equal(app.state.incompleteRecoveryTimers["history|GONE"], undefined,
      "a timer that outlives its view would re-read a flow nobody is looking at");
    assert.equal(app.state.incompleteRecoveryAttempts["history|GONE"], undefined,
      "and its streak is forgotten, so the next interruption starts fresh");
    restoreHistoryVisibility();
  });

  // -- (I5) ------------------------------------------------------------------
  check("(I5) mergeHistoryResponse surfaces `incomplete` on every delivery shape", () => {
    const shapes = [
      { delivery: "not_modified", records: [] },
      { delivery: "delta", records: [record(1)] },
      { delivery: "full", records: [record(0), record(1)] },
    ];
    shapes.forEach((base) => {
      const held = [record(0)];
      const flagged = app.mergeHistoryResponse(
        { ...base, incomplete: true }, held, held);
      assert.equal(flagged.incomplete, true,
        `delivery=${base.delivery} must carry the incomplete statement through`);
      const settled = app.mergeHistoryResponse(
        { ...base, incomplete: false }, held, held);
      assert.equal(settled.incomplete, false,
        `delivery=${base.delivery} must report a settled bundle as settled`);
    });
    // A legacy server that sends no such field is a settled bundle, not an
    // interrupted one — nothing is re-read on its account.
    assert.equal(
      app.mergeHistoryResponse({ delivery: "not_modified", records: [] }, [], []).incomplete,
      false);
  });
}
