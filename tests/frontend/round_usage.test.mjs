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
    assert.equal(text.textContent, "本轮 1,000 in / 200 out · 累计 1,500 in / 250 out");
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
      assert.equal(text, "本轮 1,000 in / 200 out · 累计 1,000 in / 200 out");
    }
  });

  check("G5 buildRoundUsageFootnote never renders NaN on a partial round dict", () => {
    const foot = app.buildRoundUsageFootnote({ input_tokens: 5 }, undefined);
    const text = findOne(foot, "round-usage__text").textContent;
    assert.equal(text, "本轮 5 in / 0 out · 累计 5 in / 0 out");
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
    assert.equal(text.textContent, "本轮 1,000 in / 200 out · 累计 1,500 in / 250 out");
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
    assert.equal(text.textContent, "本轮 400 in / 30 out · 累计 400 in / 30 out");
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
    assert.equal(text.textContent, "本轮 700 in / 12 out · 累计 700 in / 12 out");
  });

  check("G5 assistant bubble with no usage renders no footnote on any path", () => {
    const norm = { stepType: "discovery", raw: { raw_json: null, raw_ndjson: null } };
    const frag = app.renderAssistantBubble("just thinking, no usage", norm);
    const wrap = document.createElement("div");
    wrap.appendChild(frag);
    assert.equal(findAll(wrap, "round-usage").length, 0);
  });
}
