/*
 * Flow-view connection-pool starvation + hung-request tests (G3).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerFlowDetailStallTests({app, check, checkAsync,
 * findOne, findAll})`.
 *
 * The defect these pin: opening a LARGE completed flow left the left sidebar on
 * its "Loading flow details…" placeholder forever — stuck, never "error".
 *
 *   * `pollFlowView` fired `refreshFlowDetail()` + `selfHealFlowConversation()`
 *     every 3s with no in-flight guard (`flowConversationEpoch` supersedes a
 *     RESPONSE, it never skips a REQUEST), and while the held record set is
 *     still empty the "silent" poll asks for the BARE full bundle — tens of MB
 *     and seconds of server work per response. Once per-response wall time
 *     passed ~6 x 3s the browser's per-origin connection pool was full of
 *     bundle pulls and `/api/flows/{id}` was queued behind them indefinitely.
 *   * `authedFetch` was a bare `fetch` with no deadline and no AbortController,
 *     so a request the browser never put on the wire yielded neither `!resp.ok`
 *     nor a `catch`: `noteDetailFetchFailure` never ran, and the placeholder was
 *     never replaced by the "Retrying…" copy.
 *
 * Coverage:
 *   (S1) a hung bundle pull is never stacked on by the poll, so the flow-detail
 *        request keeps getting a connection slot and the sidebar still renders
 *   (S2) the skipped self-heal tick is made up once the in-flight pull completes
 *   (S3) a hung flow-detail request hits its deadline, counts as a failure and
 *        swaps the placeholder for the "Retrying…" copy
 *   (S4) that copy is rendered from the locale pack by key (zh-CN proves it)
 *   (S5) a flow SWITCH aborts the outstanding requests and is NOT miscounted as
 *        a failure (the placeholder stays "Loading…", not "Retrying…")
 *   (S6) a deadline aborts the request, handing the connection slot back
 *   (S7) closing the view aborts what it left on the wire
 *   (S8) a first-open pull is never skipped by the guard (only polls defer)
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const I18N_DIR = path.join(HERE, "..", "..", "src", "tianluo", "server", "static", "i18n");

export async function registerFlowDetailStallTests(ctx) {
  const { app, check, checkAsync, findAll } = ctx;

  const flush = async (n = 6) => {
    for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0));
  };
  // The production deadline timer is unref'd — a pending deadline must never be
  // the only thing keeping the runtime alive — so a case that WAITS on one has
  // to keep Node's event loop busy for the duration, or the process just exits.
  const awaitingDeadline = async (fn) => {
    const keepAlive = setInterval(() => {}, 5);
    try {
      return await fn();
    } finally {
      clearInterval(keepAlive);
    }
  };

  const asstRecord = (content, ts) => ({
    step_id: "01_discovery_a",
    step_type: "discovery",
    ordinal: ts,
    message: { role: "assistant", content, timestamp: ts },
  });

  const sidebarText = () => document.getElementById("flow-sidebar-body").textContent;

  // openFlowView/doCloseFlowView reach for the mobile drawer's aria state; the
  // shared FakeNode has no attribute API, so give just that node a no-op one
  // rather than widening the stub for every suite.
  const toggleNode = document.getElementById("flow-sidebar-toggle");
  if (typeof toggleNode.setAttribute !== "function") toggleNode.setAttribute = () => {};

  // A fetch double that models a browser's per-origin connection pool: at most
  // `limit` requests are ON THE WIRE, and everything beyond that sits in a queue
  // the server never sees — precisely the state in which a request neither
  // resolves nor rejects, which is what made the sidebar hang instead of error.
  // `classify(entry)` returns a payload to answer with immediately, or null to
  // leave the request hanging.
  function installPool(limit, classify) {
    const pool = { entries: [], active: 0, queue: [] };
    const pump = () => {
      while (pool.active < limit && pool.queue.length) start(pool.queue.shift());
    };
    const start = (entry) => {
      entry.started = true;
      pool.active += 1;
      const auto = classify ? classify(entry) : null;
      if (auto) entry.settle(auto);
    };
    globalThis.fetch = (url, init) => new Promise((resolve, reject) => {
      const entry = {
        url: String(url), started: false, settled: false, aborted: false,
        sawSignal: !!(init && init.signal),
      };
      const release = () => {
        if (entry.settled) return false;
        entry.settled = true;
        if (entry.started) { pool.active -= 1; pump(); }
        else {
          const i = pool.queue.indexOf(entry);
          if (i >= 0) pool.queue.splice(i, 1);
        }
        return true;
      };
      entry.settle = (payload, ok = true, status = 200) => {
        if (release()) resolve({ ok, status, json: () => Promise.resolve(payload) });
      };
      entry.fail = (err) => { if (release()) reject(err); };
      if (init && init.signal) {
        const onAbort = () => {
          if (entry.settled) return;
          entry.aborted = true;
          const err = new Error("The operation was aborted");
          err.name = "AbortError";
          entry.fail(err);
        };
        if (init.signal.aborted) onAbort();
        else init.signal.addEventListener("abort", onAbort);
      }
      if (entry.settled) return;
      pool.entries.push(entry);
      if (pool.active < limit) start(entry); else pool.queue.push(entry);
    });
    pool.of = (prefix) => pool.entries.filter((e) => e.url.startsWith(prefix));
    pool.bundles = () => pool.of("/api/history/");
    pool.details = () => pool.of("/api/flows/");
    return pool;
  }

  // Put the module state back where a fresh openFlowView would leave it, without
  // starting the real 3s interval (the cases below drive pollFlowView by hand).
  function freshFlowView(flowId) {
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];
    app.state.flowConversationProgress = null;
    app.state.flowConversationSignature = null;
    app.state.flowConversationInFlight = null;
    app.state.flowConversationDeferredSelfHeal = false;
    app.state.flowProgressionMarker = null;
    app.state.flowDetailReqSeq = 0;
    app.state.flowDetailAppliedSeq = 0;
    app.state.detailLoaded = false;
    app.state.detailFetchFailures = 0;
    app.state.periodicSnapshotActive = true;
    app.cancelProgressionGrace();
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    app.renderSidebarPlaceholder(app.tf("flow.sidebarLoading", "Loading flow details…"));
    return c;
  }

  const flowPayload = (flowId) => ({
    flow: {
      flow_id: flowId, current_step: "summarize", current_step_index: 51,
      status: "completed", task_type: "bugfix", project_root: "/repo",
      task_description: "big flow", total_steps: 52, step_history: [],
    },
    machine_id: "m1",
  });

  // -- (S1) + (S2) -----------------------------------------------------------
  await checkAsync(
    "(S1) a hung bundle pull is never stacked on: the detail poll keeps its slot",
    async () => {
      const saved = globalThis.fetch;
      freshFlowView("BIG");
      // 6 slots, like a real HTTP/1.1 origin pool. The history bundle hangs (the
      // 49MB flow the server is still shaping); the flow-detail read is small
      // and answers at once — IF it can get on the wire at all.
      const pool = installPool(6, (entry) =>
        entry.url.startsWith("/api/flows/") ? flowPayload("BIG") : null);
      try {
        for (let tick = 0; tick < 10; tick++) {
          app.pollFlowView();
          await flush();
        }
        const bundles = pool.bundles();
        assert.equal(bundles.length, 1,
          "ten poll ticks must leave exactly ONE bundle pull on the wire");
        assert.ok(bundles[0].url.endsWith("/api/history/BIG"),
          "the empty held record set still forces the bare full URL: " + bundles[0].url);
        assert.equal(app.state.flowConversationDeferredSelfHeal, true,
          "the skipped ticks are remembered, not silently dropped");

        const details = pool.details();
        assert.equal(details.length, 10, "every tick still asks for the flow detail");
        assert.ok(details.every((e) => e.started),
          "no flow-detail request may be queued behind the bundle pulls");
        assert.equal(pool.queue.length, 0, "nothing may be stuck in the pool queue");
        assert.equal(app.state.detailLoaded, true,
          "the sidebar got its data — the placeholder is gone");
        assert.ok(sidebarText().includes("BIG") && sidebarText().includes("completed"),
          "the sidebar rendered the flow, not the loading placeholder: " + sidebarText());
        assert.equal(app.state.detailFetchFailures, 0,
          "a healthy detail read must not be counted as a failure");

        // -- (S2) the deferred tick is made up as the pull completes ----------
        bundles[0].settle({
          records: [asstRecord("tail", 1)], progress: "p1", delivery: "full",
        });
        await flush();
        assert.equal(pool.bundles().length, 2,
          "the skipped self-heal is re-issued once the wire is free");
        assert.equal(app.state.flowConversationDeferredSelfHeal, false);
        pool.bundles()[1].settle({ records: [], delivery: "not_modified" });
        await flush();
        assert.equal(app.state.flowConversationInFlight, null,
          "the catch-up pull hands the guard back");
        assert.equal(pool.bundles().length, 2, "and does not loop");
      } finally {
        globalThis.fetch = saved;
        app.state.flowConversationInFlight = null;
        app.state.flowConversationDeferredSelfHeal = false;
        app.state.periodicSnapshotActive = false;
      }
    });

  // -- (S3) ------------------------------------------------------------------
  await checkAsync(
    "(S3) a hung flow-detail request hits its deadline and reaches the failure copy",
    async () => {
      const saved = globalThis.fetch;
      const savedTimeout = app.FETCH_TIMEOUTS.flowDetail;
      freshFlowView("HUNG");
      const pool = installPool(6, null);   // nothing ever answers
      app.FETCH_TIMEOUTS.flowDetail = 20;
      try {
        const loading = sidebarText();
        assert.ok(loading.includes("Loading flow details"),
          "precondition: the view opens on the loading placeholder");

        await awaitingDeadline(() => app.refreshFlowDetail());
        assert.equal(app.state.detailFetchFailures, 1,
          "a request that never answers must count as a failure, not hang forever");
        assert.equal(sidebarText(), loading,
          "one blip keeps the placeholder — only a repeat escalates");

        await awaitingDeadline(() => app.refreshFlowDetail());
        assert.equal(app.state.detailFetchFailures, 2);
        const text = sidebarText();
        assert.ok(text.includes("Retrying"),
          "the second failure must replace the placeholder with the retry copy: " + text);
        assert.ok(text.includes("timed out"),
          "and it must say the request timed out, not 'network error': " + text);

        const details = pool.details();
        assert.equal(details.length, 2);
        assert.ok(details.every((e) => e.sawSignal),
          "authedFetch must pass an AbortSignal so the request is cancellable");
        assert.ok(details.every((e) => e.aborted),
          "a deadline must ABORT the request — otherwise the slot is leaked");
      } finally {
        app.FETCH_TIMEOUTS.flowDetail = savedTimeout;
        globalThis.fetch = saved;
      }
    });

  // -- (S4) ------------------------------------------------------------------
  await checkAsync(
    "(S4) the stall copy is rendered from the locale pack, not hardcoded",
    async () => {
      const saved = globalThis.fetch;
      const savedTimeout = app.FETCH_TIMEOUTS.flowDetail;
      const savedLang = app.I18N.lang;
      const savedDicts = {
        "en-US": app.I18N.dicts["en-US"], "zh-CN": app.I18N.dicts["zh-CN"],
      };
      freshFlowView("I18N");
      installPool(6, null);
      app.FETCH_TIMEOUTS.flowDetail = 20;
      try {
        for (const code of ["en-US", "zh-CN"]) {
          app.I18N.dicts[code] = JSON.parse(
            fs.readFileSync(path.join(I18N_DIR, `${code}.json`), "utf-8"));
        }
        app.I18N.lang = "zh-CN";
        await awaitingDeadline(() => app.refreshFlowDetail());
        await awaitingDeadline(() => app.refreshFlowDetail());
        const text = sidebarText();
        assert.equal(text, app.I18N.dicts["zh-CN"]["flow.detailRetrying"]
          .replace("{message}", app.I18N.dicts["zh-CN"]["flow.detailTimeout"]),
          "the retry line must come from the zh-CN pack by key: " + text);
      } finally {
        app.I18N.lang = savedLang;
        app.I18N.dicts["en-US"] = savedDicts["en-US"];
        app.I18N.dicts["zh-CN"] = savedDicts["zh-CN"];
        app.FETCH_TIMEOUTS.flowDetail = savedTimeout;
        globalThis.fetch = saved;
      }
    });

  // -- (S5) ------------------------------------------------------------------
  await checkAsync(
    "(S5) switching flows aborts the old requests and is not counted as a failure",
    async () => {
      const saved = globalThis.fetch;
      const pool = installPool(6, null);   // both flows' requests hang
      try {
        app.openFlowView("FLOW-A");
        await flush();
        const aDetails = pool.details();
        const aBundles = pool.bundles();
        assert.equal(aDetails.length, 1, "opening a flow reads its detail");
        assert.equal(aBundles.length, 1, "…and pulls its conversation");
        assert.ok(sidebarText().includes("Loading flow details"),
          "precondition: flow A sits on the loading placeholder");

        app.openFlowView("FLOW-B");
        await flush();
        assert.ok(aDetails[0].aborted,
          "flow A's detail read must be cancelled, not left occupying a slot");
        assert.ok(aBundles[0].aborted,
          "flow A's bundle pull must be cancelled too");
        assert.equal(app.state.detailFetchFailures, 0,
          "a deliberate cancel is NOT a detail-fetch failure");
        assert.ok(!sidebarText().includes("Retrying"),
          "…so the flow switch must not paint the retry copy: " + sidebarText());
        assert.equal(app.state.flowConversationInFlight != null, true,
          "flow B's own pull owns the guard after the switch");

        // -- (S7) closing the view lets go of what it left on the wire --------
        const bDetails = pool.details().slice(1);
        const bBundles = pool.bundles().slice(1);
        app.doCloseFlowView();
        await flush();
        assert.ok(bDetails.every((e) => e.aborted) && bBundles.every((e) => e.aborted),
          "(S7) closing the flow view aborts its outstanding requests");
        assert.equal(app.state.flowConversationInFlight, null);
        assert.equal(app.state.detailFetchFailures, 0,
          "closing the view must not be reported as a failure either");
      } finally {
        globalThis.fetch = saved;
        app.state.flowConversationInFlight = null;
        app.state.flowConversationDeferredSelfHeal = false;
      }
    });

  // -- (S6) ------------------------------------------------------------------
  await checkAsync(
    "(S6) a deadline aborts the request so the connection slot comes back",
    async () => {
      const saved = globalThis.fetch;
      const pool = installPool(1, null);   // a one-slot pool makes the leak visible
      try {
        let err = null;
        try {
          await awaitingDeadline(() =>
            app.authedFetch("/api/flows/SLOW", undefined, { timeoutMs: 20 }));
        } catch (e) { err = e; }
        assert.ok(err && err.isTimeout, "the deadline must surface as a rejection");
        assert.equal(app.isAbortError(err), false,
          "a deadline is distinguishable from a deliberate cancel");
        assert.ok(pool.entries[0].aborted, "…and it aborts the request");

        // The freed slot is the whole point: the next request runs immediately.
        const second = app.authedFetch("/api/issues", undefined, { timeoutMs: 0 });
        await flush(2);
        assert.equal(pool.entries.length, 2);
        assert.ok(pool.entries[1].started,
          "the timed-out request released its connection slot");
        pool.entries[1].settle({ issues: [] });
        await second;
      } finally {
        globalThis.fetch = saved;
      }
    });

  // -- (S8) ------------------------------------------------------------------
  await checkAsync(
    "(S8) only the poll defers — a first open / reconnect pull is never skipped",
    async () => {
      const saved = globalThis.fetch;
      freshFlowView("GUARD");
      const pool = installPool(6, null);
      try {
        const first = app.loadFlowConversation("GUARD");
        await flush(2);
        assert.equal(pool.bundles().length, 1);

        // The 3s self-heal defers to it…
        await app.loadFlowConversation("GUARD", { silent: true });
        assert.equal(pool.bundles().length, 1, "the silent poll defers");

        // …but a WS reconnect refresh is event-driven and must still go out.
        const reconnect = app.loadFlowConversation("GUARD", { incremental: true });
        await flush(2);
        assert.equal(pool.bundles().length, 2,
          "a reconnect refresh must not be swallowed by the poll guard");

        pool.bundles()[1].settle({ records: [], delivery: "not_modified" });
        await reconnect;
        pool.bundles()[0].settle({
          records: [asstRecord("x", 1)], progress: "p", delivery: "full",
        });
        await first;
        await flush();
      } finally {
        globalThis.fetch = saved;
        app.state.flowConversationInFlight = null;
        app.state.flowConversationDeferredSelfHeal = false;
        app.state.periodicSnapshotActive = false;
      }
    });

  // Pure guard: the copy keys exist in code (the packs are checked by pytest).
  check("(S9) the stall states are addressed by i18n key", () => {
    assert.equal(typeof app.tf("flow.detailTimeout", "Loading flow details timed out."),
      "string");
    assert.equal(findAll(document.getElementById("flow-sidebar-body"), "nope").length, 0);
  });
}
