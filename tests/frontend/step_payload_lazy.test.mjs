/*
 * A step event's held-back payload in the web console.
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerStepPayloadLazyTests({app, check, checkAsync,
 * findOne, findAll})`.
 *
 * The change under test: a `step_completed` / `step_failed` / `step_output`
 * record no longer ships its StepState snapshot's `inputs` — on a check-class
 * step that is a whole `scope_diff` plus `test_results` / `fix_history`, and
 * the default render reads none of it. The server replaces it with the handful
 * of scalars the renderers DO read and marks the record `step_inputs_lazy`; the
 * "查看原始" chip fetches the original on expand
 * (`GET /api/history/{flow}/detail?source=step`).
 *
 * Coverage:
 *   (S1) the report card renders identically from a shaped and a full record
 *   (S2) the usage chip's footnote survives the shaping (step_output)
 *   (S3) a fix round is still told apart from round one (inline `inputs`)
 *   (S4) the raw chip issues no request and builds no body while collapsed
 *   (S5) expanding fetches the step source (no tool_use_id) and prints the
 *        ORIGINAL record — markers gone, `scope_diff` back
 *   (S6) an unreachable payload renders the i18n unavailable line, retryably
 *   (S7) an UNMARKED step record still prints inline, with no request
 *   (S8) the record carries its detail address through normalizeRecord
 */
import assert from "node:assert/strict";

export async function registerStepPayloadLazyTests(ctx) {
  const { app, check, checkAsync, findOne, findAll } = ctx;

  const FLOW = "flow-step-payload";
  const STEP = "49_self_check_815ee905";
  const SCOPE_DIFF = Array.from(
    { length: 200 }, (_, i) => `+    line ${i} of a scope diff hunk`).join("\n");

  // The part of a self_check snapshot the report card actually reads.
  const OUTPUTS = {
    issues: [{ severity: "high", description: "an actionable finding" }],
    actionable_count: 1,
    self_check_result: { passed: false },
    token_usage: { input_tokens: 12000, output_tokens: 900 },
  };

  // The full `inputs` as the engine recorded it...
  const FULL_INPUTS = {
    task_description: "把 WebUI flow 历史视图改为尾部起步的窗口化加载。",
    task_description_base: "把 WebUI flow 历史视图改为尾部起步的窗口化加载。",
    adjudicated_description: "把 WebUI flow 历史视图改为尾部起步的窗口化加载。",
    scope_diff: SCOPE_DIFF,
    test_results: { passed: false, output_tail: "FAILED tests/test_x.py" },
    fix_history: [{ round: 1, note: "a long round note" }],
    fix_iteration: 2,
    is_fix_iteration: true,
  };
  // ...and what the server leaves inline in its place.
  const INLINE_INPUTS = { fix_iteration: 2, is_fix_iteration: true };

  const stepRecord = (opts) => {
    const o = opts || {};
    const message = {
      type: o.type || "step_completed",
      step_id: STEP,
      step_type: o.stepType || "self_check",
      timestamp: 1,
      data: {
        step: {
          step_id: STEP,
          step_type: o.stepType || "self_check",
          status: o.status || "completed",
          inputs: o.inputs || FULL_INPUTS,
          outputs: o.outputs || OUTPUTS,
          error_message: o.errorMessage || "",
        },
      },
    };
    if (o.lazy) {
      message.step_inputs_lazy = true;
      message.detail_flow = FLOW;
      message.detail_version = o.version || "v1";
    }
    return {
      step_id: STEP,
      step_type: o.stepType || "self_check",
      ordinal: o.ordinal === undefined ? 7 : o.ordinal,
      message: message,
    };
  };

  const renderOne = (records) => {
    const container = document.createElement("div");
    app.renderConversation(container, records, false);
    return container;
  };

  let calls = [];
  const savedFetch = globalThis.fetch;
  const installFetch = (handler) => {
    calls = [];
    globalThis.fetch = (url) => {
      calls.push(String(url));
      return Promise.resolve(handler(String(url)));
    };
  };
  const okJson = (payload) => ({
    ok: true, status: 200, json: () => Promise.resolve(payload),
  });
  const httpError = (status) => ({
    ok: false, status, json: () => Promise.resolve({}),
  });
    // The failure path threads through authedFetch, the shared detail cache's
  // eviction and paintRawInto's catch before the painter's own continuation,
  // so it needs a deeper microtask drain than the happy path.
  const settle = async () => { for (let i = 0; i < 60; i++) await Promise.resolve(); };

  // The chip button that opens the raw payload, and the <pre> it fills.
  const rawChip = (container) =>
    findOne(container, "step-event-chip-button");
  const rawPre = (container) => findOne(container, "raw-json");

  // (S1) ---------------------------------------------------------------------
  check("(S1) the report card is identical with the payload held back", () => {
    const full = renderOne([stepRecord({})]);
    const lazy = renderOne([stepRecord({ inputs: INLINE_INPUTS, lazy: true })]);
    const fullCard = findOne(full, "step-report");
    const lazyCard = findOne(lazy, "step-report");
    assert.ok(fullCard, "the un-summarized record renders a report card");
    assert.ok(lazyCard, "so must the summarized one");
    assert.equal(lazyCard.textContent, fullCard.textContent,
      "the default report card must not change by one character");
    // The chip header line is part of the default view too.
    assert.equal(rawChip(lazy).textContent, rawChip(full).textContent);
  });

  // (S2) ---------------------------------------------------------------------
  check("(S2) a step_output usage chip keeps its footnote", () => {
    const opts = {
      type: "step_output", status: "paused", stepType: "implement",
      outputs: { token_usage: { input_tokens: 12000, output_tokens: 900 } },
    };
    const full = renderOne([stepRecord(opts)]);
    const lazy = renderOne([stepRecord(
      Object.assign({ inputs: INLINE_INPUTS, lazy: true }, opts))]);
    const fullChip = rawChip(full);
    const lazyChip = rawChip(lazy);
    assert.ok(/12,?000|12000|12k|12\.0k/i.test(fullChip.textContent),
      `usage chip must name the token count, got ${fullChip.textContent}`);
    assert.equal(lazyChip.textContent, fullChip.textContent,
      "the usage chip label must be byte-identical");
    const fullFoot = findOne(full, "step-usage-footnote")
      || findOne(full, "step-report__usage");
    const lazyFoot = findOne(lazy, "step-usage-footnote")
      || findOne(lazy, "step-report__usage");
    assert.equal(!!lazyFoot, !!fullFoot, "the usage footnote must survive");
    if (fullFoot) assert.equal(lazyFoot.textContent, fullFoot.textContent);
  });

  // (S3) ---------------------------------------------------------------------
  check("(S3) a fix round is still told apart from round one", () => {
    // `implementFixIteration` reads `inputs.fix_iteration` — one of the very
    // few `inputs` keys the default render consumes, so it rides inline.
    assert.equal(
      app.implementFixIteration({ step_type: "implement", inputs: INLINE_INPUTS }),
      2, "the inline inputs must still carry the round number");
    assert.equal(
      app.implementFixIteration({ step_type: "implement", inputs: {} }), 0);
  });

  // (S4) ---------------------------------------------------------------------
  await checkAsync("(S4) a collapsed raw chip fetches nothing", async () => {
    app.clearLazyDetailCache();
    installFetch(() => okJson({}));
    const container = renderOne([
      stepRecord({ inputs: INLINE_INPUTS, lazy: true }),
    ]);
    await settle();
    assert.equal(calls.length, 0, "a collapsed chip must issue no request");
    assert.equal(rawPre(container), null,
      "and build no raw body until it is expanded");
  });

  // (S5) ---------------------------------------------------------------------
  await checkAsync("(S5) expanding fetches the step source and prints the original",
    async () => {
      app.clearLazyDetailCache();
      const original = stepRecord({}).message;
      installFetch(() => okJson({
        flow_id: FLOW, tool_use_id: "", source: "step",
        record: original, inputs: FULL_INPUTS,
      }));
      const container = renderOne([
        stepRecord({ inputs: INLINE_INPUTS, lazy: true }),
      ]);
      rawChip(container).dispatch("click");
      await settle();
      assert.equal(calls.length, 1, `expected one request, got ${calls}`);
      const url = calls[0];
      assert.ok(url.includes("source=step"), url);
      assert.ok(url.includes(`step_id=${encodeURIComponent(STEP)}`), url);
      assert.ok(url.includes("ordinal=7"), url);
      assert.ok(!url.includes("tool_use_id"),
        `a step event names no tool call: ${url}`);
      const text = rawPre(container).textContent;
      assert.ok(text.includes("+    line 0 of a scope diff hunk"),
        "the held-back scope_diff must come back on expand");
      assert.ok(!text.includes("step_inputs_lazy"),
        "the wire-only markers must not survive into the printed record");
      assert.ok(!text.includes("detail_flow"), text.slice(0, 200));
      // Re-expanding is served from the cache, not from a second request.
      rawChip(container).dispatch("click");
      rawChip(container).dispatch("click");
      await settle();
      assert.equal(calls.length, 1, "a second expand must reuse the cache");
    });

  // (S6) ---------------------------------------------------------------------
  await checkAsync("(S6) an unreachable payload says so and stays retryable",
    async () => {
      app.clearLazyDetailCache();
      installFetch(() => httpError(503));
      const container = renderOne([
        stepRecord({ inputs: INLINE_INPUTS, lazy: true, version: "v-down" }),
      ]);
      rawChip(container).dispatch("click");
      await settle();
      const text = rawPre(container).textContent;
      // Through i18n, never a hardcoded literal: the fallback passed here is
      // the same one app.js passes, so the assertion holds in either language.
      const unavailable = app.tf(
        "raw.unavailable",
        "The original record is unavailable right now; showing the summary.");
      assert.ok(unavailable, "the unavailable line must resolve to some text");
      assert.ok(text.includes(unavailable),
        `expected the localized unavailable line, got: ${text.slice(0, 120)}`);
      // The summary stays visible underneath — never passed off as the original.
      assert.ok(text.includes("fix_iteration"), text.slice(0, 200));
      const first = calls.length;
      assert.equal(first, 1);
      // Collapsing and re-expanding retries: that is the only recovery the
      // chip has, and a failure must not be cached as an answer.
      rawChip(container).dispatch("click");
      rawChip(container).dispatch("click");
      await settle();
      assert.ok(calls.length > first, "a failed body must stay retryable");
    });

  // (S7) ---------------------------------------------------------------------
  await checkAsync("(S7) an unmarked step record prints inline, silently",
    async () => {
      app.clearLazyDetailCache();
      installFetch(() => okJson({}));
      const container = renderOne([stepRecord({})]);
      rawChip(container).dispatch("click");
      await settle();
      assert.equal(calls.length, 0,
        "a record that lost nothing must not ask for anything");
      assert.ok(rawPre(container).textContent.includes("scope diff hunk"),
        "an inline payload prints exactly what it was handed");
    });

  // (S8) ---------------------------------------------------------------------
  check("(S8) the step record carries its detail address", () => {
    const norm = app.normalizeRecord(
      stepRecord({ inputs: INLINE_INPUTS, lazy: true, version: "v9" }));
    assert.equal(norm.kind, "step_completed");
    assert.equal(norm.detailFlow, FLOW);
    assert.equal(norm.detailVersion, "v9");
    assert.equal(norm.stepId, STEP);
    assert.equal(norm.ordinal, 7);
    // ...and the refs helper sees exactly one thing to fetch back.
    const refs = app.lazyRawRefsFor(norm, norm.raw.raw_json);
    assert.ok(refs, "the marked payload must report something to restore");
    assert.equal(refs.step, true);
    assert.equal(refs.raw.size, 0);
    assert.equal(refs.progress.size, 0);
    // An unmarked one reports nothing, so it prints without a request.
    const plain = app.normalizeRecord(stepRecord({}));
    assert.equal(app.lazyRawRefsFor(plain, plain.raw.raw_json), null);
  });

  globalThis.fetch = savedFetch;
}
