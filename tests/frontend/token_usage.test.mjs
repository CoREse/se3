/*
 * Token-usage display tests (Group G4).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerTokenUsageTests({app, check, findOne, findAll})` so the parent
 * harness drives the same check() reporter and the same `app` module export.
 *
 * Coverage:
 *   (a) formatTokenUsage — labelled, unit-suffixed string; safe on missing /
 *       empty / partial input (missing fields → 0, no NaN).
 *   (b) isTokenUsageEmpty — empty / zero / partial detection.
 *   (c) accumulateSessionUsage — sums per-step token_usage, de-dups by step_id,
 *       order-independent, ignores non-step / empty / malformed records.
 *   (d) per-step report card footnote (`.step-report__usage`) — present when
 *       the step has usage, absent otherwise; the rest of the card is intact.
 *   (e) flow-view session badge (`#flow-usage-badge`) — hidden with no usage,
 *       shown + populated once usage exists.
 *   (f) discovery step cumulative usage footnote — present with all fields
 *       (in/out/cache r/w/cost) on the discovery report card when the step
 *       has multi-round cumulative usage, absent when usage is missing or
 *       all-zero; card body (refined_description) coexists with footnote.
 *   (g) session badge includes discovery steps — the badge sums discovery
 *       cumulative usage alongside other steps, including cache and cost.
 */
import assert from "node:assert/strict";

export function registerTokenUsageTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  const USAGE = (over = {}) => ({
    input_tokens: 1000,
    output_tokens: 200,
    cache_creation_input_tokens: 50,
    cache_read_input_tokens: 800,
    total_cost_usd: 0.0123,
    ...over,
  });

  // A step_completed conversation record carrying token_usage in outputs.
  // `ts` (optional) sets the record timestamp so two executions of the same
  // step_id (a fix-loop re-run) carry distinct per-record identities.
  const stepEvent = (stepId, usage, stepType = "analyze", ts = undefined) => ({
    step_id: stepId,
    step_type: stepType,
    message: {
      type: "step_completed",
      step_id: stepId,
      timestamp: ts,
      data: {
        step: {
          step_type: stepType,
          step_id: stepId,
          status: "completed",
          outputs: usage === undefined ? {} : { token_usage: usage },
        },
      },
    },
  });

  // ---- (a) formatTokenUsage -----------------------------------------------
  check("G4 formatTokenUsage renders labelled, comma-grouped fields", () => {
    const s = app.formatTokenUsage({
      input_tokens: 12345,
      output_tokens: 6789,
      cache_read_input_tokens: 1000,
      cache_creation_input_tokens: 200,
      total_cost_usd: 0.0123,
    });
    assert.equal(s, "in 12,345 · out 6,789 · cache r/w 1,000/200 · $0.0123");
  });

  check("G4 formatTokenUsage is safe on empty / null / partial input", () => {
    const zero = "in 0 · out 0 · cache r/w 0/0 · $0.0000";
    assert.equal(app.formatTokenUsage(undefined), zero);
    assert.equal(app.formatTokenUsage(null), zero);
    assert.equal(app.formatTokenUsage({}), zero);
    // Partial dict — only some fields present; the rest read as 0, no NaN.
    const partial = app.formatTokenUsage({ input_tokens: 5, total_cost_usd: 1 });
    assert.equal(partial, "in 5 · out 0 · cache r/w 0/0 · $1.0000");
    assert.equal(partial.includes("NaN"), false);
  });

  check("formatTokenUsage defers the cost column to the shared UsageSummary", () => {
    // A step whose calls report tokens but no provider actual cost must show
    // "unknown", never a fabricated $0 from the legacy five-field projection.
    const usage = {
      input_tokens: 100,
      output_tokens: 10,
      cache_read_input_tokens: 0,
      cache_creation_input_tokens: 0,
      total_cost_usd: 0,
    };
    const withSummary = app.formatTokenUsage(usage, {
      actual_cost_usd: null,
      totals: { logical_input_tokens: 100, output_tokens: 10 },
    });
    assert.equal(withSummary.includes("$0.0000"), false);
    assert.equal(withSummary.includes("unknown"), true);
    // A summary that DOES carry an actual cost renders it.
    const priced = app.formatTokenUsage(usage, {
      actual_cost_usd: 0.0123,
      totals: { logical_input_tokens: 100, output_tokens: 10 },
    });
    assert.equal(priced.includes("$0.0123"), true);
    // No summary → unchanged legacy behaviour.
    assert.equal(app.formatTokenUsage(usage).includes("$0.0000"), true);
  });

  check("G4 formatCostUsd fixes 4 decimal places and tolerates junk", () => {
    assert.equal(app.formatCostUsd(0.5), "$0.5000");
    assert.equal(app.formatCostUsd(null), "$0.0000");
    assert.equal(app.formatCostUsd("nope"), "$0.0000");
  });

  // ---- (b) isTokenUsageEmpty ----------------------------------------------
  check("G4 isTokenUsageEmpty true for null / {} / all-zero", () => {
    assert.equal(app.isTokenUsageEmpty(undefined), true);
    assert.equal(app.isTokenUsageEmpty(null), true);
    assert.equal(app.isTokenUsageEmpty({}), true);
    assert.equal(app.isTokenUsageEmpty({
      input_tokens: 0, output_tokens: 0,
      cache_creation_input_tokens: 0, cache_read_input_tokens: 0,
      total_cost_usd: 0,
    }), true);
  });

  check("G4 isTokenUsageEmpty false when any token or cost is non-zero", () => {
    assert.equal(app.isTokenUsageEmpty({ input_tokens: 1 }), false);
    assert.equal(app.isTokenUsageEmpty({ cache_read_input_tokens: 5 }), false);
    // Cost-only (e.g. a rounding artefact) still counts as non-empty.
    assert.equal(app.isTokenUsageEmpty({ total_cost_usd: 0.0001 }), false);
  });

  // ---- (c) accumulateSessionUsage -----------------------------------------
  check("G4 accumulateSessionUsage sums per-step token_usage", () => {
    const totals = app.accumulateSessionUsage([
      stepEvent("01_analyze_a", USAGE()),
      stepEvent("02_plan_b", USAGE({ input_tokens: 500, total_cost_usd: 0.01 })),
    ]);
    assert.equal(totals.input_tokens, 1500);
    assert.equal(totals.output_tokens, 400);
    assert.equal(totals.cache_creation_input_tokens, 100);
    assert.equal(totals.cache_read_input_tokens, 1600);
    // 0.0123 + 0.01 — compare with tolerance for float drift.
    assert.ok(Math.abs(totals.total_cost_usd - 0.0223) < 1e-9);
  });

  check("G4 accumulateSessionUsage de-dups identical re-delivered records", () => {
    // The SAME execution's record delivered twice (re-fetch / reconnect) shares
    // a recordKey and must count once.
    const totals = app.accumulateSessionUsage([
      stepEvent("01_analyze_a", USAGE()),
      stepEvent("01_analyze_a", USAGE()),
    ]);
    assert.equal(totals.input_tokens, 1000);
    assert.equal(totals.output_tokens, 200);
  });

  check("G4 accumulateSessionUsage counts fix-loop re-runs of one step_id", () => {
    // A fix loop re-runs test/self_check/verify_spec on the SAME step_id, each
    // emitting a distinct step_completed record (different timestamp + usage).
    // The engine folds every run into the session total, so the badge must too:
    // distinct per-record identity (recordKey) => counted separately, NOT
    // collapsed to the first occurrence the way a step_id-only dedup would.
    const totals = app.accumulateSessionUsage([
      stepEvent("05_verify_spec_x", USAGE(), "verify_spec", "2026-06-03T10:00:00Z"),
      stepEvent("05_verify_spec_x", USAGE({ input_tokens: 400, output_tokens: 30 }),
        "verify_spec", "2026-06-03T10:05:00Z"),
    ]);
    assert.equal(totals.input_tokens, 1400);
    assert.equal(totals.output_tokens, 230);
  });

  check("G4 accumulateSessionUsage is order-independent", () => {
    const a = app.accumulateSessionUsage([
      stepEvent("01_analyze_a", USAGE()),
      stepEvent("02_plan_b", USAGE({ input_tokens: 7 })),
    ]);
    const b = app.accumulateSessionUsage([
      stepEvent("02_plan_b", USAGE({ input_tokens: 7 })),
      stepEvent("01_analyze_a", USAGE()),
    ]);
    assert.deepEqual(a, b);
  });

  check("G4 accumulateSessionUsage ignores non-step / empty / chat records", () => {
    const totals = app.accumulateSessionUsage([
      stepEvent("01_analyze_a", USAGE()),
      stepEvent("02_test_z", undefined),                 // no token_usage
      stepEvent("03_x", { input_tokens: 0, total_cost_usd: 0 }), // empty usage
      { message: { role: "assistant", content: "hi", timestamp: 1 } }, // chat
      null,                                              // malformed
      "garbage",                                         // malformed
    ]);
    assert.equal(totals.input_tokens, 1000);
    assert.equal(totals.output_tokens, 200);
  });

  check("G4 accumulateSessionUsage returns zeros for empty / non-array input", () => {
    const zeros = {
      input_tokens: 0, output_tokens: 0,
      cache_creation_input_tokens: 0, cache_read_input_tokens: 0,
      total_cost_usd: 0,
    };
    assert.deepEqual(app.accumulateSessionUsage([]), zeros);
    assert.deepEqual(app.accumulateSessionUsage(null), zeros);
    assert.deepEqual(app.accumulateSessionUsage(undefined), zeros);
  });

  // ---- (c+) accumulateSessionUsage with step_output records ------------------
  // step_output records (from STEP_OUTPUT events) carry non-terminal step
  // usage. They must be included in the session total, but when a
  // step_completed/step_failed record also exists for the same step_id,
  // only the terminal record is counted (it already includes all prior
  // rounds via carried_token_usage). A step_id with only step_output
  // records (e.g. self_check REVISION_NEEDED abandoned in a fix loop)
  // is counted from the LAST step_output record whose token_usage carries
  // the combined total including all prior rounds.

  const stepOutputEvent = (stepId, usage, stepType = "self_check", status = "revision_needed") => ({
    step_id: stepId,
    step_type: stepType,
    message: {
      type: "step_output",
      step_id: stepId,
      timestamp: Date.now(),
      data: {
        step: {
          step_type: stepType,
          step_id: stepId,
          status: status,
          outputs: usage === undefined ? {} : { token_usage: usage },
        },
      },
    },
  });

  check("G4 accumulateSessionUsage includes step_output records for abandoned steps", () => {
    // A self_check step that returned REVISION_NEEDED is abandoned in the
    // fix loop. Its step_output record carries the step's usage.
    const totals = app.accumulateSessionUsage([
      stepEvent("01_analyze_a", USAGE()),
      stepOutputEvent("07_self_check_x", USAGE({ input_tokens: 300 }), "self_check"),
    ]);
    assert.equal(totals.input_tokens, 1300);  // 1000 + 300
    assert.equal(totals.output_tokens, 400);   // 200 + 200
  });

  check("G4 accumulateSessionUsage prefers step_completed over step_output for same step_id", () => {
    // When a step_id has both step_output (intermediate) and step_completed
    // (terminal) records, only step_completed is counted. The terminal
    // record's token_usage includes all prior rounds via carried_token_usage.
    const totals = app.accumulateSessionUsage([
      // Discovery PAUSED round (intermediate) — combined total so far = 100
      stepOutputEvent("01_discovery_a", USAGE({ input_tokens: 100 }), "discovery", "paused"),
      // Discovery COMPLETED (terminal) — combined total = 150 (carried 100 + round 50)
      stepEvent("01_discovery_a", USAGE({ input_tokens: 150 }), "discovery"),
    ]);
    // Only step_completed is counted: 150, NOT 100 + 150 = 250
    assert.equal(totals.input_tokens, 150);
    assert.equal(totals.output_tokens, 200);
  });

  check("G4 accumulateSessionUsage with step_output only: uses last record per step_id", () => {
    // A step that went PAUSED → REVISION_NEEDED (same step_id, same step
    // object). Each round emits a step_output record. Only the LAST one
    // should be counted, as its token_usage includes all prior rounds.
    const totals = app.accumulateSessionUsage([
      // Round 1 (PAUSED): usage = 100
      stepOutputEvent("07_self_check_x", USAGE({ input_tokens: 100 }), "self_check", "paused"),
      // Round 2 (REVISION_NEEDED): combined = 150 (carried 100 + round 50)
      stepOutputEvent("07_self_check_x", USAGE({ input_tokens: 150 }), "self_check", "revision_needed"),
    ]);
    // Only the LAST step_output (combined 150) is counted, NOT 100 + 150 = 250
    assert.equal(totals.input_tokens, 150);
    assert.equal(totals.output_tokens, 200);
  });

  check("G4 accumulateSessionUsage mixes step_output and step_completed across different step_ids", () => {
    // Fix-loop scenario: self_check_1 returns REVISION_NEEDED (abandoned,
    // step_output only), then a new self_check_2 completes (step_completed).
    // These have different step_ids, so both are counted.
    const totals = app.accumulateSessionUsage([
      stepOutputEvent("07_self_check_abc", USAGE({ input_tokens: 100 }), "self_check"),
      stepEvent("07_self_check_def", USAGE({ input_tokens: 50 }), "self_check"),
    ]);
    // Both counted: 100 (abandoned) + 50 (new step) = 150
    assert.equal(totals.input_tokens, 150);
    assert.equal(totals.output_tokens, 400);   // 200 + 200
  });

  // ---- (d) per-step report card footnote ----------------------------------
  check("G4 report card shows a usage footnote when the step has usage", () => {
    const card = app.renderStepReport({
      step_type: "analyze",
      step_id: "01_analyze_a",
      status: "completed",
      outputs: { reasoning: "did stuff", token_usage: USAGE() },
    });
    assert.ok(card, "expected a report card");
    const foot = findOne(card, "step-report__usage");
    assert.ok(foot, "expected a .step-report__usage footnote");
    const val = findOne(foot, "step-report__usage-value");
    assert.ok(val && val.textContent.startsWith("in 1,000"),
      `footnote should carry the formatted usage, got ${val && val.textContent}`);
  });

  check("G4 report card has NO usage footnote when usage is absent / empty", () => {
    const noUsage = app.renderStepReport({
      step_type: "analyze", step_id: "01_a", status: "completed",
      outputs: { reasoning: "did stuff" },
    });
    assert.equal(findAll(noUsage, "step-report__usage").length, 0,
      "a step with no token_usage must not render the footnote row");
    const emptyUsage = app.renderStepReport({
      step_type: "analyze", step_id: "01_a", status: "completed",
      outputs: { reasoning: "x", token_usage: { input_tokens: 0, total_cost_usd: 0 } },
    });
    assert.equal(findAll(emptyUsage, "step-report__usage").length, 0,
      "all-zero token_usage must not render the footnote row");
    // The rest of the card still renders (footnote omission is non-destructive).
    assert.ok(findOne(noUsage, "step-report__title"),
      "the report card title must still render without usage");
  });

  // ---- (e) flow-view session badge ----------------------------------------
  check("G4 updateFlowUsageBadge hides the badge with no usage", () => {
    const badge = document.getElementById("flow-usage-badge");
    badge.classList.remove("hidden");
    app.updateFlowUsageBadge([]);
    assert.ok(badge.classList.contains("hidden"),
      "the badge must hide when nothing has been consumed");
    assert.equal(badge.textContent, "");
  });

  check("G4 updateFlowUsageBadge shows explicit unavailable state without a backend payload", () => {
    const badge = document.getElementById("flow-usage-badge");
    app.updateFlowUsageBadge([
      stepEvent("01_analyze_a", USAGE()),
      stepEvent("02_plan_b", USAGE({ input_tokens: 500 })),
    ]);
    assert.equal(badge.classList.contains("hidden"), false,
      "the badge must be visible once usage exists");
    const label = findOne(badge, "flow-usage-badge__label");
    const value = findOne(badge, "flow-usage-badge__value");
    assert.ok(label && /session/i.test(label.textContent),
      "badge should carry a Session label");
    // No backend summary: the frontend must NOT recompute a client-side total
    // — it shows the explicit unavailable state instead.
    assert.ok(value && /unavailable/i.test(value.textContent),
      `badge must show the unavailable state, got ${value && value.textContent}`);
    assert.ok(!/in 1,500/.test(value && value.textContent),
      "client-side sums must not render when the backend payload is absent");
  });

  // ---- (f) discovery step cumulative usage footnote -------------------------
  check("G4 discovery report card shows a cumulative usage footnote with all fields", () => {
    const usage = USAGE({
      input_tokens: 5000,
      output_tokens: 1200,
      cache_read_input_tokens: 800,
      cache_creation_input_tokens: 300,
      total_cost_usd: 0.035,
    });
    const card = app.renderStepReport({
      step_type: "discovery",
      step_id: "01_discovery_a",
      status: "completed",
      outputs: {
        refined_description: "Build a user feature",
        token_usage: usage,
      },
    });
    assert.ok(card, "expected a discovery report card");
    const foot = findOne(card, "step-report__usage");
    assert.ok(foot, "expected a .step-report__usage footnote on the discovery card");
    const val = findOne(foot, "step-report__usage-value");
    assert.ok(val, "expected a usage value span");
    const text = val.textContent;
    assert.ok(text.includes("in 5,000"), `expected input tokens, got ${text}`);
    assert.ok(text.includes("out 1,200"), `expected output tokens, got ${text}`);
    assert.ok(text.includes("cache r/w 800/300"), `expected cache tokens, got ${text}`);
    assert.ok(text.includes("$0.0350"), `expected cost, got ${text}`);
  });

  check("G4 discovery report card has NO footnote when usage is absent", () => {
    const card = app.renderStepReport({
      step_type: "discovery",
      step_id: "01_discovery_a",
      status: "completed",
      outputs: { refined_description: "Build a user feature" },
    });
    assert.equal(findAll(card, "step-report__usage").length, 0,
      "discovery step with no token_usage must not render the footnote");
  });

  check("G4 discovery report card has NO footnote when usage is all-zero", () => {
    const card = app.renderStepReport({
      step_type: "discovery",
      step_id: "01_discovery_a",
      status: "completed",
      outputs: {
        refined_description: "Build a user feature",
        token_usage: { input_tokens: 0, output_tokens: 0, total_cost_usd: 0 },
      },
    });
    assert.equal(findAll(card, "step-report__usage").length, 0,
      "all-zero token_usage must not render the footnote");
  });

  check("G4 discovery card body (refined_description) still renders alongside the footnote", () => {
    const card = app.renderStepReport({
      step_type: "discovery",
      step_id: "01_discovery_a",
      status: "completed",
      outputs: {
        refined_description: "Build a user feature for admins",
        token_usage: USAGE(),
      },
    });
    assert.ok(card.textContent.includes("Build a user feature for admins"),
      "the refined_description must still render on the card");
    assert.ok(findOne(card, "step-report__usage"),
      "the footnote must coexist with the card body");
  });

  // ---- (g) session badge includes discovery steps ---------------------------
  check("G4 session badge shows unavailable state with discovery + analyze usage", () => {
    const badge = document.getElementById("flow-usage-badge");
    app.updateFlowUsageBadge([
      stepEvent("01_discovery_a", USAGE({ input_tokens: 3000, output_tokens: 800 }), "discovery"),
      stepEvent("02_analyze_b", USAGE({ input_tokens: 1000, output_tokens: 200 }), "analyze"),
    ]);
    assert.equal(badge.classList.contains("hidden"), false,
      "badge must be visible with discovery + analyze usage");
    const value = findOne(badge, "flow-usage-badge__value");
    assert.ok(value && /unavailable/i.test(value.textContent),
      `badge must show the unavailable state, got ${value && value.textContent}`);
    assert.ok(!value.textContent.includes("4,000"),
      "no client-side token sum may render without a backend payload");
  });

  check("G4 session badge shows unavailable state with cache/cost usage only", () => {
    const badge = document.getElementById("flow-usage-badge");
    app.updateFlowUsageBadge([
      stepEvent("01_discovery_a", USAGE({
        cache_read_input_tokens: 500,
        cache_creation_input_tokens: 100,
        total_cost_usd: 0.025,
      }), "discovery"),
    ]);
    const value = findOne(badge, "flow-usage-badge__value");
    assert.ok(value && /unavailable/i.test(value.textContent),
      `badge must show the unavailable state, got ${value && value.textContent}`);
    assert.ok(!value.textContent.includes("500/100") && !value.textContent.includes("$0.0250"),
      "no client-side cache/cost recompute may render without a backend payload");
  });

  // -- (G10) backend summary payload preference ------------------------------
  // Since G10 the backend (engine → daemon → server) computes the one
  // authoritative usage summary; the badge renders that payload and shows an
  // explicit unavailable state for pre-payload daemons — never a client-side
  // recomputed total.

  const G10_COMPACT = {
    totals: {
      usage_status: "available", logical_input_tokens: 7777, output_tokens: 55,
      cache_read_input_tokens: 44, cache_creation_input_tokens: 33,
    },
    actual_cost_usd: 0.1234,
    estimated_cost_usd: 0.25,
    unknown_call_count: 0,
    unknown_model_count: 0,
    unknown_price_count: 0,
    unknown_cache_ttl_count: 0,
    partial: false,
    completeness: "complete",
  };

  check("G10 badge prefers the backend payload over client-side sums", () => {
    const badge = document.getElementById("flow-usage-badge");
    // Records sum to 1000 in; the backend payload says 7777 — the payload wins.
    app.applyUsageBadge(badge, [
      stepEvent("01_analyze_a", USAGE({ input_tokens: 1000 }), "analyze"),
    ], G10_COMPACT);
    const value = findOne(badge, "flow-usage-badge__value");
    assert.ok(value && value.textContent.includes("7,777"),
      `backend totals must win, got ${value && value.textContent}`);
    const cost = findOne(badge, "flow-usage-badge__cost");
    assert.ok(cost && cost.textContent.includes("$0.1234")
      && cost.textContent.includes("$0.2500"),
      "actual and estimated render as separate columns");
  });

  check("G10 badge shows explicit unavailable state when no payload exists", () => {
    const badge = document.getElementById("flow-usage-badge");
    app.applyUsageBadge(badge, [
      stepEvent("01_analyze_a", USAGE({ input_tokens: 1000 }), "analyze"),
    ], null);
    const value = findOne(badge, "flow-usage-badge__value");
    assert.ok(value && /unavailable/i.test(value.textContent),
      `the badge must show the unavailable state, got ${value && value.textContent}`);
    assert.ok(!value.textContent.includes("1,000"),
      "legacy client-side recomputation must not drive the badge without a payload");
  });
}
