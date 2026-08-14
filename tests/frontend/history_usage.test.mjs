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
 * applyUsageBadge(badgeEl, records) helper (backend payload first, explicit
 * unavailable state otherwise). These checks lock that the history badge
 * renders the same Session label/value on non-empty usage, hides on empty
 * usage, and produces an identical value to the flow badge for the same
 * records.
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

  check("updateHistoryUsageBadge shows explicit unavailable state once usage exists", () => {
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
    // Without a backend payload the badge shows the explicit unavailable
    // state — the frontend never recomputes a session total.
    assert.ok(value && /unavailable/i.test(value.textContent),
      `the history badge should show the unavailable state, got ${value && value.textContent}`);
    assert.ok(!/in 1,500/.test(value && value.textContent),
      "no client-side sum may render without a backend payload");
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

  // -- (G10) backend payload drives the history badge ------------------------
  // The history bundle's `usage` payload (state.historyUsage) is the backend
  // authority; the badge renders it through the same applyUsageBadge path as
  // the live-flow badge, so one schema serves both views.

  const G10_PAYLOAD = {
    summary: {
      totals: {
        usage_status: "available", logical_input_tokens: 4321, output_tokens: 99,
        cache_read_input_tokens: 88, cache_creation_input_tokens: 77,
      },
      actual_cost_usd: 0.0999,
      estimated_cost_usd: 0.2,
      unknown_call_count: 1,
      unknown_model_count: 0,
      unknown_price_count: 0,
      unknown_cache_ttl_count: 0,
      partial: false,
      completeness: "partial",
    },
    calls: [], steps: {}, legacy: false, completeness: "partial",
  };

  check("G10 history badge renders the bundle usage payload", () => {
    app.state.historyUsage = G10_PAYLOAD;
    app.updateHistoryUsageBadge([]);
    const badge = document.getElementById("history-usage-badge");
    assert.equal(badge.classList.contains("hidden"), false);
    const value = findOne(badge, "flow-usage-badge__value");
    assert.ok(value && value.textContent.includes("4,321"),
      `the badge must show the backend totals, got ${value && value.textContent}`);
    app.state.historyUsage = null;
  });

  check("G10 history + live flow render the same payload value", () => {
    app.state.historyUsage = G10_PAYLOAD;
    app.state.flowDetail = { usage_summary: G10_PAYLOAD.summary };
    app.updateFlowUsageBadge([]);
    app.updateHistoryUsageBadge([]);
    const flowVal = findOne(
      document.getElementById("flow-usage-badge"), "flow-usage-badge__value");
    const histVal = findOne(
      document.getElementById("history-usage-badge"), "flow-usage-badge__value");
    assert.ok(flowVal && histVal, "both badges must render");
    assert.equal(histVal.textContent, flowVal.textContent,
      "one backend summary, one rendered value across views");
    app.state.historyUsage = null;
    app.state.flowDetail = null;
  });
}
