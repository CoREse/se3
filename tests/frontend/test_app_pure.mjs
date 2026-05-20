/*
 * Lightweight Node assertion test for the DOM-free pure helpers in the web
 * console's `app.js` (record classification, intervention derivation).
 *
 * Run manually:  node tests/frontend/test_app_pure.mjs
 *
 * This is intentionally not a pytest module — the pytest suite is Python-only.
 * It exists so the role-based classification and intervention logic that the
 * chat view depends on can be exercised without a browser.
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const app = require(path.join(here, "..", "..", "src", "se3", "server", "static", "app.js"));

let passed = 0;
function check(name, fn) {
  fn();
  passed += 1;
  console.log("  ok -", name);
}

// -- isCollapsibleRole: strictly role-based ---------------------------------
check("user role collapses", () => {
  assert.equal(app.isCollapsibleRole("user"), true);
});
check("system role collapses", () => {
  assert.equal(app.isCollapsibleRole("system"), true);
});
check("assistant role stays expanded", () => {
  assert.equal(app.isCollapsibleRole("assistant"), false);
});
check("unknown / other role stays expanded", () => {
  assert.equal(app.isCollapsibleRole("log"), false);
  assert.equal(app.isCollapsibleRole(""), false);
  assert.equal(app.isCollapsibleRole(null), false);
});

// -- normalizeRecord folds `human` into `user` ------------------------------
check("human role normalizes to user and so collapses", () => {
  const norm = app.normalizeRecord({ message: { role: "human", content: "hi" } });
  assert.equal(norm.role, "user");
  assert.equal(app.isCollapsibleRole(norm.role), true);
});
check("assistant content recovered from raw_json stays expanded", () => {
  const norm = app.normalizeRecord({
    message: {
      role: "assistant",
      raw_json: [
        { type: "assistant", message: { content: [{ type: "text", text: "answer" }] } },
      ],
    },
  });
  assert.equal(norm.role, "assistant");
  assert.equal(norm.content, "answer");
  assert.equal(app.isCollapsibleRole(norm.role), false);
});

// -- chipLabel --------------------------------------------------------------
check("chipLabel includes role and step context", () => {
  assert.equal(
    app.chipLabel({ role: "system", stepType: "discovery" }),
    "system prompt · discovery",
  );
});
check("chipLabel falls back to bare label without context", () => {
  assert.equal(app.chipLabel({ role: "user", stepType: "" }), "user prompt");
});

// -- normalizeKind ----------------------------------------------------------
check("normalizeKind keeps known kinds", () => {
  for (const k of ["call", "interjection", "retry_decision", "cli_confirm"]) {
    assert.equal(app.normalizeKind(k), k);
  }
});
check("normalizeKind degrades unknown kind to call", () => {
  assert.equal(app.normalizeKind("mystery"), "call");
  assert.equal(app.normalizeKind(undefined), "call");
});

// -- computeInterventions ---------------------------------------------------
// The synthetic interjection entry is opt-in: it is only appended when the
// user has clicked the Interject button (state.flowInterjectRequested is
// true). The module-private flag is false by default in the require-loaded
// module, so without an opt-in toggle these tests exercise the default path.
check("running flow with no calls and no opt-in has no synthetic entry", () => {
  const entries = app.computeInterventions({ status: "running", pending_calls: [] });
  assert.equal(entries.length, 0);
});
check("completed flow with no calls has no intervention entries", () => {
  const entries = app.computeInterventions({ status: "completed", pending_calls: [] });
  assert.equal(entries.length, 0);
});
check("pending calls become entries keyed by kind and call_id", () => {
  const entries = app.computeInterventions({
    status: "running",
    pending_calls: [
      { call_id: "c1", kind: "call", prompt: "approve?" },
      { call_id: "c2", kind: "cli_confirm", prompt: "press 1", options: ["1", "2"] },
    ],
  });
  // Two real calls; no synthetic interjection without explicit opt-in.
  assert.equal(entries.length, 2);
  assert.equal(entries[0].callId, "c1");
  assert.equal(entries[1].kind, "cli_confirm");
  assert.deepEqual(entries[1].options, ["1", "2"]);
});
check("explicit interjection call surfaces as a real entry", () => {
  const entries = app.computeInterventions({
    status: "running",
    pending_calls: [{ call_id: "i1", kind: "interjection", prompt: "ctrl-c" }],
  });
  assert.equal(entries.length, 1);
  assert.equal(entries[0].callId, "i1");
  assert.equal(entries[0].synthetic, false);
});

// -- pendingCalls: flow_id fallback filter ---------------------------------
// The backend daemon aggregator filters pending_calls by the open flow's
// flow_id; pendingCalls() in app.js mirrors that strict semantics as a
// defensive fallback in case an older daemon hasn't filtered. A call whose
// context.flow_id matches the open flow is kept; a mismatching call is
// dropped; an unannotated call (no flow_id at all) is also dropped — the
// backend producers responsible for legitimate in-flow calls (confirm,
// discovery, etc.) record a flow_id, so an unattributed call indicates a
// cross-scenario artifact (merge_*, sync_conflicts_*).
check("pendingCalls keeps calls matching flow_id", () => {
  const flow = {
    flow_id: "F1",
    pending_calls: [
      { call_id: "c1", context: { flow_id: "F1" } },
    ],
  };
  assert.equal(app.pendingCalls(flow).length, 1);
});
check("pendingCalls drops calls from a different flow", () => {
  const flow = {
    flow_id: "F1",
    pending_calls: [
      { call_id: "c1", context: { flow_id: "F1" } },
      { call_id: "c2", context: { flow_id: "F2" } },
    ],
  };
  const kept = app.pendingCalls(flow);
  assert.equal(kept.length, 1);
  assert.equal(kept[0].call_id, "c1");
});
check("pendingCalls drops unattributed calls when flow_id is known", () => {
  const flow = {
    flow_id: "F1",
    pending_calls: [
      { call_id: "c1" },
      { call_id: "c2", context: {} },
      { call_id: "c3", context: null },
    ],
  };
  assert.equal(app.pendingCalls(flow).length, 0);
});
check("pendingCalls passes everything through when flow has no flow_id", () => {
  const flow = {
    pending_calls: [
      { call_id: "c1" },
      { call_id: "c2", context: { flow_id: "F2" } },
    ],
  };
  assert.equal(app.pendingCalls(flow).length, 2);
});

// -- isActiveFlow -----------------------------------------------------------
check("isActiveFlow true for running/paused, false for terminal", () => {
  assert.equal(app.isActiveFlow({ status: "running" }), true);
  assert.equal(app.isActiveFlow({ status: "paused" }), true);
  assert.equal(app.isActiveFlow({ status: "completed" }), false);
  assert.equal(app.isActiveFlow({ status: "failed" }), false);
});

// -- option label/value resolution -----------------------------------------
check("optionLabel/optionText resolve string and object forms", () => {
  assert.equal(app.optionLabel("retry"), "retry");
  assert.equal(app.optionText("retry"), "retry");
  assert.equal(app.optionLabel({ label: "Retry", value: "1" }), "Retry");
  assert.equal(app.optionText({ label: "Retry", value: "1" }), "1");
});

// -- populateProjectSelect --------------------------------------------------
// The function is a pure DOM helper but works against a tiny stub: it only
// touches `innerHTML`, `appendChild`, `disabled`, `value`, and the `Option`
// constructor. The stub below records the options it receives so the test
// can assert what got rendered.

class FakeSelect {
  constructor() {
    this.options = [];
    this.disabled = false;
    this.value = "";
    this._explicit = false;
  }
  set innerHTML(_) { this.options = []; this.value = ""; this._explicit = false; }
  appendChild(opt) {
    this.options.push(opt);
    // Replicate <select> behavior: an explicit `selected=true` wins; an
    // ambient first-enabled option becomes the value only when nothing
    // explicit has been chosen.
    if (opt.selected) {
      this.value = opt.value;
      this._explicit = true;
    } else if (!this._explicit && this.value === "" && !opt.disabled) {
      this.value = opt.value;
    }
    return opt;
  }
}

class FakeHint {
  constructor() { this.classes = new Set(["hidden"]); }
  classList = {
    add: (c) => this.classes.add(c),
    remove: (c) => this.classes.delete(c),
    contains: (c) => this.classes.has(c),
  };
  get hidden() { return this.classes.has("hidden"); }
}

// `populateProjectSelect` constructs options via `new Option(label, value)`;
// expose a minimal global so the Node test can stand in for the browser one.
globalThis.Option = class {
  constructor(label, value) {
    this.label = label;
    this.text = label;
    this.value = value;
    this.disabled = false;
    this.selected = false;
  }
};

check("populateProjectSelect: zero roots disables submit and shows hint", () => {
  const sel = new FakeSelect();
  const hint = new FakeHint();
  const submit = { disabled: false };
  const result = app.populateProjectSelect(sel, [], { emptyHint: hint, submit });
  assert.equal(result, null);
  assert.equal(sel.disabled, true);
  assert.equal(submit.disabled, true);
  assert.equal(hint.hidden, false);
});

check("populateProjectSelect: one root auto-selects", () => {
  const sel = new FakeSelect();
  const hint = new FakeHint();
  const submit = { disabled: true };
  const result = app.populateProjectSelect(sel, ["/proj/a"], {
    emptyHint: hint, submit,
  });
  assert.equal(result, "/proj/a");
  assert.equal(sel.disabled, false);
  assert.equal(sel.value, "/proj/a");
  assert.equal(submit.disabled, false);
  assert.equal(hint.hidden, true);
});

check("populateProjectSelect: multiple roots leaves placeholder selected", () => {
  const sel = new FakeSelect();
  const submit = { disabled: true };
  const result = app.populateProjectSelect(sel, ["/proj/a", "/proj/b"], { submit });
  assert.equal(result, null);
  assert.equal(sel.disabled, false);
  // Submit is enabled (something exists to pick) but no concrete root is the
  // value yet — `required` on the <select> forces the user to choose.
  assert.equal(submit.disabled, false);
  assert.equal(sel.value, "");
  // First option is the disabled "(select…)" placeholder, followed by roots.
  assert.equal(sel.options.length, 3);
  assert.equal(sel.options[0].disabled, true);
  assert.equal(sel.options[1].value, "/proj/a");
  assert.equal(sel.options[2].value, "/proj/b");
});

// -- splitUserPromptByMarker -----------------------------------------------
// The frontend splits a user-role prompt at the sentinel markers the engine
// injects (TEMPLATE_PREFIX_END / USER_CONTENT_BEGIN) so the boilerplate
// prefix renders as a default-collapsed chip and the actual user content
// renders as a default-expanded bubble. Absent markers (legacy / non-step
// prompts) fall back to the whole-message chip path — the helper returns
// null so the caller can.
check("splitUserPromptByMarker splits when both markers present", () => {
  const TPE = app.TEMPLATE_PREFIX_END;
  const UCB = app.USER_CONTENT_BEGIN;
  const sample = "You are an expert engineer.\n" + TPE + "\n" + UCB + "\n## Task\nDo it";
  const split = app.splitUserPromptByMarker(sample);
  assert.ok(split, "split returned null but should have");
  assert.equal(split.prefix.startsWith("You are an expert engineer."), true);
  assert.equal(split.content.startsWith("## Task"), true);
});
check("splitUserPromptByMarker returns null without markers (legacy)", () => {
  assert.equal(app.splitUserPromptByMarker("plain user message"), null);
});
check("splitUserPromptByMarker handles empty / non-string input", () => {
  assert.equal(app.splitUserPromptByMarker(""), null);
  assert.equal(app.splitUserPromptByMarker(null), null);
  assert.equal(app.splitUserPromptByMarker(undefined), null);
});
check("splitUserPromptByMarker tolerates missing USER_CONTENT_BEGIN", () => {
  const TPE = app.TEMPLATE_PREFIX_END;
  const sample = "prefix only\n" + TPE + "\nuser content here";
  const split = app.splitUserPromptByMarker(sample);
  assert.ok(split);
  assert.equal(split.prefix, "prefix only\n");
  assert.equal(split.content, "user content here");
});

// -- normalizeRecord: step_completed event ---------------------------------
// A step_completed (or step_failed) event riding the conversation channel
// carries `type` + structured `data.step` instead of a chat-style role; the
// normalized form exposes `kind` and `stepReport` so the renderer can build
// both the raw event chip and the default-expanded report card.
check("normalizeRecord recognises step_completed event", () => {
  const norm = app.normalizeRecord({
    step_id: "07_test",
    message: {
      type: "step_completed",
      step_id: "07_test",
      step_type: "test",
      timestamp: 1234,
      data: {
        step: {
          step_id: "07_test",
          step_type: "test",
          status: "completed",
          outputs: { test_results: { overall_passed: true } },
        },
      },
    },
  });
  assert.equal(norm.kind, "step_completed");
  assert.equal(norm.role, "step-event");
  assert.equal(norm.stepType, "test");
  assert.ok(norm.stepReport);
  assert.equal(norm.stepReport.outputs.test_results.overall_passed, true);
});
check("normalizeRecord recognises step_failed event", () => {
  const norm = app.normalizeRecord({
    message: {
      type: "step_failed",
      step_type: "implement",
      data: { step: { step_type: "implement", status: "failed", outputs: {} } },
    },
  });
  assert.equal(norm.kind, "step_failed");
  assert.equal(norm.stepReport.status, "failed");
});

// -- step report renderer registry -----------------------------------------
check("STEP_REPORT_RENDERERS covers the 11 named step types", () => {
  const expected = [
    "analyze", "plan", "implement", "test", "self_check", "verify_spec",
    "update_spec", "commit", "version_analyze", "summarize", "discovery",
  ];
  for (const t of expected) {
    assert.equal(
      typeof app.STEP_REPORT_RENDERERS[t], "function",
      "missing renderer for " + t,
    );
  }
  // Exactly 11 — the CLI registry has 11 custom renderers (PROPOSE/DESIGN are
  // deprecated and intentionally excluded; DISCOVERY adds a frontend renderer).
  assert.equal(Object.keys(app.STEP_REPORT_RENDERERS).length, expected.length);
});
check("STEP_REPORT_TITLES covers every step type from models.StepType", () => {
  const expected = [
    "discovery", "analyze", "project_summary", "propose", "design", "plan",
    "plan_tasks", "confirm", "implement", "test", "self_check", "verify_spec",
    "update_spec", "version_analyze", "commit", "summarize",
  ];
  for (const t of expected) assert.ok(app.STEP_REPORT_TITLES[t], "missing title " + t);
});

console.log(`\n${passed} checks passed.`);
