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
  for (const k of [
    "call",
    "interjection",
    "retry_decision",
    "cli_confirm",
    "discovery_confirm",
  ]) {
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

check("populateProjectSelect: zero roots still offers the manual sentinel", () => {
  const sel = new FakeSelect();
  const hint = new FakeHint();
  const submit = { disabled: false };
  const result = app.populateProjectSelect(sel, [], { emptyHint: hint, submit });
  // The empty hint is shown, but the manual sentinel is appended so the
  // user can still publish by typing an absolute path by hand.
  assert.equal(result, app.PROJECT_MANUAL_SENTINEL);
  assert.equal(sel.disabled, false);
  assert.equal(submit.disabled, false);
  assert.equal(hint.hidden, false);
  assert.equal(sel.options.length, 1);
  assert.equal(sel.options[0].value, app.PROJECT_MANUAL_SENTINEL);
  assert.equal(sel.value, app.PROJECT_MANUAL_SENTINEL);
});

check("populateProjectSelect: one root auto-selects and appends manual", () => {
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
  // The known root option, followed by the manual sentinel.
  assert.equal(sel.options.length, 2);
  assert.equal(sel.options[0].value, "/proj/a");
  assert.equal(sel.options[1].value, app.PROJECT_MANUAL_SENTINEL);
});

check("populateProjectSelect: multiple roots leaves placeholder + appends manual", () => {
  const sel = new FakeSelect();
  const submit = { disabled: true };
  const result = app.populateProjectSelect(sel, ["/proj/a", "/proj/b"], { submit });
  assert.equal(result, null);
  assert.equal(sel.disabled, false);
  // Submit is enabled (something exists to pick) but no concrete root is the
  // value yet — `required` on the <select> forces the user to choose.
  assert.equal(submit.disabled, false);
  assert.equal(sel.value, "");
  // Placeholder, two roots, manual sentinel.
  assert.equal(sel.options.length, 4);
  assert.equal(sel.options[0].disabled, true);
  assert.equal(sel.options[1].value, "/proj/a");
  assert.equal(sel.options[2].value, "/proj/b");
  assert.equal(sel.options[3].value, app.PROJECT_MANUAL_SENTINEL);
});

// -- isValidAbsolutePath ----------------------------------------------------
check("isValidAbsolutePath: accepts absolute paths only", () => {
  assert.equal(app.isValidAbsolutePath("/abs/path"), true);
  assert.equal(app.isValidAbsolutePath("/"), true);
  assert.equal(app.isValidAbsolutePath("relative/path"), false);
  assert.equal(app.isValidAbsolutePath("./relative"), false);
  assert.equal(app.isValidAbsolutePath(""), false);
  assert.equal(app.isValidAbsolutePath("   "), false);
  assert.equal(app.isValidAbsolutePath(null), false);
  assert.equal(app.isValidAbsolutePath(undefined), false);
});

// -- splitUserPromptByMarker -----------------------------------------------
// The frontend splits a user-role prompt at the three-segment sentinel
// markers the engine injects (TEMPLATE_PREFIX_END / USER_CONTENT_BEGIN /
// USER_CONTENT_END). Three-segment input returns `{prefix, content, suffix}`
// so the framework tail (Available Specs / runtime env / READ-ONLY / language
// directive) joins the system-prompt chip rather than leaking into the user
// content bubble. Two-segment legacy input (TEMPLATE_PREFIX_END +
// USER_CONTENT_BEGIN with no USER_CONTENT_END) returns `{prefix, content:"",
// suffix: <rest>}` — the post-BEGIN tail is framework-injected text on every
// non-discovery step prompt module, so it is collapsed into the chip's
// suffix subsection and the user-content bubble is omitted, matching the
// no-marker fallback behavior. Missing or malformed markers return null so
// the caller can fall back to the whole-message chip path.
check("splitUserPromptByMarker three-segment input returns prefix/content/suffix", () => {
  const TPE = app.TEMPLATE_PREFIX_END;
  const UCB = app.USER_CONTENT_BEGIN;
  const UCE = app.USER_CONTENT_END;
  const sample =
    "You are an expert engineer.\n" + TPE + "\n" +
    UCB + "\nDo X.\n" + UCE + "\n" +
    "## Available Specs\nspec list";
  const split = app.splitUserPromptByMarker(sample);
  assert.ok(split, "split returned null but should have");
  assert.equal(split.prefix.startsWith("You are an expert engineer."), true);
  assert.equal(split.content, "Do X.");
  assert.equal(split.suffix.startsWith("## Available Specs"), true);
});
check("splitUserPromptByMarker two-segment input routes tail into suffix", () => {
  const TPE = app.TEMPLATE_PREFIX_END;
  const UCB = app.USER_CONTENT_BEGIN;
  const sample = "You are an expert engineer.\n" + TPE + "\n" + UCB + "\n## Task\nDo it";
  const split = app.splitUserPromptByMarker(sample);
  assert.ok(split, "split returned null but should have");
  assert.equal(split.prefix.startsWith("You are an expert engineer."), true);
  // Legacy two-marker layout: the post-BEGIN tail is framework-injected
  // (task-description heading, project context, spec_content, runtime-env,
  // READ-ONLY constraint, …) and MUST collapse into the chip's suffix
  // subsection — not regress to an expanded user-content bubble.
  assert.equal(split.content, "");
  assert.equal(split.suffix.startsWith("## Task"), true);
});
check("splitUserPromptByMarker returns null without markers (legacy)", () => {
  assert.equal(app.splitUserPromptByMarker("plain user message"), null);
});
check("splitUserPromptByMarker handles empty / non-string input", () => {
  assert.equal(app.splitUserPromptByMarker(""), null);
  assert.equal(app.splitUserPromptByMarker(null), null);
  assert.equal(app.splitUserPromptByMarker(undefined), null);
});
check("splitUserPromptByMarker returns null when USER_CONTENT_BEGIN is missing", () => {
  const TPE = app.TEMPLATE_PREFIX_END;
  // Only TEMPLATE_PREFIX_END is present — the three-segment contract
  // requires at least the first two markers so we treat this as malformed
  // and let the caller fall back to the whole-message chip.
  const sample = "prefix only\n" + TPE + "\nuser content here";
  assert.equal(app.splitUserPromptByMarker(sample), null);
});
check("splitUserPromptByMarker returns null when USER_CONTENT_END precedes USER_CONTENT_BEGIN", () => {
  const TPE = app.TEMPLATE_PREFIX_END;
  const UCB = app.USER_CONTENT_BEGIN;
  const UCE = app.USER_CONTENT_END;
  // END appears in the prefix before BEGIN — order is invalid.
  const sample = "prefix " + UCE + " stuff\n" + TPE + "\n" + UCB + "\ncontent";
  const split = app.splitUserPromptByMarker(sample);
  // We don't require null here strictly (an END before BEGIN is ignored as
  // a stray, and we fall through to two-segment), but the split must NOT
  // pick the bogus end up as the content terminator. In the two-segment
  // fallback the post-BEGIN tail is routed into `suffix` and `content` is
  // empty so no bubble is rendered.
  assert.ok(split);
  assert.equal(split.content, "");
  assert.equal(split.suffix.startsWith("content"), true);
});

// -- STEP_ASSISTANT_RENDERERS registry --------------------------------------
check("STEP_ASSISTANT_RENDERERS exposes the discovery renderer", () => {
  assert.equal(typeof app.STEP_ASSISTANT_RENDERERS.discovery, "function");
});
check("registerAssistantRenderer adds a renderer to the registry", () => {
  const fakeStep = "__test_step_" + Math.random().toString(36).slice(2);
  const renderer = () => null;
  app.registerAssistantRenderer(fakeStep, renderer);
  assert.equal(app.STEP_ASSISTANT_RENDERERS[fakeStep], renderer);
  delete app.STEP_ASSISTANT_RENDERERS[fakeStep];
});
check("registerAssistantRenderer rejects non-function values", () => {
  const before = Object.keys(app.STEP_ASSISTANT_RENDERERS).length;
  app.registerAssistantRenderer("bogus", "not a function");
  app.registerAssistantRenderer("", () => null);
  assert.equal(Object.keys(app.STEP_ASSISTANT_RENDERERS).length, before);
});
check("STEP_ASSISTANT_RENDERERS covers every structured step type", () => {
  // Discovery has its dedicated card renderer; the rest share the generic
  // factory. All structured step types must have an assistant renderer so an
  // assistant turn defaults to fields, never a raw ```json``` blob.
  const expected = [
    "discovery", "analyze", "plan", "plan_tasks", "implement", "test",
    "self_check", "verify_spec", "update_spec", "commit", "version_analyze",
    "summarize",
  ];
  for (const t of expected) {
    assert.equal(
      typeof app.STEP_ASSISTANT_RENDERERS[t], "function",
      "missing assistant renderer for " + t,
    );
  }
});
// -- makeStructuredAssistantRenderer (DOM-free fallbacks) -------------------
// The factory must return null (→ caller falls back to the generic renderer)
// rather than throw, so no assistant message is ever lost.
check("makeStructuredAssistantRenderer returns null when no JSON is present", () => {
  const r = app.makeStructuredAssistantRenderer("analyze");
  assert.equal(r("just prose, no json here", {}), null);
});
check("makeStructuredAssistantRenderer returns null for a top-level array", () => {
  const r = app.makeStructuredAssistantRenderer("analyze");
  // A bare trailing array is valid JSON but not a dict step result.
  assert.equal(r("preamble\n[1, 2, 3]", {}), null);
});
check("makeStructuredAssistantRenderer returns null for an unknown step type", () => {
  const r = app.makeStructuredAssistantRenderer("__no_report_renderer__");
  assert.equal(r('```json\n{"a":1}\n```', {}), null);
});

// -- extractStructuredJson --------------------------------------------------
// Mirror of backend `parse_json_response`: pulls JSON out of fenced
// ```json…``` or a trailing bare object/array, returning the parsed value
// plus the surrounding narrative (text minus the JSON region).
check("extractStructuredJson handles a fenced ```json``` block", () => {
  const text = "Some narrative.\n```json\n{\"a\": 1}\n```\ntrailing words";
  const got = app.extractStructuredJson(text);
  assert.ok(got);
  assert.deepEqual(got.value, { a: 1 });
  assert.equal(got.narrative.includes("Some narrative."), true);
  assert.equal(got.narrative.includes("trailing words"), true);
  assert.equal(got.narrative.includes("```"), false);
});
check("extractStructuredJson handles a trailing bare JSON object", () => {
  const text = "Reading file...\nHere is the result:\n{\"content\": \"hi\"}";
  const got = app.extractStructuredJson(text);
  assert.ok(got);
  assert.deepEqual(got.value, { content: "hi" });
  assert.equal(got.narrative.includes("Reading file..."), true);
});
check("extractStructuredJson returns null when no JSON is found", () => {
  assert.equal(app.extractStructuredJson("plain narrative only"), null);
});
check("extractStructuredJson tolerates trailing commas", () => {
  const text = "```json\n{\"a\": 1, \"b\": 2,}\n```";
  const got = app.extractStructuredJson(text);
  assert.ok(got);
  assert.deepEqual(got.value, { a: 1, b: 2 });
});

// -- normalizeRecord: step_completed event ---------------------------------
// A step_completed (or step_failed) event riding the conversation channel
// carries `type` + structured `data.step` instead of a chat-style role; the
// normalized form exposes `kind` and `stepReport` so the renderer can build
// both the raw event chip and the default-expanded report card.
check("normalizeRecord recognises step_completed event", () => {
  // Real daemon shape: authoritative `step_type` at the envelope; the inner
  // `message` is the engine's structured step_completed event (no step_type).
  const norm = app.normalizeRecord({
    step_id: "07_test",
    step_type: "test",
    message: {
      type: "step_completed",
      step_id: "07_test",
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
    step_type: "implement",
    message: {
      type: "step_failed",
      data: { step: { step_type: "implement", status: "failed", outputs: {} } },
    },
  });
  assert.equal(norm.kind, "step_failed");
  assert.equal(norm.stepType, "implement");
  assert.equal(norm.stepReport.status, "failed");
});

// -- normalizeRecord: step_type envelope precedence -------------------------
// The whole point of this group: the daemon injects an authoritative
// `step_type` at the record ENVELOPE (parsed from the jsonl file-name by
// `parse_step_type_from_step_id`); real `message` payloads carry no step_type.
// normalizeRecord MUST prefer the envelope value, fall back to an inner
// `message.step_type` only for un-upgraded daemons, then to empty — never the
// reverse precedence. These are pure (no DOM) checks.
check("normalizeRecord prefers the envelope step_type over an inner one", () => {
  // A stray inner step_type must NOT shadow the daemon's authoritative
  // envelope value (the precedence bug `pick` would have introduced).
  const norm = app.normalizeRecord({
    step_id: "01_discovery_975607bb",
    step_type: "discovery",
    message: { role: "assistant", content: "hi", step_type: "stale_wrong_value" },
  });
  assert.equal(norm.stepType, "discovery");
});
check("normalizeRecord uses the envelope step_type when message has none", () => {
  // The real daemon shape: step_type only at the envelope, message has none.
  const norm = app.normalizeRecord({
    step_id: "02_analyze_8b536444",
    step_type: "analyze",
    message: { role: "assistant", content: "hi" },
  });
  assert.equal(norm.stepType, "analyze");
});
check("normalizeRecord falls back to message step_type for legacy daemons", () => {
  // A daemon that predates envelope injection sends no envelope step_type; the
  // inner message field (if any) is the backward-compatible fallback.
  const norm = app.normalizeRecord({
    step_id: "02_analyze_8b536444",
    message: { role: "assistant", content: "hi", step_type: "analyze" },
  });
  assert.equal(norm.stepType, "analyze");
});
check("normalizeRecord step_type is empty when neither level provides one", () => {
  const norm = app.normalizeRecord({
    step_id: "02_analyze_8b536444",
    message: { role: "assistant", content: "hi" },
  });
  assert.equal(norm.stepType, "");
});
check("normalizeRecord step-event branch prefers the envelope step_type", () => {
  // A stale inner data.step.step_type must not win over the envelope value.
  const norm = app.normalizeRecord({
    step_id: "05_implement_61605e42_G2",
    step_type: "implement",
    message: {
      type: "step_completed",
      data: { step: { step_type: "stale_wrong_value", status: "completed", outputs: {} } },
    },
  });
  assert.equal(norm.stepType, "implement");
  assert.equal(norm.stepReport.step_type, "implement");
});
check("normalizeRecord step-event branch falls back to inner step for legacy daemons", () => {
  const norm = app.normalizeRecord({
    step_id: "05_implement_61605e42",
    message: {
      type: "step_completed",
      data: { step: { step_type: "implement", status: "completed", outputs: {} } },
    },
  });
  assert.equal(norm.stepReport.step_type, "implement");
});

// Group-suffix and underscore-bearing types: the daemon (G1) already strips
// `_G\d+` group suffixes and the leading sequence / trailing hash, so the
// envelope value the frontend receives for `05_implement_61605e42_G2` is the
// clean `"implement"` and for `13_version_analyze_def456` it is
// `"version_analyze"`. The frontend just consumes that authoritative value.
check("normalizeRecord receives clean step_type for a group-suffixed step", () => {
  const norm = app.normalizeRecord({
    step_id: "05_implement_61605e42_G2",
    step_type: "implement",
    message: { role: "assistant", content: "done" },
  });
  assert.equal(norm.stepType, "implement");
  // The raw, group-suffixed file stem is preserved as the step id for grouping.
  assert.equal(norm.stepId, "05_implement_61605e42_G2");
});
check("normalizeRecord receives the underscore-bearing version_analyze type", () => {
  const norm = app.normalizeRecord({
    step_id: "13_version_analyze_def456",
    step_type: "version_analyze",
    message: { role: "assistant", content: "v" },
  });
  assert.equal(norm.stepType, "version_analyze");
});
check("normalizeRecord passes through the legacy commit_summary stem", () => {
  // For a legacy non-conforming name the daemon returns the original stem
  // ("commit_summary"); the frontend keeps it verbatim and degrades gracefully
  // (no registered renderer / header title — covered in the DOM section).
  const norm = app.normalizeRecord({
    step_id: "commit_summary",
    step_type: "commit_summary",
    message: { role: "assistant", content: "c" },
  });
  assert.equal(norm.stepType, "commit_summary");
});

// stepHeaderLabel + renderer lookups against the (post-G1) authoritative
// step_type values. These are pure registry/string lookups.
check("stepHeaderLabel maps the group-stripped implement type to IMPLEMENT", () => {
  assert.equal(app.stepHeaderLabel("implement"), "IMPLEMENT");
});
check("implement step_type hits the implement assistant renderer", () => {
  assert.equal(typeof app.STEP_ASSISTANT_RENDERERS.implement, "function");
});
check("legacy commit_summary stem has no renderer / header title (graceful)", () => {
  // No registered renderer → caller falls back to the generic path; no header
  // title → stepHeaderLabel returns the fallback, never the raw stem crashing.
  assert.equal(app.STEP_ASSISTANT_RENDERERS.commit_summary, undefined);
  assert.equal(app.stepHeaderLabel("commit_summary", "commit_summary"), "commit_summary");
});

// -- normalizeRecord: real jsonl sample regression --------------------------
// A literal sample of the real on-disk jsonl line shapes (from
// se3/history/<flow>/NN_<type>_<hash>(_Gk).jsonl): each `message` is just
// `{role, content}` with NO step_type. The daemon's read_flow wraps each line
// in `{step_id, step_type, message}`; this fixture reproduces that exact
// envelope so the regression catches any drift back to message-based step_type.
const REAL_JSONL_SAMPLE = [
  {
    step_id: "01_discovery_68e9f549",
    step_type: "discovery",
    message: { role: "user", content: "You are an expert software engineering assistant in DISCOVERY mode." },
  },
  {
    step_id: "02_analyze_8b536444",
    step_type: "analyze",
    message: { role: "assistant", content: "Looking at the engine." },
  },
  {
    step_id: "05_implement_0207ebe4_G2",
    step_type: "implement",
    message: { role: "user", content: "You are an expert software engineer. Implement the tasks." },
  },
];
check("normalizeRecord on real jsonl sample yields authoritative step types", () => {
  const norms = REAL_JSONL_SAMPLE.map(app.normalizeRecord);
  assert.deepEqual(norms.map((n) => n.stepType), ["discovery", "analyze", "implement"]);
  // Step ids stay the raw file stems (incl. the group suffix) for grouping.
  assert.deepEqual(
    norms.map((n) => n.stepId),
    ["01_discovery_68e9f549", "02_analyze_8b536444", "05_implement_0207ebe4_G2"],
  );
  // Every sample resolves to a known paradigm header — never the file stem.
  assert.deepEqual(
    norms.map((n) => app.stepHeaderLabel(n.stepType, n.stepId)),
    ["DISCOVERY", "ANALYZE", "IMPLEMENT"],
  );
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

// -- KIND_META: user-facing wording, no implementation vocabulary ----------
// The chip labels (and any other visible string in KIND_META) must use
// user-facing neutral phrases. They must NOT leak the internal transport
// vocabulary — `MCP`, `call_id`, or a literal `call ` followed by a hex id —
// into anything the user reads. (call_id remains only on hidden DOM
// attributes / hover tooltips.)
check("KIND_META covers the recognized kinds", () => {
  for (const k of [
    "call",
    "interjection",
    "retry_decision",
    "cli_confirm",
    "discovery_confirm",
  ]) {
    assert.ok(app.KIND_META[k], "missing KIND_META entry for " + k);
    assert.equal(typeof app.KIND_META[k].label, "string");
  }
});
check("KIND_META visible strings contain no MCP / call_id", () => {
  const FORBIDDEN_RE = /\bMCP\b|call_id|\bcall\s+[0-9a-f]+\b/i;
  for (const [kind, meta] of Object.entries(app.KIND_META)) {
    for (const field of ["label", "hint"]) {
      const v = meta[field];
      if (typeof v !== "string") continue;
      assert.equal(
        FORBIDDEN_RE.test(v), false,
        `KIND_META[${kind}].${field} leaks implementation vocabulary: ${v}`,
      );
    }
  }
});

// -- extractAssistantText: multi-shape NDJSON best-effort recovery ---------
// extractAssistantText is the recovery path the renderer takes when a
// record's `content` field is missing. It MUST cope with the several
// NDJSON shapes the engine emits, so the assistant bubble never falls
// back to dumping the raw NDJSON as a `<pre>` block. We exercise each
// recognized shape and one "best-effort summary" fallback.

check("extractAssistantText handles `{type:assistant, message:{...}}`", () => {
  const out = app.extractAssistantText([
    { type: "assistant", message: { content: [{ type: "text", text: "hello" }] } },
  ]);
  assert.equal(out, "hello");
});

check("extractAssistantText handles bare `{role:assistant, content:[...]}`", () => {
  const out = app.extractAssistantText([
    { role: "assistant", content: [{ type: "text", text: "bare" }] },
  ]);
  assert.equal(out, "bare");
});

check("extractAssistantText handles `{content:string}` envelopes", () => {
  const out = app.extractAssistantText([{ content: "plain text" }]);
  assert.equal(out, "plain text");
});

check("extractAssistantText handles `content_block_delta` deltas", () => {
  const out = app.extractAssistantText([
    { type: "content_block_delta", delta: { text: "Hello" } },
    { type: "content_block_delta", delta: { text: ", world" } },
  ]);
  assert.equal(out, "Hello, world");
});

check("extractAssistantText handles `message_delta` deltas", () => {
  const out = app.extractAssistantText([
    { type: "message_delta", delta: { text: "abc" } },
  ]);
  assert.equal(out, "abc");
});

check("extractAssistantText handles `content_block_start` with tool_use", () => {
  const out = app.extractAssistantText([
    { type: "content_block_start",
      content_block: { type: "tool_use", name: "Bash", input: { cmd: "ls" } } },
  ]);
  // tool_use rendered as inline `[Name: {…}]` marker so the tool-marker
  // layer can split it into its own block.
  assert.ok(out.includes("[Bash:"), `expected [Bash: marker, got ${out}`);
  assert.ok(out.includes("ls"));
});

check("extractAssistantText handles bare string lines", () => {
  const out = app.extractAssistantText(["raw text\n"]);
  assert.equal(out, "raw text\n");
});

check("extractAssistantText recovers tool_use blocks inside assistant message", () => {
  const out = app.extractAssistantText([
    {
      type: "assistant",
      message: {
        content: [
          { type: "text", text: "Now running:" },
          { type: "tool_use", name: "Read", input: { file_path: "/tmp/x" } },
        ],
      },
    },
  ]);
  assert.ok(out.includes("Now running:"));
  assert.ok(out.includes("[Read:"));
});

check("extractAssistantText keeps best-effort summary for unknown structured blocks", () => {
  // An unknown `type` is summarized to JSON rather than dropped silently,
  // so the bubble shows _something_ rather than degrading to raw NDJSON.
  const out = app.extractAssistantText([
    { type: "mystery_block", note: "x" },
  ]);
  assert.ok(out.length > 0, "expected non-empty best-effort summary");
  assert.ok(out.includes("mystery_block"));
});

check("extractAssistantText returns empty string for empty / invalid input", () => {
  assert.equal(app.extractAssistantText([]), "");
  assert.equal(app.extractAssistantText(null), "");
  assert.equal(app.extractAssistantText("not an array"), "");
});

check("extractAssistantText skips noise event types silently", () => {
  // These shapes occur in the Anthropic event stream but carry no text
  // payload; they should not generate noise in the bubble.
  const out = app.extractAssistantText([
    { type: "message_start" },
    { type: "message_stop" },
    { type: "content_block_stop" },
    { type: "ping" },
  ]);
  assert.equal(out, "");
});

// ---------------------------------------------------------------------------
// Minimal DOM stub for the incremental-reconciliation + chip-refresh tests
// ---------------------------------------------------------------------------
//
// renderConversation / addConversationRecords / insertBubbleSorted /
// rebuildStepHeaders / renderInterventions are DOM functions, but they only
// touch a small, well-understood slice of the DOM API. The stub below
// implements just that slice — createElement / createTextNode /
// createDocumentFragment, appendChild / insertBefore / removeChild, a
// className<->classList mirror, textContent, and a `children` view that
// excludes text/fragment nodes (mirroring the real `Element.children`). It is
// in the same spirit as the FakeSelect / FakeHint stubs above: enough to
// exercise the reconciliation logic headlessly, no more.

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
    this.id = "";
    this.value = "";
    this.disabled = false;
    this.placeholder = "";
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
  set className(v) {
    this._classes = new Set(String(v || "").split(/\s+/).filter(Boolean));
  }
  get className() { return Array.from(this._classes).join(" "); }
  set textContent(v) { this._text = String(v == null ? "" : v); this.childNodes = []; }
  get textContent() {
    if (this.childNodes.length) {
      return this.childNodes.map((c) => c.textContent).join("");
    }
    return this._text;
  }
  // Only "" is ever assigned in app.js (to clear a container).
  set innerHTML(_v) { this.childNodes = []; this._text = ""; }
  get innerHTML() { return ""; }
  get children() {
    return this.childNodes.filter((c) => c && c.nodeType !== 3);
  }
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
    for (const n of nodes) {
      this.appendChild(typeof n === "string" ? makeText(n) : n);
    }
  }
  insertBefore(node, ref) {
    if (node && node.tagName === "#FRAGMENT") {
      for (const c of node.childNodes.slice()) this.insertBefore(c, ref);
      node.childNodes = [];
      return node;
    }
    if (node.parentNode) node.parentNode._detach(node);
    node.parentNode = this;
    if (ref == null) { this.childNodes.push(node); return node; }
    const idx = this.childNodes.indexOf(ref);
    if (idx < 0) this.childNodes.push(node);
    else this.childNodes.splice(idx, 0, node);
    return node;
  }
  removeChild(node) { this._detach(node); return node; }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  dispatch(type) {
    for (const fn of (this._listeners[type] || []).slice()) {
      fn({ preventDefault() {} });
    }
  }
  closest() { return null; }
  focus() {}
  scrollIntoView() {}
}

function makeText(text) {
  const n = new FakeNode("#text");
  n._text = String(text == null ? "" : text);
  return n;
}

const _elementsById = {};
// Report renderers (reused by the structured assistant renderers) test nodes
// with `x instanceof Node`. Every node the app builds is a FakeNode, so map the
// global `Node` to FakeNode; strings stay non-instances, matching the browser.
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

// DFS the fake tree collecting element nodes carrying CSS class `cls`.
function findAll(node, cls, acc = []) {
  if (!node || !node.childNodes) return acc;
  for (const c of node.childNodes) {
    if (c.classList && c.classList.contains(cls)) acc.push(c);
    findAll(c, cls, acc);
  }
  return acc;
}
function findOne(node, cls) { return findAll(node, cls)[0] || null; }

// The ordered list of (timestamp, stepKey) for the conversation bubbles in a
// container — headers (no __convIdx) are skipped, matching the spec's notion
// of "rendered record order".
function describeBubbles(container) {
  return container.children
    .filter((c) => c.__convIdx !== undefined)
    .map((c) => ({ ts: c.__convTs, step: c.__convStepKey, idx: c.__convIdx }));
}

// Build a record in the REAL daemon shape: the authoritative `step_type` lives
// at the record *envelope* (daemon-injected from the jsonl file-name
// convention by `parse_step_type_from_step_id`), NOT inside `message` — real
// daemon `message` payloads carry only `{role, content, timestamp}`. Tests must
// exercise this shape, not a faked inner `message.step_type`, or they pass
// while the real product is broken (the bug this group fixes).
const asstRecord = (content, ts, stepId, stepType) => ({
  step_id: stepId,
  step_type: stepType,
  message: { role: "assistant", content, timestamp: ts },
});

// -- renderConversation: full-after-append consistency ----------------------
check("renderConversation: incremental append matches a one-shot full render", () => {
  const finalRecords = [
    asstRecord("A1", 1, "s1", "discovery"),
    asstRecord("A2", 2, "s1", "discovery"),
    asstRecord("A3", 3, "s2", "analyze"),
    asstRecord("A4", 4, "s2", "analyze"),
  ];

  const full = document.createElement("div");
  app.renderConversation(full, finalRecords, false);

  const incr = document.createElement("div");
  app.renderConversation(incr, finalRecords.slice(0, 2), false);
  app.renderConversation(incr, finalRecords.slice(0, 3), true);
  app.renderConversation(incr, finalRecords, true);

  assert.deepEqual(describeBubbles(incr), describeBubbles(full));
  // The bubble bodies also line up A1..A4 in order (read the .conv-bubble body,
  // not the whole row which also carries the role/time head).
  const texts = (c) => c.children
    .filter((x) => x.__convIdx !== undefined)
    .map((x) => { const b = findOne(x, "conv-bubble"); return b ? b.textContent : ""; });
  assert.deepEqual(texts(incr), ["A1", "A2", "A3", "A4"]);
  assert.deepEqual(texts(incr), texts(full));
});

// -- renderConversation: out-of-order insertion -----------------------------
check("renderConversation: a late earlier-ts record inserts into its slot, not the tail", () => {
  const container = document.createElement("div");
  // Two records arrive in ts order 1, 3.
  app.renderConversation(container, [
    asstRecord("first", 1, "s1", "discovery"),
    asstRecord("third", 3, "s1", "discovery"),
  ], false);
  // A third record arrives late carrying ts=2 (between the two on screen).
  app.renderConversation(container, [
    asstRecord("first", 1, "s1", "discovery"),
    asstRecord("third", 3, "s1", "discovery"),
    asstRecord("second", 2, "s2", "analyze"),
  ], true);

  const order = describeBubbles(container).map((b) => b.ts);
  assert.deepEqual(order, [...order].sort((a, b) => a - b),
    "bubbles must be ordered by ascending timestamp");
  const texts = container.children
    .filter((c) => c.__convIdx !== undefined)
    .map((c) => { const b = findOne(c, "conv-bubble"); return b ? b.textContent : ""; });
  // "second" (ts=2) lands between "first" (ts=1) and "third" (ts=3), not at tail.
  assert.deepEqual(texts, ["first", "second", "third"]);
});

// -- renderConversation: cross-role/step chronological order ----------------
check("renderConversation: user reply between two assistant turns keeps ts order", () => {
  // A1 (discovery, ts1) → U1 (discovery_continue, ts2) → A2 (discovery, ts3):
  // even though U1 maps to a different step key, it stays between A1 and A2.
  const container = document.createElement("div");
  app.renderConversation(container, [
    asstRecord("A1", 1, "discovery", "discovery"),
    { step_id: "discovery_continue", step_type: "discovery_continue",
      message: { role: "user", content: "U1", timestamp: 2 } },
    asstRecord("A2", 3, "discovery", "discovery"),
  ], false);
  // Read the bodies in rendered order: U1 must sit between A1 and A2 even
  // though it maps to a different step key.
  const bodies = container.children
    .filter((c) => c.__convIdx !== undefined)
    .map((c) => { const b = findOne(c, "conv-bubble"); return b ? b.textContent : c.textContent; });
  assert.ok(bodies[0].includes("A1"));
  assert.ok(bodies[2].includes("A2"));
  const order = describeBubbles(container).map((b) => b.ts);
  assert.deepEqual(order, [...order].sort((a, b) => a - b));
});

// -- renderConversation: step headers are separators, not reorderers --------
check("renderConversation: a step header is inserted at each step boundary", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    asstRecord("A1", 1, "s1", "discovery"),
    asstRecord("A2", 2, "s1", "discovery"),
    asstRecord("A3", 3, "s2", "analyze"),
  ], false);
  const headers = container.children.filter(
    (c) => c.classList && c.classList.contains("history-step-header"));
  // Two distinct step keys → two header separators (s1 then s2).
  assert.equal(headers.length, 2);
  // The bubbles themselves stay in ascending ts order regardless of headers.
  const ts = describeBubbles(container).map((b) => b.ts);
  assert.deepEqual(ts, [...ts].sort((a, b) => a - b));
  assert.equal(describeBubbles(container).length, 3);
});

// -- renderConversation: append never collapses an expanded fold ------------
// Uses a non-assistant ("log"/other) record: its long body still folds via
// makeFoldable. (An assistant no-result turn renders its thinking inline and is
// never folded, per the message paradigm, so it is not the vehicle here.)
const logRecord = (content, ts, stepId, stepType) => ({
  step_id: stepId,
  step_type: stepType,
  message: { role: "log", content, timestamp: ts },
});
check("renderConversation: append does not re-collapse a user-expanded fold", () => {
  const container = document.createElement("div");
  const longBody = "x".repeat(2500); // exceeds FOLD_THRESHOLD → makeFoldable
  app.renderConversation(container, [logRecord(longBody, 1, "s1", "discovery")], false);

  const fold = findOne(container, "foldable");
  assert.ok(fold, "expected a foldable wrapper for the long body");
  assert.equal(fold.classList.contains("folded"), true);
  // The reader expands it.
  const toggle = findOne(fold, "fold-toggle");
  assert.ok(toggle, "expected a fold-toggle button");
  toggle.dispatch("click");
  assert.equal(fold.classList.contains("expanded"), true);
  assert.equal(fold.classList.contains("folded"), false);

  // A new record streams in (append fast-path).
  app.renderConversation(container, [
    logRecord(longBody, 1, "s1", "discovery"),
    logRecord("new turn", 2, "s1", "discovery"),
  ], true);

  // The SAME fold node must still be expanded — append must not rebuild it.
  assert.equal(fold.classList.contains("expanded"), true,
    "an append must not re-collapse the reader's expanded fold");
  assert.equal(fold.classList.contains("folded"), false);
  assert.equal(describeBubbles(container).length, 2);
});

// -- renderConversation: a malformed record cannot stall the stream ---------
check("renderConversation: a record that throws degrades to a placeholder and never freezes", () => {
  const container = document.createElement("div");
  // A record whose `message` getter throws during normalize/render. The stream
  // must keep flowing: a placeholder bubble takes its slot and subsequent
  // appends are not blocked.
  const boom = {};
  Object.defineProperty(boom, "message", {
    enumerable: true,
    get() { throw new Error("boom"); },
  });
  app.renderConversation(container, [
    asstRecord("ok1", 1, "s1", "discovery"),
    boom,
    asstRecord("ok3", 3, "s1", "discovery"),
  ], false);
  // All three slots are present (the bad one as a placeholder).
  assert.equal(describeBubbles(container).length, 3);
  const errBubble = findOne(container, "role-error");
  assert.ok(errBubble, "expected a placeholder bubble for the failed record");

  // A further append still lands — the cursor advanced past the bad record.
  app.renderConversation(container, [
    asstRecord("ok1", 1, "s1", "discovery"),
    boom,
    asstRecord("ok3", 3, "s1", "discovery"),
    asstRecord("ok4", 4, "s1", "discovery"),
  ], true);
  assert.equal(describeBubbles(container).length, 4);
});

// -- renderConversation: a shorter array falls back to a clean rebuild ------
check("renderConversation: a shorter records array rebuilds rather than stalling", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    asstRecord("A1", 1, "s1", "discovery"),
    asstRecord("A2", 2, "s1", "discovery"),
    asstRecord("A3", 3, "s1", "discovery"),
  ], false);
  assert.equal(describeBubbles(container).length, 3);
  // A replacement (shorter) snapshot arrives flagged append — must not be a
  // no-op; the view rebuilds to the new, shorter content.
  app.renderConversation(container, [asstRecord("only", 5, "s9", "commit")], true);
  const d = describeBubbles(container);
  assert.equal(d.length, 1);
  const body = findOne(container, "conv-bubble");
  assert.ok(body && body.textContent.includes("only"));
});

// -- reconcileReplyTarget (pure): chip-bar selection survival / reset -------
check("reconcileReplyTarget keeps a still-present selection", () => {
  const entries = [
    { id: "call:c1", synthetic: false },
    { id: "interjection:new", synthetic: true },
  ];
  assert.equal(app.reconcileReplyTarget(entries, "call:c1"), "call:c1");
});
check("reconcileReplyTarget re-homes onto the first real call when selection vanished", () => {
  const entries = [
    { id: "interjection:new", synthetic: true },
    { id: "call:c1", synthetic: false },
  ];
  // Prefer the real pending call over the synthetic interjection.
  assert.equal(app.reconcileReplyTarget(entries, "call:gone"), "call:c1");
});
check("reconcileReplyTarget falls back to the first entry when no real call exists", () => {
  const entries = [{ id: "interjection:new", synthetic: true }];
  assert.equal(app.reconcileReplyTarget(entries, "call:gone"), "interjection:new");
});
check("reconcileReplyTarget resets to null when the chip bar is empty", () => {
  assert.equal(app.reconcileReplyTarget([], "call:c1"), null);
  assert.equal(app.reconcileReplyTarget(null, "call:c1"), null);
});

// -- renderInterventions (DOM): chip bar tracks pending_calls --------------
// Drives the real renderInterventions against the DOM stub. The sequence
// add → shrink → empty → re-add exercises every chip-refresh acceptance
// criterion in one consistent timeline (module state carries over between
// calls exactly as it does in the browser).
check("renderInterventions: chips appear, disappear, and the reply box resets", () => {
  const region = document.getElementById("flow-interventions");
  const submit = document.getElementById("flow-reply-submit");
  const flow = (calls) => ({ status: "running", pending_calls: calls });

  // Two pending calls → two chips, reply box armed (a target exists).
  app.renderInterventions(flow([
    { call_id: "c1", kind: "call", prompt: "approve?" },
    { call_id: "c2", kind: "cli_confirm", prompt: "press 1", options: ["1"] },
  ]));
  assert.equal(region.children.length, 2, "two pending calls → two chips");
  assert.equal(submit.disabled, false, "send enabled when a target exists");

  // Backend stops reporting c2 → its chip disappears immediately.
  app.renderInterventions(flow([
    { call_id: "c1", kind: "call", prompt: "approve?" },
  ]));
  assert.equal(region.children.length, 1, "withdrawn call loses its chip");
  assert.equal(submit.disabled, false);

  // All calls cleared → no chips, reply box disarms (target reset to null).
  app.renderInterventions(flow([]));
  assert.equal(region.children.length, 0, "no pending calls → empty chip bar");
  assert.equal(submit.disabled, true, "send disabled once the bar is empty");

  // A brand-new call arrives → chip appears and the box re-arms.
  app.renderInterventions(flow([
    { call_id: "c9", kind: "call", prompt: "continue?" },
  ]));
  assert.equal(region.children.length, 1, "new call surfaces a chip");
  assert.equal(submit.disabled, false);
});

// -- mergeSnapshotWithLiveAppends: dedup snapshot vs in-flight appends ------
check("mergeSnapshotWithLiveAppends appends only records absent from the snapshot", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const r3 = asstRecord("A3", 3, "s1", "discovery");
  // Snapshot already contains r1, r2; the live array had r2 (dup) and r3 (new).
  const merged = app.mergeSnapshotWithLiveAppends([r1, r2], [r2, r3]);
  assert.equal(merged.length, 3);
  assert.equal(app.recordKey(merged[2]), app.recordKey(r3));
});
check("mergeSnapshotWithLiveAppends returns the snapshot unchanged when no live appends", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const snap = [r1];
  assert.equal(app.mergeSnapshotWithLiveAppends(snap, []), snap);
});

// ---------------------------------------------------------------------------
// Assistant two-layer progressive disclosure (DOM)
// ---------------------------------------------------------------------------
//
// Drive the real renderConversationRecord against the DOM stub for assistant
// turns of several step types. The assistant side is TWO layers (the user side
// keeps three): Layer 1 = the narrative + clean structured fields (both visible
// by default); Layer 2 = a single "查看原始" fold (button always visible, body
// folded by default) holding the turn's original record (raw NDJSON, or the
// unrendered content literal when no raw payload exists). There is no assistant
// "展开全部" wrapper and no "process-toggle". Parse failure must degrade to the
// generic text path without losing the message.

// Build a normalized assistant record (optionally with a raw NDJSON payload)
// in the real daemon shape: authoritative `step_type` at the envelope, inner
// `message` carrying only chat fields.
const asstNorm = (content, stepType, rawNdjson) => app.normalizeRecord({
  step_id: stepType,
  step_type: stepType,
  message: {
    role: "assistant",
    content,
    timestamp: 1,
    raw_ndjson: rawNdjson != null ? rawNdjson : null,
  },
});

check("assistant analyze turn renders structured fields, not a JSON blob", () => {
  const content =
    "Looking at the engine.\n```json\n" +
    JSON.stringify({
      task_type: "feature",
      complexity: "medium",
      scope: "src/engine",
      reasoning: "Touches the state machine.",
      relevant_specs: ["base:Directory Structure"],
    }) +
    "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "analyze"));

  const result = findOne(row, "assistant-result");
  assert.ok(result, "expected a Layer-1 structured result wrapper");
  const bar = findOne(result, "step-report__status-bar");
  assert.ok(bar, "expected a status bar in the structured render");
  assert.ok(bar.textContent.includes("feature"));
  assert.ok(result.textContent.includes("src/engine"));
  assert.ok(result.textContent.includes("Touches the state machine."));
  // The narrative outside the JSON is still shown at the top.
  assert.ok(result.textContent.includes("Looking at the engine."));
  // The default (Layer-1) view must NOT dump the raw JSON keys as a code block.
  assert.equal(result.textContent.includes('"task_type"'), false);
});

check("assistant commit turn renders the commit report fields", () => {
  const content = JSON.stringify({
    committed: true,
    commit_hash: "abcdef1234567890",
    commit_message: "feat: add the thing",
  });
  const row = app.renderConversationRecord(asstNorm(content, "commit"));
  const result = findOne(row, "assistant-result");
  assert.ok(result, "expected a structured result for commit");
  // Short hash (first 7 chars) and the commit message body are surfaced.
  assert.ok(result.textContent.includes("abcdef1"));
  assert.ok(result.textContent.includes("feat: add the thing"));
});

check("assistant plan_tasks turn reuses the plan renderer (task groups)", () => {
  const content = "```json\n" + JSON.stringify({
    task_groups: [
      { group_id: "G1", name: "core", tasks: [{ estimated_loc: 40 }], depends_on: [] },
      { group_id: "G2", name: "tests", tasks: [{ estimated_loc: 20 }], depends_on: ["G1"] },
    ],
  }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "plan_tasks"));
  const result = findOne(row, "assistant-result");
  assert.ok(result, "expected a structured result for plan_tasks");
  assert.ok(result.textContent.includes("G1"));
  assert.ok(result.textContent.includes("G2"));
  assert.ok(result.textContent.includes("core"));
});

check("assistant turn falls back to text when the body has no JSON", () => {
  const content = "I inspected the repo but produced only this prose summary.";
  const row = app.renderConversationRecord(asstNorm(content, "analyze"));
  // No structured wrapper: the generic path took over.
  assert.equal(findOne(row, "assistant-result"), null);
  // No-result turn: no single 查看原始 fold is added (thinking shown inline).
  assert.equal(findOne(row, "raw-toggle"), null);
  // The assistant text is still visible — nothing is lost.
  const bubble = findOne(row, "conv-bubble");
  assert.ok(bubble && bubble.textContent.includes("prose summary"));
});

check("assistant turn falls back to text when the JSON is malformed", () => {
  // A broken fence that neither strict parse nor the trailing-comma repair can
  // recover — the renderer must not throw and must keep the text visible.
  const content = "Result follows:\n```json\n{ this is : not, valid json ]\n```";
  const row = app.renderConversationRecord(asstNorm(content, "verify_spec"));
  assert.equal(findOne(row, "assistant-result"), null);
  const bubble = findOne(row, "conv-bubble");
  assert.ok(bubble && bubble.textContent.includes("Result follows:"));
  assert.ok(bubble.textContent.includes("not, valid json"));
});

check("assistant two layers: structured default + single 查看原始 raw fold (ndjson)", () => {
  const ndjson = '{"raw_marker":"NDJSON_PAYLOAD_TOKEN"}';
  const content = "```json\n" + JSON.stringify({
    task_type: "bugfix",
    complexity: "small",
    scope: "src/x",
    reasoning: "tiny",
  }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "analyze", ndjson));

  // Layer 1: structured result is the default visible surface.
  assert.ok(findOne(row, "assistant-result"), "Layer 1 structured result present");
  // No assistant "展开全部" wrapper exists anymore — the assistant side is two
  // layers, not three.
  assert.equal(findOne(row, "process-toggle"), null,
    "assistant side must have no 展开全部 process toggle");

  // Layer 2: a single "查看原始" toggle BUTTON is visible by default (single-layer
  // fold), but its raw body is hidden until clicked.
  const rawToggle = findOne(row, "raw-toggle");
  assert.ok(rawToggle, "single 查看原始 toggle button is visible by default");
  const rawPre = findOne(row, "raw-json");
  assert.equal(rawPre.classList.contains("hidden"), true, "raw body hidden by default");
  rawToggle.dispatch("click");
  assert.equal(rawPre.classList.contains("hidden"), false, "raw expands on click");
  assert.ok(rawPre.textContent.includes("NDJSON_PAYLOAD_TOKEN"),
    "the raw fold shows the original NDJSON payload");
});

check("assistant 查看原始 fold falls back to the content literal when no raw payload", () => {
  // No raw_ndjson / raw_json on the record → the assistant raw fold must fall
  // back to the unrendered content literal so the original record stays reachable.
  const content = "```json\n" + JSON.stringify({
    task_type: "feature",
    complexity: "medium",
    scope: "src/y",
    reasoning: "RAW_FALLBACK_TOKEN in the body",
  }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "analyze")); // no ndjson
  assert.ok(findOne(row, "assistant-result"), "structured result present");
  const rawToggle = findOne(row, "raw-toggle");
  assert.ok(rawToggle, "single 查看原始 toggle present even without a raw payload");
  const rawPre = findOne(row, "raw-json");
  assert.equal(rawPre.classList.contains("hidden"), true, "raw body hidden by default");
  rawToggle.dispatch("click");
  assert.ok(rawPre.textContent.includes("RAW_FALLBACK_TOKEN"),
    "the raw fold falls back to the unrendered content literal");
});

check("assistant structured renderer that throws is caught and degrades", () => {
  // Register a renderer that throws for a synthetic step type; renderAssistantBubble
  // must swallow it and fall back to the generic text path.
  const fakeStep = "__throwing_" + Math.random().toString(36).slice(2);
  app.registerAssistantRenderer(fakeStep, () => { throw new Error("kaboom"); });
  const row = app.renderConversationRecord(asstNorm("still visible body", fakeStep));
  delete app.STEP_ASSISTANT_RENDERERS[fakeStep];
  assert.equal(findOne(row, "assistant-result"), null);
  const bubble = findOne(row, "conv-bubble");
  assert.ok(bubble && bubble.textContent.includes("still visible body"));
});

// -- assistant no-result turn: thinking shown inline, never folded ----------
// Per the message paradigm, an assistant turn with NO structured result keeps
// its thinking process shown in full — it MUST NOT be folded/contracted and
// MUST NOT carry a default-visible 展开全部 / 查看原始 control.
check("assistant no-result turn shows thinking inline, not folded, no toggles", () => {
  const longProse = "Reasoning step. ".repeat(400); // > FOLD_THRESHOLD
  const row = app.renderConversationRecord(asstNorm(longProse, "analyze"));
  // No structured result, and the body did not parse to JSON.
  assert.equal(findOne(row, "assistant-result"), null);
  // The thinking is rendered inline (not behind a fold).
  const inline = findOne(row, "assistant-process-inline");
  assert.ok(inline, "expected an inline process wrapper");
  assert.equal(findOne(row, "foldable"), null,
    "a no-result assistant turn must not collapse its thinking into a fold");
  assert.ok(inline.textContent.includes("Reasoning step."),
    "the full thinking process is visible by default");
  // No fold controls in the no-result inline view: no assistant "展开全部" wrapper
  // (it no longer exists), and no single 查看原始 fold either (thinking is inline).
  assert.equal(findOne(row, "process-toggle"), null,
    "no 展开全部 toggle exists on the assistant side at all");
  assert.equal(findOne(row, "raw-toggle"), null,
    "查看原始 must not show for a no-result turn — thinking is shown inline");
});

// ---------------------------------------------------------------------------
// Message-paradigm §2: in-progress (2a/2c) vs. final (2b) assistant turns
// ---------------------------------------------------------------------------
//
// These assertions pin the result-identification contract: a turn renders a
// structured result (Layer 1) ONLY when its JSON carries a real result field for
// the step. When it does, the assistant side shows the narrative + result as the
// visible default and folds only the turn's original record behind the single
// "查看原始" entry. A tool-call JSON (Bash/Edit/Grep/… args), including 2+ such
// segments, is thinking process — it stays inline, never folds, and never gets a
// 查看原始 fold. The previous renderer mistook any parseable JSON for a result
// (analyze/version_analyze always draw a status bar), so a tool-call-only turn
// was wrongly collapsed; these tests guard the fix. All assertions are
// DOM-structural (presence of assistant-result / raw-toggle /
// assistant-process-inline), not text guesses.

// 2a/2c: a turn whose only JSON is a single tool call is NOT an analyze result
// (no task_type/complexity/… key) — even though renderAnalyzeReport would draw
// a status bar from any dict. It must stay inline, not fold.
check("2c: single tool-call JSON (no result field) stays inline, no fold, no toggle", () => {
  const content = "Let me list files.\n```json\n" +
    JSON.stringify({ command: "ls -la", description: "list files" }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "analyze"));
  assert.equal(findOne(row, "assistant-result"), null,
    "a tool-call-only turn must not render a structured result");
  assert.equal(findOne(row, "raw-toggle"), null,
    "no 查看原始 fold for a tool-call-only turn — thinking is shown inline");
  const inline = findOne(row, "assistant-process-inline");
  assert.ok(inline, "thinking is shown inline");
  assert.ok(inline.textContent.includes("Let me list files."),
    "the narrative is preserved");
  assert.ok(inline.textContent.includes("ls -la"),
    "the tool-call JSON content is not lost");
});

// 2c: two or more tool-call JSON segments in one turn are all thinking process.
check("2c: 2+ tool-call JSON segments stay inline, never get a 查看原始 fold", () => {
  const content =
    "Checking.\n```json\n" + JSON.stringify({ command: "ls" }) + "\n```\n" +
    "Now editing.\n```json\n" +
    JSON.stringify({ file_path: "x.py", old_string: "a", new_string: "b" }) +
    "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "implement"));
  assert.equal(findOne(row, "assistant-result"), null,
    "neither tool-call JSON is an implement result");
  assert.equal(findOne(row, "raw-toggle"), null,
    "2+ tool-call JSONs with no result stay inline — no 查看原始 fold");
  const inline = findOne(row, "assistant-process-inline");
  assert.ok(inline, "thinking shown inline");
  assert.ok(inline.textContent.includes("Checking."));
  assert.ok(inline.textContent.includes("Now editing."),
    "both thinking segments are visible by default");
});

// 2b: a turn carrying a real result field renders the structured result by
// default and folds only the original record behind the single "查看原始" entry.
check("2b: real result JSON renders structured result + single 查看原始 fold", () => {
  const content = "```json\n" + JSON.stringify({
    completion_status: "complete",
    files_changed: ["src/x.py"],
    summary: "Did the thing.",
  }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "implement"));
  const result = findOne(row, "assistant-result");
  assert.ok(result, "result JSON → structured result is the default view");
  assert.ok(result.textContent.includes("Did the thing."));
  assert.equal(findOne(row, "assistant-process-inline"), null,
    "a result turn does not also render the inline thinking path");
  // No assistant "展开全部" wrapper; the single 查看原始 toggle button is visible but
  // its raw body is folded by default.
  assert.equal(findOne(row, "process-toggle"), null,
    "assistant side has no 展开全部 process toggle");
  const rawToggle = findOne(row, "raw-toggle");
  assert.ok(rawToggle, "single 查看原始 toggle button present for a result turn");
  const rawPre = findOne(row, "raw-json");
  assert.equal(rawPre.classList.contains("hidden"), true,
    "the raw body is folded by default");
});

// 2b mixed: tool-call JSON followed by a real result JSON — the result is the
// chosen Layer-1 surface; the intermediate tool-call JSON does not leak into the
// clean default view but stays reachable in the single 查看原始 fold (which falls
// back to the full unrendered content body when no raw payload exists).
check("2b: tool-call JSON before a result JSON — result shown, tool JSON in 查看原始 fold", () => {
  const content =
    "Running tests.\n```json\n" + JSON.stringify({ command: "pytest -q" }) + "\n```\n" +
    "Result:\n```json\n" + JSON.stringify({
      test_results: {
        overall_passed: true,
        phases: [{ name: "unit", passed: true }],
      },
    }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "test"));
  const result = findOne(row, "assistant-result");
  assert.ok(result, "the real test result is the structured default surface");
  assert.ok(result.textContent.includes("PASSED"));
  assert.equal(result.textContent.includes("pytest -q"), false,
    "the intermediate tool-call JSON must not leak into the clean Layer-1 view");
  // The intermediate tool-call JSON stays reachable via the single 查看原始 fold,
  // which falls back to the full unrendered content body (no raw payload here).
  const rawToggle = findOne(row, "raw-toggle");
  assert.ok(rawToggle, "single 查看原始 fold present");
  rawToggle.dispatch("click");
  const rawPre = findOne(row, "raw-json");
  assert.ok(rawPre.textContent.includes("pytest -q"),
    "the tool-call JSON is reachable in the original record via 查看原始");
});

// 2b narrative+result: narrative outside the JSON is surfaced alongside the
// structured result (both in the visible Layer 1); only the original record is
// behind the single 查看原始 fold.
check("2b: narrative + result JSON — both render in the default view, single 查看原始 fold", () => {
  const content = "Analyzed the engine.\n```json\n" + JSON.stringify({
    task_type: "feature",
    complexity: "medium",
    scope: "src/engine",
    reasoning: "Touches the state machine.",
  }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "analyze"));
  const result = findOne(row, "assistant-result");
  assert.ok(result, "structured analyze result present");
  assert.ok(result.textContent.includes("Analyzed the engine."),
    "the narrative is shown alongside the result in the visible Layer 1");
  assert.ok(result.textContent.includes("Touches the state machine."));
  assert.equal(findOne(row, "process-toggle"), null,
    "assistant side has no 展开全部 process toggle");
  assert.ok(findOne(row, "raw-toggle"),
    "the original record is reachable via the single 查看原始 fold");
});

// Result identification is step-scoped: a JSON whose only keys belong to a
// DIFFERENT step's result set is not this step's result.
check("result identification is scoped to the step's own result fields", () => {
  // `verified` is a verify_spec result field, not an analyze one. For an analyze
  // turn this is therefore NOT a result → stays inline.
  const content = "```json\n" + JSON.stringify({ verified: true }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "analyze"));
  assert.equal(findOne(row, "assistant-result"), null,
    "a foreign-step result field does not count as this step's result");
  assert.ok(findOne(row, "assistant-process-inline"),
    "the turn stays inline");
  // The same JSON IS a verify_spec result for a verify_spec turn → folds.
  const row2 = app.renderConversationRecord(asstNorm(content, "verify_spec"));
  assert.ok(findOne(row2, "assistant-result"),
    "verified is a verify_spec result field → structured result");
});

// Discovery's dedicated renderer follows the same contract.
check("2c: discovery turn with only a tool-call JSON stays inline", () => {
  const content = "Reading the spec.\n```json\n" +
    JSON.stringify({ file_path: "se3/specs/base/spec.md" }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "discovery"));
  assert.equal(findOne(row, "assistant-result"), null,
    "a discovery tool-call turn (no content/refined/questions) is not a result");
  assert.equal(findOne(row, "raw-toggle"), null,
    "no 查看原始 fold for a tool-call-only discovery turn — thinking is inline");
  const inline = findOne(row, "assistant-process-inline");
  assert.ok(inline && inline.textContent.includes("Reading the spec."));
});

check("2b: discovery result JSON renders structured fields + single 查看原始 fold", () => {
  const content = "Thinking about scope.\n```json\n" + JSON.stringify({
    content: "Here is what I understand.",
    refined_description: "Fix the chat renderer.",
    questions: ["Which browser?", "Headless?"],
  }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "discovery"));
  const result = findOne(row, "assistant-result");
  assert.ok(result, "discovery result → structured result");
  assert.ok(result.textContent.includes("Here is what I understand."));
  assert.ok(findOne(result, "step-report--proposed-task"),
    "refined_description renders as a Proposed Task Description card");
  assert.ok(result.textContent.includes("Which browser?"));
  assert.equal(findOne(row, "process-toggle"), null,
    "assistant side has no 展开全部 process toggle");
  assert.ok(findOne(row, "raw-toggle"),
    "the original record is reachable via the single 查看原始 fold");
});

// A turn whose JSON has a result KEY but renders to nothing must not fold into
// an empty toggle — it degrades to the inline thinking path so nothing is lost.
check("2c: discovery result keys present but all empty → inline, no empty fold", () => {
  const content = "Just thinking.\n```json\n" + JSON.stringify({
    content: "",
    refined_description: "   ",
    questions: [],
  }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "discovery"));
  assert.equal(findOne(row, "assistant-result"), null,
    "empty result fields do not constitute a final result");
  assert.equal(findOne(row, "raw-toggle"), null,
    "must not add a 查看原始 fold — thinking stays inline");
  assert.ok(findOne(row, "assistant-process-inline"),
    "thinking stays inline, content preserved");
});

// -- end-to-end dispatch on the real (post-G1) envelope shape ---------------
// A group-suffixed implement record arrives from the daemon already carrying
// the clean envelope step_type "implement" (the `_G2` suffix stripped by
// parse_step_type_from_step_id). The full render path must dispatch it to the
// implement structured renderer — exactly the chain that was dead on real data
// while the unit tests faked an inner message.step_type.
check("group-suffixed implement record dispatches to the implement structured renderer", () => {
  const content = "```json\n" + JSON.stringify({
    summary: "Did the thing.",
    files_changed: ["src/x.py"],
    completion_status: "complete",
  }) + "\n```";
  const norm = app.normalizeRecord({
    step_id: "05_implement_61605e42_G2",
    step_type: "implement",
    message: { role: "assistant", content },
  });
  assert.equal(norm.stepType, "implement");
  const row = app.renderConversationRecord(norm);
  // Layer-1 structured result wrapper present → the implement renderer fired
  // (not the raw ```json``` fallback). Without the envelope-first fix this stayed
  // empty because norm.stepType was "".
  const result = findOne(row, "assistant-result");
  assert.ok(result, "expected the implement structured result wrapper");
  assert.ok(result.textContent.includes("Did the thing."));
  // The raw JSON keys are NOT dumped in the Layer-1 default view.
  assert.equal(result.textContent.includes('"completion_status"'), false);
});

check("group-suffixed implement record renders the IMPLEMENT step header", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    asstRecord("```json\n{\"summary\":\"a\"}\n```", 1, "05_implement_61605e42_G2", "implement"),
  ], false);
  const header = container.children.find(
    (c) => c.classList && c.classList.contains("history-step-header"));
  assert.ok(header, "expected a step header");
  assert.ok(header.textContent.includes("IMPLEMENT"),
    "header must be the paradigm name IMPLEMENT, not the file stem");
  assert.equal(header.textContent.includes("05_implement_61605e42_G2"), false,
    "the ugly file stem must not be the visible header");
});

check("legacy commit_summary record degrades gracefully (no renderer, fallback header)", () => {
  // The daemon returns the original stem "commit_summary" for this legacy name;
  // there is no registered renderer and no header title. The render path must
  // not crash and must keep the content reachable.
  const container = document.createElement("div");
  app.renderConversation(container, [
    asstRecord("just a commit summary body", 1, "commit_summary", "commit_summary"),
  ], false);
  const header = container.children.find(
    (c) => c.classList && c.classList.contains("history-step-header"));
  assert.ok(header, "expected a step header even for the unknown type");
  // No paradigm title for an unknown type → falls back to the step key/stem.
  assert.ok(header.textContent.includes("commit_summary"));
  // Content is still rendered (no structured result, generic path keeps text).
  const bubbles = container.children.filter((c) => c.__convIdx !== undefined);
  assert.equal(bubbles.length, 1);
  assert.equal(findOne(bubbles[0], "assistant-result"), null);
  assert.ok(bubbles[0].textContent.includes("just a commit summary body"));
});

// -- step section headers use paradigm names -------------------------------
// stepHeaderLabel maps a step_type to the paradigm heading; rebuildStepHeaders
// renders those names (not the raw step_type literal).
check("stepHeaderLabel maps known step types to paradigm names", () => {
  assert.equal(app.stepHeaderLabel("discovery"), "DISCOVERY");
  assert.equal(app.stepHeaderLabel("self_check"), "SELF CHECK");
  assert.equal(app.stepHeaderLabel("update_spec"), "UPDATE SPEC");
  assert.equal(app.stepHeaderLabel("version_analyze"), "VERSION ANALYZE");
  assert.equal(app.stepHeaderLabel("summarize"), "SUMMARY");
});
check("stepHeaderLabel falls back to the original key for unknown steps", () => {
  // Unknown step types keep the original key so grouping / ordering is intact.
  assert.equal(app.stepHeaderLabel("mystery_step", "mystery_step"), "mystery_step");
  assert.equal(app.stepHeaderLabel("", "raw_key"), "raw_key");
});
check("rebuildStepHeaders renders paradigm step names, not raw step_type", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    asstRecord("A1", 1, "01_discovery", "discovery"),
    asstRecord("A2", 2, "06_self_check", "self_check"),
  ], false);
  const headers = findAll(container, "history-step-title");
  const titles = headers.map((h) => h.textContent);
  assert.deepEqual(titles, ["DISCOVERY", "SELF CHECK"],
    "step headers must use the paradigm names, not the raw step_type");
});
check("rebuildStepHeaders falls back to the step key for an unknown step type", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    asstRecord("A1", 1, "99_mystery", "mystery_step"),
  ], false);
  const header = findOne(container, "history-step-title");
  // Unknown step → original label (step_type here) rather than crashing or
  // dropping the header.
  assert.ok(header && header.textContent.length > 0,
    "an unknown step still gets a header (fallback to its original label)");
  assert.equal(header.textContent, "mystery_step");
});

// ---------------------------------------------------------------------------
// User-turn three-layer progressive disclosure (DOM)
// ---------------------------------------------------------------------------
//
// A marker-bearing `user` record mirrors the assistant side's three layers:
// Layer 1 = the user's literal input bubble (default-expanded, no framework
// boilerplate); Layer 2 = the "展开全部" toggle revealing 模板前缀 / 框架后缀;
// Layer 3 = the "查看原始" raw NDJSON. An empty user-content section (legacy
// two-segment layout) degrades to a single collapsed system-prompt chip, and a
// marker-free body falls all the way back to the whole-message chip.

// Build a normalized `user` record whose body carries the sentinel markers.
// `content === null` emits the legacy two-segment layout (TEMPLATE_PREFIX_END +
// USER_CONTENT_BEGIN, no USER_CONTENT_END) where the post-BEGIN tail is the
// framework `suffix`.
const userMarkerNorm = (prefix, content, suffix, stepType, rawNdjson) => {
  const TPE = app.TEMPLATE_PREFIX_END;
  const UCB = app.USER_CONTENT_BEGIN;
  const UCE = app.USER_CONTENT_END;
  const body = content == null
    ? prefix + "\n" + TPE + "\n" + UCB + "\n" + (suffix || "")
    : prefix + "\n" + TPE + "\n" + UCB + "\n" + content + "\n" + UCE + "\n" + (suffix || "");
  return app.normalizeRecord({
    step_id: stepType,
    step_type: stepType,
    message: {
      role: "user",
      content: body,
      timestamp: 1,
      raw_ndjson: rawNdjson != null ? rawNdjson : null,
    },
  });
};

check("user three layers: literal bubble default, 展开全部 prefix/suffix, 查看原始 ndjson", () => {
  const ndjson = '{"raw_marker":"USER_NDJSON_TOKEN"}';
  const norm = userMarkerNorm(
    "You are an expert engineer.\n## Project Context\nlots of boilerplate",
    "Please add retry logic to the daemon.",
    "## Available Specs\nspec list\nREAD-ONLY CONSTRAINT",
    "discovery", ndjson);
  const row = app.renderConversationRecord(norm);

  // Layer 1: the default-expanded bubble surfaces ONLY the user's literal input.
  const bubble = findOne(row, "user-content-bubble");
  assert.ok(bubble, "Layer 1 user-content bubble present");
  assert.ok(bubble.textContent.includes("Please add retry logic to the daemon."));
  // No framework boilerplate leaks into the default view — the 展开全部 body is
  // built lazily, so prefix/suffix are absent from the DOM until expanded.
  assert.equal(row.textContent.includes("Project Context"), false,
    "template prefix must not appear in the default view");
  assert.equal(row.textContent.includes("Available Specs"), false,
    "framework suffix must not appear in the default view");

  // Layer 3 ("查看原始") MUST NOT be present in the default Layer-1 view — it is
  // nested inside the Layer-2 expand area, built lazily on first expand.
  assert.equal(findOne(row, "raw-toggle"), null,
    "查看原始 must not show at the default Layer-1 user view");

  // Layer 2: the "展开全部" toggle reveals 模板前缀 / 框架后缀, folded by default.
  const wrap = findOne(row, "user-prompt-toggle-wrap");
  assert.ok(wrap, "Layer 2 展开全部 toggle present");
  const toggle = findOne(wrap, "process-toggle");
  assert.ok(toggle.textContent.includes("展开全部"));
  const full = findOne(wrap, "process-full");
  assert.equal(full.classList.contains("hidden"), true, "prefix/suffix folded by default");
  toggle.dispatch("click");
  assert.equal(full.classList.contains("hidden"), false, "expands on click");
  assert.ok(full.textContent.includes("模板前缀"), "template-prefix subsection labeled");
  assert.ok(full.textContent.includes("框架后缀"), "framework-suffix subsection labeled");
  assert.ok(full.textContent.includes("Project Context"), "prefix body now visible");
  assert.ok(full.textContent.includes("Available Specs"), "suffix body now visible");

  // Layer 3: the "查看原始" toggle now appears nested INSIDE the expanded Layer-2
  // area (not at the row level), and reveals the raw NDJSON.
  const rawToggle = findOne(full, "raw-toggle");
  assert.ok(rawToggle, "Layer 3 raw toggle nested inside the 展开全部 area");
  const rawPre = findOne(full, "raw-json");
  assert.equal(rawPre.classList.contains("hidden"), true, "raw hidden by default");
  rawToggle.dispatch("click");
  assert.equal(rawPre.classList.contains("hidden"), false, "raw expands on click");
  assert.ok(rawPre.textContent.includes("USER_NDJSON_TOKEN"),
    "the raw layer shows the original NDJSON payload");
});

check("user two-segment marker degrades to a single collapsed chip (no bubble)", () => {
  const norm = userMarkerNorm(
    "You are an expert engineer.",
    null, // legacy two-segment: no USER_CONTENT_END
    "## Task\nframework tail and project context",
    "analyze");
  const row = app.renderConversationRecord(norm);
  // No user literal to surface → no Layer-1 bubble and no 展开全部 toggle.
  assert.equal(findOne(row, "user-content-bubble"), null);
  assert.equal(findOne(row, "user-prompt-toggle-wrap"), null);
  // A single collapsed system-prompt chip instead.
  const wrap = findOne(row, "user-prompt-chip");
  assert.ok(wrap, "degraded system-prompt chip present");
  assert.equal(wrap.classList.contains("collapsed"), true, "collapsed by default");
  const chip = findOne(wrap, "msg-chip");
  assert.ok(chip.textContent.includes("system prompt · analyze"));
  // The framework tail must NOT leak before expansion.
  assert.equal(row.textContent.includes("framework tail"), false);
  chip.dispatch("click");
  assert.equal(wrap.classList.contains("collapsed"), false, "expands on click");
  const detail = findOne(wrap, "msg-chip-detail");
  assert.ok(detail.textContent.includes("模板前缀"));
  assert.ok(detail.textContent.includes("框架后缀"));
  assert.ok(detail.textContent.includes("framework tail"), "suffix body now visible");
});

check("user message without markers falls back to a whole-message chip", () => {
  const norm = app.normalizeRecord({
    step_id: "discovery",
    step_type: "discovery",
    message: { role: "user", content: "just a plain reply", timestamp: 1 },
  });
  const row = app.renderConversationRecord(norm);
  // Not the marker path: no user-prompt-marker class, no Layer-1 bubble.
  assert.equal(row.classList.contains("user-prompt-marker"), false);
  assert.equal(findOne(row, "user-content-bubble"), null);
  // The legacy collapsible chip carries the whole message (lazy: hidden until click).
  const chip = findOne(row, "msg-chip");
  assert.ok(chip, "whole-message chip present");
  assert.ok(chip.textContent.includes("user prompt"));
  assert.equal(row.textContent.includes("just a plain reply"), false,
    "content hidden until the chip is expanded");
  chip.dispatch("click");
  assert.ok(row.textContent.includes("just a plain reply"), "content visible after expand");
});

// ---------------------------------------------------------------------------
// Stage sectioning with the new user render path (DOM)
// ---------------------------------------------------------------------------
//
// A marker-bearing user reply produced mid-discovery must stay in strict
// timestamp order between the surrounding assistant turns, with step headers
// acting only as visual separators — never reordering records.
check("renderConversation: user marker reply interleaves by ts; step headers only separate", () => {
  const TPE = app.TEMPLATE_PREFIX_END;
  const UCB = app.USER_CONTENT_BEGIN;
  const UCE = app.USER_CONTENT_END;
  const userBody = "boiler\n" + TPE + "\n" + UCB + "\nmy answer\n" + UCE + "\ntail";
  const container = document.createElement("div");
  app.renderConversation(container, [
    asstRecord("A1", 1, "discovery", "discovery"),
    { step_id: "discovery_continue", step_type: "discovery_continue",
      message: { role: "user", content: userBody, timestamp: 2 } },
    asstRecord("A2", 3, "discovery", "discovery"),
  ], false);

  // Strict timestamp order is preserved across the role/step boundary.
  const order = describeBubbles(container).map((b) => b.ts);
  assert.deepEqual(order, [...order].sort((a, b) => a - b),
    "bubbles must be ordered by ascending timestamp");

  // The middle bubble is the new user-marker record carrying the literal input.
  const bubbles = container.children.filter((c) => c.__convIdx !== undefined);
  const mid = bubbles[1];
  assert.ok(mid.classList.contains("user-prompt-marker"), "middle record uses the marker path");
  const ucb = findOne(mid, "user-content-bubble");
  assert.ok(ucb && ucb.textContent.includes("my answer"));

  // Step headers separate discovery / discovery_continue / discovery (visual only):
  // three boundaries → three headers, and they never shuffle the bubbles.
  const headers = findAll(container, "history-step-header");
  assert.equal(headers.length, 3, "a header at each step boundary");
});

// -- renderHistoryList: refresh-in-progress feedback ------------------------
// The history list must distinguish "still refreshing" from "confirmed no
// history" so opening the view never shows a bare blank page while the
// /api/history round-trip is in flight.
check("renderHistoryList: empty + loading shows the refreshing hint, not the empty state", () => {
  app.state.historySessions = [];
  app.state.historyIndexLoading = true;
  app.renderHistoryList();
  const list = document.getElementById("history-list");
  const texts = findAll(list, "empty").map((n) => n.textContent);
  assert.ok(texts.some((t) => t.includes("正在刷新历史")),
    "loading hint must be shown");
  assert.ok(!texts.some((t) => t.includes("No history sessions reported.")),
    "empty state must NOT be shown while loading");
});

check("renderHistoryList: empty + not loading shows the original empty state", () => {
  app.state.historySessions = [];
  app.state.historyIndexLoading = false;
  app.renderHistoryList();
  const list = document.getElementById("history-list");
  const texts = findAll(list, "empty").map((n) => n.textContent);
  assert.ok(texts.some((t) => t.includes("No history sessions reported.")),
    "empty state must be shown when not loading");
  assert.ok(!texts.some((t) => t.includes("正在刷新历史")),
    "refreshing hint must NOT be shown when not loading");
});

check("renderHistoryList: non-empty + loading prepends a refresh bar above the items", () => {
  app.state.historySessions = [
    { flow_id: "f1", task_description: "task one", status: "running" },
    { flow_id: "f2", task_description: "task two", status: "completed" },
  ];
  app.state.historyIndexLoading = true;
  app.renderHistoryList();
  const list = document.getElementById("history-list");
  assert.equal(findAll(list, "history-item").length, 2, "all sessions render");
  const bar = findOne(list, "history-refreshing");
  assert.ok(bar && bar.textContent.includes("正在刷新历史"),
    "a lightweight refresh bar is prepended while loading");
});

check("renderHistoryList: non-empty + not loading renders items with no refresh bar", () => {
  app.state.historySessions = [
    { flow_id: "f1", task_description: "task one", status: "running" },
  ];
  app.state.historyIndexLoading = false;
  app.renderHistoryList();
  const list = document.getElementById("history-list");
  assert.equal(findAll(list, "history-item").length, 1, "the session renders");
  assert.equal(findOne(list, "history-refreshing"), null,
    "no refresh bar when not loading");
});

console.log(`\n${passed} checks passed.`);
