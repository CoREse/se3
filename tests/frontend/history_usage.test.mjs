/*
 * History-detail session token-usage badge tests (Part 3).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerHistoryUsageTests({app, check, findOne, findAll})` so the parent
 * harness drives the same check() reporter and the same `app` module export.
 *
 * The history view reuses the running-flow view's exact rendering logic: both
 * updateFlowUsageBadge and updateHistoryUsageBadge delegate to the shared
 * applyUsageBadge(badgeEl, records) helper (accumulateSessionUsage +
 * formatTokenUsage + isTokenUsageEmpty suppression). These checks lock that the
 * history badge renders the same Session label/value on non-empty usage, hides
 * on empty usage, and produces an identical value to the flow badge for the
 * same records.
 */
import assert from "node:assert/strict";

export function registerHistoryUsageTests(ctx) {
  const { app, check, findOne } = ctx;

  const USAGE = (over = {}) => ({
    input_tokens: 1000,
    output_tokens: 200,
    cache_creation_input_tokens: 50,
    cache_read_input_tokens: 800,
    total_cost_usd: 0.0123,
    ...over,
  });

  // A step_completed record carrying token_usage in outputs (same shape the
  // token_usage.test.mjs accumulate tests use).
  const stepEvent = (stepId, usage, stepType = "analyze") => ({
    step_id: stepId,
    step_type: stepType,
    message: {
      type: "step_completed",
      step_id: stepId,
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

  check("updateHistoryUsageBadge hides + clears the badge with no usage", () => {
    const badge = document.getElementById("history-usage-badge");
    badge.classList.remove("hidden");
    app.updateHistoryUsageBadge([]);
    assert.ok(badge.classList.contains("hidden"),
      "the history badge must hide when nothing has been consumed");
    assert.equal(badge.textContent, "");
  });

  check("updateHistoryUsageBadge shows + populates the badge once usage exists", () => {
    const badge = document.getElementById("history-usage-badge");
    app.updateHistoryUsageBadge([
      stepEvent("01_analyze_a", USAGE()),
      stepEvent("02_plan_b", USAGE({ input_tokens: 500 })),
    ]);
    assert.equal(badge.classList.contains("hidden"), false,
      "the history badge must be visible once usage exists");
    const label = findOne(badge, "flow-usage-badge__label");
    const value = findOne(badge, "flow-usage-badge__value");
    assert.ok(label && /session/i.test(label.textContent),
      "the history badge should carry a Session label");
    // 1000 + 500 input tokens summed across the two steps.
    assert.ok(value && value.textContent.startsWith("in 1,500"),
      `the history badge should show the session total, got ${value && value.textContent}`);
  });

  check("history + flow badges render an identical value for the same records (shared helper)", () => {
    const records = [
      stepEvent("01_analyze_a", USAGE()),
      stepEvent("02_plan_b", USAGE({ input_tokens: 500, total_cost_usd: 0.01 })),
    ];
    app.updateFlowUsageBadge(records);
    app.updateHistoryUsageBadge(records);
    const flowVal = findOne(
      document.getElementById("flow-usage-badge"), "flow-usage-badge__value");
    const histVal = findOne(
      document.getElementById("history-usage-badge"), "flow-usage-badge__value");
    assert.ok(flowVal && histVal, "both badges must render a value span");
    assert.equal(histVal.textContent, flowVal.textContent,
      "the history view must reuse the flow view's exact usage rendering");
  });
}
