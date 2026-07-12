/*
 * Per-round token-usage footnote tests (Group G5).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerRoundUsageTests({app, check, findOne, findAll})` so the parent
 * harness drives the same check() reporter and the same `app` module export.
 *
 * Coverage:
 *   (a) buildRoundUsageFootnote — 『本轮 X in / Y out · 累计 X in / Y out』wording,
 *       null on empty round, cumulative fallback to the round when missing/empty.
 *   (b) accumulateRoundUsageByStep — running sum grouped by step_id, de-dup of
 *       re-delivered identical records, null for usage-less records.
 *   (c) normalizeRecord exposes norm.tokenUsage from a record's token_usage.
 *   (d) discovery / confirm / no-result assistant render paths append the
 *       footnote when (and only when) the round carried usage.
 *   (e) confirmation round (no LLM call) — assistant record with no token_usage
 *       renders no per-round footnote; step-level cumulative on the discovery
 *       report card still renders with all fields (in/out/cache/cost).
 *   (f) multi-round cumulative with cache and cost — accumulateRoundUsageByStep
 *       includes cache_read/cache_creation/cost in its running sum.
 */
import assert from "node:assert/strict";

export function registerRoundUsageTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  const USAGE = (over = {}) => ({
    input_tokens: 1000,
    output_tokens: 200,
    cache_creation_input_tokens: 50,
    cache_read_input_tokens: 800,
    total_cost_usd: 0.0123,
    ...over,
  });

  // An assistant chat record carrying per-call token_usage (the shape
  // record_response writes on the assistant ChatMessage, forwarded inside the
  // daemon `message` envelope). `ts` distinguishes successive rounds.
  const asstUsage = (stepId, usage, stepType = "discovery", ts = undefined,
    content = "hi") => ({
    step_id: stepId,
    step_type: stepType,
    message: {
      role: "assistant",
      content,
      timestamp: ts,
      token_usage: usage,
    },
  });

  // ---- (c) normalizeRecord exposes norm.tokenUsage ------------------------
  check("G5 normalizeRecord exposes norm.tokenUsage from record token_usage", () => {
    const norm = app.normalizeRecord(asstUsage("01_discovery_a", USAGE()));
    assert.ok(norm.tokenUsage, "expected norm.tokenUsage");
    assert.equal(norm.tokenUsage.input_tokens, 1000);
    assert.equal(norm.tokenUsage.output_tokens, 200);
  });

  check("G5 normalizeRecord tokenUsage is null when the record carries none", () => {
    const norm = app.normalizeRecord({
      step_id: "01_discovery_a", step_type: "discovery",
      message: { role: "assistant", content: "hi" },
    });
    assert.equal(norm.tokenUsage, null);
  });

  // ---- (a) buildRoundUsageFootnote ----------------------------------------
  check("G5 buildRoundUsageFootnote renders the 本轮 / 累计 wording", () => {
    const foot = app.buildRoundUsageFootnote(
      USAGE(), USAGE({ input_tokens: 1500, output_tokens: 250 }));
    assert.ok(foot, "expected a footnote node");
    const text = findOne(foot, "round-usage__text");
    assert.ok(text, "expected a .round-usage__text span");
    assert.equal(text.textContent, "This round 1,000 in / 200 out · Total 1,500 in / 250 out");
  });

  check("G5 buildRoundUsageFootnote returns null when the round consumed nothing", () => {
    assert.equal(app.buildRoundUsageFootnote(undefined, USAGE()), null);
    assert.equal(app.buildRoundUsageFootnote(null, USAGE()), null);
    assert.equal(app.buildRoundUsageFootnote({}, USAGE()), null);
    assert.equal(app.buildRoundUsageFootnote(
      { input_tokens: 0, total_cost_usd: 0 }, USAGE()), null);
  });

  check("G5 buildRoundUsageFootnote falls back to the round when cumulative empty/missing", () => {
    // Single-round step (or a direct call without a precomputed cumulative):
    // 本轮 == 累计.
    for (const cum of [undefined, null, {}]) {
      const foot = app.buildRoundUsageFootnote(USAGE(), cum);
      const text = findOne(foot, "round-usage__text").textContent;
      assert.equal(text, "This round 1,000 in / 200 out · Total 1,000 in / 200 out");
    }
  });

  check("G5 buildRoundUsageFootnote never renders NaN on a partial round dict", () => {
    const foot = app.buildRoundUsageFootnote({ input_tokens: 5 }, undefined);
    const text = findOne(foot, "round-usage__text").textContent;
    assert.equal(text, "This round 5 in / 0 out · Total 5 in / 0 out");
    assert.equal(text.includes("NaN"), false);
  });

  // ---- (b) accumulateRoundUsageByStep -------------------------------------
  check("G5 accumulateRoundUsageByStep running-sums rounds of one step_id", () => {
    // Two discovery rounds share one step_id; the cumulative at round 2 is the
    // sum of both rounds (mirroring the CLI carried + current arithmetic).
    const records = [
      asstUsage("01_discovery_a", USAGE(), "discovery", 1),
      asstUsage("01_discovery_a", USAGE({ input_tokens: 500, output_tokens: 50 }),
        "discovery", 2),
    ];
    const cum = app.accumulateRoundUsageByStep(records);
    assert.equal(cum.length, 2);
    assert.equal(cum[0].input_tokens, 1000);
    assert.equal(cum[0].output_tokens, 200);
    // Round 2 cumulative = 1000 + 500 in, 200 + 50 out.
    assert.equal(cum[1].input_tokens, 1500);
    assert.equal(cum[1].output_tokens, 250);
  });

  check("G5 accumulateRoundUsageByStep keeps step_ids independent", () => {
    const records = [
      asstUsage("01_discovery_a", USAGE(), "discovery", 1),
      asstUsage("06_confirm_b", USAGE({ input_tokens: 300 }), "confirm", 2),
    ];
    const cum = app.accumulateRoundUsageByStep(records);
    // Each step's cumulative reflects only its own rounds.
    assert.equal(cum[0].input_tokens, 1000);
    assert.equal(cum[1].input_tokens, 300);
  });

  check("G5 accumulateRoundUsageByStep de-dups re-delivered identical records", () => {
    // A record re-delivered across snapshots/reconnects shares its recordKey and
    // must NOT advance the running sum; both positions snapshot the same total.
    const rec = asstUsage("01_discovery_a", USAGE(), "discovery", 1);
    const cum = app.accumulateRoundUsageByStep([rec, rec]);
    assert.equal(cum[0].input_tokens, 1000);
    assert.equal(cum[1].input_tokens, 1000); // not 2000
  });

  check("G5 accumulateRoundUsageByStep yields null for usage-less / malformed records", () => {
    const cum = app.accumulateRoundUsageByStep([
      asstUsage("01_discovery_a", USAGE(), "discovery", 1),
      { step_id: "02_x", step_type: "discovery",
        message: { role: "assistant", content: "no usage" } }, // no token_usage
      asstUsage("03_y", { input_tokens: 0, total_cost_usd: 0 }, "discovery", 3), // empty
      null,                                                      // malformed
      "garbage",                                                 // malformed
    ]);
    assert.ok(cum[0]);
    assert.equal(cum[1], null);
    assert.equal(cum[2], null);
    assert.equal(cum[3], null);
    assert.equal(cum[4], null);
  });

  check("G5 accumulateRoundUsageByStep returns [] for empty / non-array input", () => {
    assert.deepEqual(app.accumulateRoundUsageByStep([]), []);
    assert.deepEqual(app.accumulateRoundUsageByStep(null), []);
    assert.deepEqual(app.accumulateRoundUsageByStep(undefined), []);
  });

  // ---- (d) assistant render paths append the footnote ----------------------
  const discoveryResult = "```json\n" + JSON.stringify({
    refined_description: "Do the thing",
    questions: ["q1?"],
  }) + "\n```";

  check("G5 discovery assistant bubble shows the per-round footnote", () => {
    const norm = {
      raw: { raw_json: null, raw_ndjson: null },
      tokenUsage: USAGE(),
      cumulativeUsage: USAGE({ input_tokens: 1500, output_tokens: 250 }),
    };
    const frag = app.renderDiscoveryAssistant(discoveryResult, norm);
    assert.ok(frag, "discovery renderer returns a fragment");
    const wrap = document.createElement("div");
    wrap.appendChild(frag);
    const text = findOne(wrap, "round-usage__text");
    assert.ok(text, "expected a per-round footnote in the discovery bubble");
    assert.equal(text.textContent, "This round 1,000 in / 200 out · Total 1,500 in / 250 out");
  });

  check("G5 discovery assistant bubble has NO footnote when the round carried no usage", () => {
    const norm = { raw: { raw_json: null, raw_ndjson: null } }; // no tokenUsage
    const frag = app.renderDiscoveryAssistant(discoveryResult, norm);
    const wrap = document.createElement("div");
    wrap.appendChild(frag);
    assert.equal(findAll(wrap, "round-usage").length, 0,
      "a round with no LLM usage must not render a footnote");
  });

  check("G5 confirm assistant bubble (generic fallback) shows the footnote", () => {
    // confirm is not in STEP_ASSISTANT_RENDERERS, so it flows through the
    // generic step.outputs fallback in renderAssistantBubble.
    const content = "Reviewing the plan.\n" + "```json\n" +
      JSON.stringify({ approved: true, feedback: "looks good" }) + "\n```";
    const norm = {
      stepType: "confirm",
      raw: { raw_json: null, raw_ndjson: null },
      tokenUsage: USAGE({ input_tokens: 400, output_tokens: 30 }),
      cumulativeUsage: USAGE({ input_tokens: 400, output_tokens: 30 }),
    };
    const frag = app.renderAssistantBubble(content, norm);
    const wrap = document.createElement("div");
    wrap.appendChild(frag);
    const text = findOne(wrap, "round-usage__text");
    assert.ok(text, "expected a per-round footnote on the confirm bubble");
    assert.equal(text.textContent, "This round 400 in / 30 out · Total 400 in / 30 out");
  });

  check("G5 no-result assistant bubble still shows the footnote when usage present", () => {
    // A thinking-only turn (no structured result) that nevertheless carried a
    // round usage reports it at the tail via the inline (no-result) path.
    const norm = {
      stepType: "discovery",
      raw: { raw_json: null, raw_ndjson: null },
      tokenUsage: USAGE({ input_tokens: 700, output_tokens: 12 }),
      cumulativeUsage: USAGE({ input_tokens: 700, output_tokens: 12 }),
    };
    const frag = app.renderAssistantBubble("just thinking, no json result", norm);
    const wrap = document.createElement("div");
    wrap.appendChild(frag);
    const text = findOne(wrap, "round-usage__text");
    assert.ok(text, "expected a footnote on the no-result inline path");
    assert.equal(text.textContent, "This round 700 in / 12 out · Total 700 in / 12 out");
  });

  check("G5 assistant bubble with no usage renders no footnote on any path", () => {
    const norm = { stepType: "discovery", raw: { raw_json: null, raw_ndjson: null } };
    const frag = app.renderAssistantBubble("just thinking, no usage", norm);
    const wrap = document.createElement("div");
    wrap.appendChild(frag);
    assert.equal(findAll(wrap, "round-usage").length, 0);
  });

  // ---- (e) confirmation round: no LLM call → no per-round footnote ---------
  //
  // The programmatic confirmation round does not call the LLM, so the assistant
  // record for that round carries no `token_usage`.  The per-round footnote
  // must be absent, but the step-level cumulative (from prior discovery rounds)
  // is still present on the step_completed report card.
  check("G5 confirmation-round assistant record with no token_usage renders no footnote", () => {
    // The confirmation round's assistant record has no LLM usage.
    const norm = {
      stepType: "discovery",
      raw: { raw_json: null, raw_ndjson: null },
      // tokenUsage is null/absent — the confirmation round made no LLM call.
      tokenUsage: null,
      cumulativeUsage: USAGE({ input_tokens: 3000, output_tokens: 800 }),
    };
    const frag = app.renderDiscoveryAssistant(discoveryResult, norm);
    const wrap = document.createElement("div");
    wrap.appendChild(frag);
    assert.equal(findAll(wrap, "round-usage").length, 0,
      "a confirmation round with no LLM call must not render a per-round footnote");
  });

  check("G5 step-level cumulative usage still renders on discovery report card after confirmation", () => {
    // After confirmation, the step_completed event carries the cumulative
    // token_usage across all discovery rounds. The report card footnote must
    // still render even though the confirmation round itself had no LLM call.
    const cumulativeUsage = USAGE({
      input_tokens: 3000,
      output_tokens: 800,
      cache_read_input_tokens: 500,
      cache_creation_input_tokens: 100,
      total_cost_usd: 0.025,
    });
    const card = app.renderStepReport({
      step_type: "discovery",
      step_id: "01_discovery_a",
      status: "completed",
      outputs: {
        refined_description: "Build a user feature",
        token_usage: cumulativeUsage,
      },
    });
    assert.ok(card, "expected a discovery report card");
    const foot = findOne(card, "step-report__usage");
    assert.ok(foot, "step-level cumulative footnote must render after confirmation");
    const val = findOne(foot, "step-report__usage-value");
    assert.ok(val, "expected a usage value span");
    const text = val.textContent;
    assert.ok(text.includes("in 3,000"), `expected cumulative input, got ${text}`);
    assert.ok(text.includes("out 800"), `expected cumulative output, got ${text}`);
    assert.ok(text.includes("cache r/w 500/100"), `expected cumulative cache, got ${text}`);
    assert.ok(text.includes("$0.0250"), `expected cumulative cost, got ${text}`);
  });

  // ---- (f) multi-round cumulative: accumulateRoundUsageByStep ---------------
  check("G5 accumulateRoundUsageByStep cumulative matches multi-round discovery total", () => {
    // Three discovery rounds: round 1 and 2 carry usage (LLM calls),
    // round 3 is the confirmation round with no LLM call (no token_usage).
    // accumulateRoundUsageByStep returns null for usage-less records, so
    // the confirmation round's position is null — the step-level cumulative
    // (from step.outputs.token_usage) is the authoritative total, not the
    // per-position running sum for usage-less rounds.
    const records = [
      asstUsage("01_discovery_a", USAGE({ input_tokens: 1000, output_tokens: 200 }),
        "discovery", 1),
      asstUsage("01_discovery_a", USAGE({ input_tokens: 2000, output_tokens: 600 }),
        "discovery", 2),
      // Round 3 (confirmation): no token_usage on the assistant record.
      {
        step_id: "01_discovery_a", step_type: "discovery",
        message: { role: "assistant", content: "confirmed", timestamp: 3 },
      },
    ];
    const cum = app.accumulateRoundUsageByStep(records);
    assert.equal(cum.length, 3);
    // Round 1 cumulative: 1000 in, 200 out.
    assert.equal(cum[0].input_tokens, 1000);
    assert.equal(cum[0].output_tokens, 200);
    // Round 2 cumulative: 1000+2000=3000 in, 200+600=800 out.
    assert.equal(cum[1].input_tokens, 3000);
    assert.equal(cum[1].output_tokens, 800);
    // Round 3 (confirmation, no LLM call): null — the per-round running sum
    // is undefined for usage-less rounds. The step-level cumulative is on
    // step.outputs.token_usage, tested separately in token_usage.test.mjs.
    assert.equal(cum[2], null);
  });

  check("G5 accumulateRoundUsageByStep includes cache and cost in running sum", () => {
    const records = [
      asstUsage("01_discovery_a", USAGE({
        input_tokens: 1000, output_tokens: 200,
        cache_read_input_tokens: 300, cache_creation_input_tokens: 50,
        total_cost_usd: 0.01,
      }), "discovery", 1),
      asstUsage("01_discovery_a", USAGE({
        input_tokens: 2000, output_tokens: 600,
        cache_read_input_tokens: 200, cache_creation_input_tokens: 100,
        total_cost_usd: 0.02,
      }), "discovery", 2),
    ];
    const cum = app.accumulateRoundUsageByStep(records);
    assert.equal(cum[1].cache_read_input_tokens, 500);
    assert.equal(cum[1].cache_creation_input_tokens, 150);
    assert.ok(Math.abs(cum[1].total_cost_usd - 0.03) < 1e-9);
  });
}
