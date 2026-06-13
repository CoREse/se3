/*
 * Reply-send error-handling tests (Group G1 / G2).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerReplySendErrorHandlingTests({app, check, findOne, findAll})` so the
 * parent harness can drive the same check() reporter and the same `app` module
 * export.
 *
 * Regression context (issue #193, same class as the #191 webui-issue-create
 * false-timeout): clicking "确认并继续 (输入 1)" against a discovery_confirm
 * pending item POSTed the "1", the backend received it and advanced the flow,
 * yet the frontend popped "Could not send — network error reaching the server"
 * and the reply never appeared in the conversation. The root cause was a
 * too-wide try/catch in `sendReply`: a fault in the post-success rendering
 * (inside `appendLocalReply`) fell into the network-error catch, so a delivered
 * reply was reported as a failed send and its optimistic echo was lost.
 *
 * The fix splits the concerns:
 *   - success is decided solely by `resp.ok` (the success toast + input clear
 *     run before any rendering), and
 *   - `appendLocalReply` writes the echo into `state.flowConversationRecords`
 *     FIRST, then renders behind its own try/catch so a render fault is logged
 *     (best-effort) and never bubbles back into the success path.
 *
 * These tests drive the exported `appendLocalReply` against the shared DOM stub
 * to lock that contract: the echo lands in state even when a post-success
 * render throws, and the call never rethrows.
 */
import assert from "node:assert/strict";

export async function registerReplySendErrorHandlingTests(ctx) {
  const { app } = ctx;
  const check = ctx.check;
  const checkAsync = ctx.checkAsync;

  // Toast spy helpers. `showToast` appends a `<div class="toast toast-<kind>">`
  // (text = message) into the `toast-container` element. We read that container
  // back to assert which toast a code path produced.
  function clearToasts() {
    const c = document.getElementById("toast-container");
    c.childNodes = [];
    return c;
  }
  function toastsOfKind(kind) {
    const c = document.getElementById("toast-container");
    return (c.children || []).filter((n) => n && n.classList && n.classList.contains("toast-" + kind));
  }
  function anyToastTextIncludes(needle) {
    const c = document.getElementById("toast-container");
    return (c.children || []).some((n) => n && String(n.textContent || "").includes(needle));
  }

  // Drive sendReply with a stubbed fetch + suppressed real timers (the 8s send
  // gate + the toast TTL timeouts would otherwise keep the node event loop
  // alive after the suite finishes). Returns whether sendReply rejected.
  async function runSendReply(flowId, target, text, { ok = true, reject = false } = {}) {
    const savedFetch = globalThis.fetch;
    const savedSetTimeout = globalThis.setTimeout;
    // Neuter scheduling so no timer outlives the test (and so the success path
    // does not arm the real 8s settle timer). clearTimeout on the dummy id is a
    // harmless no-op.
    globalThis.setTimeout = () => 0;
    globalThis.fetch = () =>
      reject
        ? Promise.reject(new TypeError("Failed to fetch"))
        : Promise.resolve({ ok, status: ok ? 200 : 500, json: () => Promise.resolve({}) });
    let rejected = null;
    try {
      await app.sendReply(flowId, target, text);
    } catch (e) {
      rejected = e;
    } finally {
      globalThis.fetch = savedFetch;
      globalThis.setTimeout = savedSetTimeout;
      // Belt-and-suspenders: drop any send gate the success path left armed.
      app.settlePendingSend();
    }
    return rejected;
  }

  function freshFlow(flowId, callId) {
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];
    app.state.flowDetail = {
      flow_id: flowId,
      status: "running",
      pending_calls: [{ call_id: callId, kind: "discovery_confirm", prompt: "Confirm?" }],
    };
    app.state.pendingSendTimer = null;
    app.state.pendingSendSettleKey = null;
    app.state.pendingSendBaselineCallIds = null;
    clearToasts();
  }

  // ---- baseline: a normal reply lands as a tagged optimistic echo ----------
  check("G1 appendLocalReply baseline: a normal reply lands as a tagged user echo", () => {
    const flowId = "flow-reply-baseline";
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];

    app.appendLocalReply(flowId, { kind: "call", callId: "c1" }, "continue");

    assert.equal(app.state.flowConversationRecords.length, 1,
      "exactly one echo record appended");
    const rec = app.state.flowConversationRecords[0];
    assert.equal(rec.__localEcho, true, "echo tagged for reconciliation");
    assert.equal(rec.__localEchoText, "continue", "literal reply text retained");
    assert.equal(typeof rec.__localEchoPriorAuth, "number",
      "stable per-text rank recorded");
    const norm = app.normalizeRecord(rec);
    assert.equal(norm.role, "user", "echo normalizes to a user bubble");
    assert.equal(norm.content, "continue", "echo carries the reply text");
  });

  // ---- core regression: render throws -> echo still lands, no bubble -------
  check("G1 appendLocalReply records the echo before a post-success render throws and never bubbles", () => {
    const flowId = "flow-reply-render-throw";
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];

    // Force the rebuild path (clear any prior incremental state) so the very
    // first render op — `container.innerHTML = ""` inside renderConversation —
    // runs, then make it throw. This stands in for a fault ANYWHERE in the
    // post-success render chain (renderConversation / refreshFlowStickyHeader /
    // updateFlowUsageBadge / scrollFlowConversationToBottom).
    const container = document.getElementById("flow-conversation");
    container.__convState = undefined;
    Object.defineProperty(container, "innerHTML", {
      configurable: true,
      get() { return ""; },
      set() { throw new Error("simulated post-success render failure"); },
    });

    // Spy on console.error so we can assert the fault is logged (observable)
    // rather than silently swallowed — and keep the test output clean.
    const savedError = console.error;
    let errorLogged = 0;
    console.error = () => { errorLogged += 1; };

    let threw = null;
    try {
      // The discovery-confirm "1" path the issue reported.
      app.appendLocalReply(flowId, { kind: "discovery_confirm", callId: "dc1" }, "1");
    } catch (e) {
      threw = e;
    } finally {
      console.error = savedError;
      // Restore the prototype innerHTML accessor for the shared container.
      delete container.innerHTML;
    }

    assert.equal(threw, null,
      "appendLocalReply must swallow a post-success render fault, not rethrow it");
    assert.ok(errorLogged >= 1,
      "the render fault is logged (observable) rather than silently dropped");

    // The echo landed in state BEFORE the render threw, so the "1" confirm is
    // in the conversation list and survives the render failure — the next ws
    // history_data push will re-render it (or its reconciled authoritative copy).
    const echoes = app.state.flowConversationRecords.filter((r) => r && r.__localEcho);
    assert.equal(echoes.length, 1, "exactly one optimistic echo recorded");
    assert.equal(echoes[0].__localEchoText, "1", "the '1' confirm text is preserved");
    const norm = app.normalizeRecord(echoes[0]);
    assert.equal(norm.role, "user");
    assert.equal(norm.content, "1");
  });

  // ---- core regression: rank computation throws -> echo still lands --------
  check("G1 appendLocalReply records the echo even when the per-text rank computation throws", () => {
    const flowId = "flow-reply-rank-throw";
    app.state.selectedFlowId = flowId;
    // Seed the conversation with a pathological record whose property access
    // throws when normalizeRecord / comparableUserText walk it during rank
    // computation. The guard around the rank loop must swallow that so the echo
    // is still recorded and appendLocalReply never bubbles into sendReply's
    // catch (which would re-fire the spurious network-error toast — issue #193).
    const poison = {};
    Object.defineProperty(poison, "message", {
      configurable: true,
      enumerable: true,
      get() { throw new Error("simulated pathological record during rank scan"); },
    });
    app.state.flowConversationRecords = [poison];

    const savedError = console.error;
    let errorLogged = 0;
    console.error = () => { errorLogged += 1; };

    let threw = null;
    try {
      app.appendLocalReply(flowId, { kind: "discovery_confirm", callId: "dcR" }, "1");
    } catch (e) {
      threw = e;
    } finally {
      console.error = savedError;
    }

    assert.equal(threw, null,
      "a throw during rank computation must be swallowed, not rethrown");
    assert.ok(errorLogged >= 1,
      "the rank-computation fault is logged (observable) rather than silently dropped");
    // The echo still landed despite the rank scan throwing — the "1" confirm is
    // in the conversation list (rank falls back to 0).
    const echoes = app.state.flowConversationRecords.filter((r) => r && r.__localEcho);
    assert.equal(echoes.length, 1, "the '1' confirm echo is recorded despite the rank fault");
    assert.equal(echoes[0].__localEchoText, "1", "the '1' confirm text is preserved");
    assert.equal(echoes[0].__localEchoPriorAuth, 0, "rank falls back to 0 on a scan fault");
  });

  // ---- guard: a reply for a non-selected flow is ignored -------------------
  check("G1 appendLocalReply ignores a reply for a non-selected flow", () => {
    app.state.selectedFlowId = "flow-A";
    app.state.flowConversationRecords = [];
    app.appendLocalReply("flow-B", { kind: "call", callId: "x" }, "nope");
    assert.equal(app.state.flowConversationRecords.length, 0,
      "no echo is spliced into a conversation the user is not viewing");
  });

  // ---- integrated: the actual #193 symptom through sendReply --------------
  //
  // The unit tests above only exercise `appendLocalReply` in isolation. They do
  // NOT observe the user-facing symptom the issue reported: a delivered
  // discovery-confirm "1" whose post-success render throws being mis-reported as
  // a network error. These integrated tests drive the real `sendReply` against a
  // stubbed `fetch` and read back the toast container, so a future reorder
  // (moving `appendLocalReply` ahead of `showToast`, or re-widening the
  // try/catch) is caught — every existing test would still pass otherwise.

  await checkAsync(
    "G2 sendReply: delivered '1' whose post-success render throws still shows success, not a network error",
    async () => {
      const flowId = "flow-send-render-throw";
      freshFlow(flowId, "dc1");

      // Force the rebuild path and make the first render op throw — standing in
      // for a fault ANYWHERE in the post-success render chain reached via
      // appendLocalReply -> renderConversation.
      const container = document.getElementById("flow-conversation");
      container.__convState = undefined;
      Object.defineProperty(container, "innerHTML", {
        configurable: true,
        get() { return ""; },
        set() { throw new Error("simulated post-success render failure"); },
      });

      const savedError = console.error;
      let errorLogged = 0;
      console.error = () => { errorLogged += 1; };

      let rejected;
      try {
        rejected = await runSendReply(flowId, { kind: "discovery_confirm", callId: "dc1" }, "1", { ok: true });
      } finally {
        console.error = savedError;
        delete container.innerHTML;
      }

      assert.equal(rejected, null, "sendReply must not reject on a delivered reply");
      // The success toast fired, decided solely by resp.ok …
      const successes = toastsOfKind("success");
      assert.equal(successes.length, 1, "exactly one success toast");
      assert.equal(successes[0].textContent, "Response sent.",
        "the delivered '1' shows the 'Response sent.' success toast");
      // … and the spurious network-error toast did NOT fire.
      assert.equal(toastsOfKind("error").length, 0, "no error toast on a delivered reply");
      assert.ok(!anyToastTextIncludes("network error"),
        "the 'network error reaching the server' toast must be suppressed on success");
      // The render fault was still observable (best-effort logging), not silent.
      assert.ok(errorLogged >= 1, "the post-success render fault is logged");
      // And the optimistic echo landed despite the render throw.
      const echoes = app.state.flowConversationRecords.filter((r) => r && r.__localEcho);
      assert.equal(echoes.length, 1, "the '1' confirm echo is in the conversation list");
      assert.equal(echoes[0].__localEchoText, "1");
    },
  );

  await checkAsync(
    "G2 sendReply: a clean successful send shows the success toast and the echo",
    async () => {
      const flowId = "flow-send-clean";
      freshFlow(flowId, "dc2");
      const container = document.getElementById("flow-conversation");
      container.__convState = undefined;

      const rejected = await runSendReply(flowId, { kind: "discovery_confirm", callId: "dc2" }, "1", { ok: true });

      assert.equal(rejected, null, "a clean send never rejects");
      assert.equal(toastsOfKind("success").length, 1, "success toast on a clean send");
      assert.equal(toastsOfKind("error").length, 0, "no error toast on a clean send");
      const echoes = app.state.flowConversationRecords.filter((r) => r && r.__localEcho);
      assert.equal(echoes.length, 1, "echo recorded on a clean send");
    },
  );

  await checkAsync(
    "G2 sendReply: a genuine fetch network failure DOES surface the network-error toast",
    async () => {
      const flowId = "flow-send-netfail";
      freshFlow(flowId, "dc3");

      const rejected = await runSendReply(
        flowId, { kind: "discovery_confirm", callId: "dc3" }, "1", { reject: true });

      // sendReply catches the network error internally (it does not rethrow),
      // but it MUST report it — this is the true-negative that proves the
      // success-path suppression above is not blanket-swallowing real failures.
      assert.equal(rejected, null, "sendReply handles the network error without rethrowing");
      assert.equal(toastsOfKind("success").length, 0, "no success toast on a real network failure");
      assert.equal(toastsOfKind("error").length, 1, "the network-error toast fires on a real failure");
      assert.ok(anyToastTextIncludes("network error"),
        "a real fetch rejection still reports 'network error reaching the server'");
      // No optimistic echo on a failed send.
      const echoes = app.state.flowConversationRecords.filter((r) => r && r.__localEcho);
      assert.equal(echoes.length, 0, "no echo recorded when the send actually failed");
    },
  );

  await checkAsync(
    "G2 sendReply: a non-2xx response stays on the failure path (no success toast, no echo)",
    async () => {
      // The fix deliberately keeps an HTTP-level failure (resp.ok === false) on
      // the failure branch — only the SUCCESS path suppresses the spurious
      // network-error toast. This locks the boundary: a future change that
      // re-widened success handling to treat a non-2xx as success (or dropped
      // the else branch's settlePendingSend) must fail here.
      const flowId = "flow-send-http-fail";
      freshFlow(flowId, "dc4");

      const rejected = await runSendReply(
        flowId, { kind: "discovery_confirm", callId: "dc4" }, "1", { ok: false });

      assert.equal(rejected, null, "sendReply handles a non-2xx response without rethrowing");
      // It is the "Could not send: ..." failure toast, NOT a success and NOT the
      // network-error toast (which is reserved for an actual fetch rejection).
      assert.equal(toastsOfKind("success").length, 0, "no success toast on a non-2xx response");
      assert.equal(toastsOfKind("error").length, 1, "exactly one error toast on a non-2xx response");
      assert.ok(anyToastTextIncludes("Could not send:"),
        "a non-2xx response shows the 'Could not send: ...' failure toast");
      assert.ok(!anyToastTextIncludes("network error"),
        "a non-2xx response is NOT mis-reported as a network error");
      // No optimistic echo on a rejected (non-2xx) send — the else branch never
      // reaches appendLocalReply, proving the success/failure boundary holds.
      const echoes = app.state.flowConversationRecords.filter((r) => r && r.__localEcho);
      assert.equal(echoes.length, 0, "no echo recorded when the server rejected the send");
    },
  );
}
