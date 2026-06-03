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

  check("G4 updateFlowUsageBadge shows + populates the badge once usage exists", () => {
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
    // 1000 + 500 input tokens summed across the two steps.
    assert.ok(value && value.textContent.startsWith("in 1,500"),
      `badge should show the session total, got ${value && value.textContent}`);
  });
}
