/*
 * Backend strategy / usage-summary rendering tests (Group G10).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerStrategyUsageSummaryTests({app, check, findOne, findAll})` so the
 * parent harness drives the same check() reporter and the same `app` module
 * export.
 *
 * Coverage:
 *   (a) buildNewFlowBody / buildIssueFlowBody — project-default OMITS the
 *       implementation_strategy field (the daemon then resolves the project
 *       configuration / planned default); auto/direct/planned are sent
 *       explicitly.
 *   (b) usageStatusMark / formatUsageTotals — explicit zero with status
 *       "available" renders as real zeros; unavailable / partial /
 *       legacy_ambiguous render their own label, never a misleading 0.
 *   (c) formatCostOrUnknown / formatCostOrDash — absent cost is "unknown"
 *       (never $0.0000); per-call estimates render "—".
 *   (d) usagePayloadSummary — one shape normalizer for the compact flow/session
 *       summary and the full history payload.
 *   (e) renderCompactUsageSummary / renderUsagePayloadRegion — totals, actual /
 *       estimated / unknown / completeness, per-call and per-step tables, the
 *       legacy note, and the no-usage empty state.
 *   (f) applyUsageBadge — backend payload first (same schema for history and
 *       live flow), legacy client accumulation only as the pre-payload
 *       fallback.
 *   (g) strategyValueLabel / buildStrategyRows / buildScopeRows /
 *       collectScopeAuditFromRecords / renderHistoryStrategyScope — effective
 *       strategy + reason + inferred note, not_applicable labels, scope audit
 *       round/baseline/changed-paths/full-rounds facts.
 *   (h) renderHistoryUsageRegion — hidden without a payload, populated from
 *       state.historyUsage when present.
 */
import assert from "node:assert/strict";

export function registerStrategyUsageSummaryTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  // -- (a) strategy request bodies ------------------------------------------

  check("G10 buildNewFlowBody omits strategy when project-default", () => {
    const body = app.buildNewFlowBody({
      machineId: "m1", task: "t", taskType: "feature",
      discover: false, worktree: false, projectRoot: "/p", strategy: "",
    });
    assert.equal("implementation_strategy" in body, false,
      "project default must omit the field so the daemon resolves config");
    assert.equal(body.task_type, "feature");
  });

  check("G10 buildNewFlowBody sends explicit auto/direct/planned", () => {
    for (const strategy of ["auto", "direct", "planned"]) {
      const body = app.buildNewFlowBody({
        machineId: "m1", task: "t", taskType: "feature",
        discover: false, worktree: false, projectRoot: "/p", strategy,
      });
      assert.equal(body.implementation_strategy, strategy);
    }
  });

  check("G10 buildIssueFlowBody omits strategy when project-default", () => {
    const body = app.buildIssueFlowBody(
      { id: "i1", machine_id: "m1", project_root: "/p" }, false, false, "");
    assert.equal("implementation_strategy" in body, false);
    assert.equal(body.from_issue_id, "i1");
  });

  check("G10 buildIssueFlowBody sends an explicit strategy", () => {
    const body = app.buildIssueFlowBody(
      { id: "i1", machine_id: "m1", project_root: "/p" }, true, true, "direct");
    assert.equal(body.implementation_strategy, "direct");
    assert.equal(body.discover, true);
    assert.equal(body.worktree, true);
  });

  // -- (b) usage status marks + totals --------------------------------------

  check("G10 usageStatusMark is empty for available, labels the rest", () => {
    assert.equal(app.usageStatusMark("available"), "");
    assert.equal(app.usageStatusMark(""), "");
    for (const status of ["partial", "unavailable", "legacy_ambiguous"]) {
      const mark = app.usageStatusMark(status);
      assert.ok(mark && mark.length > 0, `status ${status} must have a label`);
    }
  });

  check("G10 formatUsageTotals renders real zeros for available", () => {
    const line = app.formatUsageTotals({
      usage_status: "available",
      logical_input_tokens: 0,
      output_tokens: 0,
      cache_read_input_tokens: 0,
      cache_creation_input_tokens: 0,
    });
    assert.ok(line.includes("0"), "an explicit zero report renders as 0");
    assert.ok(!line.includes("NaN"));
  });

  check("G10 formatUsageTotals marks non-available instead of zero", () => {
    const line = app.formatUsageTotals({
      usage_status: "unavailable",
      logical_input_tokens: 0,
      output_tokens: 0,
      cache_read_input_tokens: 0,
      cache_creation_input_tokens: 0,
    });
    assert.ok(line.includes(app.usageStatusMark("unavailable")),
      "unavailable usage shows its status label");
  });

  check("G10 formatUsageTotals sums the three cache-creation buckets", () => {
    const line = app.formatUsageTotals({
      usage_status: "available",
      logical_input_tokens: 100,
      output_tokens: 200,
      cache_read_input_tokens: 300,
      cache_creation_input_tokens: 400,
      cache_creation_5m_input_tokens: 500,
      cache_creation_1h_input_tokens: 600,
    });
    assert.ok(line.includes("1,500"), "cache create must total all TTL buckets");
  });

  // -- (c) cost formatting --------------------------------------------------

  check("G10 formatCostOrUnknown never fabricates a zero cost", () => {
    assert.equal(app.formatCostOrUnknown(null), app.tf("usage.unknown", "unknown"));
    assert.equal(app.formatCostOrUnknown(""), app.tf("usage.unknown", "unknown"));
    assert.equal(app.formatCostOrUnknown(0.5), "$0.5000");
  });

  check("G10 formatCostOrDash distinguishes not-computed from unknown", () => {
    assert.equal(app.formatCostOrDash(null), "—");
    assert.equal(app.formatCostOrDash(0.25), "$0.2500");
  });

  // -- (d) payload shape normalization --------------------------------------

  const COMPACT = {
    totals: {
      usage_status: "available", logical_input_tokens: 1000, output_tokens: 200,
      cache_read_input_tokens: 300, cache_creation_input_tokens: 50,
    },
    actual_cost_usd: 0.0123,
    estimated_cost_usd: 0.045,
    unknown_call_count: 1,
    unknown_model_count: 0,
    unknown_price_count: 0,
    unknown_cache_ttl_count: 0,
    partial: false,
    diagnostics: [],
    completeness: "partial",
  };

  check("G10 usagePayloadSummary normalizes compact + full payloads", () => {
    assert.equal(app.usagePayloadSummary(COMPACT), COMPACT);
    const full = { summary: COMPACT, calls: [], steps: {}, legacy: false, completeness: "partial" };
    assert.equal(app.usagePayloadSummary(full), COMPACT);
    assert.equal(app.usagePayloadSummary(null), null);
    assert.equal(app.usagePayloadSummary({}), null);
  });

  // -- (e) summary region rendering -----------------------------------------

  check("G10 renderCompactUsageSummary renders totals + costs + completeness", () => {
    const container = app.el("div");
    app.renderCompactUsageSummary(container, COMPACT);
    const line = findOne(container, "usage-totals-line");
    assert.ok(line, "totals line must render");
    const costRow = findOne(container, "usage-cost-row");
    assert.ok(costRow, "cost row must render");
    assert.ok(costRow.textContent.includes("Actual $0.0123"),
      "actual cost renders");
    assert.ok(costRow.textContent.includes("Estimated $0.0450"),
      "estimated cost stays a separate column");
    const unknown = findOne(container, "usage-unknown-line");
    assert.ok(unknown && unknown.textContent.includes("1"),
      "non-zero unknown counters must render");
    const complete = findOne(container, "usage-completeness");
    assert.ok(complete && complete.textContent.toLowerCase().includes("partial"),
      "completeness badge must render");
  });

  check("G10 renderUsagePayloadRegion renders calls + steps + flow totals", () => {
    const payload = {
      summary: COMPACT,
      legacy: false,
      completeness: "partial",
      calls: [{
        call_id: "c1", attempt: 1, usage_status: "available",
        agent_name: "a1", runner_type: "claude-code", provider: "anthropic",
        resolved_model: "claude-fable-5", reported_model: "claude-fable-5",
        logical_input_tokens: 100, output_tokens: 20,
        cache_read_input_tokens: 10, cache_creation_input_tokens: 5,
        actual_cost_usd: 0.001,
      }],
      steps: {
        "analyze:1": {
          record_count: 2,
          summary: { totals: { usage_status: "available", logical_input_tokens: 100, output_tokens: 20 }, actual_cost_usd: 0.001, completeness: "complete" },
        },
      },
    };
    const container = app.el("div");
    app.renderUsagePayloadRegion(container, payload);
    const tables = findAll(container, "usage-table");
    assert.equal(tables.length, 2, "calls table + steps table must render");
    assert.ok(tables[0].textContent.includes("c1#1"), "call row renders call_id + attempt");
    assert.ok(tables[0].textContent.includes("claude-fable-5"), "resolved model renders");
    assert.ok(tables[1].textContent.includes("analyze:1"), "step row renders");
    assert.ok(findOne(container, "usage-region__title"),
      "flow totals header renders");
  });

  check("G10 renderUsagePayloadRegion flags legacy payloads", () => {
    const container = app.el("div");
    app.renderUsagePayloadRegion(container, {
      summary: COMPACT, legacy: true, completeness: "partial", calls: [], steps: {},
    });
    const note = findOne(container, "usage-note");
    assert.ok(note, "legacy note must render");
  });

  check("G10 renderUsagePayloadRegion shows the no-usage empty state", () => {
    const container = app.el("div");
    app.renderUsagePayloadRegion(container, {
      calls: [], steps: {}, summary: null, legacy: false, completeness: "none",
    });
    assert.ok(container.textContent.length > 0,
      "a completeness:none payload shows the no-usage note");
  });

  // -- (f) badge rendering (backend-first) -----------------------------------

  const USAGE_RECORDS = [{
    step_id: "s1", step_type: "analyze",
    message: { type: "step_completed", step_id: "s1", data: { step: {
      step_type: "analyze", step_id: "s1", status: "completed",
      outputs: { token_usage: { input_tokens: 1000, output_tokens: 200, total_cost_usd: 0.01 } },
    } } },
  }];

  check("G10 applyUsageBadge prefers the backend payload over client sums", () => {
    const badge = app.el("div");
    app.applyUsageBadge(badge, USAGE_RECORDS, COMPACT);
    assert.ok(!badge.classList.contains("hidden"));
    const value = findOne(badge, "flow-usage-badge__value");
    assert.ok(value.textContent.includes("1,000"),
      "badge shows the backend totals");
    const cost = findOne(badge, "flow-usage-badge__cost");
    assert.ok(cost && cost.textContent.includes("Actual $0.0123")
      && cost.textContent.includes("Estimated $0.0450"),
      "badge shows actual and estimated separately");
  });

  check("G10 applyUsageBadge shows explicit unavailable state without a payload", () => {
    const badge = app.el("div");
    app.applyUsageBadge(badge, USAGE_RECORDS, null);
    assert.ok(!badge.classList.contains("hidden"));
    const value = findOne(badge, "flow-usage-badge__value");
    assert.ok(/unavailable/i.test(value.textContent),
      `badge must show the unavailable state, got ${value.textContent}`);
    assert.ok(!value.textContent.includes("1,000"),
      "the frontend must not recompute client-side totals without a payload");
  });

  check("G10 history + live flow badges render the SAME backend summary", () => {
    const historyBadge = app.el("div");
    app.state.historyUsage = { summary: COMPACT };
    app.updateHistoryUsageBadge([]);
    // updateHistoryUsageBadge writes into #history-usage-badge; re-render the
    // same payload into an explicit badge for the comparison below.
    app.applyUsageBadge(historyBadge, [], { summary: COMPACT });
    const flowBadge = app.el("div");
    app.applyUsageBadge(flowBadge, [], COMPACT);
    assert.equal(
      findOne(historyBadge, "flow-usage-badge__value").textContent,
      findOne(flowBadge, "flow-usage-badge__value").textContent,
      "one payload schema, one rendered value",
    );
    app.state.historyUsage = null;
  });

  check("G10 applyUsageBadge hides when neither payload nor records carry usage", () => {
    const badge = app.el("div");
    app.applyUsageBadge(badge, [], null);
    assert.ok(badge.classList.contains("hidden"));
  });

  // -- (g) strategy + scope display ------------------------------------------

  check("G10 strategyValueLabel localizes known values, degrades unknowns", () => {
    assert.equal(app.strategyValueLabel("planned"), "planned");
    assert.equal(app.strategyValueLabel(""), app.tf("strategy.value.unknown", "unknown"));
    // With the shipped dictionary loaded, not_applicable renders its real label;
    // without any dict (the node harness boot state) the raw value is the
    // fixable fallback — never a crash.
    const saved = app.I18N.dicts["en-US"];
    app.I18N.dicts["en-US"] = { "strategy.value.not_applicable": "not applicable" };
    assert.equal(app.strategyValueLabel("not_applicable"), "not applicable");
    if (saved === undefined) delete app.I18N.dicts["en-US"];
    else app.I18N.dicts["en-US"] = saved;
  });

  check("G10 buildStrategyRows shows effective + reason + inferred note", () => {
    const rows = app.buildStrategyRows({
      requested: "direct", effective: "direct", reason: "chosen by analyze", inferred: false,
    });
    assert.ok(rows && rows.textContent.includes("direct"));
    assert.ok(rows.textContent.includes("chosen by analyze"));
    const inferred = app.buildStrategyRows({
      requested: "planned", effective: "planned", reason: "", inferred: true,
    });
    assert.ok(inferred.textContent.includes(
      app.tf("strategy.inferredNote", "inferred from legacy records")));
  });

  check("G10 strategy reason_key renders through i18n, plain reason verbatim", () => {
    // The legacy-inference sentence is authored by the backend PROJECTION
    // (UI chrome), so it must render from the catalog; a persisted reason is
    // flow data and stays verbatim.
    const saved = app.I18N.dicts["en-US"];
    app.I18N.dicts["en-US"] = {
      "strategy.reason.legacy_inference": "TRANSLATED LEGACY REASON",
    };
    const rows = app.buildStrategyRows({
      requested: "planned",
      effective: "not_applicable",
      reason: "Inferred from persisted legacy task type and selected_steps.",
      reason_key: "legacy_inference",
      inferred: true,
    });
    assert.ok(rows.textContent.includes("TRANSLATED LEGACY REASON"));
    assert.ok(!rows.textContent.includes("Inferred from persisted legacy"));
    if (saved === undefined) delete app.I18N.dicts["en-US"];
    else app.I18N.dicts["en-US"] = saved;

    const persisted = app.buildStrategyRows({
      requested: "auto", effective: "direct",
      reason: "ANALYZE recommended direct.", reason_key: "", inferred: false,
    });
    assert.ok(persisted.textContent.includes("ANALYZE recommended direct."));
  });

  check("G10 buildStrategyRows shows a mismatched explicit request", () => {
    const rows = app.buildStrategyRows({
      requested: "direct", effective: "planned", reason: "", inferred: false,
    });
    assert.ok(rows.textContent.includes("requested direct"),
      "an explicit request differing from the effective value is surfaced");
  });

  check("G10 buildStrategyRows is null without an effective value", () => {
    assert.equal(app.buildStrategyRows(null), null);
    assert.equal(app.buildStrategyRows({}), null);
  });

  check("G10 buildScopeRows renders round / baseline / changed paths / full rounds", () => {
    const row = app.buildScopeRows({
      last_round: {
        scope_mode: "incremental", baseline_id: "abc123def456", fix_iteration: 2, pass_index: 3,
      },
      completed_full_rounds: 1,
    }, 4);
    assert.ok(row.textContent.includes("fix 2"), "fix iteration renders");
    assert.ok(row.textContent.includes("abc123def4"), "baseline id renders");
    assert.ok(row.textContent.includes("4 changed path(s)"), "changed paths render");
    assert.ok(row.textContent.includes("1 full round(s)"), "full rounds render");
  });

  check("G10 buildScopeRows is null for a flow with no scope audit", () => {
    assert.equal(app.buildScopeRows(null), null);
    assert.equal(app.buildScopeRows({}), null);
  });

  const scopeRecord = (outputs) => ({
    step_id: "self_check_1", step_type: "self_check",
    message: { type: "step_completed", step_id: "self_check_1", data: { step: {
      step_type: "self_check", step_id: "self_check_1", status: "completed", outputs,
    } } },
  });

  check("G10 collectScopeAuditFromRecords extracts the latest scope facts", () => {
    const audit = app.collectScopeAuditFromRecords([
      scopeRecord({ scope_mode: "full", baseline_id: "b1", fix_iteration: 0, self_check_pass_index: 1, scope_changed_paths: ["a.py"] }),
      scopeRecord({ scope_mode: "incremental", baseline_id: "b2", fix_iteration: 1, self_check_pass_index: 2, scope_changed_paths: ["a.py", "b.py"] }),
    ]);
    assert.equal(audit.scope_mode, "incremental", "the latest record wins");
    assert.equal(audit.baseline_id, "b2");
    assert.equal(audit.fix_iteration, 1);
    assert.equal(audit.pass_index, 2);
    assert.deepEqual(audit.changed_paths, ["a.py", "b.py"]);
  });

  check("G10 collectScopeAuditFromRecords ignores records without scope fields", () => {
    assert.equal(app.collectScopeAuditFromRecords([
      scopeRecord({ issues: [] }),
      scopeRecord({}),
    ]), null);
    assert.equal(app.collectScopeAuditFromRecords([]), null);
  });

  check("G10 renderHistoryStrategyScope renders strategy + scope meta", () => {
    const container = app.el("div");
    app.renderHistoryStrategyScope(container,
      { implementation_strategy: { requested: "planned", effective: "planned", reason: "r", inferred: false } },
      [scopeRecord({ scope_mode: "full", baseline_id: "b9", fix_iteration: 0, self_check_pass_index: 1, scope_changed_paths: ["x.py"] })]);
    assert.ok(container.textContent.includes("planned"), "strategy renders");
    assert.ok(container.textContent.includes("b9"), "scope baseline renders");
  });

  check("G10 renderHistoryStrategyScope leaves the container untouched without data", () => {
    const container = app.el("div");
    app.renderHistoryStrategyScope(container, {}, []);
    assert.equal(container.childNodes.length, 0);
  });

  // -- (h) history usage region ----------------------------------------------

  check("G10 renderHistoryUsageRegion hides without a payload", () => {
    app.state.historyUsage = null;
    app.renderHistoryUsageRegion();
    const region = app.$("history-usage-region");
    assert.ok(region.classList.contains("hidden"));
  });

  check("G10 renderHistoryUsageRegion populates from the shared payload", () => {
    app.state.historyUsage = { summary: COMPACT, calls: [], steps: {}, legacy: false, completeness: "partial" };
    app.renderHistoryUsageRegion();
    const region = app.$("history-usage-region");
    assert.ok(!region.classList.contains("hidden"));
    assert.ok(findOne(region, "usage-totals-line"), "totals line renders in the region");
    app.state.historyUsage = null;
  });

  check("G10 WS history_data adopts the backend usage payload", () => {
    app.state.selectedHistoryId = "f-ws";
    app.state.historyRecords = [];
    app.state.historyUsage = null;
    app.applyHistoryData({
      flow_id: "f-ws", mode: "full", records: [],
      usage: { summary: COMPACT, calls: [], steps: {}, legacy: false, completeness: "partial" },
    });
    assert.ok(app.state.historyUsage, "the WS payload must be adopted");
    assert.equal(app.usagePayloadSummary(app.state.historyUsage), COMPACT);
    app.state.selectedHistoryId = null;
    app.state.historyUsage = null;
    app.state.historyRecords = [];
  });

  check("G10 per-call unknown model renders through the language pack", () => {
    const payload = {
      summary: COMPACT, legacy: false, completeness: "partial",
      calls: [{
        call_id: "c2", attempt: 0, usage_status: "available",
        agent_name: "a1", runner_type: "claude-code", provider: "anthropic",
        resolved_model: "unknown", reported_model: "$ANTHROPIC_MODEL",
        logical_input_tokens: 100, output_tokens: 20,
        actual_cost_usd: 0.001,
      }],
      steps: {},
    };
    const container = app.el("div");
    app.renderUsagePayloadRegion(container, payload);
    const table = findAll(container, "usage-table")[0];
    // Under zh-CN the sentinel must localize (未知), never leak raw "unknown".
    const { I18N } = app;
    const savedLang = I18N.lang;
    const savedDicts = I18N.dicts;
    try {
      I18N.dicts = { "en-US": {}, "zh-CN": { "usage.unknown": "未知" } };
      I18N.lang = "zh-CN";
      const containerZh = app.el("div");
      app.renderUsagePayloadRegion(containerZh, payload);
      const tableZh = findAll(containerZh, "usage-table")[0];
      assert.ok(tableZh.textContent.includes("未知"),
        "the unknown sentinel must follow the UI language");
      assert.ok(!tableZh.textContent.includes("unknown"),
        "the raw English sentinel must not leak under zh-CN");
      assert.ok(!tableZh.textContent.includes("$ANTHROPIC_MODEL"),
        "an unexpanded literal must never render");
    } finally {
      I18N.lang = savedLang;
      I18N.dicts = savedDicts;
    }
    assert.ok(table.textContent.includes("unknown"),
      "en-US renders the sentinel through the pack");
  });

  check("G10 per-call embedded estimate renders a figure not a dash", () => {
    const payload = {
      summary: COMPACT, legacy: false, completeness: "partial",
      calls: [{
        call_id: "c-estimate", attempt: 0, usage_status: "available",
        agent_name: "a1", runner_type: "claude-code", provider: "anthropic",
        resolved_model: "claude-fable-5",
        logical_input_tokens: 100, output_tokens: 20,
        actual_cost_usd: null, estimated_cost_usd: 0.25,
      }],
      steps: {},
    };
    const container = app.el("div");
    app.renderUsagePayloadRegion(container, payload);
    const table = findAll(container, "usage-table")[0];
    assert.ok(table.textContent.includes("$0.2500"),
      "the shared backend per-call estimate must render");
  });
}

