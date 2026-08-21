/*
 * Step-report renderer-family tests (G1).
 *
 * Three defects in one family, all in the `step_completed` report-card path of
 * `app.js`:
 *
 *   A. The generic key/value renderer dumped the usage-metadata keys
 *      (`token_usage` / `usage_records` / `usage_summary`) as ordinary fields,
 *      even though `renderStepReport` already renders them as the card's
 *      compact `buildStepUsageFootnote` line — so every unregistered step's
 *      card repeated a wall of accounting rows above its own result.
 *   B/C/G. `confirm`, `invariant_check` and `adjudicate` had no dedicated
 *      renderer at all and fell through that same generic dump; `adjudicate`'s
 *      audit structures (candidate_verdicts / rejected_candidates / …) buried
 *      the ruling itself, and `invariant_check`/`adjudicate`/`e2e` were also
 *      missing from STEP_REPORT_TITLES so their card title degraded to the raw
 *      step key.
 *
 * This file is dual-mode:
 *   - `registerStepReportRendererTests(ctx)` is imported by
 *     tests/frontend/test_app_pure.mjs so the checks run under that harness's
 *     shared DOM stub (and thus inside the pytest bridge that shells out to the
 *     Node runner).
 *   - Run directly (`node tests/frontend/step_report_renderers.test.mjs`) it
 *     installs its own DOM stub and runs the same checks.
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

// ---------------------------------------------------------------------------
// The registrable test body — shared between the harness and standalone paths.
// ---------------------------------------------------------------------------
export async function registerStepReportRendererTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  const USAGE_SAMPLE = {
    token_usage: { input_tokens: 10, output_tokens: 20, total_cost_usd: 0.5 },
    usage_records: [{ agent: "claude", input_tokens: 10 }],
    usage_summary: { total: { input_tokens: 10 } },
  };

  function render(stepType, outputs, step) {
    const renderer = app.STEP_REPORT_RENDERERS[stepType];
    assert.ok(renderer, `${stepType} must be registered in STEP_REPORT_RENDERERS`);
    return renderer(Object.assign({ step_type: stepType, status: "completed" }, step || {}), outputs);
  }
  function kvKeys(frag) {
    return findAll(frag, "step-report__kv-k").map((n) => n.textContent);
  }

  // =========================================================================
  // A. usage metadata never reaches a generic key/value block
  // =========================================================================
  check("usage keys: the exclusion set is exactly the three usage-metadata keys", () => {
    assert.deepEqual(
      Array.from(app.USAGE_META_KEYS).sort(),
      ["token_usage", "usage_records", "usage_summary"],
    );
  });

  check("usage keys: renderGenericOutputs drops them and keeps every other field", () => {
    const frag = app.renderGenericOutputs(Object.assign({
      verdict: "ok",
      count: 3,
    }, USAGE_SAMPLE));
    assert.deepEqual(kvKeys(frag), ["verdict", "count"]);
    assert.ok(!frag.textContent.includes("total_cost_usd"),
      `usage internals must not leak into the kv block: ${frag.textContent}`);
  });

  check("usage keys: an outputs dict of NOTHING BUT usage metadata renders no kv block at all", () => {
    const frag = app.renderGenericOutputs(Object.assign({}, USAGE_SAMPLE));
    assert.equal(findOne(frag, "step-report__kv"), null,
      "an empty-after-exclusion dict must not render an empty kv frame");
    assert.equal(frag.childNodes.length, 0);
  });

  check("usage keys: renderDefaultReport falls to the empty state when only usage keys remain", () => {
    const frag = app.renderDefaultReport(
      { step_type: "merge_integrate", status: "completed" },
      Object.assign({}, USAGE_SAMPLE),
    );
    const empty = findOne(frag, "step-report__empty");
    assert.ok(empty, "the shared '(step produced no outputs)' empty state must render");
    assert.equal(empty.textContent, "(step produced no outputs)");
    assert.equal(findOne(frag, "step-report__kv"), null);
  });

  check("usage keys: renderDefaultReport still renders the real fields alongside", () => {
    const frag = app.renderDefaultReport(
      { step_type: "version_reconcile", status: "completed" },
      Object.assign({ reconciled: true }, USAGE_SAMPLE),
    );
    assert.equal(findOne(frag, "step-report__empty"), null);
    assert.deepEqual(kvKeys(frag), ["reconciled"]);
  });

  // =========================================================================
  // B. confirm
  // =========================================================================
  check("confirm: an approved verdict renders a ✓ status label, reviewer and reviewed step", () => {
    const frag = render("confirm", {
      review_result: {
        approved: true,
        feedback: "Grouping matches the doctrine.",
        reviewer: "llm",
        step_to_review_type: "plan",
        step_to_review_id: "plan_1",
      },
      revision_feedback: "Grouping matches the doctrine.",
    });
    const label = findOne(frag, "step-report__label");
    assert.ok(label.classList.contains("ok"), `expected an ok label, got ${label.className}`);
    assert.equal(label.textContent, "✓ Approved");
    const bar = findOne(frag, "step-report__status-bar");
    assert.ok(bar.textContent.includes("reviewer: llm"), bar.textContent);
    assert.ok(bar.textContent.includes("plan (plan_1)"), bar.textContent);
    assert.ok(frag.textContent.includes("Grouping matches the doctrine."));
  });

  check("confirm: a rejected verdict renders a ✗ status label", () => {
    const frag = render("confirm", {
      review_result: { approved: false, feedback: "G2 must not run in parallel." },
    });
    const label = findOne(frag, "step-report__label");
    assert.ok(label.classList.contains("fail"), `expected a fail label, got ${label.className}`);
    assert.equal(label.textContent, "✗ Revision requested");
  });

  check("confirm: reviewer falls back to step.inputs when review_result omits it", () => {
    const frag = render("confirm",
      { review_result: { approved: true, step_to_review_type: "implement" } },
      { inputs: { reviewer: "human", step_to_review_id: "implement_2" } });
    const bar = findOne(frag, "step-report__status-bar");
    assert.ok(bar.textContent.includes("reviewer: human"), bar.textContent);
    assert.ok(bar.textContent.includes("implement (implement_2)"), bar.textContent);
  });

  check("confirm: revision_feedback identical to feedback renders ONE section, not two", () => {
    const feedback = "Rework the grouping.";
    const frag = render("confirm", {
      review_result: { approved: false, feedback },
      revision_feedback: feedback,
    });
    const titles = findAll(frag, "step-report__section-title").map((n) => n.textContent);
    assert.deepEqual(titles, ["Feedback"]);
    const occurrences = frag.textContent.split(feedback).length - 1;
    assert.equal(occurrences, 1, `feedback must appear once, appeared ${occurrences}×`);
  });

  check("confirm: a genuinely different revision_feedback gets its own section", () => {
    const frag = render("confirm", {
      review_result: { approved: false, feedback: "Rework the grouping." },
      revision_feedback: "Split G2 out first.",
    });
    const titles = findAll(frag, "step-report__section-title").map((n) => n.textContent);
    assert.deepEqual(titles, ["Feedback", "Revision Feedback"]);
    assert.ok(frag.textContent.includes("Split G2 out first."));
  });

  check("confirm: long feedback is folded rather than dumped inline", () => {
    const long = "x".repeat(4000);
    const frag = render("confirm", { review_result: { approved: true, feedback: long } });
    assert.ok(findOne(frag, "foldable"), "long feedback must go through makeFoldable");
  });

  check("confirm: usage metadata never reaches the card", () => {
    const frag = render("confirm", Object.assign({
      review_result: { approved: true, feedback: "ok" },
    }, USAGE_SAMPLE));
    assert.ok(!frag.textContent.includes("total_cost_usd"), frag.textContent);
    assert.deepEqual(kvKeys(frag), []);
  });

  // =========================================================================
  // C. invariant_check
  // =========================================================================
  check("invariant_check: zero actionable issues renders ✓ PASSED plus the summary", () => {
    const frag = render("invariant_check", {
      issues: [],
      actionable_count: 0,
      invariant_check_result: { summary: "No recorded invariant is violated." },
    });
    const label = findOne(frag, "step-report__label");
    assert.ok(label.classList.contains("ok"), label.className);
    assert.equal(label.textContent, "✓ PASSED");
    assert.ok(frag.textContent.includes("No recorded invariant is violated."));
  });

  check("invariant_check: issues render grouped by severity with the anchored issue schema", () => {
    const frag = render("invariant_check", {
      actionable_count: 2,
      issues: [
        {
          severity: "critical",
          actual_behavior: "cache is stat-keyed",
          divergence: "charter requires content-keyed",
          evidence_lines: ["src/tianluo/engine/persistence.py:88"],
        },
        { severity: "low", description: "legacy schema row" },
      ],
      invariant_check_result: { summary: "Two violations." },
    });
    const label = findOne(frag, "step-report__label");
    assert.ok(label.classList.contains("fail"), label.className);
    assert.equal(label.textContent, "✗ 2 actionable issue(s)");
    const titles = findAll(frag, "step-report__section-title").map((n) => n.textContent);
    assert.deepEqual(titles, ["Summary", "critical (1)", "low (1)"]);
    assert.ok(frag.textContent.includes("cache is stat-keyed — charter requires content-keyed"),
      frag.textContent);
    assert.ok(frag.textContent.includes("@ src/tianluo/engine/persistence.py:88"),
      frag.textContent);
    assert.ok(frag.textContent.includes("legacy schema row"), frag.textContent);
  });

  check("invariant_check: a missing actionable_count falls back to issues.length, never ✓ PASSED", () => {
    const frag = render("invariant_check", {
      issues: [{ severity: "high", actual_behavior: "a", divergence: "b" }],
    });
    const label = findOne(frag, "step-report__label");
    assert.ok(label.classList.contains("fail"), label.className);
    assert.equal(label.textContent, "✗ 1 issue(s)");
    assert.ok(!frag.textContent.includes("✓ PASSED"), frag.textContent);
  });

  check("invariant_check: a failed step status wins over the outputs-derived branch", () => {
    const frag = render("invariant_check", { issues: [], actionable_count: 0 },
      { status: "failed" });
    const label = findOne(frag, "step-report__label");
    assert.ok(label.classList.contains("fail"), label.className);
    assert.equal(label.textContent, "✗ FAILED");
  });

  check("invariant_check: diagnostic payloads stay out of the card", () => {
    const frag = render("invariant_check", Object.assign({
      issues: [],
      actionable_count: 0,
      invariant_check_result: { summary: "clean" },
      raw_issues: [{ severity: "high", actual_behavior: "dropped by validation" }],
      validation_stats: { input_count: 4, kept_count: 0 },
      why_comment_hard_violations: [{ severity: "critical", description: "deleted WHY:" }],
      why_comment_losses: [{ file: "a.py", body: "lost comment" }],
      skipped_reason: "no_diff",
    }, USAGE_SAMPLE));
    const text = frag.textContent;
    for (const needle of ["dropped by validation", "input_count", "deleted WHY:",
      "lost comment", "no_diff", "total_cost_usd"]) {
      assert.ok(!text.includes(needle), `${needle} must not appear in the card: ${text}`);
    }
  });

  check("invariant_check: a non-array issues value degrades without throwing", () => {
    let frag;
    assert.doesNotThrow(() => { frag = render("invariant_check", { issues: null }); });
    assert.equal(findOne(frag, "step-report__label").textContent, "✓ PASSED");
  });

  // =========================================================================
  // G. adjudicate
  // =========================================================================
  check("adjudicate: a real ruling renders the type, rationale and adjudicated description", () => {
    const frag = render("adjudicate", {
      adjudication_noop: false,
      contradiction_type: "task_vs_plan",
      adjudication_rationale: "The task description governs; the plan narrowed it.",
      adjudicated_description: "Fix the renderer family, including adjudicate.",
    });
    const label = findOne(frag, "step-report__label");
    assert.equal(label.textContent, "contradiction ruled");
    assert.ok(label.classList.contains("highlight"), label.className);
    const bar = findOne(frag, "step-report__status-bar");
    assert.ok(bar.textContent.includes("type: task_vs_plan"), bar.textContent);
    const titles = findAll(frag, "step-report__section-title").map((n) => n.textContent);
    assert.deepEqual(titles, ["Rationale", "Adjudicated Description"]);
    assert.ok(frag.textContent.includes("The task description governs"));
    assert.ok(frag.textContent.includes("Fix the renderer family"));
  });

  check("adjudicate: a no-op ruling reads as a no-op and shows no description section", () => {
    const frag = render("adjudicate", {
      adjudication_noop: true,
      contradiction_type: "review_divergence",
      adjudication_rationale: "Every candidate was benign.",
    });
    const label = findOne(frag, "step-report__label");
    assert.equal(label.textContent, "no-op (no contradiction ruled)");
    assert.ok(label.classList.contains("muted"), label.className);
    const titles = findAll(frag, "step-report__section-title").map((n) => n.textContent);
    assert.deepEqual(titles, ["Rationale"]);
  });

  check("adjudicate: the audit structures stay out of the card", () => {
    const frag = render("adjudicate", Object.assign({
      adjudication_noop: false,
      contradiction_type: "task_vs_plan",
      adjudication_rationale: "ruled",
      candidate_verdicts: [{ verdict: "real", quote: "CANDIDATE_VERDICT_MARKER" }],
      rejected_candidates: [{ quote: "REJECTED_CANDIDATE_MARKER" }],
      rejected_positions: ["REJECTED_POSITION_MARKER"],
      superseded_fix_instructions: "SUPERSEDED_MARKER",
      candidates_considered: [{ file: "CANDIDATE_FILE_MARKER" }],
      abolished_fingerprints: ["ABOLISHED_MARKER"],
    }, USAGE_SAMPLE));
    const text = frag.textContent;
    for (const needle of ["CANDIDATE_VERDICT_MARKER", "REJECTED_CANDIDATE_MARKER",
      "REJECTED_POSITION_MARKER", "SUPERSEDED_MARKER", "CANDIDATE_FILE_MARKER",
      "ABOLISHED_MARKER", "total_cost_usd"]) {
      assert.ok(!text.includes(needle), `${needle} must not appear in the card: ${text}`);
    }
  });

  // =========================================================================
  // G. STEP_REPORT_TITLES fallback entries
  // =========================================================================
  check("titles: invariant_check / adjudicate / e2e no longer degrade to the raw step key", () => {
    for (const [type, expected] of [
      ["invariant_check", "Invariant Check · Result"],
      ["adjudicate", "Adjudication · Result"],
      ["e2e", "E2E Scenarios · Result"],
    ]) {
      assert.equal(app.STEP_REPORT_TITLES[type] != null, true,
        `${type} missing from STEP_REPORT_TITLES`);
      assert.equal(app.reportCardTitle(type), expected);
    }
  });

  check("titles: an unknown step type still degrades to the raw key (unchanged)", () => {
    assert.equal(app.reportCardTitle("brand_new_step"), "brand_new_step · Result");
  });

  // =========================================================================
  // shared issue projection
  // =========================================================================
  check("reportIssueFields: anchored schema wins, legacy schema still resolves", () => {
    assert.deepEqual(app.reportIssueFields({
      severity: "medium",
      actual_behavior: "A",
      divergence: "B",
      evidence_lines: ["", "f.py:1"],
    }), { severity: "medium", description: "A — B", location: "f.py:1" });
    assert.deepEqual(app.reportIssueFields({ description: "legacy", location: "x.py" }),
      { severity: "high", description: "legacy", location: "x.py" });
    assert.deepEqual(app.reportIssueFields({ message: "msg", missing_in: ["charter"] }),
      { severity: "high", description: "msg", location: "missing_in: charter" });
  });
}

// ---------------------------------------------------------------------------
// Standalone bootstrap: install a self-contained DOM stub and run the checks.
// Only executed when this module is the process entry point; when imported by
// test_app_pure.mjs the export above is used against that harness's stub.
// ---------------------------------------------------------------------------
function isMainEntry() {
  try {
    return process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
  } catch (_) {
    return false;
  }
}

if (isMainEntry()) {
  class FakeNode {
    constructor(tag) {
      this.tagName = String(tag || "").toUpperCase();
      this.nodeType = this.tagName === "#TEXT" ? 3 : 1;
      this._classes = new Set();
      this.childNodes = [];
      this._text = "";
      this.dataset = {};
      this.style = {};
      this._listeners = {};
      this.parentNode = null;
      this.type = "";
      this.title = "";
      this.classList = {
        add: (...cs) => { for (const c of cs) if (c) this._classes.add(c); },
        remove: (...cs) => { for (const c of cs) this._classes.delete(c); },
        contains: (c) => this._classes.has(c),
        toggle: (c, force) => {
          const want = force === undefined ? !this._classes.has(c) : !!force;
          if (want) this._classes.add(c); else this._classes.delete(c);
          return want;
        },
      };
    }
    set className(v) { this._classes = new Set(String(v || "").split(/\s+/).filter(Boolean)); }
    get className() { return Array.from(this._classes).join(" "); }
    set textContent(v) { this._text = String(v == null ? "" : v); this.childNodes = []; }
    get textContent() {
      if (this.childNodes.length) return this.childNodes.map((c) => c.textContent).join("");
      return this._text;
    }
    set innerHTML(_v) { this.childNodes = []; this._text = ""; }
    get innerHTML() { return ""; }
    get children() { return this.childNodes.filter((c) => c && c.nodeType !== 3); }
    _detach(node) {
      const i = this.childNodes.indexOf(node);
      if (i >= 0) { this.childNodes.splice(i, 1); node.parentNode = null; }
    }
    appendChild(child) {
      if (child && child.tagName === "#FRAGMENT") {
        for (const c of child.childNodes.slice()) this.appendChild(c);
        child.childNodes = [];
        return child;
      }
      if (child.parentNode) child.parentNode._detach(child);
      child.parentNode = this;
      this.childNodes.push(child);
      return child;
    }
    append(...nodes) {
      for (const n of nodes) this.appendChild(typeof n === "string" ? makeText(n) : n);
    }
    removeChild(node) { this._detach(node); return node; }
    remove() { if (this.parentNode) this.parentNode._detach(this); }
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
    closest() { return null; }
    scrollIntoView() {}
  }
  function makeText(text) {
    const n = new FakeNode("#text");
    n._text = String(text == null ? "" : text);
    return n;
  }
  const _elementsById = {};
  globalThis.Node = FakeNode;
  globalThis.requestAnimationFrame = () => {};
  globalThis.document = {
    createElement: (tag) => new FakeNode(tag),
    createTextNode: (text) => makeText(text),
    createDocumentFragment: () => new FakeNode("#fragment"),
    getElementById: (id) => {
      if (!_elementsById[id]) _elementsById[id] = new FakeNode("div");
      return _elementsById[id];
    },
    addEventListener: () => {},
  };
  function findAll(node, cls, acc = []) {
    if (!node || !node.childNodes) return acc;
    for (const c of node.childNodes) {
      if (c.classList && c.classList.contains(cls)) acc.push(c);
      findAll(c, cls, acc);
    }
    return acc;
  }
  function findOne(node, cls) { return findAll(node, cls)[0] || null; }

  const require = createRequire(import.meta.url);
  const here = path.dirname(fileURLToPath(import.meta.url));
  const app = require(path.join(here, "..", "..", "src", "tianluo", "server", "static", "app.js"));

  let passed = 0;
  let failed = 0;
  function check(name, fn) {
    try { fn(); passed += 1; console.log("  ok -", name); }
    catch (e) { failed += 1; console.error("  FAIL -", name, "\n", e && e.stack ? e.stack : e); }
  }

  await registerStepReportRendererTests({ app, check, findOne, findAll });

  console.log(`\nstep_report_renderers: ${passed} passed, ${failed} failed — ${passed} checks passed`);
  if (failed > 0) process.exit(1);
}
