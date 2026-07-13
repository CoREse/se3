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
check("STEP_REPORT_RENDERERS covers the 13 named step types", () => {
  const expected = [
    "analyze", "plan", "implement", "test", "self_check", "verify_spec",
    "update_spec", "spec_gate", "commit", "version_analyze", "summarize",
    "discovery", "charter_freshness",
  ];
  for (const t of expected) {
    assert.equal(
      typeof app.STEP_REPORT_RENDERERS[t], "function",
      "missing renderer for " + t,
    );
  }
  // Exactly 13 — the prior 12 plus the charter_freshness report renderer
  // (PROPOSE/DESIGN are deprecated and intentionally excluded).
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
    // Scroll geometry — defaults to 0; tests assign explicit values to drive
    // the sticky-header viewport logic. `__rect` backs getBoundingClientRect.
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.__rect = null;
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
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() {
    return this.childNodes.length ? this.childNodes[this.childNodes.length - 1] : null;
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
  remove() { if (this.parentNode) this.parentNode._detach(this); }
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
  // Sticky-header geometry stubs. getBoundingClientRect returns a settable rect
  // (defaults to all-zero); scrollTo mirrors the browser's options form by
  // assigning scrollTop, so smoothScrollTo can be exercised headlessly.
  //
  // `__rect` is stored in *content space* (the layout position at scrollTop 0).
  // A real browser's rect is *screen space*: a scrolled-down ancestor pulls a
  // child's rect.top up by that ancestor's scrollTop. To make re-measuring at a
  // non-zero scroll position behave like the browser (so updateStickyHeader can
  // safely re-measure on every scroll / after a fold-expand reflow), the stub
  // subtracts the summed scrollTop of every strict ancestor from the stored
  // top/bottom. A node's OWN scrollTop is never subtracted from itself (it is
  // the reference frame), so a scroller's rect stays put while its children
  // move — exactly the relationship measureStepHeaderOffsets relies on.
  getBoundingClientRect() {
    if (!this.__rect) {
      return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
    }
    let scroll = 0;
    for (let n = this.parentNode; n; n = n.parentNode) {
      scroll += Number(n.scrollTop) || 0;
    }
    return {
      ...this.__rect,
      top: this.__rect.top - scroll,
      bottom: this.__rect.bottom - scroll,
    };
  }
  scrollTo(opts) {
    if (opts && typeof opts.top === "number") this.scrollTop = opts.top;
  }
  // Sibling helpers used by openHistorySession's Resume-bar bookkeeping.
  get nextElementSibling() {
    if (!this.parentNode) return null;
    const sibs = this.parentNode.children;
    const i = sibs.indexOf(this);
    return i >= 0 ? (sibs[i + 1] || null) : null;
  }
  after(node) {
    if (!this.parentNode) return;
    const i = this.parentNode.childNodes.indexOf(this);
    if (i < 0) { this.parentNode.appendChild(node); return; }
    if (node.parentNode) node.parentNode._detach(node);
    this.parentNode.childNodes.splice(i + 1, 0, node);
    node.parentNode = this.parentNode;
  }
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
  // Read the inline-process body (not the whole bubble) so the always-present
  // "查看原始" toggle chrome on a no-result assistant turn does not bleed into the
  // ordering comparison.
  const texts = (c) => c.children
    .filter((x) => x.__convIdx !== undefined)
    .map((x) => { const b = findOne(x, "assistant-process-inline"); return b ? b.textContent : ""; });
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
    .map((c) => { const b = findOne(c, "assistant-process-inline"); return b ? b.textContent : ""; });
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

// -- stream_progress (partial) normalization + supersede folding ------------
//
// Daemon-forwarded partial records ride the same {step_id, step_type, message}
// envelope; the inner message carries type:'stream_progress' / partial:true.
// They render live (line by line) while a step thinks, then fold away once the
// turn's final (non-partial) assistant result for the same (step_id, attempt)
// arrives.
const partialRecord = (content, ts, stepId, stepType, attempt) => ({
  step_id: stepId,
  step_type: stepType,
  message: {
    type: "stream_progress",
    role: "assistant",
    content,
    timestamp: ts,
    attempt,
    partial: true,
  },
});
const finalRecord = (content, ts, stepId, stepType, attempt) => ({
  step_id: stepId,
  step_type: stepType,
  message: { role: "assistant", content, timestamp: ts, attempt },
});

check("normalizeRecord flags stream_progress as a partial assistant record", () => {
  const norm = app.normalizeRecord(partialRecord("thinking…", 1, "s1", "discovery", 0));
  assert.equal(norm.role, "assistant");
  assert.equal(norm.partial, true);
  assert.equal(norm.content, "thinking…");
  assert.equal(norm.stepType, "discovery");
});
check("normalizeRecord also honors an explicit partial:true without the type tag", () => {
  const norm = app.normalizeRecord({
    step_id: "s1", step_type: "analyze",
    message: { role: "assistant", content: "x", partial: true },
  });
  assert.equal(norm.partial, true);
  assert.equal(norm.role, "assistant");
});
check("normalizeRecord leaves an ordinary record non-partial", () => {
  const norm = app.normalizeRecord(finalRecord("done", 2, "s1", "discovery", 0));
  assert.equal(norm.role, "assistant");
  assert.equal(norm.partial, false);
});

check("markSupersededProgress: partials with no final turn supersede nothing", () => {
  const superseded = app.markSupersededProgress([
    partialRecord("p1", 1, "s1", "discovery", 0),
    partialRecord("p2", 2, "s1", "discovery", 0),
  ]);
  assert.equal(superseded.size, 0);
});
check("markSupersededProgress: a final result supersedes its turn's partials", () => {
  const superseded = app.markSupersededProgress([
    partialRecord("p1", 1, "s1", "discovery", 0),  // idx 0
    partialRecord("p2", 2, "s1", "discovery", 0),  // idx 1
    finalRecord("result", 3, "s1", "discovery", 0), // idx 2 — terminal
  ]);
  // Both partials (0,1) are superseded; the final (2) is not.
  assert.deepEqual([...superseded].sort((a, b) => a - b), [0, 1]);
});
check("markSupersededProgress: only same (stepId, attempt) partials fold", () => {
  const superseded = app.markSupersededProgress([
    partialRecord("a-p", 1, "s1", "discovery", 0),   // idx 0 — folds
    partialRecord("b-p", 2, "s2", "analyze", 0),     // idx 1 — different step, stays
    partialRecord("a2-p", 3, "s1", "discovery", 1),  // idx 2 — different attempt, stays
    finalRecord("a-final", 4, "s1", "discovery", 0), // idx 3 — terminal for (s1,0)
  ]);
  assert.deepEqual([...superseded], [0]);
});
check("markSupersededProgress: a later round's partials stay live despite an earlier final on the same (stepId, attempt) key", () => {
  // Multi-round discovery / fix-loop implement re-run the SAME step_id with
  // retry_count reset to 0, so round 1's final and round 2's freshly-streaming
  // partials share the identical (s1, attempt=0) key. Round 2's progress must
  // remain visible until its OWN final lands — only round 1's partials fold.
  const superseded = app.markSupersededProgress([
    partialRecord("r1-p1", 1, "s1", "discovery", 0),   // idx 0 — round 1, folds
    partialRecord("r1-p2", 2, "s1", "discovery", 0),   // idx 1 — round 1, folds
    finalRecord("r1-final", 3, "s1", "discovery", 0),  // idx 2 — round 1 terminal
    partialRecord("r2-p1", 4, "s1", "discovery", 0),   // idx 3 — round 2, stays live
    partialRecord("r2-p2", 5, "s1", "discovery", 0),   // idx 4 — round 2, stays live
  ]);
  // Only round 1's partials (0,1) are superseded; round 2's (3,4) stay live.
  assert.deepEqual([...superseded].sort((a, b) => a - b), [0, 1]);
});
check("markSupersededProgress: round 2's partials fold once round 2's own final lands", () => {
  const superseded = app.markSupersededProgress([
    partialRecord("r1-p1", 1, "s1", "discovery", 0),   // idx 0 — round 1, folds
    finalRecord("r1-final", 2, "s1", "discovery", 0),  // idx 1 — round 1 terminal
    partialRecord("r2-p1", 3, "s1", "discovery", 0),   // idx 2 — round 2, folds
    partialRecord("r2-p2", 4, "s1", "discovery", 0),   // idx 3 — round 2, folds
    finalRecord("r2-final", 5, "s1", "discovery", 0),  // idx 4 — round 2 terminal
  ]);
  assert.deepEqual([...superseded].sort((a, b) => a - b), [0, 2, 3]);
});

check("renderConversation: live partials merge into ONE bubble, then fold away when the result lands", () => {
  const container = document.createElement("div");
  // First push: two streamed partial lines for the discovery turn. They belong
  // to the SAME turn (one segment) so they merge into a single accumulating
  // assistant bubble rather than one bubble each.
  app.renderConversation(container, [
    partialRecord("step 1 thinking", 1, "s1", "discovery", 0),
    partialRecord("step 1 tool use", 2, "s1", "discovery", 0),
  ], false);
  assert.equal(describeBubbles(container).length, 1,
    "both partial fragments must merge into a single accumulating bubble");
  assert.equal(findAll(container, "conv-partial").length, 1);
  // The single bubble carries BOTH fragments' text.
  const liveBody = findOne(container, "conv-bubble");
  assert.ok(liveBody && liveBody.textContent.includes("step 1 thinking"));
  assert.ok(liveBody && liveBody.textContent.includes("step 1 tool use"));

  // Append the final result — the accumulating bubble must be removed, leaving
  // only the final result bubble.
  app.renderConversation(container, [
    partialRecord("step 1 thinking", 1, "s1", "discovery", 0),
    partialRecord("step 1 tool use", 2, "s1", "discovery", 0),
    finalRecord("final answer", 3, "s1", "discovery", 0),
  ], true);
  const bubbles = describeBubbles(container);
  assert.equal(bubbles.length, 1, "only the final result bubble should remain");
  assert.equal(findAll(container, "conv-partial").length, 0);
  const body = findOne(container, "conv-bubble");
  assert.ok(body && body.textContent.includes("final answer"));
});

// -- partialSegments (pure): segment-key computation ------------------------
check("partialSegments: consecutive same-turn partials share one segment key", () => {
  const segs = app.partialSegments([
    partialRecord("p1", 1, "s1", "discovery", 0),
    partialRecord("p2", 2, "s1", "discovery", 0),
    partialRecord("p3", 3, "s1", "discovery", 0),
  ]);
  assert.equal(segs.length, 3);
  assert.ok(segs[0] != null);
  assert.equal(segs[0], segs[1]);
  assert.equal(segs[1], segs[2]);
});
check("partialSegments: two rounds split by a final get different segment keys", () => {
  // Round 1 partials + round 1 final + round 2 partials, all reusing the same
  // (s1, attempt=0) — yet the two rounds must fall in distinct segments.
  const segs = app.partialSegments([
    partialRecord("r1-p1", 1, "s1", "discovery", 0),  // idx 0 — round 1
    partialRecord("r1-p2", 2, "s1", "discovery", 0),  // idx 1 — round 1
    finalRecord("r1-final", 3, "s1", "discovery", 0), // idx 2 — final (null)
    partialRecord("r2-p1", 4, "s1", "discovery", 0),  // idx 3 — round 2
    partialRecord("r2-p2", 5, "s1", "discovery", 0),  // idx 4 — round 2
  ]);
  assert.equal(segs[2], null, "the final is non-partial → null");
  assert.equal(segs[0], segs[1], "round 1 partials share a key");
  assert.equal(segs[3], segs[4], "round 2 partials share a key");
  assert.notEqual(segs[0], segs[3], "round 1 and round 2 must differ");
});
check("partialSegments: non-partial / non-assistant / step-event records are null", () => {
  const segs = app.partialSegments([
    finalRecord("done", 1, "s1", "discovery", 0),                       // assistant final
    { step_id: "s1", step_type: "discovery",
      message: { role: "user", content: "hi", timestamp: 2 } },          // user
    { step_id: "s1", step_type: "discovery",
      message: { role: "system", content: "sys", timestamp: 3 } },       // system
    { step_id: "s2", step_type: "analyze",
      message: { type: "step_completed", timestamp: 4, data: {} } },     // step-event
  ]);
  assert.deepEqual(segs, [null, null, null, null]);
});

// -- renderConversation: single-turn accumulation in ONE bubble -------------
check("renderConversation: many same-turn partials accumulate into a single bubble", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    partialRecord("alpha", 1, "s1", "discovery", 0),
    partialRecord("beta", 2, "s1", "discovery", 0),
    partialRecord("gamma", 3, "s1", "discovery", 0),
    partialRecord("delta", 4, "s1", "discovery", 0),
  ], false);
  assert.equal(findAll(container, "conv-partial").length, 1,
    "all four fragments belong to one turn → one accumulating bubble");
  assert.equal(describeBubbles(container).length, 1);
  const body = findOne(container, "conv-bubble");
  for (const frag of ["alpha", "beta", "gamma", "delta"]) {
    assert.ok(body.textContent.includes(frag),
      `accumulating bubble must contain fragment ${frag}`);
  }
});

// -- renderConversation: different (step_id, attempt) → separate bubbles ----
check("renderConversation: partials of different (step_id, attempt) stay separate", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    partialRecord("a-p1", 1, "s1", "discovery", 0),  // turn A
    partialRecord("a-p2", 2, "s1", "discovery", 0),  // turn A
    partialRecord("b-p1", 3, "s2", "analyze", 0),    // turn B (different step)
    partialRecord("c-p1", 4, "s1", "discovery", 1),  // turn C (different attempt)
  ], false);
  // Three distinct turns → three accumulating bubbles.
  assert.equal(findAll(container, "conv-partial").length, 3);
  assert.equal(describeBubbles(container).length, 3);
});

// -- renderConversation: multi-round (round1 final + round2 partials) -------
check("renderConversation: round 1 folds while round 2 accumulates independently", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    partialRecord("r1-p1", 1, "s1", "discovery", 0),
    partialRecord("r1-p2", 2, "s1", "discovery", 0),
    finalRecord("r1-final", 3, "s1", "discovery", 0),
    partialRecord("r2-p1", 4, "s1", "discovery", 0),
    partialRecord("r2-p2", 5, "s1", "discovery", 0),
  ], false);
  // Round 1's accumulating bubble is superseded by r1-final and removed; round 2
  // remains as a single live accumulating bubble holding only round 2 fragments.
  const partials = findAll(container, "conv-partial");
  assert.equal(partials.length, 1, "only round 2 stays live as one bubble");
  assert.ok(partials[0].textContent.includes("r2-p1"));
  assert.ok(partials[0].textContent.includes("r2-p2"));
  assert.ok(!partials[0].textContent.includes("r1-p1"),
    "round 2 bubble must not absorb round 1 fragments");
  // The round 1 final survives as a normal (non-partial) bubble.
  const bodies = container.children
    .filter((c) => c.__convIdx !== undefined)
    .map((c) => { const b = findOne(c, "conv-bubble"); return b ? b.textContent : ""; });
  assert.ok(bodies.some((t) => t.includes("r1-final")),
    "round 1's final result bubble must remain");
});

// -- renderConversation: incremental append accumulation == full render -----
check("renderConversation: incremental partial appends match a one-shot render", () => {
  const records = [
    partialRecord("one", 1, "s1", "discovery", 0),
    partialRecord("two", 2, "s1", "discovery", 0),
    partialRecord("three", 3, "s1", "discovery", 0),
  ];
  const full = document.createElement("div");
  app.renderConversation(full, records, false);

  const incr = document.createElement("div");
  app.renderConversation(incr, records.slice(0, 1), false);
  app.renderConversation(incr, records.slice(0, 2), true);
  app.renderConversation(incr, records, true);

  // Both paths produce one accumulating bubble with all three fragments.
  assert.equal(findAll(incr, "conv-partial").length, 1);
  assert.equal(findAll(full, "conv-partial").length, 1);
  const incrBody = findOne(incr, "conv-bubble").textContent;
  const fullBody = findOne(full, "conv-bubble").textContent;
  for (const frag of ["one", "two", "three"]) {
    assert.ok(incrBody.includes(frag), `incremental bubble missing ${frag}`);
    assert.ok(fullBody.includes(frag), `full bubble missing ${frag}`);
  }
  assert.deepEqual(describeBubbles(incr).length, describeBubbles(full).length);
});

// -- renderConversation: multi-round split ACROSS separate append batches ----
check("renderConversation: round 1 final lands in its own append batch, then round 2 partials append into a distinct bubble", () => {
  // The product streams in batches: round 1's partials arrive first, round 1's
  // final arrives in a LATER append, then round 2's partials trickle in across
  // further appends — all reusing the same (s1, attempt=0) key. This exercises
  // the exact cross-batch path the one-shot multi-round test cannot: each batch
  // re-runs partialSegments over the FULL array (so round 2 lands at #seg1), and
  // the live round-1 bubble's __convIdx must have advanced to its latest fragment
  // so removeSupersededProgress can drop it once round 1's final appends. A
  // regression that keyed the bubble by its first fragment's index, or that probed
  // the DOM instead of segment keys, would either strand round 1's stale bubble or
  // merge round 2 into round 1 — and only this split-across-appends sequence
  // catches it.
  const container = document.createElement("div");

  // Batch 1 (full): round 1's two partials → one accumulating bubble.
  app.renderConversation(container, [
    partialRecord("r1-p1", 1, "s1", "discovery", 0),
    partialRecord("r1-p2", 2, "s1", "discovery", 0),
  ], false);
  assert.equal(findAll(container, "conv-partial").length, 1,
    "round 1 partials accumulate into one bubble");

  // Batch 2 (append): round 1's final arrives in its OWN batch. The live round-1
  // partial bubble must be superseded and removed, leaving only the final bubble.
  app.renderConversation(container, [
    partialRecord("r1-p1", 1, "s1", "discovery", 0),
    partialRecord("r1-p2", 2, "s1", "discovery", 0),
    finalRecord("r1-final", 3, "s1", "discovery", 0),
  ], true);
  assert.equal(findAll(container, "conv-partial").length, 0,
    "round 1's accumulating bubble is dropped once its final appends");
  let bodies = container.children
    .filter((c) => c.__convIdx !== undefined)
    .map((c) => { const b = findOne(c, "conv-bubble"); return b ? b.textContent : ""; });
  assert.ok(bodies.some((t) => t.includes("r1-final")),
    "round 1's final result bubble must remain");

  // Batch 3 (append): round 2's first partial appears in a later batch. It must
  // open a DISTINCT accumulating bubble (#seg1), not resurrect/append round 1's.
  app.renderConversation(container, [
    partialRecord("r1-p1", 1, "s1", "discovery", 0),
    partialRecord("r1-p2", 2, "s1", "discovery", 0),
    finalRecord("r1-final", 3, "s1", "discovery", 0),
    partialRecord("r2-p1", 4, "s1", "discovery", 0),
  ], true);
  assert.equal(findAll(container, "conv-partial").length, 1,
    "round 2's first partial opens exactly one new accumulating bubble");

  // Batch 4 (append): round 2's second partial appends into that SAME bubble.
  app.renderConversation(container, [
    partialRecord("r1-p1", 1, "s1", "discovery", 0),
    partialRecord("r1-p2", 2, "s1", "discovery", 0),
    finalRecord("r1-final", 3, "s1", "discovery", 0),
    partialRecord("r2-p1", 4, "s1", "discovery", 0),
    partialRecord("r2-p2", 5, "s1", "discovery", 0),
  ], true);

  const partials = findAll(container, "conv-partial");
  assert.equal(partials.length, 1,
    "round 2 stays a single live accumulating bubble across appends");
  assert.ok(partials[0].textContent.includes("r2-p1"));
  assert.ok(partials[0].textContent.includes("r2-p2"),
    "round 2's later partial appended into the same bubble");
  assert.ok(!partials[0].textContent.includes("r1-p1"),
    "round 2 bubble must not absorb round 1 fragments");
  assert.ok(!partials[0].textContent.includes("r1-p2"),
    "round 2 bubble must not absorb round 1 fragments");
  // Round 1's final still stands and no stale round-1 partial lingers.
  bodies = container.children
    .filter((c) => c.__convIdx !== undefined)
    .map((c) => { const b = findOne(c, "conv-bubble"); return b ? b.textContent : ""; });
  assert.ok(bodies.some((t) => t.includes("r1-final")),
    "round 1's final result bubble must remain after round 2 accumulates");
  assert.ok(!bodies.some((t) => t.includes("r1-p1")),
    "no stale round-1 partial bubble may linger on screen");
});

// -- renderConversation: head timestamp tracks the latest fragment ----------
check("renderConversation: the accumulating bubble's head time tracks the newest fragment", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    partialRecord("first", 10, "s1", "discovery", 0),
    partialRecord("second", 20, "s1", "discovery", 0),
    partialRecord("third", 30, "s1", "discovery", 0),
  ], true);
  const bubble = findAll(container, "conv-partial")[0];
  // Ordering key (and thus the rendered head) reflect the newest fragment.
  assert.equal(bubble.__convTs, app.tsValue(30),
    "ordering timestamp must be the latest fragment's");
  // Exactly one head is present (the stale heads were swapped out, not stacked).
  assert.equal(findAll(bubble, "history-record-head").length, 1);
  const time = findOne(bubble, "record-time");
  assert.ok(time, "the head must carry a record-time span");
});

// -- renderConversation: leftover history partials (no final) merge ----------
check("renderConversation: leftover history partials (no final) merge into one bubble", () => {
  // A run interrupted mid-turn leaves partials with no final in history. They
  // must still collapse into a single accumulating bubble, not one each.
  const container = document.createElement("div");
  app.renderConversation(container, [
    partialRecord("hist-1", 1, "s1", "implement", 0),
    partialRecord("hist-2", 2, "s1", "implement", 0),
    partialRecord("hist-3", 3, "s1", "implement", 0),
  ], false);
  assert.equal(findAll(container, "conv-partial").length, 1);
  const body = findOne(container, "conv-bubble").textContent;
  assert.ok(body.includes("hist-1") && body.includes("hist-2") && body.includes("hist-3"));
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

// -- updateReplyBox: no kind renders the duplicated context block
// (item 4) ------------------------------------------------------------------
// Every pending intervention's prompt already carries the human-meaningful
// text the user needs to act on (discovery_confirm embeds the refined task
// description; the other kinds carry their full prompt), and the backend
// mirrors the same payload into the prompt. A separate context <pre> below the
// prompt therefore only duplicated content, so updateReplyBox now suppresses
// the context block for ALL kinds (matching the prior discovery_confirm-only
// behavior). These DOM-stub checks drive the branch through the exported
// renderInterventions (which calls updateReplyBox) so a future regression —
// re-introducing a per-kind context block — cannot ship undetected. They also
// assert header / prompt / options rendering is unaffected.
check("updateReplyBox: discovery_confirm hides the duplicated context block", () => {
  const ctx = document.getElementById("flow-reply-context");
  app.state.flowInterjectRequested = false;
  app.renderInterventions({
    status: "running",
    pending_calls: [
      {
        call_id: "dc1",
        kind: "discovery_confirm",
        prompt: "Proposed task description: do the thing\n输入 1 确认",
        // Non-empty context that would normally render a <pre> block — for
        // discovery_confirm it must be suppressed to avoid the duplicate.
        context: "Proposed task description: do the thing",
      },
    ],
  });
  assert.equal(findOne(ctx, "flow-reply-context-block"), null,
    "discovery_confirm must NOT render the duplicated context block even " +
    "when context is non-empty");
  // Header / prompt / options rendering is unaffected by context suppression.
  assert.ok(findOne(ctx, "flow-reply-head"), "header still renders");
  assert.ok(findOne(ctx, "flow-reply-prompt"), "prompt still renders");
  assert.ok(findOne(ctx, "flow-reply-options"),
    "discovery_confirm still synthesizes its confirm option button");
});

check("updateReplyBox: all kinds suppress the duplicated context block", () => {
  const ctx = document.getElementById("flow-reply-context");
  for (const kind of ["call", "cli_confirm", "retry_decision", "interjection"]) {
    app.state.flowInterjectRequested = false;
    app.renderInterventions({
      status: "running",
      pending_calls: [
        {
          call_id: "k_" + kind,
          kind,
          prompt: "please respond",
          // Non-empty context that the old renderer would have shown as a
          // <pre> block — it must now be suppressed for every kind.
          context: "CONTEXT_BODY_TOKEN for " + kind,
          options: [{ label: "OK", value: "ok" }],
        },
      ],
    });
    assert.equal(findOne(ctx, "flow-reply-context-block"), null,
      kind + " must NOT render a context block (suppressed for all kinds)");
    // Header / prompt / options rendering remains intact.
    assert.ok(findOne(ctx, "flow-reply-head"), kind + " header still renders");
    assert.ok(findOne(ctx, "flow-reply-prompt"), kind + " prompt still renders");
    assert.ok(findOne(ctx, "flow-reply-options"),
      kind + " options still render");
  }
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
check("mergeSnapshotWithLiveAppends does NOT roll an authoritative snapshot line back to an older live copy", () => {
  // Reverse race: baseline had line 1 = A, a WS append advanced the held view to
  // 1 = B, then the cache advanced to 1 = C (newest). The dropped WS frame means
  // the live-held copy is still the older B, but the full snapshot correctly
  // carries C. The merge must keep the authoritative C, NOT regress to B.
  const ord = (ordinal, content, ts) => ({
    step_id: "s1", step_type: "discovery", ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });
  const merged = app.mergeSnapshotWithLiveAppends(
    [ord(0, "d0", 1), ord(1, "C", 4)],   // snapshot: newest content, later ts
    [ord(1, "B", 2)],                    // live-held: older content, earlier ts
  );
  const bodies = merged.map(app.normalizeRecord).map((n) => n.content);
  assert.deepEqual(bodies, ["d0", "C"],
    "the authoritative snapshot line wins over the stale live-held copy");
});
check("mergeSnapshotWithLiveAppends keeps the snapshot line on an equal-timestamp tie", () => {
  // Ties resolve to the snapshot (the correctness source); a truly-missed
  // forward rewrite self-heals at the next full pull rather than risking a
  // backward regression on an ambiguous same-timestamp collision.
  const ord = (ordinal, content, ts) => ({
    step_id: "s1", step_type: "discovery", ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });
  const merged = app.mergeSnapshotWithLiveAppends(
    [ord(0, "snap", 5)],
    [ord(0, "live", 5)],
  );
  const bodies = merged.map(app.normalizeRecord).map((n) => n.content);
  assert.deepEqual(bodies, ["snap"], "equal timestamp keeps the snapshot copy");
});

// -- dedupeAppendRecords: filter duplicate records from WS append batches -----
//
// Covers the symmetric race: HTTP snapshot lands AFTER server cache write but
// BEFORE WS broadcast, so the snapshot already contains the batch. When the
// same batch arrives as a `history_data` append, dedupeAppendRecords strips
// the duplicates before concat.

check("dedupeAppendRecords returns empty array when all incoming records already exist", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const existing = [r1, r2];
  const incoming = [r1, r2];
  const fresh = app.dedupeAppendRecords(existing, incoming);
  assert.equal(fresh.length, 0);
});

check("dedupeAppendRecords returns only new records, preserving incoming order", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const r3 = asstRecord("A3", 3, "s1", "discovery");
  const r4 = asstRecord("A4", 4, "s1", "discovery");
  const existing = [r1, r2];
  const incoming = [r2, r3, r4];
  const fresh = app.dedupeAppendRecords(existing, incoming);
  assert.equal(fresh.length, 2);
  assert.equal(app.recordKey(fresh[0]), app.recordKey(r3));
  assert.equal(app.recordKey(fresh[1]), app.recordKey(r4));
});

check("dedupeAppendRecords returns all records when existing array is empty", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const fresh = app.dedupeAppendRecords([], [r1, r2]);
  assert.equal(fresh.length, 2);
  assert.equal(app.recordKey(fresh[0]), app.recordKey(r1));
  assert.equal(app.recordKey(fresh[1]), app.recordKey(r2));
});

check("dedupeAppendRecords returns all records when incoming array is empty", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const fresh = app.dedupeAppendRecords([r1], []);
  assert.equal(fresh.length, 0);
});

check("dedupeAppendRecords: partial/stream_progress records with accumulating content are NOT deduplicated", () => {
  // Simulates a partial record whose content grows each push — the recordKey
  // changes because content.length and the content prefix differ, so it should
  // NOT be filtered out even if a prior partial with the same stepId/role/ts
  // already exists.
  const partial1 = asstRecord("🔧 Read src/foo.py", 1, "s1", "analyze");
  const partial2 = asstRecord("🔧 Read src/foo.py\n✅ Read ✓", 1, "s1", "analyze");
  // Same stepId, role, timestamp, attempt — but different content.
  const fresh = app.dedupeAppendRecords([partial1], [partial2]);
  assert.equal(fresh.length, 1,
    "accumulating partial with different content must not be filtered");
});

check("dedupeAppendRecords race regression: snapshot already contains batch, same batch arrives as append", () => {
  // Simulate: fetch snapshot completes, containing records R1-R3.
  // Then the same R1-R3 arrive via WS history_data append.
  // Without dedupeAppendRecords, concat would produce 6 records (3 dupes).
  const r1 = asstRecord("Step output 1", 1, "s1", "discovery");
  const r2 = asstRecord("Step output 2", 2, "s1", "discovery");
  const r3 = asstRecord("Step output 3", 3, "s1", "discovery");

  // State after snapshot merge (records already in the array)
  const existing = [r1, r2, r3];
  // Same batch arrives as WS append
  const incoming = [r1, r2, r3];

  const fresh = app.dedupeAppendRecords(existing, incoming);
  assert.equal(fresh.length, 0, "entire duplicate batch must be filtered");

  // Simulate the full path: dedup then concat — result should have exactly 3
  const final = fresh.length ? existing.concat(fresh) : existing;
  assert.equal(final.length, 3);

  // Verify no duplicate recordKeys
  const keys = final.map(app.recordKey);
  const uniqueKeys = new Set(keys);
  assert.equal(keys.length, uniqueKeys.size, "no duplicate recordKey in final array");
});

check("dedupeAppendRecords race regression: partial new records after snapshot", () => {
  // Snapshot already has R1, R2. WS append carries R2 (dup), R3 (new), R4 (new).
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const r3 = asstRecord("A3", 3, "s1", "discovery");
  const r4 = asstRecord("A4", 4, "s1", "discovery");

  const existing = [r1, r2];
  const incoming = [r2, r3, r4];

  const fresh = app.dedupeAppendRecords(existing, incoming);
  assert.equal(fresh.length, 2, "only R3 and R4 should pass through");
  assert.equal(app.recordKey(fresh[0]), app.recordKey(r3));
  assert.equal(app.recordKey(fresh[1]), app.recordKey(r4));

  // Final merged array: R1, R2, R3, R4 — no duplicates
  const final = existing.concat(fresh);
  assert.equal(final.length, 4);
  const keys = final.map(app.recordKey);
  assert.equal(keys.length, new Set(keys).size);
});

check("dedupeAppendRecords: a fresh record colliding with a FAR-BACK existing record is NOT suppressed (bounded window)", () => {
  // Regression (the "live render stalls after respond" bug): recordKey is coarse
  // (stepId+role+second-ts+attempt+len+content[:96]), so a genuinely-new reply
  // can coincidentally hash identically to an OLD record way back in the held
  // array — e.g. a discovery continuation reuses its step_id and the operator
  // sends the same short reply ("1") again at the same wall-clock second. The
  // PRE-FIX dedupe built `seen` from the WHOLE array, so that distant collision
  // permanently filtered the new record (fresh.length === 0) and every later
  // append sharing the key stalled forever. The bounded tail window must let the
  // fresh record through because the collision is far outside the recent tail.
  const collide = asstRecord("1", 100, "s1", "discovery");
  const existing = [collide];
  // Pad well beyond the tail window so the colliding record sits far back.
  for (let i = 0; i < 80; i++) existing.push(asstRecord("filler " + i, 200 + i, "s2", "analyze"));
  // Same stepId/role/second-ts/attempt/content as `collide` → identical recordKey.
  const incoming = [asstRecord("1", 100, "s1", "discovery")];
  assert.equal(app.recordKey(incoming[0]), app.recordKey(collide),
    "the incoming record genuinely collides on recordKey with the far-back one");
  const fresh = app.dedupeAppendRecords(existing, incoming);
  // PRE-FIX: fresh.length === 0 (the bug). POST-FIX (bounded window): 1.
  assert.equal(fresh.length, 1,
    "a collision with a far-back record beyond the tail window must not suppress the fresh record");
});

check("dedupeAppendRecords: a TRUE tail-overlap duplicate is still filtered (bounded window keeps real dedup)", () => {
  // The bounded window must still catch the real snapshot/WS overlap, which lands
  // at the tail. A short held array (within the window) dedups exactly as before.
  const r1 = asstRecord("tail-1", 1, "s1", "discovery");
  const r2 = asstRecord("tail-2", 2, "s1", "discovery");
  const existing = [r1, r2];
  const fresh = app.dedupeAppendRecords(existing, [r2]);
  assert.equal(fresh.length, 0, "a real tail duplicate is still filtered");
});

// -- historySnapshotUrl: incremental fetch URL construction ------------------
//
// The reconnect loaders echo the held opaque progress token as `?after=` so
// the server can serve a delta; the first open (no token) sends a bare URL.

check("historySnapshotUrl omits the after param when no progress is held", () => {
  assert.equal(app.historySnapshotUrl("F1", null), "/api/history/F1");
  assert.equal(app.historySnapshotUrl("F1", ""), "/api/history/F1");
  assert.equal(app.historySnapshotUrl("F1", undefined), "/api/history/F1");
});

check("historySnapshotUrl appends a held progress token as after", () => {
  const url = app.historySnapshotUrl("F1", "tok-123");
  assert.equal(url, "/api/history/F1?after=tok-123");
});

check("historySnapshotUrl encodes the flow id and the progress token safely", () => {
  // A base64url progress token can contain '-', '_', '=' which URLSearchParams
  // percent-encodes where required; the flow id is encodeURIComponent'd.
  const token = "Zm9v+bar/=baz_-";
  const url = app.historySnapshotUrl("a b/c", token);
  // Flow id space + slash are percent-encoded.
  assert.ok(url.startsWith("/api/history/a%20b%2Fc?after="));
  // The token round-trips out of the query string unchanged.
  const qs = url.slice(url.indexOf("?") + 1);
  const params = new URLSearchParams(qs);
  assert.equal(params.get("after"), token);
});

// -- mergeHistoryResponse: shared full/delta merge decision ------------------
//
// Folds a GET /api/history response into the records a view already holds,
// choosing append (delta) vs rebuild (full) from the server `delivery` tag and
// reporting the render mode + fresh progress token back to the caller.

check("mergeHistoryResponse full delivery replaces records and reports render=full", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const resp = { delivery: "full", records: [r1, r2], progress: "tok-full" };
  const out = app.mergeHistoryResponse(resp, []);
  assert.equal(out.render, "full");
  assert.equal(out.progress, "tok-full");
  assert.equal(out.records.length, 2);
  assert.equal(app.recordKey(out.records[0]), app.recordKey(r1));
  assert.equal(app.recordKey(out.records[1]), app.recordKey(r2));
});

check("mergeHistoryResponse full fallback preserves live appends arrived during fetch", () => {
  // Snapshot has R1, R2; a live append R3 landed in the held array during the
  // await. A full delivery must not drop R3.
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const r3 = asstRecord("A3", 3, "s1", "discovery");
  const resp = { delivery: "full", records: [r1, r2], progress: "p" };
  const out = app.mergeHistoryResponse(resp, [r3], []);
  assert.equal(out.render, "full");
  assert.equal(out.records.length, 3);
  // R3 (the live append not in the snapshot) is appended after the snapshot.
  assert.equal(app.recordKey(out.records[2]), app.recordKey(r3));
});

check("mergeHistoryResponse full fallback discards records from the invalidated generation", () => {
  const oldA = asstRecord("old-A", 1, "s1", "discovery");
  const oldB = asstRecord("old-B", 2, "s1", "discovery");
  const newX = asstRecord("new-X", 10, "s2", "analyze");
  const newY = asstRecord("new-Y", 11, "s2", "analyze");
  const baseline = [oldA, oldB];
  const resp = {
    delivery: "full",
    records: [newX, newY],
    progress: "new-generation",
  };
  const out = app.mergeHistoryResponse(resp, baseline, baseline);
  assert.equal(out.render, "full");
  assert.deepEqual(
    out.records.map((record) => record.message.content),
    ["new-X", "new-Y"],
  );
});

check("mergeHistoryResponse full fallback preserves a pending local echo from the baseline", () => {
  // A user reply was optimistically echoed (appendLocalReply) BEFORE the
  // reconnect refetch started, so the echo is in the request baseline. The
  // daemon has not yet persisted its authoritative copy, so the new snapshot
  // does NOT contain it. A full fallback must keep the echo (client-only UI
  // state) rather than dropping it with the invalidated generation.
  const oldA = asstRecord("old-A", 1, "s1", "discovery");
  const echo = {
    __localEcho: true, __localEchoText: "yes", __localEchoPriorAuth: 0,
    message: { role: "user", content: "yes", timestamp: 5 },
  };
  const baseline = [oldA, echo];
  const newX = asstRecord("new-X", 10, "s2", "analyze");
  const resp = { delivery: "full", records: [newX], progress: "gen2" };
  const out = app.mergeHistoryResponse(resp, baseline, baseline);
  assert.equal(out.render, "full");
  // The snapshot record plus the surviving echo; the stale oldA is dropped.
  assert.ok(out.records.some((r) => r.__localEcho), "pending echo must survive full fallback");
  assert.ok(
    out.records.some((r) => app.normalizeRecord(r).content === "new-X"),
    "new snapshot record is present",
  );
  assert.ok(
    !out.records.some((r) => app.normalizeRecord(r).content === "old-A"),
    "stale generation record is discarded",
  );
});

check("mergeHistoryResponse full fallback drops an echo already authoritative in the snapshot", () => {
  // The new snapshot already carries the daemon's authoritative copy of the
  // reply, so the echo is redundant. mergeHistoryResponse still appends it (it
  // has a different recordKey); the caller's reconcileLocalEchoes removes it.
  // Here we only assert the echo is preserved through the merge so reconcile
  // can act on it (matching the old full-reload + reconcile behaviour).
  const echo = {
    __localEcho: true, __localEchoText: "yes", __localEchoPriorAuth: 0,
    message: { role: "user", content: "yes", timestamp: 5 },
  };
  const baseline = [echo];
  const authUser = {
    step_id: "s1c", step_type: "discovery",
    message: { role: "user", content: "yes", timestamp: 11 },
  };
  const resp = { delivery: "full", records: [authUser], progress: "gen2" };
  const merged = app.mergeHistoryResponse(resp, baseline, baseline);
  assert.ok(merged.records.some((r) => r.__localEcho), "echo carried into merge");
  // reconcileLocalEchoes then collapses the pair to a single user record.
  const reconciled = app.reconcileLocalEchoes(merged.records);
  const yes = reconciled.filter(
    (r) => app.normalizeRecord(r).role === "user"
      && app.comparableUserText(app.normalizeRecord(r).content) === "yes");
  assert.equal(yes.length, 1, "reply shown exactly once after reconcile");
  assert.ok(!reconciled.some((r) => r.__localEcho), "echo reconciled away");
});

check("mergeHistoryResponse full fallback keeps a live in-place rewrite over a stale snapshot line", () => {
  // A REST full pull raced a WS in-place rewrite of the SAME stepId#ordinal.
  // The request baseline held line 1's pre-rewrite content; during the fetch a
  // WS append advanced it to the new content (the held `base` array). The REST
  // snapshot resolves STALE, still carrying the pre-rewrite content. The merge
  // must NOT regress the view backward to the snapshot's old content — the live
  // rewrite already advanced it, so its content is the newer authority for that
  // line. dedupeAppendRecords omits it (its key already exists in the baseline);
  // stableInPlaceRewrites surfaces it and the idempotent snapshot merge applies
  // it in place.
  const ord = (stepId, ordinal, content, ts) => ({
    step_id: stepId, step_type: "discovery", ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });
  const baseline = [ord("s1", 0, "d0", 1), ord("s1", 1, "old", 2)];
  const base = [ord("s1", 0, "d0", 1), ord("s1", 1, "new", 3)];
  const resp = {
    delivery: "full",
    records: [ord("s1", 0, "d0", 1), ord("s1", 1, "old", 2)],
    progress: "gen2",
  };
  const out = app.mergeHistoryResponse(resp, base, baseline);
  assert.equal(out.render, "full");
  const bodies = out.records.map(app.normalizeRecord).map((n) => n.content);
  assert.deepEqual(bodies, ["d0", "new"],
    "the live in-place rewrite wins over the stale snapshot line");
  const keys = out.records.map(app.recordKey);
  assert.equal(new Set(keys).size, keys.length, "no duplicate line after the merge");
});

check("mergeHistoryResponse full fallback keeps an authoritative snapshot line over an OLDER live-held copy", () => {
  // The inverse of the test above: the WS in-place rewrite the held view carries
  // is now STALE — the server cache advanced past it and the full snapshot is the
  // newer authority. The request baseline held line 1 = A; during the fetch a WS
  // append advanced the held `base` to 1 = B; but the cache reached 1 = C and the
  // dropped WS frame means the view never saw C. The REST full snapshot correctly
  // resolves to C. stableInPlaceRewrites still surfaces B (it changed vs baseline
  // A), but the timestamp-gated merge must NOT let the older B overwrite C —
  // otherwise the right side regresses backward until a later poll repairs it.
  const ord = (stepId, ordinal, content, ts) => ({
    step_id: stepId, step_type: "discovery", ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });
  const baseline = [ord("s1", 0, "d0", 1), ord("s1", 1, "A", 2)];
  const base = [ord("s1", 0, "d0", 1), ord("s1", 1, "B", 3)];
  const resp = {
    delivery: "full",
    records: [ord("s1", 0, "d0", 1), ord("s1", 1, "C", 5)],
    progress: "gen3",
  };
  const out = app.mergeHistoryResponse(resp, base, baseline);
  assert.equal(out.render, "full");
  const bodies = out.records.map(app.normalizeRecord).map((n) => n.content);
  assert.deepEqual(bodies, ["d0", "C"],
    "the authoritative full snapshot wins over the older live-held rewrite");
  const keys = out.records.map(app.recordKey);
  assert.equal(new Set(keys).size, keys.length, "no duplicate line after the merge");
});

check("mergeSnapshotWithLiveAppends updates a snapshot line in place when a live rewrite shares its ordinal", () => {
  // The stable-identity idempotent merge: a live record whose stepId#ordinal
  // matches a snapshot line but whose content advanced updates that line in
  // place rather than being dropped as a key collision.
  const ord = (ordinal, content, ts) => ({
    step_id: "s1", step_type: "discovery", ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });
  const merged = app.mergeSnapshotWithLiveAppends(
    [ord(0, "d0", 1), ord(1, "stale", 2)],
    [ord(1, "fresh", 3)],
  );
  const bodies = merged.map(app.normalizeRecord).map((n) => n.content);
  assert.deepEqual(bodies, ["d0", "fresh"]);
});

check("mergeHistoryResponse unrecognised delivery defaults to full", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const resp = { records: [r1], progress: "p" };   // no delivery field
  const out = app.mergeHistoryResponse(resp, []);
  assert.equal(out.render, "full");
  assert.equal(out.records.length, 1);
});

check("mergeHistoryResponse not_modified is a noop that preserves the held array and carries the signature", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const held = [r1];
  const resp = { delivery: "not_modified", records: [], progress: "p2", signature: "sig2" };
  const out = app.mergeHistoryResponse(resp, held);
  assert.equal(out.render, "noop", "not_modified is a noop — nothing to repaint");
  assert.equal(out.records, held, "the held array is returned by reference (no adopt)");
  assert.equal(out.progress, "p2", "the fresh token is surfaced for the next poll");
  assert.equal(out.signature, "sig2", "the signature is surfaced for the next poll");
});

check("mergeHistoryResponse delta with all-new records appends and reports render=delta", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const r3 = asstRecord("A3", 3, "s1", "discovery");
  // Held: R1, R2. Delta tail: R3 only (the server already knew R1, R2).
  const resp = { delivery: "delta", records: [r3], progress: "tok-d" };
  const out = app.mergeHistoryResponse(resp, [r1, r2]);
  assert.equal(out.render, "delta");
  assert.equal(out.progress, "tok-d");
  assert.equal(out.records.length, 3);
  assert.equal(app.recordKey(out.records[2]), app.recordKey(r3));
  // Held-record order is preserved.
  assert.equal(app.recordKey(out.records[0]), app.recordKey(r1));
});

check("mergeHistoryResponse delta filters records already held (WS append race)", () => {
  // During the outage some delta records also arrived as a live WS append, so
  // they are already in the held array; the delta must not re-add them.
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const r3 = asstRecord("A3", 3, "s1", "discovery");
  const r4 = asstRecord("A4", 4, "s1", "discovery");
  // Held R1, R2, R3 (R3 came via a live append). Delta returns R3, R4.
  const resp = { delivery: "delta", records: [r3, r4], progress: "p" };
  const out = app.mergeHistoryResponse(resp, [r1, r2, r3]);
  assert.equal(out.render, "delta");
  assert.equal(out.records.length, 4, "only the genuinely new R4 is appended");
  assert.equal(app.recordKey(out.records[3]), app.recordKey(r4));
  // No duplicate recordKeys in the merged array.
  const keys = out.records.map(app.recordKey);
  assert.equal(keys.length, new Set(keys).size);
});

check("mergeHistoryResponse delta older than held tail merges in order and rebuilds (full)", () => {
  // Reconnect-while-streaming race: during the incremental fetch's await a live
  // WS append delivered the current turn's FINAL result (ts=5) into the held
  // tail. The delta then returns the outage-window gap records, including that
  // turn's earlier PARTIAL fragment (ts=3). Appending the partial after the
  // final would invert array order and leave a stale streaming bubble; instead
  // it must be merged before the final and the caller must do a full rebuild.
  const a1 = asstRecord("A1", 1, "s0", "analyze");
  const finalB = asstRecord("Bfinal", 5, "s1", "discovery");
  const partialB = {
    step_id: "s1", step_type: "discovery",
    message: { role: "assistant", content: "B…", timestamp: 3, partial: true },
  };
  // Held tail already has the newer final (WS append won the race).
  const resp = { delivery: "delta", records: [partialB], progress: "p" };
  const out = app.mergeHistoryResponse(resp, [a1, finalB]);
  assert.equal(out.render, "full", "out-of-order delta forces a full rebuild");
  // Records are ordered by timestamp: A1(1) → partialB(3) → finalB(5).
  const tss = out.records.map((r) => app.recordSortTs(r));
  assert.deepEqual(tss, [1000, 3000, 5000]);
  // The partial now precedes its final, so partialSegments groups them together
  // (#seg0) and markSupersededProgress supersedes the partial.
  const segs = app.partialSegments(out.records);
  assert.ok(segs[1] && segs[1].endsWith("#seg0"), "partial lands in seg0, not a phantom later segment");
  const superseded = app.markSupersededProgress(out.records);
  assert.ok(superseded.has(1), "the partial is superseded by its turn's final");
});

check("mergeHistoryResponse delta newer than held tail still appends (delta)", () => {
  // The common case: gap records are all newer than the held tail, so a plain
  // tail append (incremental render) is still correct.
  const a1 = asstRecord("A1", 1, "s1", "discovery");
  const a2 = asstRecord("A2", 2, "s1", "discovery");
  const a3 = asstRecord("A3", 3, "s1", "discovery");
  const resp = { delivery: "delta", records: [a3], progress: "p" };
  const out = app.mergeHistoryResponse(resp, [a1, a2]);
  assert.equal(out.render, "delta");
  assert.equal(out.records.length, 3);
  assert.equal(app.recordKey(out.records[2]), app.recordKey(a3));
});

check("stableMergeByTimestamp interleaves by (timestamp, index) and is stable", () => {
  const a = asstRecord("A", 1, "s1", "discovery");
  const b = asstRecord("B", 5, "s1", "discovery");
  const c = asstRecord("C", 3, "s1", "discovery");
  // held=[A(1), B(5)] (B is the newer WS append), fresh=[C(3)] (the gap record).
  const merged = app.stableMergeByTimestamp([a, b], [c]);
  assert.deepEqual(merged.map((r) => r.message.content), ["A", "C", "B"]);
});

check("stableMergeByTimestamp orders the REST delta before a held WS record on an equal-timestamp tie", () => {
  // The held tail is a WS final (ts=3) that arrived later during the request;
  // the REST delta carries that turn's earlier partial at the SAME timestamp.
  // On the tie the REST delta (fresh) must precede the held WS record so the
  // partial lands before its final rather than after it.
  const finalWs = asstRecord("final", 3, "s1", "discovery");
  const partialRest = {
    step_id: "s1", step_type: "discovery",
    message: { role: "assistant", content: "partial", timestamp: 3, partial: true },
  };
  const merged = app.stableMergeByTimestamp([finalWs], [partialRest]);
  assert.deepEqual(merged.map((r) => r.message.content), ["partial", "final"]);
});

check("stableMergeByTimestamp keeps a baseline-held record before an equal-timestamp delta record", () => {
  // Record A was already held before the reconnect (it lives in requestBaseline)
  // and the server delta carries a LATER record B sharing A's timestamp. The
  // server bundle order is A then B (B comes after A's progress offset), so the
  // baseline record A must retain its authoritative earlier position rather than
  // being shoved behind the fresh delta.
  const a = asstRecord("A", 3, "s1", "discovery");
  const b = asstRecord("B", 3, "s1", "discovery");
  const merged = app.stableMergeByTimestamp([a], [b], [a]);
  assert.deepEqual(merged.map((r) => r.message.content), ["A", "B"]);
});

check("stableMergeByTimestamp orders a live append after an equal-timestamp delta but keeps the baseline first", () => {
  // base = [baselineA(ts=3), wsFinal(ts=3)] where baselineA predates the request
  // and wsFinal arrived during it; the REST delta returns restPartial(ts=3). The
  // baseline record must stay first, the delta partial precedes its WS final.
  const baselineA = asstRecord("A", 3, "s0", "analyze");
  const wsFinal = asstRecord("final", 3, "s1", "discovery");
  const restPartial = {
    step_id: "s1", step_type: "discovery",
    message: { role: "assistant", content: "partial", timestamp: 3, partial: true },
  };
  const merged = app.stableMergeByTimestamp(
    [baselineA, wsFinal], [restPartial], [baselineA],
  );
  assert.deepEqual(
    merged.map((r) => r.message.content), ["A", "partial", "final"],
  );
});

check("mergeHistoryResponse delta at the held tail's timestamp is not append-safe and orders the partial first", () => {
  // Reconnect-while-streaming race with EQUAL timestamps: the WS final (ts=3)
  // won the race into the held tail, and the REST delta returns that turn's
  // earlier partial, also stamped ts=3. The equal timestamp must NOT be treated
  // as append-safe (which would invert the partial after the final and strand a
  // stale streaming bubble); instead the merge orders the partial before the
  // final and forces a full rebuild.
  const a1 = asstRecord("A1", 1, "s0", "analyze");
  const finalB = asstRecord("Bfinal", 3, "s1", "discovery");
  const partialB = {
    step_id: "s1", step_type: "discovery",
    message: { role: "assistant", content: "B…", timestamp: 3, partial: true },
  };
  const resp = { delivery: "delta", records: [partialB], progress: "p" };
  const out = app.mergeHistoryResponse(resp, [a1, finalB]);
  assert.equal(out.render, "full", "equal-timestamp delta forces a full rebuild");
  // The partial precedes its final despite the equal timestamp.
  assert.deepEqual(
    out.records.map((r) => r.message.content), ["A1", "B…", "Bfinal"],
  );
  // partialSegments pairs the partial with its final (#seg0), not a phantom
  // later segment, and markSupersededProgress supersedes it.
  const segs = app.partialSegments(out.records);
  assert.ok(segs[1] && segs[1].endsWith("#seg0"), "partial lands in seg0");
  const superseded = app.markSupersededProgress(out.records);
  assert.ok(superseded.has(1), "the partial is superseded by its turn's final");
});

check("mergeHistoryResponse delta with all-duplicate records is a noop (same reference)", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const held = [r1, r2];
  // Delta returns records already entirely held (the WS append won the race).
  const resp = { delivery: "delta", records: [r1, r2], progress: "p" };
  const out = app.mergeHistoryResponse(resp, held);
  assert.equal(out.render, "noop");
  assert.equal(out.records, held, "held array returned unchanged by reference");
  assert.equal(out.progress, "p", "fresh progress token still reported on a noop");
});

check("mergeHistoryResponse empty delta is a noop and keeps the held array", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const held = [r1];
  const resp = { delivery: "delta", records: [], progress: "p2" };
  const out = app.mergeHistoryResponse(resp, held);
  assert.equal(out.render, "noop");
  assert.equal(out.records, held);
  assert.equal(out.progress, "p2");
});

check("mergeHistoryResponse reports null progress when the response carries none", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const resp = { delivery: "full", records: [r1] };   // no progress field
  const out = app.mergeHistoryResponse(resp, []);
  assert.equal(out.progress, null);
  // A non-string progress is also normalised to null.
  const out2 = app.mergeHistoryResponse(
    { delivery: "full", records: [r1], progress: 42 }, []);
  assert.equal(out2.progress, null);
});

check("mergeHistoryResponse delta from an empty held array appends everything", () => {
  // A delta delivered while the view holds nothing (e.g. progress survived a
  // record reset): every delta record is new, so all are appended.
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const r2 = asstRecord("A2", 2, "s1", "discovery");
  const resp = { delivery: "delta", records: [r1, r2], progress: "p" };
  const out = app.mergeHistoryResponse(resp, []);
  assert.equal(out.render, "delta");
  assert.equal(out.records.length, 2);
});

check("mergeHistoryResponse tolerates a missing records array", () => {
  const out = app.mergeHistoryResponse({ delivery: "full", progress: "p" }, []);
  assert.equal(out.render, "full");
  assert.deepEqual(out.records, []);
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
  // No-result turn: thinking is shown inline, AND (per the unified "every
  // conversation message can view raw" principle) a default-folded 查看原始
  // toggle is always appended below it.
  assert.ok(findOne(row, "raw-toggle"),
    "查看原始 toggle is always present, even for a no-result turn");
  assert.ok(findOne(row, "assistant-process-inline"),
    "the inline thinking process stays shown (not folded)");
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
  // No assistant "展开全部" wrapper exists; but per the unified principle a
  // default-folded 查看原始 toggle IS always appended below the inline thinking,
  // which itself stays shown in full (never collapsed into the fold).
  assert.equal(findOne(row, "process-toggle"), null,
    "no 展开全部 toggle exists on the assistant side at all");
  const rawToggle = findOne(row, "raw-toggle");
  assert.ok(rawToggle, "查看原始 toggle is always present, even for a no-result turn");
  const rawPre = findOne(row, "raw-json");
  assert.equal(rawPre.classList.contains("hidden"), true,
    "the raw body is folded by default; the inline thinking stays visible");
});

// -- Q1 fallback: unregistered step types (confirm / project_summary / …) --
// Without the fallback, step types NOT in STEP_ASSISTANT_RENDERERS fall through
// to renderAssistantProcessInline → renderMarkdown, which turns a ```json fence
// in the body into a raw code block — burying field names inside JSON syntax.
// The fix routes such bodies through `renderGenericOutputs` so the user sees
// `key: value` rows just like the CLI `_default_render`.

check("Q1 generic fallback: confirm step renders kv rows, not a raw json fence", () => {
  const outputs = {
    decision: "approved",
    reasoning: "All checks passed and the plan looks correct.",
  };
  const content = "Reviewing the plan now.\n```json\n" +
    JSON.stringify(outputs) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "confirm"));
  const result = findOne(row, "assistant-result");
  assert.ok(result, "expected an assistant-result wrapper for the confirm fallback");
  assert.ok(result.classList.contains("assistant-result--generic"),
    "the generic-fallback variant class is applied");
  // Field rows present.
  const rows = findAll(result, "step-report__kv-row");
  assert.ok(rows.length >= 2, `expected ≥2 kv rows, got ${rows.length}`);
  const keys = findAll(result, "step-report__kv-k").map((n) => n.textContent);
  assert.ok(keys.includes("decision"));
  assert.ok(keys.includes("reasoning"));
  // The ```json fence body must NOT have surfaced as a raw markdown code
  // block under the bubble.
  assert.equal(findOne(row, "md-code"), null,
    "unregistered step must not render the outputs as a raw ```json fence");
  // Narrative is preserved above the kv block.
  assert.ok(result.textContent.includes("Reviewing the plan now."),
    "narrative prose above the JSON is kept");
  // Single 查看原始 fold is still attached so the original record is reachable.
  assert.ok(findOne(row, "raw-toggle"), "查看原始 fold present for fallback path");
});

check("Q1 generic fallback: registered analyze step is NOT routed through fallback", () => {
  // analyze IS in STEP_RESULT_FIELDS; a body with no matching result field
  // (pure tool-call) must keep the existing inline thinking behavior. The
  // fallback's `!renderer` guard prevents it from re-rendering the tool call
  // as kv rows.
  const content = "Let me list files.\n```json\n" +
    JSON.stringify({ command: "ls -la", description: "list files" }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "analyze"));
  assert.equal(findOne(row, "assistant-result"), null,
    "fallback must not fire for registered analyze step");
  assert.ok(findOne(row, "raw-toggle"),
    "查看原始 toggle is always present below the inline (tool-call-only) thinking");
  const inline = findOne(row, "assistant-process-inline");
  assert.ok(inline, "thinking inline preserved for registered step");
});

check("Q1 generic fallback: prose-only unregistered step keeps thinking inline", () => {
  // No JSON region in the body → fallback returns null → inline thinking
  // is shown, never a kv card.
  const content = "Just discussing the change. No JSON here.";
  const row = app.renderConversationRecord(asstNorm(content, "confirm"));
  assert.equal(findOne(row, "assistant-result"), null,
    "no kv card without a JSON region");
  const inline = findOne(row, "assistant-process-inline");
  assert.ok(inline, "inline thinking is rendered");
  assert.ok(inline.textContent.includes("Just discussing"),
    "narrative is preserved");
});

// -- Q1 renderGenericOutputs unit checks ------------------------------------

check("Q1 renderGenericOutputs: long string preview includes char count", () => {
  const longStr = "x".repeat(450);
  const frag = app.renderGenericOutputs({ field: longStr });
  const valEl = findOne(frag, "step-report__kv-v");
  assert.ok(valEl, "value element rendered");
  assert.ok(valEl.textContent.includes("(450 chars)"),
    "preview suffix shows the original length");
  assert.ok(valEl.textContent.length < longStr.length,
    "the long value was previewed, not fully inlined");
});

// -- JS-generated chrome follows the selected UI language -------------------
//
// The char-count suffix and the grep/glob pattern/path header are
// framework-authored chrome: under zh-CN they must not leak English labels into
// an otherwise localized UI. The tool-call arguments themselves (pattern, path)
// are data and pass through verbatim.
function withZhDicts(dict, fn) {
  const { I18N } = app;
  const savedLang = I18N.lang;
  const savedDicts = I18N.dicts;
  try {
    I18N.dicts = { "en-US": {}, "zh-CN": dict };
    I18N.lang = "zh-CN";
    fn();
  } finally {
    I18N.lang = savedLang;
    I18N.dicts = savedDicts;
  }
}

check("i18n renderGenericOutputs: char-count suffix follows the UI language", () => {
  withZhDicts({ "common.size.chars": "{n} 字符" }, () => {
    const valEl = findOne(app.renderGenericOutputs({ field: "x".repeat(450) }),
      "step-report__kv-v");
    assert.ok(valEl.textContent.includes("(450 字符)"), valEl.textContent);
    assert.ok(!valEl.textContent.includes("chars"), "no English label leaks");
    assert.equal(valEl.title, "450 字符");
  });
});

check("i18n grep detail: pattern/path labels localize, values pass through", () => {
  withZhDicts({ "tool.detail.patternPath": "模式={pattern} 路径={path}" }, () => {
    for (const kind of ["grep_matches", "glob_matches"]) {
      const panel = app.renderToolDetailPanel({
        kind, pattern: "TODO", path: "src/", matches: [], files: [],
      });
      const head = findOne(panel, "tool-marker-diff-path");
      assert.equal(head.textContent, "模式=TODO 路径=src/", kind);
    }
  });
});

check("Q1 renderGenericOutputs: nested dict expands one indented level", () => {
  const frag = app.renderGenericOutputs({
    top: "scalar",
    nested: { inner_a: "v1", inner_b: 42 },
  });
  const nested = findOne(frag, "step-report__kv-nested");
  assert.ok(nested, "nested-dict wrapper rendered");
  const innerKeys = findAll(nested, "step-report__kv-k").map((n) => n.textContent);
  assert.ok(innerKeys.includes("inner_a"));
  assert.ok(innerKeys.includes("inner_b"));
});

check("Q1 renderGenericOutputs: empty / non-dict input produces no rows", () => {
  assert.equal(findOne(app.renderGenericOutputs({}), "step-report__kv-row"), null);
  assert.equal(findOne(app.renderGenericOutputs(null), "step-report__kv-row"), null);
  assert.equal(findOne(app.renderGenericOutputs([1, 2]), "step-report__kv-row"), null);
});

check("Q1 renderDefaultReport reuses renderGenericOutputs for non-empty outputs", () => {
  const step = { step_type: "confirm", status: "completed" };
  const frag = app.renderDefaultReport(step, { decision: "approve" });
  // Empty hint must not surface — outputs are non-empty.
  assert.equal(findOne(frag, "step-report__empty"), null);
  // Field rows came from renderGenericOutputs.
  const keys = findAll(frag, "step-report__kv-k").map((n) => n.textContent);
  assert.deepEqual(keys, ["decision"]);
});

check("Q1 renderDefaultReport still shows empty hint for empty outputs", () => {
  const frag = app.renderDefaultReport({ step_type: "unknown" }, {});
  assert.ok(findOne(frag, "step-report__empty"),
    "empty-outputs hint preserved");
});

// -- G2: plan report inner proposal/design field-by-field rendering ---------
// Prior renderer dumped plan.proposal / plan.design as a single
// `pre.step-report__json` blob, burying summary / files_to_modify / overview /
// components inside JSON syntax. The field-by-field path mirrors the CLI
// display.render_proposal / render_design output so web and CLI users see the
// same structured fields.

check("G2 renderPlanReport: proposal renders as field sections, not raw json pre", () => {
  const outputs = {
    plan: {
      proposal: {
        summary: "Refactor the engine to support X.",
        files_to_modify: [
          { path: "src/engine/a.py", reason: "wire new option" },
          { path: "src/engine/b.py", reason: "thread context" },
        ],
        files_to_create: [
          { path: "src/engine/c.py", purpose: "new helper" },
        ],
        rationale: "Decouples A from B.",
      },
      design: {
        overview: "High-level design.",
        components: [
          { name: "CompA", description: "owns state" },
        ],
        interfaces: [
          { name: "iface1", signature: "f(x) -> y", description: "the iface" },
        ],
        decisions: [
          { decision: "use option Z", reason: "simpler" },
        ],
      },
    },
    task_groups: [
      { group_id: "G1", name: "core", tasks: [{ estimated_loc: 30 }], depends_on: [] },
    ],
  };
  const step = { step_type: "plan", status: "completed" };
  const frag = app.renderPlanReport(step, outputs);

  // The proposal must NOT render as a raw `pre.step-report__json` dump.
  const jsonPres = findAll(frag, "step-report__json");
  assert.equal(jsonPres.length, 0,
    "no `pre.step-report__json` blobs — fields replaced the raw JSON dump");

  const sectionTitles = findAll(frag, "step-report__section-title")
    .map((n) => n.textContent);
  // Proposal field-section titles.
  assert.ok(sectionTitles.includes("Summary"), "Summary field section present");
  assert.ok(sectionTitles.some((t) => t.startsWith("Files to Modify")),
    "Files to Modify section present");
  assert.ok(sectionTitles.some((t) => t.startsWith("Files to Create")),
    "Files to Create section present");
  assert.ok(sectionTitles.includes("Rationale"), "Rationale field section present");
  // Design field-section titles.
  assert.ok(sectionTitles.includes("Overview"), "Overview field section present");
  assert.ok(sectionTitles.some((t) => t.startsWith("Components")),
    "Components section present");
  assert.ok(sectionTitles.some((t) => t.startsWith("Interfaces")),
    "Interfaces section present");
  assert.ok(sectionTitles.some((t) => t.startsWith("Key Decisions")),
    "Key Decisions section present");

  // Per-item dict expansion: path/reason and component name surface as text.
  const text = frag.textContent;
  assert.ok(text.includes("src/engine/a.py"));
  assert.ok(text.includes("wire new option"));
  assert.ok(text.includes("src/engine/c.py"));
  assert.ok(text.includes("new helper"));
  assert.ok(text.includes("CompA"));
  assert.ok(text.includes("owns state"));
  assert.ok(text.includes("iface1"));
  assert.ok(text.includes("f(x) -> y"));
  assert.ok(text.includes("use option Z"));
  assert.ok(text.includes("simpler"));

  // Task groups still render unchanged (parity with the prior contract).
  assert.ok(text.includes("G1"));
  assert.ok(text.includes("core"));
});

check("G2 renderProposalFields: string proposal falls through plan path", () => {
  const outputs = { plan: { proposal: "just a string proposal text" } };
  const step = { step_type: "plan", status: "completed" };
  const frag = app.renderPlanReport(step, outputs);
  const titles = findAll(frag, "step-report__section-title")
    .map((n) => n.textContent);
  assert.ok(titles.includes("Proposal"), "string proposal still gets its section");
  // The string path renders the value as a paragraph, not as a pre.json blob
  // and not via the field expander.
  assert.equal(findOne(frag, "step-report__json"), null);
  assert.ok(frag.textContent.includes("just a string proposal text"));
});

check("G2 renderProposalFields: unknown fields fall back via renderGenericOutputs", () => {
  const frag = app.renderProposalFields({
    summary: "main summary",
    risks: ["r1", "r2"],
    extra_field: "leftover value",
  });
  // The "Other Fields" section captures unknown keys via renderGenericOutputs
  // so nothing is silently dropped.
  const titles = findAll(frag, "step-report__section-title")
    .map((n) => n.textContent);
  assert.ok(titles.includes("Summary"));
  assert.ok(titles.includes("Other Fields"),
    "unknown keys fall through the generic-outputs bucket");
  const keys = findAll(frag, "step-report__kv-k").map((n) => n.textContent);
  assert.ok(keys.includes("risks"));
  assert.ok(keys.includes("extra_field"));
});

check("G2 renderDesignFields: unknown fields fall back via renderGenericOutputs", () => {
  const frag = app.renderDesignFields({
    overview: "the overview",
    data_flow: "request → engine → store",
  });
  const titles = findAll(frag, "step-report__section-title")
    .map((n) => n.textContent);
  assert.ok(titles.includes("Overview"));
  assert.ok(titles.includes("Other Fields"));
  const keys = findAll(frag, "step-report__kv-k").map((n) => n.textContent);
  assert.ok(keys.includes("data_flow"));
});

check("G2 renderPlanReport: only proposal present, design omitted -> only proposal section", () => {
  const outputs = {
    plan: { proposal: { summary: "S", files_to_modify: [{ path: "a.py" }] } },
  };
  const step = { step_type: "plan", status: "completed" };
  const frag = app.renderPlanReport(step, outputs);
  const titles = findAll(frag, "step-report__section-title").map((n) => n.textContent);
  assert.ok(titles.includes("Proposal"));
  assert.ok(!titles.includes("Design"), "no Design section without design data");
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
check("2c: single tool-call JSON (no result field) stays inline (always-present 查看原始)", () => {
  const content = "Let me list files.\n```json\n" +
    JSON.stringify({ command: "ls -la", description: "list files" }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "analyze"));
  assert.equal(findOne(row, "assistant-result"), null,
    "a tool-call-only turn must not render a structured result");
  assert.ok(findOne(row, "raw-toggle"),
    "查看原始 toggle is always present below the inline thinking, even tool-call-only");
  const inline = findOne(row, "assistant-process-inline");
  assert.ok(inline, "thinking is shown inline");
  assert.ok(inline.textContent.includes("Let me list files."),
    "the narrative is preserved");
  assert.ok(inline.textContent.includes("ls -la"),
    "the tool-call JSON content is not lost");
});

// 2c: two or more tool-call JSON segments in one turn are all thinking process.
check("2c: 2+ tool-call JSON segments stay inline (always-present 查看原始 toggle)", () => {
  const content =
    "Checking.\n```json\n" + JSON.stringify({ command: "ls" }) + "\n```\n" +
    "Now editing.\n```json\n" +
    JSON.stringify({ file_path: "x.py", old_string: "a", new_string: "b" }) +
    "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "implement"));
  assert.equal(findOne(row, "assistant-result"), null,
    "neither tool-call JSON is an implement result");
  assert.ok(findOne(row, "raw-toggle"),
    "2+ tool-call JSONs with no result stay inline, with a default-folded 查看原始");
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
  assert.ok(findOne(row, "raw-toggle"),
    "查看原始 toggle is always present below the inline thinking for discovery too");
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
  assert.ok(findOne(row, "raw-toggle"),
    "查看原始 toggle is always present even for an empty-result discovery turn");
  assert.ok(findOne(row, "assistant-process-inline"),
    "thinking stays inline, content preserved");
});

// ---------------------------------------------------------------------------
// Unified "every conversation message can view raw" — the three remaining
// conversation branches (assistant inline, system chip, other row).
// ---------------------------------------------------------------------------
//
// Regression fix: the "查看原始" affordance was missing on (1) the assistant
// no-result / unstructurable inline turn, and (2) system / other role messages
// whose chip used the nullable makeRawToggle (which vanished when no raw payload
// existed). The unified principle is now: ALL four conversation roles
// (user / assistant / system / other) ALWAYS expose "查看原始" — preferring the
// raw_json / raw_ndjson payload and falling back to the record's own original
// text (assistant / system / other) or .jsonl envelope (user). The
// non-conversation synthetic UI (group_status markers) keeps NO such affordance.

// Build a normalized record for an arbitrary role (system / tool / log / …) in
// the real daemon envelope shape, optionally carrying a raw NDJSON payload.
const roleNorm = (role, content, stepType, rawNdjson) => app.normalizeRecord({
  step_id: stepType,
  step_type: stepType,
  message: {
    role,
    content,
    timestamp: 1,
    raw_ndjson: rawNdjson != null ? rawNdjson : null,
  },
});

// -- (a) assistant inline (no-result) branch: 查看原始 always present ----------
check("assistant inline turn: 查看原始 shows the raw NDJSON payload when present", () => {
  const ndjson = '{"raw_marker":"INLINE_NDJSON_TOKEN"}';
  // Pure prose for a registered step → no result JSON → the no-result inline
  // branch of renderAssistantBubble.
  const row = app.renderConversationRecord(
    asstNorm("Just reasoning, no JSON here.", "analyze", ndjson));
  // The inline thinking stays fully shown (not folded into the toggle).
  const inline = findOne(row, "assistant-process-inline");
  assert.ok(inline, "inline thinking process present");
  assert.ok(inline.textContent.includes("Just reasoning"),
    "inline thinking shown in full by default");
  assert.equal(findOne(row, "foldable"), null,
    "the inline thinking is not collapsed into a fold");
  // A default-folded 查看原始 toggle is appended below it.
  const rawToggle = findOne(row, "raw-toggle");
  assert.ok(rawToggle, "查看原始 toggle present on the inline branch");
  const rawPre = findOne(row, "raw-json");
  assert.equal(rawPre.classList.contains("hidden"), true, "raw body folded by default");
  rawToggle.dispatch("click");
  assert.equal(rawPre.classList.contains("hidden"), false, "expands on click");
  assert.ok(rawPre.textContent.includes("INLINE_NDJSON_TOKEN"),
    "shows the original raw NDJSON payload");
});

check("assistant inline turn: 查看原始 falls back to the content 原文 when no raw payload", () => {
  const row = app.renderConversationRecord(
    asstNorm("PROSE_FALLBACK_TOKEN with no JSON and no raw.", "analyze")); // no ndjson
  assert.ok(findOne(row, "assistant-process-inline"), "inline thinking present");
  const rawToggle = findOne(row, "raw-toggle");
  assert.ok(rawToggle, "查看原始 toggle present even without a raw payload");
  rawToggle.dispatch("click");
  const rawPre = findOne(row, "raw-json");
  assert.ok(rawPre.textContent.includes("PROSE_FALLBACK_TOKEN"),
    "falls back to the unrendered content 原文");
  assert.ok(rawToggle.textContent.includes("content"),
    "the fallback labels the raw kind as 'content'");
});

// -- (b) system role chip: 查看原始 always present after expand ----------------
check("system chip: 查看原始 always present on expand, shows the raw payload", () => {
  const ndjson = '{"raw_marker":"SYS_NDJSON_TOKEN"}';
  const row = app.renderConversationRecord(
    roleNorm("system", "system boilerplate body", "analyze", ndjson));
  // Collapsed chip by default — the raw toggle is built lazily on first expand.
  assert.equal(findOne(row, "raw-toggle"), null, "raw toggle not built until expand");
  const chip = findOne(row, "msg-chip");
  assert.ok(chip, "system collapsible chip present");
  chip.dispatch("click");
  const detail = findOne(row, "msg-chip-detail");
  const rawToggle = findOne(detail, "raw-toggle");
  assert.ok(rawToggle, "查看原始 present inside the expanded system chip");
  rawToggle.dispatch("click");
  const rawPre = findOne(detail, "raw-json");
  assert.ok(rawPre.textContent.includes("SYS_NDJSON_TOKEN"),
    "system chip 查看原始 shows the raw payload");
});

check("system chip: 查看原始 present even with no raw payload (falls back to content)", () => {
  // Previously makeRawToggle returned null here, so the system chip had no
  // 查看原始 at all — the regression this fixes.
  const row = app.renderConversationRecord(
    roleNorm("system", "SYS_FALLBACK_TOKEN body", "analyze")); // no ndjson
  const chip = findOne(row, "msg-chip");
  chip.dispatch("click");
  const detail = findOne(row, "msg-chip-detail");
  const rawToggle = findOne(detail, "raw-toggle");
  assert.ok(rawToggle, "查看原始 present even without a raw payload (no longer null)");
  rawToggle.dispatch("click");
  const rawPre = findOne(detail, "raw-json");
  assert.ok(rawPre.textContent.includes("SYS_FALLBACK_TOKEN"),
    "system chip 查看原始 falls back to the content 原文");
});

// -- (c) other role (non-collapsible row): row-level 查看原始 always present ----
check("other-role row: row-level 查看原始 always present, shows the raw payload", () => {
  const ndjson = '{"raw_marker":"OTHER_NDJSON_TOKEN"}';
  const norm = roleNorm("tool", "tool output body", "implement", ndjson);
  assert.equal(norm.role, "tool", "an unknown role is preserved (display → other)");
  const row = app.renderConversationRecord(norm);
  assert.ok(row.classList.contains("role-other"),
    "renders on the non-collapsible other row");
  // Not a collapsible chip — the row is directly expanded, so the toggle shows
  // immediately at the row level (no ▸ chip).
  assert.equal(findOne(row, "msg-chip"), null, "other role is not a collapsible chip");
  const rawToggle = findOne(row, "raw-toggle");
  assert.ok(rawToggle, "row-level 查看原始 present for an other-role record");
  rawToggle.dispatch("click");
  const rawPre = findOne(row, "raw-json");
  assert.ok(rawPre.textContent.includes("OTHER_NDJSON_TOKEN"),
    "other-role 查看原始 shows the raw payload");
});

check("other-role row: 查看原始 falls back to the content 原文 with no raw payload", () => {
  const norm = roleNorm("log", "OTHER_FALLBACK_TOKEN body", "implement"); // no ndjson
  const row = app.renderConversationRecord(norm);
  const rawToggle = findOne(row, "raw-toggle");
  assert.ok(rawToggle, "row-level 查看原始 present without a raw payload");
  rawToggle.dispatch("click");
  const rawPre = findOne(row, "raw-json");
  assert.ok(rawPre.textContent.includes("OTHER_FALLBACK_TOKEN"),
    "other-role 查看原始 falls back to the content 原文");
});

// -- assistant non-collapsible row gets NO duplicate row-level toggle ---------
check("assistant row: single 查看原始 inside the bubble, no row-level duplicate", () => {
  // A result-JSON assistant turn: makeAssistantRawToggle is appended INSIDE the
  // bubble (renderAssistantBubble). The new row-level append is guarded by
  // role !== "assistant", so there must be exactly ONE raw toggle, inside the
  // conv-bubble — never a second one at the row level.
  const content = "```json\n" + JSON.stringify({
    task_type: "bugfix", complexity: "small", scope: "src/x", reasoning: "tiny",
  }) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "analyze"));
  assert.equal(findAll(row, "raw-toggle").length, 1,
    "exactly one 查看原始 toggle for an assistant turn (no row-level duplicate)");
  const bubble = findOne(row, "conv-bubble");
  assert.ok(bubble && findOne(bubble, "raw-toggle"),
    "the single toggle lives inside the assistant bubble, not at the row level");
});

// -- empty-content assistant turn still exposes 查看原始 ------------------------
// A pure tool-call assistant turn may be stored with empty text content while
// its raw_json / raw_ndjson carries the real payload. buildBubble then takes the
// "(no readable content)" branch and never invokes renderAssistantBubble, so the
// in-bubble fold is absent — the row-level append must still surface 查看原始 so
// the raw payload stays reachable (unified principle for ALL four roles).
check("empty-content assistant turn still exposes a row-level 查看原始 for its raw payload", () => {
  const ndjson = '{"raw_marker":"EMPTY_ASST_NDJSON_TOKEN"}';
  const row = app.renderConversationRecord(asstNorm("", "implement", ndjson));
  assert.ok(row.classList.contains("role-assistant"),
    "renders on the assistant row");
  assert.ok(findOne(row, "conv-empty"),
    "empty content takes the (no readable content) branch");
  const rawToggle = findOne(row, "raw-toggle");
  assert.ok(rawToggle, "row-level 查看原始 present for an empty-content assistant turn");
  rawToggle.dispatch("click");
  const rawPre = findOne(row, "raw-json");
  assert.ok(rawPre.textContent.includes("EMPTY_ASST_NDJSON_TOKEN"),
    "the empty-content assistant 查看原始 shows the raw payload");
  // Exactly one toggle — the empty branch builds no in-bubble fold, so no dup.
  assert.equal(findAll(row, "raw-toggle").length, 1,
    "exactly one 查看原始 toggle (no duplicate) for the empty-content assistant turn");
});

// -- step_completed / step_failed report card stays affordance-free -----------
// Like group_status, the step-event report-card path (renderStepEventRecord) is
// non-conversation synthetic UI: its own raw event uses a `raw-json` source view,
// but it must carry NO conversation 查看原始 toggle after the unified change.
check("step_completed report card has no 查看原始 toggle (non-conversation synthetic UI)", () => {
  const norm = app.normalizeRecord({
    step_id: "03_analyze_deadbeef",
    step_type: "analyze",
    message: { type: "step_completed",
      data: { step: { step_type: "analyze", status: "completed", outputs: {} } },
      timestamp: 1 },
  });
  assert.equal(norm.kind, "step_completed");
  // renderConversationRecord dispatches step_completed/step_failed to
  // renderStepEventRecord — drive the real exported entry point.
  const row = app.renderConversationRecord(norm);
  assert.equal(findOne(row, "raw-toggle"), null,
    "a step_completed report card must carry NO 查看原始 affordance");
});

// -- (d) group_status marker stays affordance-free (no 查看原始) ----------------
check("group_status marker has no 查看原始 toggle (non-conversation synthetic UI)", () => {
  const norm = app.normalizeRecord({
    step_id: "07_implement_abcd1234",
    step_type: "implement",
    message: { type: "group_status", role: "system", group_id: "G3",
      status: "running", timestamp: 1 },
  });
  const row = app.renderGroupStatusRecord(norm);
  assert.equal(findOne(row, "raw-toggle"), null,
    "a group_status marker must carry NO 查看原始 affordance");
  assert.equal(findOne(row, "msg-chip"), null, "and no fold chip");
  assert.ok(row.classList.contains("group-status-marker"),
    "still renders as the lightweight status marker");
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
  assert.ok(toggle.textContent.includes("Expand all"));
  const full = findOne(wrap, "process-full");
  assert.equal(full.classList.contains("hidden"), true, "prefix/suffix folded by default");
  toggle.dispatch("click");
  assert.equal(full.classList.contains("hidden"), false, "expands on click");
  assert.ok(full.textContent.includes("Template prefix"), "template-prefix subsection labeled");
  assert.ok(full.textContent.includes("Framework suffix"), "framework-suffix subsection labeled");
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

// -- Regression A: user Layer 3 stably reaches the original .jsonl envelope --
// A user record carries raw_json=[] and no raw_ndjson, so the shared
// makeRawToggle (无 raw → null) would leave Layer 3 empty. The user side must
// instead fall back to the record's original .jsonl envelope so "查看原始" is
// always reachable — without weakening makeRawToggle's null contract.

// Build a user marker record carrying NO second-layer raw payload (raw_json=[],
// raw_ndjson absent), in the real daemon envelope shape {step_id, step_type,
// message}. normalizeRecord must expose that envelope at norm.raw.envelope.
const userMarkerNormNoRaw = (content, stepType) => {
  const TPE = app.TEMPLATE_PREFIX_END;
  const UCB = app.USER_CONTENT_BEGIN;
  const UCE = app.USER_CONTENT_END;
  const body = "BOILER_PREFIX\n" + TPE + "\n" + UCB + "\n" + content + "\n" +
    UCE + "\nFRAMEWORK_SUFFIX";
  return app.normalizeRecord({
    step_id: stepType,
    step_type: stepType,
    message: { role: "user", content: body, timestamp: 1, raw_json: [] },
  });
};

check("normalizeRecord exposes the original .jsonl envelope at norm.raw.envelope", () => {
  const norm = userMarkerNormNoRaw("ENVELOPE_USER_TOKEN", "discovery");
  assert.ok(norm.raw && norm.raw.envelope, "norm.raw.envelope present");
  assert.equal(norm.raw.envelope.step_type, "discovery");
  assert.equal(norm.raw.envelope.step_id, "discovery");
  assert.ok(norm.raw.envelope.message, "envelope carries the message");
  // Existing raw_json / raw_ndjson fields are untouched (raw_json stays []).
  assert.deepEqual(norm.raw.raw_json, []);
});

check("user Layer 3 reaches the .jsonl envelope when no raw payload (regression A)", () => {
  const norm = userMarkerNormNoRaw("ENVELOPE_USER_TOKEN", "discovery");
  const row = app.renderConversationRecord(norm);

  // Layer 1 bubble surfaces only the literal input.
  const bubble = findOne(row, "user-content-bubble");
  assert.ok(bubble && bubble.textContent.includes("ENVELOPE_USER_TOKEN"));

  // Layer 2 toggle is offered even with no raw payload (no hasRawPayload gate).
  const wrap = findOne(row, "user-prompt-toggle-wrap");
  assert.ok(wrap, "Layer 2 展开全部 toggle present without a raw payload");
  // Layer 3 not visible until Layer 2 is expanded.
  assert.equal(findOne(row, "raw-toggle"), null,
    "Layer 3 must not show in the default view");

  const toggle = findOne(wrap, "process-toggle");
  toggle.dispatch("click");
  const full = findOne(wrap, "process-full");
  // Layer 3 ("查看原始") is now reachable, nested inside the Layer-2 area.
  const rawToggle = findOne(full, "raw-toggle");
  assert.ok(rawToggle, "Layer 3 raw toggle reachable via the user-side fallback");
  const rawPre = findOne(full, "raw-json");
  assert.equal(rawPre.classList.contains("hidden"), true, "raw hidden by default");
  rawToggle.dispatch("click");
  assert.equal(rawPre.classList.contains("hidden"), false, "raw expands on click");
  // It shows the original .jsonl envelope record (step_type + the user body).
  assert.ok(rawPre.textContent.includes("step_type"),
    "Layer 3 shows the original .jsonl envelope record");
  assert.ok(rawPre.textContent.includes("ENVELOPE_USER_TOKEN"),
    "the envelope carries the original message body");
  // The fallback button labels the kind as the envelope source.
  assert.ok(rawToggle.textContent.includes("envelope"),
    "the fallback labels the raw kind as 'envelope'");
});

check("makeRawToggle keeps its null contract while makeUserRawToggle never returns null", () => {
  const norm = userMarkerNormNoRaw("CONTRACT_TOKEN", "analyze");
  // Shared helper's "无 raw 载荷 → null" contract is preserved for this input.
  assert.equal(app.makeRawToggle(norm), null,
    "makeRawToggle must still return null when there is no raw payload");
  // The dedicated user helper always returns a usable toggle.
  const userToggle = app.makeUserRawToggle(norm);
  assert.ok(userToggle, "makeUserRawToggle must never return null");
  const btn = findOne(userToggle, "raw-toggle");
  const pre = findOne(userToggle, "raw-json");
  btn.dispatch("click");
  assert.ok(pre.textContent.includes("CONTRACT_TOKEN"),
    "makeUserRawToggle falls back to the envelope record body");
});

check("makeUserRawToggle prefers the second-layer raw payload when present", () => {
  // When a raw_ndjson payload IS present, the user toggle prefers it over the
  // envelope fallback (kind labeled raw_ndjson, not envelope).
  const norm = userMarkerNorm(
    "prefix", "PREFER_TOKEN", "suffix", "discovery",
    '{"raw_marker":"PREFER_NDJSON_TOKEN"}');
  const userToggle = app.makeUserRawToggle(norm);
  const btn = findOne(userToggle, "raw-toggle");
  const pre = findOne(userToggle, "raw-json");
  btn.dispatch("click");
  assert.ok(pre.textContent.includes("PREFER_NDJSON_TOKEN"),
    "prefers the raw_ndjson payload");
  assert.ok(btn.textContent.includes("raw_ndjson"),
    "labels the kind as raw_ndjson, not envelope");
});

check("empty user-content chip reaches Layer 3 via the envelope (regression A)", () => {
  // Legacy two-segment record with NO raw payload → degrades to a collapsed
  // chip; its expand detail must still reach the original envelope record.
  const TPE = app.TEMPLATE_PREFIX_END;
  const UCB = app.USER_CONTENT_BEGIN;
  const body = "PREFIX_BODY\n" + TPE + "\n" + UCB + "\nSUFFIX_TAIL_TOKEN";
  const norm = app.normalizeRecord({
    step_id: "analyze",
    step_type: "analyze",
    message: { role: "user", content: body, timestamp: 1, raw_json: [] },
  });
  const row = app.renderConversationRecord(norm);
  const chip = findOne(row, "msg-chip");
  chip.dispatch("click");
  const detail = findOne(row, "msg-chip-detail");
  const rawToggle = findOne(detail, "raw-toggle");
  assert.ok(rawToggle, "Layer 3 raw toggle present inside the chip detail");
  rawToggle.dispatch("click");
  const rawPre = findOne(detail, "raw-json");
  assert.ok(rawPre.textContent.includes("step_type"),
    "the chip's Layer 3 shows the original .jsonl envelope record");
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
  assert.ok(detail.textContent.includes("Template prefix"));
  assert.ok(detail.textContent.includes("Framework suffix"));
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

  // Step headers separate the regions (visual only) WITHOUT ever shuffling the
  // bubbles. The two assistant turns share one step_id ("discovery"); the user
  // reply carries a different step_id ("discovery_continue"). Under strict
  // chronological order the second "discovery" turn (A2) physically sits AFTER
  // the discovery_continue reply, so it is a fresh CONTIGUOUS run and opens its
  // own boundary header — otherwise A2 would render beneath the
  // discovery_continue header and be mis-attributed. So there are three headers
  // (DISCOVERY, discovery_continue, DISCOVERY) while the bubbles stay in strict
  // timestamp order.
  const headers = findAll(container, "history-step-header");
  assert.equal(headers.length, 3,
    "a re-appearing step_id opens its own boundary header per contiguous run");
  const titles = headers.map((h) => { const t = findOne(h, "history-step-title"); return t ? t.textContent : ""; });
  assert.deepEqual(titles, ["DISCOVERY", "discovery_continue", "DISCOVERY"],
    "the second discovery turn opens its own boundary header (correct attribution)");
});

// -- historyListEmptyState: loading / connecting / confirmed-empty split ----
// The history list must split "empty" into three distinct semantics so opening
// the view never shows a bare empty-state while the daemon is still connecting
// or has not yet pushed its history_index (the ~1min window before the WS push).
check("historyListEmptyState: any sessions -> has-sessions", () => {
  assert.equal(
    app.historyListEmptyState({
      sessions: [{ flow_id: "f1" }], loading: false,
      daemonConnected: false, indexConfirmed: false,
    }),
    "has-sessions");
  // has-sessions short-circuits regardless of the other flags.
  assert.equal(
    app.historyListEmptyState({
      sessions: [{ flow_id: "f1" }], loading: true,
      daemonConnected: true, indexConfirmed: true,
    }),
    "has-sessions");
});

check("historyListEmptyState: empty + loading -> loading-refresh", () => {
  assert.equal(
    app.historyListEmptyState({
      sessions: [], loading: true,
      daemonConnected: true, indexConfirmed: true,
    }),
    "loading-refresh");
});

check("historyListEmptyState: empty + no daemon -> loading-connect", () => {
  assert.equal(
    app.historyListEmptyState({
      sessions: [], loading: false,
      daemonConnected: false, indexConfirmed: false,
    }),
    "loading-connect");
});

check("historyListEmptyState: empty + daemon connected but unconfirmed -> loading-connect", () => {
  assert.equal(
    app.historyListEmptyState({
      sessions: [], loading: false,
      daemonConnected: true, indexConfirmed: false,
    }),
    "loading-connect");
});

check("historyListEmptyState: empty + connected + confirmed zero -> empty-confirmed", () => {
  assert.equal(
    app.historyListEmptyState({
      sessions: [], loading: false,
      daemonConnected: true, indexConfirmed: true,
    }),
    "empty-confirmed");
});

// -- renderHistoryList: empty/loading/connect feedback rendered ---------------
check("renderHistoryList: empty + loading shows the refreshing hint, not the empty state", () => {
  app.state.historySessions = [];
  app.state.historyIndexLoading = true;
  app.state.machines = [{ machine_id: "m1", online: true }];
  app.state.historyIndexConfirmed = true;
  app.renderHistoryList();
  const list = document.getElementById("history-list");
  // loading wins over connect/confirmed even when the daemon is connected.
  assert.ok(findOne(list, "empty-loading-refresh"),
    "loading-refresh modifier class must be present");
  const texts = findAll(list, "empty").map((n) => n.textContent);
  assert.ok(texts.some((t) => t.includes("Refreshing history")),
    "loading hint must be shown");
  assert.ok(!texts.some((t) => t.includes("No history sessions reported.")),
    "empty state must NOT be shown while loading");
});

check("renderHistoryList: empty + not loading + unconfirmed shows the connecting hint, not the empty state", () => {
  app.state.historySessions = [];
  app.state.historyIndexLoading = false;
  app.state.machines = [];
  app.state.historyIndexConfirmed = false;
  app.renderHistoryList();
  const list = document.getElementById("history-list");
  assert.ok(findOne(list, "empty-loading-connect"),
    "loading-connect modifier class must be present");
  const texts = findAll(list, "empty").map((n) => n.textContent);
  assert.ok(texts.some((t) => t.includes("Connecting") || t.includes("waiting for history data")),
    "connecting/waiting hint must be shown");
  assert.ok(!texts.some((t) => t.includes("No history sessions reported.")),
    "empty state must NOT be shown before history is confirmed");
});

check("renderHistoryList: empty + connected + confirmed shows the confirmed empty state", () => {
  app.state.historySessions = [];
  app.state.historyIndexLoading = false;
  app.state.machines = [{ machine_id: "m1", online: true }];
  app.state.historyIndexConfirmed = true;
  app.renderHistoryList();
  const list = document.getElementById("history-list");
  assert.ok(findOne(list, "empty-confirmed"),
    "empty-confirmed modifier class must be present");
  const texts = findAll(list, "empty").map((n) => n.textContent);
  assert.ok(texts.some((t) => t.includes("No history sessions reported.")),
    "empty state must be shown once confirmed");
  assert.ok(!texts.some((t) => t.includes("Refreshing history") || t.includes("正在连接")),
    "no loading/connecting hint once confirmed");
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
  assert.ok(bar && bar.textContent.includes("Refreshing history"),
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

// -- live partials with bracket-format tool markers render as .tool-marker --
//
// Backend regression: the in-progress stream now emits tool events as the same
// `[Name: detail]` bracket markers `extract_assistant_text` produces for final
// state (instead of the prior `🔧 Name: detail` / `✅ …` / `❌ …` emoji form).
// That keeps the frontend running on a single TOOL_MARKER_RE path so the live
// accumulating bubble boxes tool calls the same way the final bubble does.
check("live partials with bracket-format tool_use mark render as .tool-marker", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    partialRecord("[Read: src/foo.py]", 1, "s1", "implement", 0),
  ], false);
  const liveBubble = findOne(container, "conv-bubble");
  assert.ok(liveBubble, "expected a live accumulating bubble");
  const marker = findOne(liveBubble, "tool-marker");
  assert.ok(marker, "live bubble should box [Read: …] as a .tool-marker block");
  const name = findOne(marker, "tool-marker-name");
  assert.ok(name && name.textContent === "Read",
    `tool-marker-name should be 'Read', got ${name && name.textContent}`);
});

check("live partial with bracket-format tool_result success renders as .tool-marker", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    partialRecord("[Read ✓ (10 lines)]", 1, "s1", "implement", 0),
  ], false);
  const liveBubble = findOne(container, "conv-bubble");
  assert.ok(liveBubble, "expected a live accumulating bubble");
  const marker = findOne(liveBubble, "tool-marker");
  assert.ok(marker, "live bubble should box [Read ✓ …] as a .tool-marker block");
});

check("live partial with bracket-format tool_result error renders as .tool-marker", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    partialRecord("[Edit ✗ permission denied]", 1, "s1", "implement", 0),
  ], false);
  const liveBubble = findOne(container, "conv-bubble");
  assert.ok(liveBubble, "expected a live accumulating bubble");
  const marker = findOne(liveBubble, "tool-marker");
  assert.ok(marker, "live bubble should box [Edit ✗ …] as a .tool-marker block");
  const name = findOne(marker, "tool-marker-name");
  assert.ok(name && name.textContent === "Edit",
    `tool-marker-name should be 'Edit', got ${name && name.textContent}`);
});

check("live partial with bracket-format generic 'Tool error' renders as .tool-marker", () => {
  const container = document.createElement("div");
  app.renderConversation(container, [
    partialRecord("[Tool error: stream blew up]", 1, "s1", "implement", 0),
  ], false);
  const liveBubble = findOne(container, "conv-bubble");
  assert.ok(liveBubble, "expected a live accumulating bubble");
  const marker = findOne(liveBubble, "tool-marker");
  assert.ok(marker, "live bubble should box [Tool error: …] as a .tool-marker block");
});

// Register the G3 tool-chip state-machine tests (separate module — same `check`
// reporter, same `app` module, same shared DOM stub already installed above).
const chipMod = await import("./tool_chip_state.test.mjs");
chipMod.registerToolChipStateTests({ app, check, findOne, findAll });

// Register the G4 per-group DAG status marker tests (separate module — same
// `check` reporter, same `app` module, same shared DOM stub already installed
// above).
const groupStatusMod = await import("./group_status.test.mjs");
groupStatusMod.registerGroupStatusTests({ app, check, findOne, findAll });

// Register the G3 code-index update-progress marker tests (separate module —
// same `check` reporter, same `app` module, same shared DOM stub already
// installed above).
const indexProgressMod = await import("./index_progress.test.mjs");
indexProgressMod.registerIndexProgressTests({ app, check, findOne, findAll });

// Register the G2 step_started RUNNING-region tests (separate module — same
// `check` reporter, same `app` module, same shared DOM stub already installed
// above).
const stepStartedMod = await import("./step_started_region.test.mjs");
stepStartedMod.registerStepStartedRegionTests({ app, check, findOne, findAll });

// Register the G2 step-grouping tests (separate module — same `check` reporter,
// same `app` module, same shared DOM stub already installed above).
const stepGroupingMod = await import("./step_grouping.test.mjs");
stepGroupingMod.registerStepGroupingTests({ app, check, findOne, findAll });

// Register the G5 viewport-driven sticky floating step-header tests (separate
// module — same `check` reporter, same `app` module, same shared DOM stub).
const stickyHeaderMod = await import("./sticky_step_header.test.mjs");
stickyHeaderMod.registerStickyStepHeaderTests({ app, check, findOne, findAll });

// Register the G3 live accumulating-bubble agent/model badge tests (separate
// module — same `check` reporter, same `app` module, same shared DOM stub
// already installed above).
const agentBadgeLiveMod = await import("./agent_badge_live.test.mjs");
agentBadgeLiveMod.registerAgentBadgeLiveTests({ app, check, findOne, findAll });

// Register the G1 docked reply-box prompt-collapse tests (separate module —
// same `check` reporter, same `app` module, same shared DOM stub already
// installed above).
const replyBoxCollapseMod = await import("./reply_box_prompt_collapse.test.mjs");
replyBoxCollapseMod.registerReplyBoxPromptCollapseTests({ app, check, findOne, findAll });

// Register the G4 token-usage display tests (separate module — same `check`
// reporter, same `app` module, same shared DOM stub already installed above).
const tokenUsageMod = await import("./token_usage.test.mjs");
tokenUsageMod.registerTokenUsageTests({ app, check, findOne, findAll });

// Register the G5 per-round token-usage footnote tests (separate module — same
// `check` reporter, same `app` module, same shared DOM stub already installed
// above).
const roundUsageMod = await import("./round_usage.test.mjs");
roundUsageMod.registerRoundUsageTests({ app, check, findOne, findAll });

// Register the history flow_id display tests (Parts 1 & 2): the list-card meta
// flow_id span and the detail-header dedicated full flow_id line. Async because
// the detail-line check drives openHistorySession through a canned fetch.
const historyFlowIdMod = await import("./history_flow_id.test.mjs");
await historyFlowIdMod.registerHistoryFlowIdTests({ app, check, checkAsync, findOne, findAll });

// Register the history-detail session token-usage badge tests (Part 3): the
// history badge reuses the running-flow view's applyUsageBadge renderer.
const historyUsageMod = await import("./history_usage.test.mjs");
historyUsageMod.registerHistoryUsageTests({ app, check, findOne, findAll });

// Register the G3 admin-only user-management row-model tests (separate module —
// same `check` reporter, same `app` module, same shared DOM stub).
const userMgmtMod = await import("./user_mgmt.test.mjs");
userMgmtMod.registerUserMgmtTests({ app, check, findOne, findAll });

// Register the G7 issue-management pure-helper tests (separate module — same
// `check` reporter, same `app` module, same shared DOM stub).
const issueMgmtMod = await import("./issue_management.test.mjs");
issueMgmtMod.registerIssueManagementTests({ app, check, findOne, findAll });

// Register the G7 mobile-responsive pure-helper tests (separate module — same
// `check` reporter, same `app` module, same shared DOM stub already installed
// above). These exercise the DOM-free state-transition helpers the mobile pass
// added (navMenuNextState / listPanelState / historyPanelState /
// flowSidebarNextState).
const mobileResponsiveMod = await import("./mobile_responsive.test.mjs");
mobileResponsiveMod.registerMobileResponsiveTests({ app, check, findOne, findAll });

// Register the G1/G2 reply-send error-handling tests (separate module — same
// `check` reporter, same `app` module, same shared DOM stub already installed
// above). These lock the issue #193 regression: a post-success render fault in
// appendLocalReply must not be reported as a network error and must not drop
// the optimistic echo.
const replySendErrMod = await import("./reply_send_error_handling.test.mjs");
await replySendErrMod.registerReplySendErrorHandlingTests({ app, check, checkAsync, findOne, findAll });

// Register the G3 CONFIRM approval-gate chip tests: kind=='confirm' renders the
// 批准/打回 buttons + note textarea, both button clicks and the recognized
// free-text words POST a structured {response:{approved,feedback}} decision, an
// unrecognized note ("1") is only sent after an explicit second-guess, and a
// legacy kind-less confirm degrades to the plain free-text box.
const confirmChipMod = await import("./confirm_chip.test.mjs");
await confirmChipMod.registerConfirmChipTests({ app, check, checkAsync, findOne, findAll });

// Register the G4 ADJUDICATE approval-review tests: when a confirm chip reviews
// an adjudicate ruling, renderAdjudicateReview surfaces the adjudication_rationale
// panel + a baseline→adjudicated_description before/after diff (reusing the shared
// diff renderer); a non-adjudicate target renders nothing, and missing fields
// degrade gracefully instead of throwing.
const adjudicateReviewMod = await import("./adjudicate_review.test.mjs");
await adjudicateReviewMod.registerAdjudicateReviewTests({ app, check, checkAsync, findOne, findAll });

// Register the self_check status-bar fallback tests: an assistant message's raw
// LLM JSON reaches renderSelfCheckReport as synthetic outputs without the
// `actionable_count` that self_check_handler adds later, so the renderer must
// derive the count from issues.length instead of falling back to 0 and painting
// a green ✓ PASSED above the very issues it lists.
const selfCheckFallbackMod = await import("./self_check_passed_fallback.test.mjs");
await selfCheckFallbackMod.registerSelfCheckPassedFallbackTests({ app, check, findOne, findAll });

// Register the G3 live-append-after-respond tests (symptom A/B alignment).
// These lock the #193 leftover "消息不显示" half: after a respond/interject the
// daemon-pushed `mode: append` increments (re-broadcast by G1's ws.py fix) keep
// streaming into the running-flow view through dedupeAppendRecords +
// reconcileLocalEchoes — shown exactly once, none dropped, partials not falsely
// deduped — and a worktree/discovery first assistant body normalizes non-empty.
const liveAppendRespondMod = await import("./live_append_after_respond.test.mjs");
liveAppendRespondMod.registerLiveAppendAfterRespondTests({ app, check, checkAsync, findOne, findAll });

// Register the G2 continuous step-lane background tests (separate module — same
// `check` reporter, reads style.css directly). These lock the CSS-only 方案A:
// per-type rules moved to --step-lane, the ±7px ::before underlay that makes the
// lane continuous across the gap, and the boundary overflow suppression.
const stepLaneMod = await import("./step_lane_continuity.test.mjs");
stepLaneMod.registerStepLaneContinuityTests({ app, check, findOne, findAll });

// Register the G2 waiting-for-lock running sub-state tests (isWaitingForLock /
// flowStatusLabel) — the flag rides the existing flow snapshot, so a queued
// flow must read as running·waiting-for-lock instead of appearing stalled.
const waitingForLockMod = await import("./waiting_for_lock.test.mjs");
waitingForLockMod.registerWaitingForLockTests({ app, check, findOne, findAll });

// Register the G1 discovery→analyze step-transition tests. These pin the
// long-standing freeze: after the operator confirms discovery the engine steps
// into analyze, and the daemon-pushed `mode: append` increments (the discovery
// step_completed terminal, the analyze step_started anchor, analyze's first
// turns) must keep rendering live — the incremental path converging on the same
// conversation a full `mode: full` reload would show, with no loss, no dup, and
// no cursor stall after a duplicate short-circuit.
const stepTransitionMod = await import("./live_append_step_transition.test.mjs");
stepTransitionMod.registerLiveAppendStepTransitionTests({ app, check, findOne, findAll });

// Register the G1 retry-after-error step-transition tests. After a later step
// (e.g. update_spec) FAILS and the operator retries, the daemon re-runs the
// step reusing its step_id: step_failed terminal → step_status=retrying →
// step_started=running → fresh assistant turns (with content similar to the
// failed attempt). The live view must keep streaming the retry through the
// supersede anchors without dropping the resumed records.
const retryAfterErrorMod = await import("./live_append_retry_after_error.test.mjs");
retryAfterErrorMod.registerLiveAppendRetryAfterErrorTests({ app, check, findOne, findAll });

// Register the G4 end-to-end console-consistency capstone. Unlike the G1
// hand-authored scenarios above, this replays a GOLDEN FIXTURE produced by the
// Python test (tests/test_server_history_live_append_broadcast.py) — the exact
// /ws/ui broadcast frames the REAL daemon reader + REAL server emit for the
// discovery→analyze transition and the update_spec failure→retry scenarios. It
// proves the live incremental render path converges on the full-reload snapshot
// across the whole daemon→server→frontend pipeline (no loss, no dup, no freeze).
const e2eConsistencyMod = await import("./live_append_e2e_consistency.test.mjs");
e2eConsistencyMod.registerConsoleE2EConsistencyTests({ app, check, findOne, findAll });

// Register the G3 worktree-mode merged-snapshot discovery de-dup guard. A flow
// whose history is split across the main-repo root (discovery) and the worktree
// root (later steps + its own copy of discovery) is merged by the daemon and
// de-duped at the file layer; this frontend backstop ensures a `mode: full`
// snapshot that still carries a duplicate discovery record renders it exactly
// once, scoped strictly to discovery (later steps / recordKey identity intact).
const snapshotDiscoveryMod = await import("./snapshot_discovery_dedup.test.mjs");
snapshotDiscoveryMod.registerSnapshotDiscoveryDedupTests({ app, check, findOne, findAll });

// Register the G3 worktree multi-round discovery reconcile guard. Pins that the
// frontend consumes G1's per-physical-file disambiguated step_id so a worktree
// flow's 2nd+ discovery round (primary file rounds + a ``.from-<branch>``
// sidecar) all render, and that dedupeSnapshotDiscovery drops only a
// byte-identical clone — never a legitimately-different record on ordinal reuse.
const worktreeDiscoveryMod = await import("./worktree_discovery_multiround.test.mjs");
worktreeDiscoveryMod.registerWorktreeDiscoveryMultiroundTests({ app, check, findOne, findAll });

// Register the issue-#209 frontend real-frame replay guard (G3 task 2). Replays
// the EXACT real frame sequence G1 captured
// (tests/frontend/fixtures/issue_209/daemon_frames.json) through the production
// applyHistoryData / dedupeAppendRecords, proving the live incremental path
// converges on the full reload — pinning that the frontend stays correct on the
// real #209 frames (the root-cause fail-before/pass-after lock is at the daemon
// layer in tests/test_issue209_live_append_regression.py).
const issue209ReplayMod = await import("./issue209_real_frame_replay.test.mjs");
issue209ReplayMod.registerIssue209RealFrameReplayTests({ app, check, findOne, findAll });

// Register the ordinal-identity reconcile tests (G2): each record's stable
// `stepId#ordinal` identity keeps empty-content marker records distinct, and the
// idempotent reconcile updates a retry-rewritten line in place instead of
// dropping or duplicating it — the discovery/commit "chat stops advancing" fix.
const markerDedupMod = await import("./marker_dedup_ordinal.test.mjs");
markerDedupMod.registerMarkerDedupOrdinalTests({ app, check, findOne, findAll });

// Register the incremental-drop self-heal tests (G2): a dropped/mis-judged WS
// append frame is recovered when the next periodic full snapshot re-delivers the
// whole flow through the same idempotent reconcile — correctness no longer
// depends on every increment arriving.
const selfHealMod = await import("./incremental_selfheal.test.mjs");
selfHealMod.registerIncrementalSelfHealTests({ app, check, findOne, findAll });

// Register the cause-immune progression-refresh fallback tests (G2): a detected
// advance of the open flow (current_step / current_step_index change, or a
// status flip on an in-step retry) fires one silent full /api/history rebuild,
// equivalent to exit-and-re-enter but without the blank flash or scroll jump,
// and never touching the reply region.
const progressionRefreshMod = await import("./progression_refresh.test.mjs");
await progressionRefreshMod.registerProgressionRefreshTests({ app, check, checkAsync, findOne, findAll });

// Register the PERIODIC progression-fallback retry tests (G4 / issue #260): the
// grace timer now re-arms itself after each silent rebuild and keeps pulling on
// the progressionGraceMs cadence until a genuine WS increment lands, so a WS that
// stays dead across a whole step (the discovery→analyze break) still surfaces
// mid-step content without the reader exiting and re-entering the session.
const progressionFallbackRetryMod = await import("./progression_fallback_retry.test.mjs");
await progressionFallbackRetryMod.registerProgressionFallbackRetryTests({ app, check, checkAsync, findOne, findAll });

// Register the G5 differential-protocol tests: HISTORY_INDEX delta merge by
// flow_id, the silent self-heal's not_modified/delta/full signature-check
// handling, and issue-detail lazy-loading of the untruncated description.
const historyIndexMergeMod = await import("./history_index_merge.test.mjs");
await historyIndexMergeMod.registerHistoryIndexMergeTests({ app, check, checkAsync, findOne, findAll });

// Register the element-anchored scroll-preservation tests (issue #217 / #209
// jump fix): the silent rebuild anchors on the bubble the reader is looking at
// (by recordKey) and restores its viewport offset, so a content-height change
// above it no longer scrolls the conversation up a large stretch.
const issue217AnchorMod = await import("./issue217_scroll_anchor.test.mjs");
issue217AnchorMod.registerIssue217ScrollAnchorTests({ app, check, checkAsync, findOne, findAll });

// Register the discovery→analyze boundary scroll-anchor tests (issue #260 / G5):
// the silent rebuild reads the persistent flowConversationFollowingBottom intent
// so a bottom-follower drifted off the bottom by a stalled boundary increment
// still sticks to the new bottom (no up-jump), while a genuinely scrolled-up
// reader keeps their element-anchored viewport offset.
const discoveryAnalyzeAnchorMod = await import("./discovery_analyze_scroll_anchor.test.mjs");
await discoveryAnalyzeAnchorMod.registerDiscoveryAnalyzeScrollAnchorTests({ app, check, checkAsync, findOne, findAll });

// Register the empty-full no-clobber guard (issue #287 / G4): a zero-record
// `delivery:"full"` (REST) or `mode:"full"` (WS) frame — the worktree self-heal's
// pseudo-empty snapshot — must not blank an already-rendered conversation, while
// a grown full still rebuilds authoritatively and a genuinely empty first load
// still renders the empty state.
const fullNoClobberMod = await import("./test_full_delivery_no_clobber.mjs");
await fullNoClobberMod.registerFullDeliveryNoClobberTests({ app, check, checkAsync, findOne, findAll });

// ---------------------------------------------------------------------------
// Narrative chip rendering inside structured-result assistant turns
// ---------------------------------------------------------------------------
//
// A result-JSON assistant turn (discovery with `refined_description`, plan with
// `task_groups`, …) whose narrative prefix carries inline `[Tool: …]` bracket
// markers must, when `norm.raw.raw_json` is available, render those tool calls
// as the same RICH chip the thinking-only assistant path produces: ✓/✗ glyph,
// collapsible detail panel, success-state class. Otherwise the chip-events
// pipeline must be skipped and the narrative falls back to the bare bracket
// chip from `renderToolMarkers`.
//
// Regression context: prior to G1 the structured assistant renderers piped the
// narrative directly through `renderToolMarkers`, which parses the bracket
// header and produces an in-flight `.tool-marker` chip with no terminal
// upgrade. So within the same session, thinking-only turns showed rich chips
// while result-JSON turns showed bare brackets — visually inconsistent.

// Build the `raw_json` shape Claude CLI emits for a finished tool_use +
// tool_result pair, so the chip-events pipeline can fold them into a single
// upgraded chip.
const toolBlocksRawJson = (blocks) => [
  { type: "assistant", message: { content: blocks } },
];

check("renderDiscoveryAssistant: narrative renders rich chips when raw_json carries tool pair", () => {
  const blocks = [
    { type: "tool_use", id: "tu_1", name: "Read",
      input: { file_path: "src/foo.py", offset: 0, limit: 200 } },
    { type: "tool_result", tool_use_id: "tu_1",
      content: [{ type: "text", text: "ok" }], is_error: false },
  ];
  const norm = { raw: { raw_json: toolBlocksRawJson(blocks), raw_ndjson: null } };
  const content =
    "[Read: src/foo.py:0-200]\n\n" +
    "```json\n" +
    JSON.stringify({
      refined_description: "Do the thing",
      questions: ["q1?"],
    }) +
    "\n```";
  const frag = app.renderDiscoveryAssistant(content, norm);
  assert.ok(frag, "renderer returns a fragment when a discovery result is present");
  const wrap = document.createElement("div");
  wrap.appendChild(frag);
  const narrative = findOne(wrap, "assistant-narrative");
  assert.ok(narrative, "narrative wrapper rendered above the result fields");
  const chips = findAll(narrative, "tool-marker");
  assert.equal(chips.length, 1,
    "exactly one chip in the narrative — paired in place by tool_use_id");
  assert.equal(chips[0].classList.contains("success"), true,
    "narrative tool chip carries the ✓ success state from the paired tool_result");
  assert.equal(chips[0].classList.contains("in-flight"), false);
  const panel = findOne(chips[0], "tool-marker-details");
  assert.ok(panel,
    "success chip carries a collapsible detail panel (was missing on bracket-only path)");
  // Proposed Task Description card still renders from the JSON region.
  assert.ok(findOne(wrap, "step-report--proposed-task"),
    "Proposed Task Description card still renders alongside the rich chip");
});

check("makeStructuredAssistantRenderer: narrative renders rich chips when raw_json carries tool pair", () => {
  const blocks = [
    { type: "tool_use", id: "tu_2", name: "Read",
      input: { file_path: "src/bar.py", offset: 0, limit: 200 } },
    { type: "tool_result", tool_use_id: "tu_2",
      content: [{ type: "text", text: "ok" }], is_error: false },
  ];
  const norm = { raw: { raw_json: toolBlocksRawJson(blocks), raw_ndjson: null } };
  // `analyze` flows through `makeStructuredAssistantRenderer`. We pick a result
  // field the analyze STEP_RESULT_FIELDS predicate accepts so a result region
  // is identified (the render-path under test runs only on result turns).
  // We don't pin the exact analyze field set — we ask the registry which keys
  // count for `analyze` and pick the first one with a non-null value.
  const analyzeFields = app.STEP_RESULT_FIELDS && app.STEP_RESULT_FIELDS.analyze;
  assert.ok(Array.isArray(analyzeFields) && analyzeFields.length,
    "STEP_RESULT_FIELDS.analyze is non-empty");
  const result = {};
  // Best-effort sentinel values for common analyze fields; isStepResultDict
  // accepts presence (non-null), not non-empty.
  for (const key of analyzeFields) result[key] = "x";
  const renderer = app.makeStructuredAssistantRenderer("analyze");
  const content =
    "[Read: src/bar.py:0-200]\n\n" +
    "```json\n" + JSON.stringify(result) + "\n```";
  const frag = renderer(content, norm);
  assert.ok(frag, "renderer returns a fragment when an analyze result is present");
  const wrap = document.createElement("div");
  wrap.appendChild(frag);
  const narrative = findOne(wrap, "assistant-narrative");
  assert.ok(narrative, "narrative wrapper rendered above the result");
  const chips = findAll(narrative, "tool-marker");
  assert.equal(chips.length, 1,
    "exactly one chip in the narrative");
  assert.equal(chips[0].classList.contains("success"), true,
    "narrative tool chip is upgraded to success via the chip-events pipeline");
  const panel = findOne(chips[0], "tool-marker-details");
  assert.ok(panel,
    "success chip carries the collapsible detail panel from the chip-events path");
});

check("renderDiscoveryAssistant: narrative falls back to bare bracket chip when raw_json is unavailable", () => {
  // No norm.raw / no raw_json → renderNarrativeNodes must fall back to
  // renderToolMarkers, producing an in-flight bare chip (no detail panel, no
  // success class) — the legacy behavior, preserved for backward compatibility.
  const content =
    "[Read: src/foo.py:0-200]\n\n" +
    "```json\n" +
    JSON.stringify({
      refined_description: "Do the thing",
      questions: ["q1?"],
    }) +
    "\n```";
  for (const norm of [
    {},                          // no raw at all
    { raw: null },               // raw is null
    { raw: { raw_json: null } }, // raw present but raw_json missing
    { raw: { raw_json: "not an array" } },
  ]) {
    const frag = app.renderDiscoveryAssistant(content, norm);
    assert.ok(frag, "renderer still returns a fragment");
    const wrap = document.createElement("div");
    wrap.appendChild(frag);
    const narrative = findOne(wrap, "assistant-narrative");
    assert.ok(narrative, "narrative wrapper still rendered");
    const chips = findAll(narrative, "tool-marker");
    assert.equal(chips.length, 1, "fallback path renders exactly one bracket chip");
    assert.equal(chips[0].classList.contains("in-flight"), true,
      "fallback bracket chip stays in the in-flight state (no terminal upgrade)");
    assert.equal(chips[0].classList.contains("success"), false);
    assert.equal(findOne(chips[0], "tool-marker-details"), null,
      "fallback bracket chip has no detail panel (no tool_use_id pairing)");
  }
});

check("makeStructuredAssistantRenderer: narrative falls back to bare bracket chip when raw_json is unavailable", () => {
  const analyzeFields = app.STEP_RESULT_FIELDS && app.STEP_RESULT_FIELDS.analyze;
  assert.ok(Array.isArray(analyzeFields) && analyzeFields.length);
  const result = {};
  for (const key of analyzeFields) result[key] = "x";
  const renderer = app.makeStructuredAssistantRenderer("analyze");
  const content =
    "[Read: src/bar.py:0-200]\n\n" +
    "```json\n" + JSON.stringify(result) + "\n```";
  const frag = renderer(content, { raw: { raw_json: null } });
  assert.ok(frag);
  const wrap = document.createElement("div");
  wrap.appendChild(frag);
  const narrative = findOne(wrap, "assistant-narrative");
  assert.ok(narrative);
  const chips = findAll(narrative, "tool-marker");
  assert.equal(chips.length, 1);
  assert.equal(chips[0].classList.contains("in-flight"), true,
    "fallback bare bracket chip stays in-flight");
  assert.equal(findOne(chips[0], "tool-marker-details"), null,
    "fallback bare bracket chip has no detail panel");
});

// Production-shape regression: in real history records the assistant message's
// raw_json content is a single `text` block carrying the FULL assistant body
// (narrative prose + the trailing ```json fenced result literal), with NO
// inner tool_use / tool_result blocks (see e.g.
// se3/history/20260518-090652_6f11df31/01_discovery_65ee0848.jsonl). Earlier
// the narrative helper piped chip-event text events through `renderToolMarkers`
// which then re-rendered the embedded ```json fence as a markdown
// `<code class="language-json">` block — duplicating the JSON the structured
// renderer already shows below as Proposed Task Description + Questions.
// The narrative wrapper MUST contain only JSON-stripped prose for this shape.
check("renderDiscoveryAssistant: narrative does NOT duplicate result JSON when raw_json text block carries full body", () => {
  const resultObj = {
    refined_description: "Do the thing",
    questions: ["q1?"],
  };
  const bodyText =
    "Narrative prose explaining the plan.\n" +
    "```json\n" + JSON.stringify(resultObj) + "\n```";
  // Production shape: a single `text` block with no tool_use / tool_result.
  const rawJson = [{ type: "assistant", message: { content: [
    { type: "text", text: bodyText },
  ] } }];
  const norm = { raw: { raw_json: rawJson, raw_ndjson: null } };
  // The `content` arg the structured renderer sees mirrors the raw text body.
  const frag = app.renderDiscoveryAssistant(bodyText, norm);
  assert.ok(frag, "renderer returns a fragment");
  const wrap = document.createElement("div");
  wrap.appendChild(frag);
  const narrative = findOne(wrap, "assistant-narrative");
  assert.ok(narrative, "narrative wrapper renders");
  // Critical: no ```json fence leaks into the narrative as a code block, and
  // no duplicate of the result JSON literal appears.
  const codeBlocks = findAll(narrative, "md-code");
  assert.equal(codeBlocks.length, 0,
    "narrative must NOT carry a fenced code block (md-code)");
  const narrativeText = narrative.textContent || "";
  assert.equal(narrativeText.indexOf("refined_description"), -1,
    "narrative must NOT contain the result JSON's field names");
  // The structured card still renders the result as before.
  assert.ok(findOne(wrap, "step-report--proposed-task"),
    "Proposed Task Description card still renders alongside the clean narrative");
});

check("makeStructuredAssistantRenderer: narrative does NOT duplicate result JSON when raw_json text block carries full body", () => {
  const analyzeFields = app.STEP_RESULT_FIELDS && app.STEP_RESULT_FIELDS.analyze;
  assert.ok(Array.isArray(analyzeFields) && analyzeFields.length);
  const resultObj = {};
  for (const key of analyzeFields) resultObj[key] = "x";
  const bodyText =
    "Some analysis narrative.\n" +
    "```json\n" + JSON.stringify(resultObj) + "\n```";
  const rawJson = [{ type: "assistant", message: { content: [
    { type: "text", text: bodyText },
  ] } }];
  const norm = { raw: { raw_json: rawJson, raw_ndjson: null } };
  const renderer = app.makeStructuredAssistantRenderer("analyze");
  const frag = renderer(bodyText, norm);
  assert.ok(frag);
  const wrap = document.createElement("div");
  wrap.appendChild(frag);
  const narrative = findOne(wrap, "assistant-narrative");
  assert.ok(narrative);
  assert.equal(findAll(narrative, "md-code").length, 0,
    "narrative must NOT carry a fenced code block (md-code)");
  const narrativeText = narrative.textContent || "";
  for (const key of analyzeFields) {
    assert.equal(narrativeText.indexOf(key), -1,
      `narrative must NOT contain the result field name "${key}"`);
  }
});

// ---------------------------------------------------------------------------
// groupHistorySessionsByProjectRoot + pickDefaultHistoryProjectRoot (pure)
// ---------------------------------------------------------------------------
//
// The History view groups its session cards by `project_root`, with a tab bar
// at the top of the list. These two pure helpers do the work the renderer
// builds on:
//   * groupHistorySessionsByProjectRoot folds a flat session list into
//     ordered buckets (recency-desc, UNKNOWN pinned to the tail), preserving
//     the original session order within each bucket.
//   * pickDefaultHistoryProjectRoot resolves "which tab is selected now"
//     across re-renders: preserve the user's choice if it still exists,
//     else fall back to the most-recently-active bucket, else null.

const sess = (project_root, updated_at, extra) => Object.assign(
  { flow_id: "f-" + Math.random().toString(36).slice(2), project_root, updated_at },
  extra || {},
);

check("groupHistorySessionsByProjectRoot returns [] for empty / non-array input", () => {
  assert.deepEqual(app.groupHistorySessionsByProjectRoot([]), []);
  assert.deepEqual(app.groupHistorySessionsByProjectRoot(null), []);
  assert.deepEqual(app.groupHistorySessionsByProjectRoot(undefined), []);
  assert.deepEqual(app.groupHistorySessionsByProjectRoot("nope"), []);
});

check("groupHistorySessionsByProjectRoot folds one project into a single bucket", () => {
  const buckets = app.groupHistorySessionsByProjectRoot([
    sess("/proj/a", 100),
    sess("/proj/a", 200),
  ]);
  assert.equal(buckets.length, 1);
  assert.equal(buckets[0].project_root, "/proj/a");
  assert.equal(buckets[0].label, "/proj/a");
  assert.equal(buckets[0].sessions.length, 2);
});

check("groupHistorySessionsByProjectRoot orders buckets by recency desc", () => {
  // /proj/c is touched most recently (ts=300), then /proj/a (ts=250),
  // then /proj/b (ts=100). Order: c, a, b.
  const buckets = app.groupHistorySessionsByProjectRoot([
    sess("/proj/a", 200),
    sess("/proj/b", 100),
    sess("/proj/c", 300),
    sess("/proj/a", 250),
  ]);
  assert.deepEqual(
    buckets.map((b) => b.project_root),
    ["/proj/c", "/proj/a", "/proj/b"],
  );
});

check("groupHistorySessionsByProjectRoot preserves input order within a bucket", () => {
  const s1 = sess("/proj/a", 100);
  const s2 = sess("/proj/a", 300);
  const s3 = sess("/proj/a", 200);
  const buckets = app.groupHistorySessionsByProjectRoot([s1, s2, s3]);
  assert.equal(buckets.length, 1);
  // bucket.sessions follow the original input order (not re-sorted by ts).
  assert.deepEqual(buckets[0].sessions, [s1, s2, s3]);
});

check("groupHistorySessionsByProjectRoot falls back to created_at when no updated_at", () => {
  // /proj/a has only created_at; /proj/b has updated_at. The bucket with the
  // larger recency wins the top spot.
  const buckets = app.groupHistorySessionsByProjectRoot([
    { project_root: "/proj/a", created_at: 500 },
    { project_root: "/proj/b", updated_at: 100 },
  ]);
  assert.equal(buckets[0].project_root, "/proj/a");
  assert.equal(buckets[1].project_root, "/proj/b");
});

check("groupHistorySessionsByProjectRoot folds falsy project_root into UNKNOWN bucket", () => {
  const buckets = app.groupHistorySessionsByProjectRoot([
    { flow_id: "f1", updated_at: 100 },                      // missing
    { flow_id: "f2", project_root: null, updated_at: 200 },  // null
    { flow_id: "f3", project_root: "", updated_at: 300 },    // empty
    { flow_id: "f4", project_root: undefined, updated_at: 400 },
  ]);
  assert.equal(buckets.length, 1);
  assert.equal(buckets[0].project_root, app.UNKNOWN_PROJECT_ROOT);
  assert.equal(buckets[0].label, "Unknown project");
  assert.equal(buckets[0].sessions.length, 4);
});

check("groupHistorySessionsByProjectRoot pins UNKNOWN bucket to the tail", () => {
  // Even though UNKNOWN has the most recent session (ts=999), it must still
  // sit at the tail of the bucket list.
  const buckets = app.groupHistorySessionsByProjectRoot([
    sess("/proj/a", 100),
    sess("/proj/b", 50),
    { flow_id: "fx", updated_at: 999 },          // UNKNOWN
  ]);
  assert.deepEqual(
    buckets.map((b) => b.project_root),
    ["/proj/a", "/proj/b", app.UNKNOWN_PROJECT_ROOT],
  );
});

check("groupHistorySessionsByProjectRoot omits UNKNOWN bucket when no falsy sessions", () => {
  const buckets = app.groupHistorySessionsByProjectRoot([
    sess("/proj/a", 100),
    sess("/proj/b", 200),
  ]);
  assert.equal(buckets.some((b) => b.project_root === app.UNKNOWN_PROJECT_ROOT), false);
});

check("groupHistorySessionsByProjectRoot exposes a numeric latestTs per bucket", () => {
  const buckets = app.groupHistorySessionsByProjectRoot([
    sess("/proj/a", 100),
    sess("/proj/a", 250),
    sess("/proj/a", 200),
  ]);
  assert.equal(buckets[0].project_root, "/proj/a");
  assert.equal(typeof buckets[0].latestTs, "number");
  // tsValue(250) — epoch seconds < 1e12 are scaled to ms.
  assert.equal(buckets[0].latestTs, app.tsValue(250));
});

// -- pickDefaultHistoryProjectRoot -----------------------------------------

check("pickDefaultHistoryProjectRoot returns null for empty / non-array buckets", () => {
  assert.equal(app.pickDefaultHistoryProjectRoot([], null), null);
  assert.equal(app.pickDefaultHistoryProjectRoot([], "/proj/a"), null);
  assert.equal(app.pickDefaultHistoryProjectRoot(null, "/proj/a"), null);
  assert.equal(app.pickDefaultHistoryProjectRoot(undefined, "/proj/a"), null);
});

check("pickDefaultHistoryProjectRoot preserves currentSelected when still present", () => {
  const buckets = [
    { project_root: "/proj/a" },
    { project_root: "/proj/b" },
  ];
  assert.equal(app.pickDefaultHistoryProjectRoot(buckets, "/proj/b"), "/proj/b");
});

check("pickDefaultHistoryProjectRoot falls back to buckets[0] when currentSelected vanished", () => {
  const buckets = [
    { project_root: "/proj/a" },
    { project_root: "/proj/b" },
  ];
  // The selected key no longer exists in the bucket list — fall back to the
  // first (most-recently-active) bucket rather than leaving a dead reference.
  assert.equal(app.pickDefaultHistoryProjectRoot(buckets, "/proj/gone"), "/proj/a");
});

check("pickDefaultHistoryProjectRoot falls back to buckets[0] when currentSelected is null", () => {
  const buckets = [{ project_root: "/proj/a" }, { project_root: "/proj/b" }];
  assert.equal(app.pickDefaultHistoryProjectRoot(buckets, null), "/proj/a");
  assert.equal(app.pickDefaultHistoryProjectRoot(buckets, undefined), "/proj/a");
});

// -- renderHistoryList: project tabs + filtering ----------------------------
// Helper to install a fresh #history-list container and reset the bits of
// shared state these tests touch, so ordering doesn't leak between cases.
function resetHistoryListFixture() {
  _elementsById["history-list"] = new FakeNode("div");
  app.state.historySessions = [];
  app.state.historyIndexLoading = false;
  app.state.historyIndexConfirmed = true;
  app.state.machines = [{ machine_id: "m1", online: true }];
  app.state.historySelectedProjectRoot = null;
  app.state.selectedHistoryId = null;
}

// Helper: collect the <option> children of a history-project-select dropdown.
function projectSelectOptions(list) {
  const select = findOne(list, "history-project-select");
  if (!select) return [];
  return select.children.filter((c) => c.tagName === "OPTION");
}

check("renderHistoryList: multi-project renders select dropdown with first bucket as default", () => {
  resetHistoryListFixture();
  app.state.historySessions = [
    { flow_id: "f1", task_description: "newer A", project_root: "/proj/a",
      updated_at: 200 },
    { flow_id: "f2", task_description: "older B", project_root: "/proj/b",
      updated_at: 100 },
  ];
  app.renderHistoryList();
  const list = document.getElementById("history-list");
  const select = findOne(list, "history-project-select");
  assert.ok(select, "select dropdown should render with >= 2 buckets");
  const options = projectSelectOptions(list);
  assert.equal(options.length, 2, "one option per bucket");
  // /proj/a is more recent so it ranks first and is the default selection.
  assert.equal(options[0].value, "/proj/a");
  assert.equal(options[1].value, "/proj/b");
  assert.equal(select.value, "/proj/a");
  // Only the selected bucket's card is rendered.
  const items = findAll(list, "history-item");
  assert.equal(items.length, 1);
  assert.ok(items[0].textContent.includes("newer A"));
  assert.equal(app.state.historySelectedProjectRoot, "/proj/a");
});

check("renderHistoryList: changing the select value swaps the visible cards", () => {
  resetHistoryListFixture();
  app.state.historySessions = [
    { flow_id: "f1", task_description: "card A", project_root: "/proj/a",
      updated_at: 200 },
    { flow_id: "f2", task_description: "card B", project_root: "/proj/b",
      updated_at: 100 },
  ];
  app.renderHistoryList();
  const list = document.getElementById("history-list");
  const select = findOne(list, "history-project-select");
  assert.ok(select, "expected a history-project-select to change");
  select.value = "/proj/b";
  select.dispatch("change");
  // After change, only /proj/b's card is visible and the select reflects it.
  const list2 = document.getElementById("history-list");
  const items = findAll(list2, "history-item");
  assert.equal(items.length, 1);
  assert.ok(items[0].textContent.includes("card B"));
  const select2 = findOne(list2, "history-project-select");
  assert.equal(select2.value, "/proj/b");
  assert.equal(app.state.historySelectedProjectRoot, "/proj/b");
});

check("renderHistoryList: UNKNOWN option appears only when a session lacks project_root", () => {
  resetHistoryListFixture();
  // No falsy project_root sessions: no UNKNOWN option.
  app.state.historySessions = [
    { flow_id: "f1", project_root: "/proj/a", updated_at: 200 },
    { flow_id: "f2", project_root: "/proj/b", updated_at: 100 },
  ];
  app.renderHistoryList();
  let list = document.getElementById("history-list");
  const optsNoUnknown = projectSelectOptions(list).map((o) => o.value);
  assert.ok(!optsNoUnknown.includes(app.UNKNOWN_PROJECT_ROOT),
    "UNKNOWN option must not appear when every session has a project_root");

  // Now add a legacy session with no project_root — the UNKNOWN option appears,
  // labeled 未知项目, and is pinned to the tail.
  resetHistoryListFixture();
  app.state.historySessions = [
    { flow_id: "f1", project_root: "/proj/a", updated_at: 200 },
    { flow_id: "f2", project_root: "/proj/b", updated_at: 100 },
    { flow_id: "f3", updated_at: 50 },
  ];
  app.renderHistoryList();
  list = document.getElementById("history-list");
  const opts = projectSelectOptions(list);
  assert.equal(opts.length, 3);
  assert.equal(opts[opts.length - 1].value, app.UNKNOWN_PROJECT_ROOT);
  assert.equal(opts[opts.length - 1].textContent,
    app.UNKNOWN_PROJECT_ROOT_LABEL);
});

check("renderHistoryList: single project does not render the select dropdown", () => {
  resetHistoryListFixture();
  app.state.historySessions = [
    { flow_id: "f1", task_description: "only one", project_root: "/proj/a",
      updated_at: 200 },
  ];
  app.renderHistoryList();
  const list = document.getElementById("history-list");
  assert.equal(findOne(list, "history-project-select"), null,
    "select dropdown must be suppressed with a single bucket");
  // The lone session still renders.
  assert.equal(findAll(list, "history-item").length, 1);
});

check("pickDefaultHistoryProjectRoot preserves UNKNOWN selection when bucket exists", () => {
  // The user explicitly switched to the 未知项目 tab — re-renders must not
  // bump them off it just because a real project bucket is more recent.
  const buckets = [
    { project_root: "/proj/a" },
    { project_root: app.UNKNOWN_PROJECT_ROOT },
  ];
  assert.equal(
    app.pickDefaultHistoryProjectRoot(buckets, app.UNKNOWN_PROJECT_ROOT),
    app.UNKNOWN_PROJECT_ROOT,
  );
});

// -- collectJsonRegions / extractResultJson: embedded-fence guard ----------
// Regression guard for the bug where a bare JSON object whose string field
// values embed markdown code fences caused `collectJsonRegions` to push
// `lastFenceEnd` past the bare object's start, which in turn made
// `extractTrailingBareJson` discard the bare object as "already inside a
// fenced block". The fix: only fences whose body actually parsed as JSON
// shift the trailing-bare guard.

// Mirror of the per-step result-field predicate the discovery renderer uses;
// duplicated here so the test does not depend on the renderer's own
// `isDiscoveryResultDict` (which it does, but the rule is the predicate's
// shape: presence of any result key with a non-null value counts).
const isDiscoveryResultLike = (v) =>
  !!v && typeof v === "object" && !Array.isArray(v) && (
    v.content != null ||
    v.refined_description != null ||
    (Array.isArray(v.questions) && v.questions.length > 0)
  );

check("collectJsonRegions: bare JSON with embedded markdown fence in a string value", () => {
  // Real-world synthesis sample shape: a bare JSON object (no outer
  // ```json``` wrapper) whose `content` string value carries an embedded
  // markdown code fence (prose, not JSON). The embedded fence MUST NOT
  // shift `lastFenceEnd` past the bare object's start, otherwise
  // extractTrailingBareJson is dropped and `collectJsonRegions` returns [].
  const text = '{\n  "mode": "synthesis",\n  "content": "intro\\n\\n```\\nuser-facing prose code block, not JSON\\n```\\n",\n  "refined_description": "Generate a square 1024x1024 app icon.",\n  "questions": []\n}';
  const regions = app.collectJsonRegions(text);
  assert.ok(regions.length >= 1, "expected at least one region for the bare JSON object");
  // The chosen region must be the bare object, carrying the result fields.
  const bareRegion = regions[regions.length - 1];
  assert.equal(typeof bareRegion.value, "object");
  assert.equal(bareRegion.value.mode, "synthesis");
  assert.equal(bareRegion.value.refined_description.startsWith("Generate a square"), true);

  // And the discovery result predicate must select it.
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got, "extractResultJson must return a result, not null");
  assert.equal(got.value.refined_description.startsWith("Generate a square"), true);
});

check("collectJsonRegions: pure ```json``` fence with no embedded fence still parses", () => {
  // Regression baseline: the common "well-formed ```json``` envelope" path
  // must keep working — exactly one region whose indices wrap the fence,
  // and `extractResultJson` produces a clean empty narrative.
  const text = '```json\n{"content": "hi", "questions": []}\n```';
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1);
  assert.equal(regions[0].startIndex, 0);
  assert.equal(regions[0].endIndex, text.length);

  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got);
  assert.equal(got.value.content, "hi");
  assert.equal(got.narrative, "");
});

check("collectJsonRegions: multiple JSON segments — tool-call(s) + final result", () => {
  // A single turn carrying two ```json``` fences: the first is a Bash
  // tool-call JSON (no result fields), the second is the discovery result.
  // `extractResultJson` must pick the last region satisfying the result
  // predicate and the narrative must have BOTH regions removed.
  const text = [
    "Looking up the project.",
    "```json",
    '{"command": "ls", "description": "list files"}',
    "```",
    "Now drafting the proposal:",
    "```json",
    '{"content": "draft", "refined_description": "do X", "questions": []}',
    "```",
    "trailing line",
  ].join("\n");

  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 2, "expected two JSON regions");
  // Tool-call region does NOT satisfy the discovery result predicate.
  assert.equal(isDiscoveryResultLike(regions[0].value), false);
  assert.equal(isDiscoveryResultLike(regions[1].value), true);

  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got, "must pick the second (result) region");
  assert.equal(got.value.refined_description, "do X");
  // Narrative has BOTH JSON regions removed — no tool-call JSON leaks
  // into the visible Layer-1 view.
  assert.equal(got.narrative.includes("```json"), false);
  assert.equal(got.narrative.includes('"command"'), false);
  assert.equal(got.narrative.includes('"refined_description"'), false);
  assert.equal(got.narrative.includes("Looking up the project."), true);
  assert.equal(got.narrative.includes("Now drafting the proposal:"), true);
  assert.equal(got.narrative.includes("trailing line"), true);
});

// -- collectJsonRegions / extractResultJson: structural robustness ---------
// Regression guards for the structural shapes enumerated in the bugfix task
// for `collectJsonRegions`: any assistant text that ends with a legal result
// JSON must yield a region whose value is selected by the discovery
// predicate, regardless of (a) whether the JSON is fence-wrapped, (b) what
// other ``` / ` references the prose contains, or (c) how many tool-call
// JSON segments precede it. Pure-prose input must yield zero regions and a
// null result without raising.

check("collectJsonRegions: trailing bare JSON, no outer fence, prose in front", () => {
  const text = [
    "Looking up the discovery context, then drafting the proposal.",
    "",
    '{"content": "draft markdown", "refined_description": "do thing", "questions": []}',
  ].join("\n");
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1);
  assert.equal(regions[0].value.refined_description, "do thing");
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got);
  assert.equal(got.value.refined_description, "do thing");
  assert.equal(got.narrative.includes("refined_description"), false);
  assert.equal(got.narrative.includes("```json"), false);
  assert.equal(got.narrative.includes("Looking up the discovery context"), true);
});

check("collectJsonRegions: ```json wrapped JSON with prose in front", () => {
  const text = [
    "Here is the proposed task description.",
    "```json",
    '{"content": "x", "refined_description": "y", "questions": ["q1"]}',
    "```",
  ].join("\n");
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1);
  assert.equal(regions[0].startIndex, text.indexOf("```json"));
  assert.equal(regions[0].endIndex, text.length);
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got);
  assert.equal(got.value.refined_description, "y");
  assert.equal(got.narrative, "Here is the proposed task description.");
  assert.equal(got.narrative.includes("```json"), false);
  assert.equal(got.narrative.endsWith("```"), false);
});

check("collectJsonRegions: prose with non-JSON markdown code fence + trailing JSON", () => {
  // Prose contains a fenced code block whose body is plain shell prose
  // (NOT JSON); a real result JSON follows at the end inside its own
  // ```json fence. Earlier impl was vulnerable to fences whose body did
  // not parse pushing the trailing-bare guard past the real region.
  const text = [
    "First I will list the directory:",
    "```",
    "ls -la /tmp",
    "```",
    "Then propose the fix.",
    "```json",
    '{"content": "fix steps", "refined_description": "fix the bug", "questions": []}',
    "```",
  ].join("\n");
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1, "non-JSON ``` block must not register as a region");
  assert.equal(regions[0].value.refined_description, "fix the bug");
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got);
  assert.equal(got.value.refined_description, "fix the bug");
  // The non-JSON ``` block remains in the narrative (it is real prose);
  // only the result fence is excised.
  assert.equal(got.narrative.includes("```json"), false);
  assert.equal(got.narrative.includes("refined_description"), false);
  assert.equal(got.narrative.includes("ls -la /tmp"), true);
});

check("collectJsonRegions: ```json wrapped JSON whose content embeds literal triple backticks", () => {
  // The exact new-bug-session trigger: a fenced ```json block whose JSON
  // object has a `content` string field literally embedding ``` triple
  // backticks (prose, not JSON). A fence-regex scanner truncates the
  // body at the first inner ```; a string-state-aware scanner does not.
  const inner = '{"mode": "synthesis", "content": "see ```\\nlike-this\\n``` for an example", ' +
    '"refined_description": "do thing", "questions": []}';
  const text = "Prelude prose.\n```json\n" + inner + "\n```";
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1, "string-state-aware scanner must collect the fenced region");
  assert.equal(regions[0].value.refined_description, "do thing");
  assert.equal(regions[0].value.content.includes("```"), true,
    "inner content must preserve the embedded triple backticks");
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got, "extractResultJson must not return null on the new-bug shape");
  assert.equal(got.value.refined_description, "do thing");
  assert.equal(got.narrative, "Prelude prose.");
  assert.equal(got.narrative.includes("```json"), false);
  assert.equal(got.narrative.includes("```"), false);
});

check("collectJsonRegions: bare JSON whose content embeds literal triple backticks", () => {
  // Variant of the new-bug shape: same JSON object content but emitted as
  // BARE JSON (no outer ```json wrapper). The trailing-bare detection
  // must still register the region, and the embedded ``` inside the
  // string value must not split the scan.
  const text = '{\n  "mode": "synthesis",\n  "content": "intro\\n\\n```\\nuser-facing prose, not JSON\\n```\\nend",\n  "refined_description": "do bare thing",\n  "questions": []\n}';
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1);
  assert.equal(regions[0].value.refined_description, "do bare thing");
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got);
  assert.equal(got.value.refined_description, "do bare thing");
  assert.equal(got.narrative, "");
});

check("collectJsonRegions: prose with inline ` and unpaired ``` + trailing JSON", () => {
  // Prose carries inline single backticks (e.g. `varName`) and an unpaired
  // ``` string reference (a stray triple-backtick mentioned in text but
  // not actually opening a fence). The trailing bare JSON must still
  // register; neither the inline backticks nor the stray triple-backtick
  // should derail the scanner.
  const text = [
    "Discussion: `parse_json_response` differs from frontend `collectJsonRegions`.",
    "Note also that ``` is not always balanced in prose.",
    "Here is the final result:",
    "",
    '{"content": "final", "refined_description": "ship it", "questions": []}',
  ].join("\n");
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1);
  assert.equal(regions[0].value.refined_description, "ship it");
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got);
  assert.equal(got.value.refined_description, "ship it");
  assert.equal(got.narrative.includes("refined_description"), false);
  assert.equal(got.narrative.includes("parse_json_response"), true);
});

check("collectJsonRegions: two tool-call JSON fences + one trailing result fence", () => {
  // Three JSON segments in one turn: two tool-call fences (Bash + Edit
  // arguments) followed by the discovery result. Tool calls do NOT carry
  // discovery result fields; the predicate must pick the last fence.
  const text = [
    "Plan: list directory, edit file, then propose.",
    "```json",
    '{"command": "ls /tmp", "description": "list /tmp"}',
    "```",
    "Now edit:",
    "```json",
    '{"file_path": "/tmp/x", "old_string": "a", "new_string": "b"}',
    "```",
    "Done. Proposal:",
    "```json",
    '{"content": "proposal text", "refined_description": "do many things", "questions": []}',
    "```",
  ].join("\n");
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 3, "expected three fenced JSON regions");
  assert.equal(isDiscoveryResultLike(regions[0].value), false);
  assert.equal(isDiscoveryResultLike(regions[1].value), false);
  assert.equal(isDiscoveryResultLike(regions[2].value), true);
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got, "must pick the last fence as the discovery result");
  assert.equal(got.value.refined_description, "do many things");
  // Every JSON region — both tool-call fences and the result fence —
  // must be excised so intermediate tool-call JSON never leaks into the
  // visible narrative.
  assert.equal(got.narrative.includes("```json"), false);
  assert.equal(got.narrative.includes('"command"'), false);
  assert.equal(got.narrative.includes('"file_path"'), false);
  assert.equal(got.narrative.includes('"refined_description"'), false);
  assert.equal(got.narrative.includes("Plan: list directory"), true);
  assert.equal(got.narrative.includes("Now edit:"), true);
  assert.equal(got.narrative.includes("Done. Proposal:"), true);
});

check("collectJsonRegions: pure prose returns [] and extractResultJson returns null", () => {
  // No JSON at all — the helper must return an empty regions array, and
  // extractResultJson must return null without raising, so the caller
  // falls back to the renderToolMarkers + markdown path per
  // running-flow-console *Three-Tier Progressive Disclosure*.
  const text = "Just some thoughts about the work, no JSON here.\n\nReally, none.";
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 0);
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.equal(got, null);
});

check("collectJsonRegions: real fixture from new bug session (record 36 shape)", () => {
  // Mirror of the new-bug-session record 36 final assistant content shape:
  // a short prelude carrying a tool marker, then a ```json fence whose
  // JSON object's content/refined_description fields embed real backticks
  // and prose. This is the concrete shape the previous regex-based
  // collector silently dropped.
  const innerJson = JSON.stringify({
    mode: "synthesis",
    content: "CLI `_extract_json_string` uses `text.find('{')` -> `text.rfind('}')`; " +
      "frontend `collectJsonRegions` instead collects all top-level regions. " +
      "Sample inline: ```not-a-real-fence```. End.",
    refined_description: "fix se3 web UI assistant rendering structural robustness bug",
    questions: [],
  });
  const text = "[Read: src/se3/server/static/app.js:3380-3500]\n```json\n" +
    innerJson + "\n```";
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1, "real-shape fixture must yield exactly one region");
  assert.equal(typeof regions[0].value.refined_description, "string");
  assert.equal(regions[0].value.refined_description.length > 0, true,
    "refined_description must be a non-empty string");
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got, "extractResultJson must select the result on real-shape fixture");
  assert.equal(got.value.refined_description.startsWith("fix se3 web UI"), true);
  assert.equal(got.narrative.includes("```json"), false);
  assert.equal(got.narrative.includes("refined_description"), false);
  assert.equal(got.narrative.endsWith("```"), false);
  assert.equal(got.narrative.startsWith("[Read:"), true);
});

// -- collectJsonRegions: BARE JSON + trailing non-whitespace text ----------
// G1 regression: the dropped shape pinned by reproducing a post-10.0
// ClaudeRunner discovery record. A BARE JSON object (NO outer ```json fence)
// followed by further non-whitespace text — a trailing prose tail, a second
// narrative paragraph, or another payload block — matched none of the old
// registration gate's beforeMatch / afterMatch / isTrailing conditions, so
// the region was never registered: collectJsonRegions returned [],
// extractResultJson returned null, renderDiscoveryAssistant returned null,
// and content/refined_description/questions vanished (only the thinking
// narrative survived via renderAssistantProcessInline). The fix relaxes the
// gate with a "substantive object" criterion (non-array object with >=1 key).

check("collectJsonRegions: bare JSON + trailing prose tail registers and extracts", () => {
  const text = [
    "Here is my proposed task description.",
    '{"content": "do x", "refined_description": "fix the bug", "questions": ["q1"]}',
    "Let me know if this works for you.",
  ].join("\n");
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1, "bare JSON + trailing non-whitespace must register");
  assert.equal(regions[0].value.refined_description, "fix the bug");
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got, "extractResultJson must select the bare result, not null");
  assert.equal(got.value.refined_description, "fix the bug");
  // Narrative keeps the surrounding prose (head AND trailing tail) but excises
  // the JSON region so it never leaks into the Layer-1 view.
  assert.equal(got.narrative.includes("refined_description"), false);
  assert.equal(got.narrative.includes("Here is my proposed task description."), true);
  assert.equal(got.narrative.includes("Let me know if this works for you."), true);
});

check("collectJsonRegions: bare JSON followed by a second narrative paragraph", () => {
  // Multi-paragraph trailing prose after the bare result (no fence anywhere).
  const text = [
    "Investigation complete; here is the proposal.",
    '{"content": "summary", "refined_description": "do the thing", "questions": []}',
    "",
    "I considered alternatives but settled on this.",
    "Ready to proceed when you confirm.",
  ].join("\n");
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1);
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got);
  assert.equal(got.value.refined_description, "do the thing");
  assert.equal(got.narrative.includes("Investigation complete"), true);
  assert.equal(got.narrative.includes("considered alternatives"), true);
  assert.equal(got.narrative.includes("Ready to proceed"), true);
  assert.equal(got.narrative.includes("refined_description"), false);
});

check("collectJsonRegions: stray bare array / empty object + trailing text stays unregistered", () => {
  // The substantive-object criterion must NOT pull stray prose fragments into
  // the region set, or they would be wrongly excised from the narrative.
  assert.equal(app.collectJsonRegions("See item [0] then continue reading.").length, 0,
    "stray [0] must not register");
  assert.equal(app.collectJsonRegions("nums " + JSON.stringify([1, 2, 3]) + " more").length, 0,
    "bare array + trailing text must not register");
  assert.equal(app.collectJsonRegions("an empty {} placeholder follows.").length, 0,
    "empty object + trailing text must not register");
});

check("collectJsonRegions: multi-block + interleaved tool_use/tool_result + trailing bare result", () => {
  // The post-10.0 streaming shape: a multi-block assistant message whose text
  // blocks interleave tool_use / tool_result, ending in a text block that
  // carries a BARE result JSON followed by a trailing prose line. The pipeline
  // (extractAssistantText assembly -> collectJsonRegions -> extractResultJson)
  // must surface the result and keep every tool marker + prose in the
  // narrative while excising the JSON.
  const raw = [
    { type: "assistant", message: { role: "assistant", content: [
      { type: "text", text: "Let me read the file first." },
      { type: "tool_use", id: "tu1", name: "Read", input: { file_path: "/tmp/x" } },
    ] } },
    { type: "user", message: { role: "user", content: [
      { type: "tool_result", tool_use_id: "tu1", content: "file body" },
    ] } },
    { type: "assistant", message: { role: "assistant", content: [
      { type: "text", text: "Checking git history too." },
      { type: "tool_use", id: "tu2", name: "Bash", input: { command: "git log" } },
    ] } },
    { type: "user", message: { role: "user", content: [
      { type: "tool_result", tool_use_id: "tu2", content: "abc123 commit" },
    ] } },
    { type: "assistant", message: { role: "assistant", content: [
      { type: "text", text: "Now drafting the proposal:\n" +
        '{"content": "draft", "refined_description": "do many things", "questions": ["q1"]}' +
        "\nThanks for your patience." },
    ] } },
  ];
  const text = app.extractAssistantText(raw);
  // Multi-block assembly: tool_use markers present, tool_result NOT re-emitted
  // as a zombie bracket (paired by the chip state machine downstream).
  assert.equal(text.includes("[Read"), true);
  assert.equal(text.includes("[Bash"), true);
  assert.equal(text.includes("file body"), false);
  assert.equal(text.includes("abc123 commit"), false);

  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1, "the trailing bare result must register");
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got, "extractResultJson must pick the final result region");
  assert.equal(got.value.refined_description, "do many things");
  // Narrative retains both text fragments and both tool markers, JSON excised.
  assert.equal(got.narrative.includes("Let me read the file first."), true);
  assert.equal(got.narrative.includes("Checking git history too."), true);
  assert.equal(got.narrative.includes("Now drafting the proposal:"), true);
  assert.equal(got.narrative.includes("Thanks for your patience."), true);
  assert.equal(got.narrative.includes("[Read"), true);
  assert.equal(got.narrative.includes("[Bash"), true);
  assert.equal(got.narrative.includes("refined_description"), false);
});

check("collectJsonRegions: inline tool-marker JSON is NOT registered (block-start guard)", () => {
  // The substantive-object relaxation must not register JSON embedded INLINE
  // inside a tool marker (`[Read: {"file_path": "…"}]`), whose `{` is preceded
  // by `[Read: ` mid-line — registering it would excise the marker detail from
  // the narrative. Only the block-level trailing bare result registers.
  const text = "Reading the file.\n[Read: " +
    '{"file_path": "/tmp/x", "offset": 0}' + "]\nProposal follows.\n" +
    '{"content": "c", "refined_description": "do X", "questions": []}' +
    "\nThat is all.";
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 1, "only the block-level bare result registers");
  assert.equal(regions[0].value.refined_description, "do X");
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got);
  assert.equal(got.value.refined_description, "do X");
  // The inline tool-marker JSON stays intact in the narrative.
  assert.equal(got.narrative.includes('[Read: {"file_path": "/tmp/x", "offset": 0}]'), true);
  assert.equal(got.narrative.includes("refined_description"), false);
});

check("collectJsonRegions: tool-call JSON (bare) before bare result picks the result", () => {
  // A bare tool-call JSON object (no result fields) precedes the bare result,
  // both followed by trailing prose. The tool-call object now registers too
  // (substantive object) and is excised from the narrative, while the result
  // predicate selects the keyed result object.
  const text = [
    "Plan: inspect then propose.",
    '{"command": "ls -la", "description": "list files"}',
    "Having looked, here is the result:",
    '{"content": "c", "refined_description": "do X", "questions": []}',
    "End of message.",
  ].join("\n");
  const regions = app.collectJsonRegions(text);
  assert.equal(regions.length, 2, "both bare objects register");
  assert.equal(isDiscoveryResultLike(regions[0].value), false);
  assert.equal(isDiscoveryResultLike(regions[1].value), true);
  const got = app.extractResultJson(text, isDiscoveryResultLike);
  assert.ok(got);
  assert.equal(got.value.refined_description, "do X");
  // BOTH JSON regions excised — no intermediate tool-call JSON leaks.
  assert.equal(got.narrative.includes('"command"'), false);
  assert.equal(got.narrative.includes("refined_description"), false);
  assert.equal(got.narrative.includes("Plan: inspect then propose."), true);
  assert.equal(got.narrative.includes("End of message."), true);
});

// ---------------------------------------------------------------------------
// G4: dedicated test coverage for the three webui rendering fixes
// ---------------------------------------------------------------------------
//
// G4 is the pure-logic test group for the bundle of fixes implemented in G1/G2/G3.
// Each check below pins one of the three acceptance criteria with explicit DOM
// assertions through the public render entry points — confirm assistant fallback,
// renderStepReport plan field expansion, and the tool-chip toggle DOM order.

// G4(a): a confirm step assistant record carrying a ```json fence outputs dict
// MUST surface field-by-field kv rows (Q1 generic fallback), not a raw markdown
// ```json``` code block. Goes through renderConversationRecord — the same entry
// point real history records pass through.
check("G4(a): confirm assistant outputs render as kv rows via renderConversationRecord", () => {
  const outputs = { decision: "approved", note: "looks good" };
  const content = "Reviewing.\n```json\n" + JSON.stringify(outputs) + "\n```";
  const row = app.renderConversationRecord(asstNorm(content, "confirm"));
  const result = findOne(row, "assistant-result");
  assert.ok(result, "confirm fallback wraps outputs in .assistant-result");
  assert.ok(result.classList.contains("assistant-result--generic"),
    "generic-fallback variant marker present on the wrapper");
  // No raw ```json``` markdown code block leaks under the bubble.
  assert.equal(findOne(row, "md-code"), null,
    "outputs must not render as a raw ```json``` markdown code block");
  // Field-style kv rows exist for each outputs key.
  const keys = findAll(result, "step-report__kv-k").map((n) => n.textContent);
  assert.ok(keys.includes("decision"), "decision field surfaced as kv row");
  assert.ok(keys.includes("note"), "note field surfaced as kv row");
});

// G4(b): a plan step_completed record whose outputs.plan.proposal carries
// summary / files_to_modify MUST render those as independent field sections
// via renderStepReport, never as a single `pre.step-report__json` blob.
check("G4(b): renderStepReport for plan step surfaces proposal fields, not a json pre", () => {
  const step = {
    step_id: "04_plan_abc",
    step_type: "plan",
    status: "completed",
    outputs: {
      plan: {
        proposal: {
          summary: "Restructure the renderer.",
          files_to_modify: [
            { path: "src/x.py", reason: "wire the new field" },
            { path: "src/y.py", reason: "thread context" },
          ],
        },
      },
      task_groups: [
        { group_id: "G1", name: "core", tasks: [{ estimated_loc: 10 }], depends_on: [] },
      ],
    },
  };
  const card = app.renderStepReport(step);
  assert.ok(card, "renderStepReport returns a card for a plan step");
  // No single `pre.step-report__json` blob in the rendered report.
  assert.equal(findOne(card, "step-report__json"), null,
    "plan report must not dump the proposal/design as a raw JSON pre");
  // Independent field-section titles for the proposal fields present.
  const titles = findAll(card, "step-report__section-title").map((n) => n.textContent);
  assert.ok(titles.includes("Summary"),
    "independent Summary field section present");
  assert.ok(titles.some((t) => t.startsWith("Files to Modify")),
    "independent Files to Modify field section present");
  // Per-item dict fields expanded so each file path/reason is reachable.
  const text = card.textContent;
  assert.ok(text.includes("src/x.py"));
  assert.ok(text.includes("wire the new field"));
});

// G4(c): the tool-chip details toggle must be a direct child of the chip
// (right-aligned via CSS `margin-left:auto`) and must come BEFORE the
// `.tool-marker-details` panel. When `attachChipDetail` (here driven via
// `upgradeChipToSuccess`) is invoked with no detail, no toggle is created.
check("G4(c): createInFlightChip + upgradeChipToSuccess place toggle before .tool-marker-details; null detail → no toggle", () => {
  // Case 1: with a non-empty detail, toggle + panel appear as direct chip
  // children in the right DOM order.
  const chip = app.createInFlightChip("Read", "src/foo.py:0-200");
  assert.equal(chip.classList.contains("in-flight"), true);
  // No toggle on the in-flight chip yet (no detail attached).
  assert.equal(chip.children.find(
    (c) => c.classList && c.classList.contains("tool-marker-toggle")), undefined,
    "in-flight chip has no toggle before any detail is attached");

  app.upgradeChipToSuccess(chip, "src/foo.py:0-200 · 3 lines", {
    kind: "read_text", file_path: "src/foo.py",
    text: "a\nb\nc", start_line: 1, truncated: false,
  });
  assert.equal(chip.classList.contains("success"), true);
  const toggle = chip.children.find(
    (c) => c.classList && c.classList.contains("tool-marker-toggle"));
  const panel = chip.children.find(
    (c) => c.classList && c.classList.contains("tool-marker-details"));
  assert.ok(toggle, "toggle is a direct chip child after upgrade");
  assert.ok(panel, "details panel is a direct chip child after upgrade");
  const toggleIdx = chip.children.indexOf(toggle);
  const panelIdx = chip.children.indexOf(panel);
  assert.ok(toggleIdx < panelIdx,
    `toggle must precede .tool-marker-details, got toggle=${toggleIdx} panel=${panelIdx}`);
  // Legacy nested toggle class must not appear.
  assert.equal(findOne(chip, "tool-marker-details-toggle"), null,
    "old .tool-marker-details-toggle nested-inside-panel layout is gone");
  // Toggle is NOT nested inside the panel.
  assert.equal(findOne(panel, "tool-marker-toggle"), null,
    "toggle is not nested inside .tool-marker-details");

  // Case 2: a fresh chip whose upgrade carries a null detail must NOT gain a
  // toggle — attachChipDetail's early-return path keeps the chip head clean.
  const chip2 = app.createInFlightChip("Read", "src/bar.py");
  app.upgradeChipToSuccess(chip2, "src/bar.py · 0 lines", null);
  assert.equal(chip2.children.find(
    (c) => c.classList && c.classList.contains("tool-marker-toggle")), undefined,
    "no toggle when attachChipDetail is given a null detail");
  assert.equal(chip2.children.find(
    (c) => c.classList && c.classList.contains("tool-marker-details")), undefined,
    "no details panel when attachChipDetail is given a null detail");
});

// ---------------------------------------------------------------------------
// G4: local interjection lifecycle helpers
// ---------------------------------------------------------------------------
//
// computeInterventions now folds three sources together: real pending calls,
// frontend-tracked `localInterjections` entries (one per submitted synthetic
// interjection), and the standby synthetic chip while the user is drafting.
// bindLocalInterjectionToCallId binds the oldest unmatched local entry to a
// just-pending real call_id (FIFO); consumeLocalInterjectionByCallId removes
// the matching local entry when the consumed event fires.
function resetG4State() {
  app.state.localInterjections = [];
  app.state.interjectionPhases = {};
  app.state.interjectionEventSeen = {};
  app.state.flowInterjectRequested = false;
  app.state.selectedFlowId = null;
  app.state.flowDetail = null;
  app.state.pendingSendSettleKey = null;
}

check("G4 local entries render with state-pending phase", () => {
  resetG4State();
  app.state.localInterjections.push({
    localId: 1, text: "first", callId: null, phase: "pending", submittedAt: 0,
  });
  const entries = app.computeInterventions({
    status: "running", pending_calls: [],
  });
  // One synthetic chip from the localInterjection entry.
  assert.equal(entries.length, 1);
  assert.equal(entries[0].kind, "interjection");
  assert.equal(entries[0].synthetic, true);
  assert.equal(entries[0].localId, 1);
  assert.equal(entries[0].phase, "pending");
  resetG4State();
});

check("G4 standby + local entries coexist (multi-interjection)", () => {
  resetG4State();
  app.state.localInterjections.push({
    localId: 1, text: "first", callId: null, phase: "pending", submittedAt: 0,
  });
  app.state.flowInterjectRequested = true;
  const entries = app.computeInterventions({
    status: "running", pending_calls: [],
  });
  // local entry + standby chip.
  assert.equal(entries.length, 2);
  // Standby chip is last and has no localId.
  assert.equal(entries[1].id, "interjection:new");
  assert.equal(entries[1].localId, null);
  resetG4State();
});

check("G4 bindLocalInterjectionToCallId binds FIFO", () => {
  resetG4State();
  app.state.localInterjections.push(
    { localId: 1, text: "a", callId: null, phase: "pending", submittedAt: 1 },
    { localId: 2, text: "b", callId: null, phase: "pending", submittedAt: 2 },
  );
  app.bindLocalInterjectionToCallId("call-A");
  assert.equal(app.state.localInterjections[0].callId, "call-A");
  assert.equal(app.state.localInterjections[1].callId, null);
  // Binding the same call_id twice is a no-op.
  app.bindLocalInterjectionToCallId("call-A");
  assert.equal(app.state.localInterjections[0].callId, "call-A");
  assert.equal(app.state.localInterjections[1].callId, null);
  resetG4State();
});

check("G4 consumeLocalInterjectionByCallId drops the bound entry", () => {
  resetG4State();
  app.state.localInterjections.push(
    { localId: 1, text: "a", callId: "call-A", phase: "pending", submittedAt: 1 },
    { localId: 2, text: "b", callId: null, phase: "pending", submittedAt: 2 },
  );
  app.consumeLocalInterjectionByCallId("call-A");
  assert.equal(app.state.localInterjections.length, 1);
  assert.equal(app.state.localInterjections[0].localId, 2);
  resetG4State();
});

check("G4 bound local entry hides the duplicate real chip", () => {
  resetG4State();
  app.state.localInterjections.push({
    localId: 1, text: "a", callId: "call-A", phase: "pending", submittedAt: 0,
  });
  const entries = app.computeInterventions({
    flow_id: "F1",
    status: "running",
    pending_calls: [{
      call_id: "call-A",
      kind: "interjection",
      prompt: "a",
      context: { flow_id: "F1" },
    }],
  });
  // Only the local entry's chip — the real pending_call is suppressed so we
  // don't render the same interjection twice.
  assert.equal(entries.length, 1);
  assert.equal(entries[0].synthetic, true);
  assert.equal(entries[0].localId, 1);
  resetG4State();
});

check("G4 applyInterjectionEvent pending → consumed lifecycle", () => {
  resetG4State();
  app.state.selectedFlowId = "F1";
  app.state.flowDetail = null;  // skip renderInterventions DOM work
  app.state.localInterjections.push({
    localId: 1, text: "draft", callId: null, phase: "pending", submittedAt: 0,
  });

  app.applyInterjectionEvent({
    type: "interjection_event",
    flow_id: "F1",
    call_id: "call-X",
    phase: "pending",
    ts: 0,
  });
  assert.equal(app.state.interjectionPhases["call-X"], "pending");
  assert.equal(app.state.localInterjections[0].callId, "call-X");

  app.applyInterjectionEvent({
    type: "interjection_event",
    flow_id: "F1",
    call_id: "call-X",
    phase: "consumed",
    ts: 1,
  });
  assert.equal(app.state.interjectionPhases["call-X"], "consumed");
  assert.equal(app.state.localInterjections.length, 0);

  // (call_id, phase) dedup — replaying the same phase is a no-op.
  app.applyInterjectionEvent({
    type: "interjection_event",
    flow_id: "F1",
    call_id: "call-X",
    phase: "consumed",
    ts: 2,
  });
  assert.equal(app.state.localInterjections.length, 0);
  resetG4State();
});

check("G4 applyInterjectionEvent for another flow does not touch open flow", () => {
  resetG4State();
  app.state.selectedFlowId = "F1";
  app.state.localInterjections.push({
    localId: 1, text: "draft", callId: null, phase: "pending", submittedAt: 0,
  });
  app.applyInterjectionEvent({
    type: "interjection_event",
    flow_id: "F2",
    call_id: "call-Y",
    phase: "pending",
    ts: 0,
  });
  // Different flow → no phase recorded, no local entry bound.
  assert.equal(app.state.interjectionPhases["call-Y"], undefined);
  assert.equal(app.state.localInterjections[0].callId, null);
  resetG4State();
});

// -- reportList passes the running index as the second callback arg ---------
// Regression: reportList used to call formatItem(item) without an index, so a
// (item, index) callback such as the implement Summary's `G${i + 1}` saw
// index === undefined and rendered "GNaN". Verify the index is now threaded
// through and increments from 0.
check("reportList threads an incrementing index into the callback", () => {
  const seen = [];
  app.reportList(["a", "b", "c"], (item, index) => {
    seen.push([item, index]);
    return document.createTextNode(String(item));
  });
  assert.deepEqual(seen, [["a", 0], ["b", 1], ["c", 2]]);
});

check("reportList still works for single-arg callbacks (index ignored)", () => {
  const ul = app.reportList(["x", "y"], (item) => document.createTextNode("+" + item));
  // Two list items, formatted by the first arg only — second arg is harmless.
  assert.ok(ul.textContent.includes("+x"));
  assert.ok(ul.textContent.includes("+y"));
});

// -- implement Summary numbering: G1…Gn (parity with CLI step_renderers) -----
function implStep(summary, implementedGroups) {
  return {
    step_type: "implement",
    outputs: {
      completion_status: "complete",
      summary,
      implemented_groups: implementedGroups,
      files_changed: [],
      tests_added: [],
    },
  };
}

check("implement Summary numbers multi-part summary as G1…Gn (groups non-empty)", () => {
  const step = implStep("first part; second part; third part", ["G1", "G2", "G3"]);
  const frag = app.STEP_REPORT_RENDERERS.implement(step, step.outputs);
  const ids = findAll(frag, "step-report__group-id").map((n) => n.textContent);
  assert.deepEqual(ids, ["G1.", "G2.", "G3."]);
  // No GNaN / NaN regression anywhere in the rendered output.
  assert.equal(frag.textContent.includes("GNaN"), false);
  assert.equal(frag.textContent.includes("NaN"), false);
});

check("implement Summary degrades to plain numbers 1…n when groups empty", () => {
  const step = implStep("alpha; beta; gamma", []);
  const frag = app.STEP_REPORT_RENDERERS.implement(step, step.outputs);
  const ids = findAll(frag, "step-report__group-id").map((n) => n.textContent);
  assert.deepEqual(ids, ["1.", "2.", "3."]);
  assert.equal(frag.textContent.includes("GNaN"), false);
  assert.equal(frag.textContent.includes("NaN"), false);
});

// -- implement report: tests_added long path wrapping regression -------------
// Regression: a long path like 'tests/frontend/reply_box_prompt_collapse.test.mjs'
// in tests_added must appear fully in the rendered .step-report__list li,
// proving the list item does not truncate or lose content.
check("implement report tests_added li contains full long path text", () => {
  const longPath = "tests/frontend/reply_box_prompt_collapse.test.mjs";
  const step = {
    step_type: "implement",
    outputs: {
      completion_status: "complete",
      summary: "done",
      implemented_groups: [],
      files_changed: [],
      tests_added: [longPath],
    },
  };
  const frag = app.STEP_REPORT_RENDERERS.implement(step, step.outputs);
  const uls = findAll(frag, "step-report__list");
  assert.ok(uls.length >= 1, "expected at least one .step-report__list for tests_added");
  const liTexts = [];
  for (const ul of uls) {
    for (const li of ul.childNodes) {
      liTexts.push(li.textContent);
    }
  }
  const match = liTexts.find((t) => t.includes(longPath));
  assert.ok(match, `expected a li containing '${longPath}', got: ${liTexts.join(", ")}`);
  // The li text must start with '+ ' (the formatItem prefix).
  assert.ok(match.startsWith("+ "), `li text should start with '+ ', got: ${match}`);
});

// -- spec_gate report renderer ---------------------------------------------
// A raw pytest-style blob that MUST NOT reach the DOM via the spec_gate card.
const SPEC_GATE_RAW = "tests/test_foo.py::test_bar FAILED\nE AssertionError\n"
  + "Traceback (most recent call last): ...raw stderr dump...";
const SPEC_GATE_TEST_RESULTS = {
  overall_passed: false,
  passed: false,
  command: "python -m pytest -v",
  phases: [{ name: "default", passed: false }, { name: "e2e", passed: true }],
  stdout: SPEC_GATE_RAW,
  stderr: "raw stderr dump...",
};

check("spec_gate report renders PASSED gate with re-test summary, no raw dump", () => {
  const step = { status: "completed", step_type: "spec_gate" };
  const outputs = {
    gate_passed: true,
    gate_route: "",
    test_results: { ...SPEC_GATE_TEST_RESULTS, overall_passed: true, passed: true },
  };
  const frag = app.STEP_REPORT_RENDERERS.spec_gate(step, outputs);
  const label = findOne(frag, "step-report__label");
  assert.ok(label && label.textContent.includes("PASSED"), "expected PASSED label");
  // Re-test command summary present, raw stdout/stderr absent.
  assert.ok(frag.textContent.includes("python -m pytest -v"), "expected command line");
  assert.equal(frag.textContent.includes(SPEC_GATE_RAW), false, "raw stdout leaked");
  assert.equal(frag.textContent.includes("raw stderr dump"), false, "raw stderr leaked");
  assert.equal(frag.textContent.includes("Traceback"), false, "traceback leaked");
});

check("spec_gate report renders no-op skip conclusion", () => {
  const step = { status: "completed", step_type: "spec_gate" };
  const frag = app.STEP_REPORT_RENDERERS.spec_gate(step, {
    gate_passed: true, gate_route: "", gate_skipped: true,
  });
  assert.ok(frag.textContent.includes("PASSED"), "expected PASSED");
  assert.ok(/skipped|no-op/i.test(frag.textContent), "expected skip note");
});

check("spec_gate report annotates the update_spec route", () => {
  const step = { status: "completed", step_type: "spec_gate" };
  const frag = app.STEP_REPORT_RENDERERS.spec_gate(step, {
    gate_passed: false,
    gate_route: "update_spec",
    fix_instructions: "Re-apply the intended spec update.",
  });
  assert.ok(findOne(frag, "step-report__label").textContent.includes("FAILED"),
    "expected FAILED");
  assert.ok(frag.textContent.includes("update_spec"), "expected update_spec route");
  assert.ok(frag.textContent.includes("Re-apply the intended spec update."),
    "expected fix instructions");
});

check("spec_gate report annotates the implement route with summary only", () => {
  const step = { status: "completed", step_type: "spec_gate" };
  const frag = app.STEP_REPORT_RENDERERS.spec_gate(step, {
    gate_passed: false,
    gate_route: "implement",
    test_results: SPEC_GATE_TEST_RESULTS,
  });
  assert.ok(frag.textContent.includes("FAILED"), "expected FAILED");
  assert.ok(frag.textContent.includes("implement"), "expected implement route");
  assert.ok(frag.textContent.includes("python -m pytest -v"), "expected command summary");
  assert.equal(frag.textContent.includes(SPEC_GATE_RAW), false, "raw output leaked");
  assert.equal(frag.textContent.includes("raw stderr dump"), false, "raw stderr leaked");
});

check("spec_gate STEP_RESULT_FIELDS recognizes gate result records", () => {
  assert.ok(Array.isArray(app.STEP_RESULT_FIELDS.spec_gate), "spec_gate fields missing");
  // A bare gate verdict counts as a result (presence, not non-emptiness).
  assert.equal(app.isStepResultDict("spec_gate", { gate_passed: true }), true);
  assert.equal(app.isStepResultDict("spec_gate", { test_results: {} }), true);
  // A tool-call JSON carrying none of the fields is not a result.
  assert.equal(app.isStepResultDict("spec_gate", { command: "ls" }), false);
});

check("spec_gate has a header title", () => {
  assert.equal(app.stepHeaderLabel("spec_gate", "01_spec_gate_x"), "SPEC GATE");
});

// -- charter_freshness (G5) --------------------------------------------------

check("charter_freshness STEP_RESULT_FIELDS recognizes result records", () => {
  assert.ok(Array.isArray(app.STEP_RESULT_FIELDS.charter_freshness),
    "charter_freshness fields missing");
  // Presence (not non-emptiness) of any result key counts as a result.
  assert.equal(
    app.isStepResultDict("charter_freshness", { charter_update_needed: false }), true);
  assert.equal(
    app.isStepResultDict("charter_freshness", { charter_auto_updated: true }), true);
  assert.equal(
    app.isStepResultDict("charter_freshness", { charter_diff: "--- a\n+++ b" }), true);
  // A tool-call JSON carrying none of the fields is not a result.
  assert.equal(app.isStepResultDict("charter_freshness", { command: "ls" }), false);
});

check("charter_freshness report renders the auto-update diff", () => {
  const step = { status: "completed", step_type: "charter_freshness" };
  const outputs = {
    charter_update_needed: true,
    charter_auto_updated: true,
    touched_classes: ["conventions"],
    charter_diff: "--- se3/charter.md (old)\n+++ se3/charter.md (new)\n-old line\n+new line",
  };
  const frag = app.STEP_REPORT_RENDERERS.charter_freshness(step, outputs);
  assert.ok(frag.textContent.includes("auto-updated"), "expected auto-updated label");
  const pre = findOne(frag, "step-report__diff");
  assert.ok(pre, "expected a diff block");
  assert.ok(pre.textContent.includes("+new line"), "diff content missing");
  assert.ok(frag.textContent.includes("conventions"), "touched class missing");
});

check("charter_freshness report renders the advisory (not applied) shape", () => {
  const step = { status: "completed", step_type: "charter_freshness" };
  const frag = app.STEP_REPORT_RENDERERS.charter_freshness(step, {
    charter_update_needed: true,
    charter_auto_updated: false,
    suggested_update: "Record the new runner adapter in the architecture section.",
    degraded_reason: "invariant_check_not_completed",
  });
  assert.ok(frag.textContent.includes("advised"), "expected advisory label");
  assert.ok(frag.textContent.includes("Record the new runner adapter"),
    "expected suggested_update");
  assert.ok(frag.textContent.includes("invariant_check_not_completed"),
    "expected degraded reason");
  assert.equal(!!findOne(frag, "step-report__diff"), false, "no diff when not applied");
});

check("charter_freshness report renders the fresh (no-op) shape", () => {
  const step = { status: "completed", step_type: "charter_freshness" };
  const frag = app.STEP_REPORT_RENDERERS.charter_freshness(step, {
    charter_update_needed: false,
    charter_auto_updated: false,
    reason: "No changes in this flow; charter unaffected.",
  });
  assert.ok(/fresh/i.test(frag.textContent), "expected fresh label");
  assert.ok(frag.textContent.includes("charter unaffected"), "expected reason line");
});

check("charter_freshness has a report card title", () => {
  assert.equal(app.reportCardTitle("charter_freshness"), "Charter Freshness · Result");
});

// -- Agent/model badge (G1) ---------------------------------------------------

check("formatAgentBadgeText with both agent and model", () => {
  assert.equal(app.formatAgentBadgeText("dclaude", "claude-opus-4-8"), "dclaude · claude-opus-4-8");
});

check("formatAgentBadgeText with agent only", () => {
  assert.equal(app.formatAgentBadgeText("dclaude", null), "dclaude");
  assert.equal(app.formatAgentBadgeText("dclaude", undefined), "dclaude");
  assert.equal(app.formatAgentBadgeText("dclaude", ""), "dclaude");
});

check("formatAgentBadgeText with no agent returns null", () => {
  assert.equal(app.formatAgentBadgeText(null, "claude-opus-4-8"), null);
  assert.equal(app.formatAgentBadgeText(undefined, "claude-opus-4-8"), null);
  assert.equal(app.formatAgentBadgeText("", "claude-opus-4-8"), null);
  assert.equal(app.formatAgentBadgeText(null, null), null);
});

check("normalizeRecord exposes agentName and modelName from message", () => {
  const norm = app.normalizeRecord({
    message: {
      role: "assistant",
      content: "response",
      agent_name: "dclaude",
      model_name: "claude-opus-4-8",
    },
  });
  assert.equal(norm.agentName, "dclaude");
  assert.equal(norm.modelName, "claude-opus-4-8");
});

check("normalizeRecord exposes agentName from envelope when message lacks it", () => {
  const norm = app.normalizeRecord({
    agent_name: "kclaude",
    message: {
      role: "assistant",
      content: "response",
    },
  });
  assert.equal(norm.agentName, "kclaude");
});

check("normalizeRecord returns null agentName/modelName when absent", () => {
  const norm = app.normalizeRecord({
    message: {
      role: "assistant",
      content: "response",
    },
  });
  assert.equal(norm.agentName, null);
  assert.equal(norm.modelName, null);
});

check("normalizeRecord ignores non-string agentName", () => {
  const norm = app.normalizeRecord({
    message: {
      role: "assistant",
      content: "response",
      agent_name: 42,
    },
  });
  assert.equal(norm.agentName, null);
});

check("renderAgentBadge returns null when agentName is absent", () => {
  const norm = { role: "assistant", content: "hi", agentName: null, modelName: null };
  const badge = app.renderAgentBadge(norm);
  assert.equal(badge, null);
});

check("renderAgentBadge returns null for empty norm", () => {
  const badge = app.renderAgentBadge(null);
  assert.equal(badge, null);
  const badge2 = app.renderAgentBadge(undefined);
  assert.equal(badge2, null);
});

check("old jsonl without agent fields backward compatible", () => {
  // Simulating an old record that predates agent_name/model_name fields.
  // normalizeRecord should handle it without errors and not display a badge.
  const norm = app.normalizeRecord({
    message: {
      role: "assistant",
      content: "some old response",
    },
  });
  assert.equal(norm.agentName, null);
  assert.equal(norm.modelName, null);
  // Badge rendering should be null (no placeholder).
  assert.equal(app.renderAgentBadge(norm), null);
});

// ---------------------------------------------------------------------------
// Reconnect incremental load paths (G4)
// ---------------------------------------------------------------------------
//
// These exercise the actual async load functions (loadFlowConversation /
// openHistorySession) against the DOM stub above plus a canned `fetch`, to
// lock the reconnect-incremental contract end to end:
//   * the FIRST open is a full load (no `after`, full rebuild);
//   * a reconnect refresh ({ incremental: true }) echoes the held progress
//     token via `?after=`, does NOT clear the container / __convState, and
//     appends the server delta through the same dedupe path the live WS push
//     uses (incremental render);
//   * an all-duplicate delta is a render noop (progress still advances);
//   * a `delivery: "full"` answer on a reconnect forces an authoritative full
//     rebuild;
//   * a failed reconnect request keeps the existing conversation untouched;
//   * a WS append racing a REST delta never yields a duplicate record;
//   * a running-flow local echo collapses to a single user record once the
//     daemon's authoritative copy arrives.

async function checkAsync(name, fn) {
  await fn();
  passed += 1;
  console.log("  ok -", name);
}

let __lastFetchUrl = null;
function setFetch(payload, ok = true, status = 200) {
  globalThis.fetch = (url) => {
    __lastFetchUrl = url;
    return Promise.resolve({
      ok,
      status,
      json: () => Promise.resolve(payload),
    });
  };
}
function setDeferredFetch() {
  let resolveResponse;
  globalThis.fetch = (url) => {
    __lastFetchUrl = url;
    return new Promise((resolve) => {
      resolveResponse = (payload, ok = true, status = 200) => resolve({
        ok,
        status,
        json: () => Promise.resolve(payload),
      });
    });
  };
  return (payload, ok = true, status = 200) => {
    assert.ok(resolveResponse, "the deferred fetch must have started");
    resolveResponse(payload, ok, status);
  };
}
function setQueuedDeferredFetch() {
  const requests = [];
  globalThis.fetch = (url) => new Promise((resolve) => {
    requests.push({
      url,
      resolve(payload, ok = true, status = 200) {
        resolve({
          ok,
          status,
          json: () => Promise.resolve(payload),
        });
      },
    });
  });
  return requests;
}
// The bubble (record) nodes in a container, excluding step-header separators.
function bubbleNodes(c) {
  return c.children.filter((x) => x.__convIdx !== undefined);
}
function uniqueKeys(records) {
  const keys = records.map(app.recordKey);
  return new Set(keys).size === keys.length;
}

// -- running-flow view -------------------------------------------------------

await checkAsync("flow: reconnect delta appends without clearing DOM, sends after token", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "tokA", delivery: "full",
  });
  await app.loadFlowConversation("F1");           // first open → full
  assert.equal(app.state.flowConversationProgress, "tokA");
  assert.equal(app.state.flowConversationRecords.length, 2);
  const stAfterFull = c.__convState;
  const firstBubble = bubbleNodes(c)[0];
  assert.ok(firstBubble, "first open should render a bubble");

  setFetch({
    records: [asstRecord("C", 3, "s1", "discovery")],
    progress: "tokB", delivery: "delta",
  });
  await app.loadFlowConversation("F1", { incremental: true });
  // echoed the held progress token
  assert.ok(String(__lastFetchUrl).includes("after=tokA"), __lastFetchUrl);
  assert.equal(app.state.flowConversationProgress, "tokB");
  // incremental render: same __convState object, original bubble still attached
  assert.equal(c.__convState, stAfterFull);
  assert.ok(bubbleNodes(c).includes(firstBubble), "DOM must not be cleared on a delta");
  assert.equal(app.state.flowConversationRecords.length, 3);
  assert.ok(uniqueKeys(app.state.flowConversationRecords));
});

await checkAsync("flow: WS append + REST delta dedupe to unique records", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "t0", delivery: "full",
  });
  await app.loadFlowConversation("F1");
  // a live WS append delivers C during the outage window
  app.applyHistoryData({ flow_id: "F1", mode: "append", records: [asstRecord("C", 3, "s1", "discovery")] });
  assert.equal(app.state.flowConversationRecords.length, 3);
  // reconnect delta re-sends C (already held via WS) plus a genuinely new D
  setFetch({
    records: [asstRecord("C", 3, "s1", "discovery"), asstRecord("D", 4, "s1", "discovery")],
    progress: "t1", delivery: "delta",
  });
  await app.loadFlowConversation("F1", { incremental: true });
  assert.equal(app.state.flowConversationRecords.length, 4, "C must not be appended twice");
  assert.ok(uniqueKeys(app.state.flowConversationRecords));
});

await checkAsync("flow: all-duplicate delta is a render noop", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "p0", delivery: "full",
  });
  await app.loadFlowConversation("F1");
  const recsBefore = app.state.flowConversationRecords;
  const stBefore = c.__convState;
  const countBefore = bubbleNodes(c).length;

  setFetch({ records: [asstRecord("B", 2, "s1", "discovery")], progress: "p1", delivery: "delta" });
  await app.loadFlowConversation("F1", { incremental: true });
  // progress still advances on a noop, but records / DOM are untouched
  assert.equal(app.state.flowConversationProgress, "p1");
  assert.equal(app.state.flowConversationRecords, recsBefore);
  assert.equal(c.__convState, stBefore);
  assert.equal(bubbleNodes(c).length, countBefore);
});

await checkAsync("flow: reconnect full fallback replaces the old generation", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery"), asstRecord("C", 3, "s1", "discovery")],
    progress: "g1", delivery: "full",
  });
  await app.loadFlowConversation("F1");
  const firstBubble = bubbleNodes(c)[0];
  const stBefore = c.__convState;
  // A stale token makes the server answer `full` from a replacement cache
  // generation whose records are not a superset of the old generation.
  setFetch({
    records: ["X", "Y"].map((x, i) => asstRecord(x, i + 10, "s2", "analyze")),
    progress: "g2", delivery: "full",
  });
  await app.loadFlowConversation("F1", { incremental: true });
  assert.equal(app.state.flowConversationProgress, "g2");
  assert.equal(app.state.flowConversationRecords.length, 2);
  // full rebuild: fresh __convState, original bubble detached
  assert.notEqual(c.__convState, stBefore);
  assert.ok(!bubbleNodes(c).includes(firstBubble), "full fallback must rebuild the DOM");
  assert.deepEqual(
    app.state.flowConversationRecords.map((r) => r.message.content),
    ["X", "Y"],
  );
});

await checkAsync("flow: newest overlapping reconnect refresh wins", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [asstRecord("A", 1, "s1", "discovery")];
  app.state.flowConversationProgress = "g1";
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;
  app.renderConversation(c, app.state.flowConversationRecords, false);

  const requests = setQueuedDeferredFetch();
  const older = app.loadFlowConversation("F1", { incremental: true });
  const newer = app.loadFlowConversation("F1", { incremental: true });
  assert.equal(requests.length, 2);

  requests[1].resolve({
    records: [asstRecord("Y", 20, "s3", "implement")],
    progress: "g3",
    delivery: "full",
  });
  await newer;
  requests[0].resolve({
    records: [asstRecord("X", 10, "s2", "analyze")],
    progress: "g2",
    delivery: "full",
  });
  await older;

  assert.deepEqual(
    app.state.flowConversationRecords.map((r) => r.message.content),
    ["Y"],
  );
  assert.equal(app.state.flowConversationProgress, "g3");
});

await checkAsync("flow: WS full replacement invalidates an older REST refresh", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [
    asstRecord("A", 1, "s1", "discovery"),
    asstRecord("B", 2, "s1", "discovery"),
  ];
  app.state.flowConversationProgress = "old-progress";
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;
  app.renderConversation(c, app.state.flowConversationRecords, false);

  const resolveFetch = setDeferredFetch();
  const refresh = app.loadFlowConversation("F1", { incremental: true });
  app.applyHistoryData({
    flow_id: "F1",
    mode: "full",
    records: [
      asstRecord("X", 10, "s2", "analyze"),
      asstRecord("Y", 11, "s2", "analyze"),
    ],
  });
  const epochAfterFull = app.state.flowConversationEpoch;
  const wsState = c.__convState;
  resolveFetch({
    records: [asstRecord("C", 3, "s1", "discovery")],
    progress: "stale-progress",
    delivery: "delta",
  });
  await refresh;

  assert.deepEqual(
    app.state.flowConversationRecords.map((r) => r.message.content),
    ["X", "Y"],
  );
  assert.equal(app.state.flowConversationProgress, null);
  assert.equal(app.state.flowConversationEpoch, epochAfterFull);
  assert.equal(c.__convState, wsState, "stale REST response must not repaint");
});

await checkAsync("flow: failed reconnect request keeps the existing conversation", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  setFetch({ records: [asstRecord("A", 1, "s1", "discovery")], progress: "f0", delivery: "full" });
  await app.loadFlowConversation("F1");
  const recsBefore = app.state.flowConversationRecords;
  const countBefore = bubbleNodes(c).length;

  setFetch({}, false, 503);                        // transient failure on refresh
  await app.loadFlowConversation("F1", { incremental: true });
  assert.equal(app.state.flowConversationRecords, recsBefore);
  assert.equal(bubbleNodes(c).length, countBefore);
});

await checkAsync("flow: local echo collapses to a single user record on reconnect delta", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;
  const echo = {
    __localEcho: true, __localEchoText: "yes", __localEchoPriorAuth: 0,
    message: { role: "user", content: "yes", timestamp: 10 },
  };
  app.state.flowConversationRecords = [asstRecord("A", 1, "s1", "discovery"), echo];
  app.renderConversation(c, app.state.flowConversationRecords, false);

  // the daemon's authoritative copy of the same reply arrives via reconnect delta
  const authUser = { step_id: "s1c", step_type: "discovery", message: { role: "user", content: "yes", timestamp: 11 } };
  setFetch({ records: [authUser], progress: "e1", delivery: "delta" });
  await app.loadFlowConversation("F1", { incremental: true });

  assert.equal(app.state.flowConversationRecords.length, 2, "echo must be reconciled away");
  assert.ok(!app.state.flowConversationRecords.some((r) => r.__localEcho), "no echo should survive");
  const yes = app.state.flowConversationRecords.filter(
    (r) => app.normalizeRecord(r).role === "user" && app.comparableUserText(app.normalizeRecord(r).content) === "yes");
  assert.equal(yes.length, 1, "the reply is shown exactly once");
});

await checkAsync("flow: pending local echo survives a reconnect full fallback", async () => {
  // The user replied (optimistic echo spliced in), then the connection dropped
  // and the server's cache generation was replaced, so the reconnect refetch
  // answers `delivery: "full"` from a fresh generation that does NOT yet carry
  // the authoritative user record (the daemon only writes it at the next step
  // boundary). The echo must remain visible — the pre-change full-reload kept
  // it, so the full fallback must too.
  app.state.selectedFlowId = "F1";
  app.state.flowConversationProgress = "g1";
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;
  const echo = {
    __localEcho: true, __localEchoText: "yes", __localEchoPriorAuth: 0,
    message: { role: "user", content: "yes", timestamp: 10 },
  };
  app.state.flowConversationRecords = [asstRecord("A", 1, "s1", "discovery"), echo];
  app.renderConversation(c, app.state.flowConversationRecords, false);

  // Replacement generation: new records, no authoritative copy of "yes" yet.
  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "g2", delivery: "full",
  });
  await app.loadFlowConversation("F1", { incremental: true });

  assert.equal(app.state.flowConversationProgress, "g2");
  assert.ok(
    app.state.flowConversationRecords.some((r) => r.__localEcho),
    "pending echo must survive the full fallback",
  );
  const yes = app.state.flowConversationRecords.filter(
    (r) => app.normalizeRecord(r).role === "user"
      && app.comparableUserText(app.normalizeRecord(r).content) === "yes");
  assert.equal(yes.length, 1, "the just-sent reply is still shown once");
});

// -- silent progression refresh (G1) -----------------------------------------
//
// The bottom-line "step progressed → silently rebuild the main conversation"
// workaround. loadFlowConversation(flowId, { silent:true }) does a full,
// no-`after` pull and a whole-tree rebuild equivalent to an exit/re-enter, but
// WITHOUT the destructive pre-clear (no blank flash, no "Loading…" placeholder)
// and WITHOUT forcing stick-to-bottom (scroll position preserved unless the
// reader was already near the bottom). It touches only #flow-conversation and
// never the reply region.

await checkAsync("flow silent: no pre-clear / no Loading placeholder before data arrives", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  // First open the flow with two records so there is existing rendered DOM. The
  // full open hands back a progress token + bundle signature the silent self-heal
  // will echo on its next pull (G5).
  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "tokA", signature: "sigA", delivery: "full",
  });
  await app.loadFlowConversation("F1");
  const firstBubble = bubbleNodes(c)[0];
  assert.ok(firstBubble, "existing conversation rendered");
  assert.equal(app.state.flowConversationSignature, "sigA",
    "the full open stored the bundle signature for the next self-heal");

  // A silent refresh starts; the response is deferred so we can inspect the DOM
  // in the window between fetch start and data arrival.
  const resolve = setDeferredFetch();
  const pending = app.loadFlowConversation("F1", { silent: true });
  // The container must NOT have been cleared and must NOT show a placeholder.
  assert.ok(bubbleNodes(c).includes(firstBubble),
    "silent refresh must not clear the container before data arrives");
  assert.ok(!c.textContent.includes("Loading conversation"),
    "silent refresh must not insert a Loading placeholder");
  // G5: the silent self-heal is now a SIGNATURE-CHECK pull — it echoes the held
  // progress token AND signature so the server can answer not_modified/delta
  // instead of re-shipping the whole bundle. It is no longer a bare full pull.
  assert.ok(String(__lastFetchUrl).includes("after=tokA"),
    "silent refresh echoes the held progress token: " + __lastFetchUrl);
  assert.ok(String(__lastFetchUrl).includes("sig=sigA"),
    "silent refresh echoes the held bundle signature: " + __lastFetchUrl);

  resolve({
    records: [
      asstRecord("A", 1, "s1", "discovery"),
      asstRecord("B", 2, "s1", "discovery"),
      asstRecord("C", 3, "s1", "discovery"),
    ],
    progress: "tokB", signature: "sigB", delivery: "full",
  });
  await pending;
  // Data arrived → whole-tree rebuild, fresh __convState, progress written back.
  assert.equal(app.state.flowConversationRecords.length, 3);
  assert.equal(app.state.flowConversationProgress, "tokB");
  assert.equal(app.state.flowConversationSignature, "sigB",
    "the fresh signature is written back for the next self-heal");
  assert.ok(!bubbleNodes(c).includes(firstBubble),
    "silent refresh rebuilds the DOM once data arrives (append=false)");
  assert.ok(c.__convState && c.__convState.count === 3,
    "__convState rebuilt from scratch");
  assert.ok(uniqueKeys(app.state.flowConversationRecords));
});

await checkAsync("flow silent: preserves scroll position when not near the bottom", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery")],
    progress: "p0", delivery: "full",
  });
  await app.loadFlowConversation("F1");

  // Simulate the reader scrolled UP, far from the bottom. In production the
  // scroll handler drops the follow-bottom intent the silent rebuild consults;
  // set it here to model that deliberate scroll-up (#260).
  c.scrollHeight = 1000; c.clientHeight = 100; c.scrollTop = 0;
  app.state.flowConversationFollowingBottom = false;
  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "p1", delivery: "full",
  });
  await app.loadFlowConversation("F1", { silent: true });
  // The reader deliberately scrolled up → must NOT yank to the bottom.
  assert.equal(c.scrollTop, 0,
    "silent refresh must not scroll a scrolled-up reader to the bottom");
  assert.equal(app.state.flowConversationRecords.length, 2);
});

await checkAsync("flow silent: scrolls to bottom when already near the bottom", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery")],
    progress: "p0", delivery: "full",
  });
  await app.loadFlowConversation("F1");

  // Reader is at (near) the bottom: scrollHeight - scrollTop - clientHeight <= 80.
  c.scrollHeight = 500; c.clientHeight = 500; c.scrollTop = 0;
  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "p1", delivery: "full",
  });
  await app.loadFlowConversation("F1", { silent: true });
  // scrollFlowConversationToBottom sets scrollTop = scrollHeight.
  assert.equal(c.scrollTop, c.scrollHeight,
    "silent refresh sticks to the bottom when the reader was already there");
});

await checkAsync("flow silent: failed request keeps the existing conversation untouched", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "ok0", delivery: "full",
  });
  await app.loadFlowConversation("F1");
  const recsBefore = app.state.flowConversationRecords;
  const stBefore = c.__convState;
  const bubblesBefore = bubbleNodes(c).slice();

  setFetch({}, false, 503);
  await app.loadFlowConversation("F1", { silent: true });
  // A transient failure must NOT wipe the conversation or insert an error.
  assert.equal(app.state.flowConversationRecords, recsBefore,
    "records untouched on a failed silent refresh");
  assert.equal(c.__convState, stBefore, "__convState untouched on failure");
  assert.deepEqual(bubbleNodes(c), bubblesBefore, "DOM untouched on failure");
  assert.ok(!c.textContent.includes("Could not load"),
    "no error placeholder on a silent refresh failure");
});

await checkAsync("flow silent: never touches reply-region state", async () => {
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  // Seed reply-region state with sentinel values.
  app.state.flowReplyTargetId = "chip-7";
  app.state.flowInterjectRequested = true;
  app.state.flowReplyPromptExpanded = { "chip-7": true };
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery")],
    progress: "q0", delivery: "full",
  });
  await app.loadFlowConversation("F1");

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "q1", delivery: "full",
  });
  await app.loadFlowConversation("F1", { silent: true });
  // Reply-region state is unchanged by the conversation-only refresh.
  assert.equal(app.state.flowReplyTargetId, "chip-7");
  assert.equal(app.state.flowInterjectRequested, true);
  assert.deepEqual(app.state.flowReplyPromptExpanded, { "chip-7": true });
});

// -- G3 periodic full-snapshot self-heal (3s poll) ---------------------------
//
// The running-flow view now re-pulls the whole conversation on the SAME 3s
// detailPollTimer cadence the left-side status area uses, and idempotently
// reconciles it, so any dropped/misjudged WS increment self-heals at the next
// tick — WS deltas are demoted to a pure low-latency optimization. These tests
// pin: (1) the poll callback issues a full (no-`after`) history pull and the
// self-heal renders the pulled records; (2) a silent snapshot that changed
// nothing skips the DOM rebuild entirely (cheap healthy path); (3) while the
// poll is active a detected advance updates the marker but does NOT arm the
// grace fallback (no duplicate full pull); (4) a terminal flow keeps pulling
// until its conversation content stabilizes, then the self-heal stops for it —
// and a commit/index result that lands on a pull AFTER the status flipped
// terminal is still captured rather than frozen out.

const flushTicks = async (n = 4) => {
  for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0));
};
// A fetch that routes /api/flows → detail payload, /api/history → snapshot
// payload, recording every URL so the two poll legs can be told apart.
function installRouterFetch(historyPayload, flowPayload) {
  const calls = [];
  globalThis.fetch = (url) => {
    const u = String(url);
    calls.push(u);
    const payload = u.includes("/api/history/") ? historyPayload : flowPayload;
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
  };
  return calls;
}

await checkAsync("reply-context: expanding a clipped call prompt lazy-loads the full body", async () => {
  // STATUS_UPDATE now clips a flow's own pending_calls prompt to DESC_CLIP; the
  // reply-context carries only the preview and must fetch the untruncated body
  // on demand (GET /api/calls/{id}/detail) when the operator expands it.
  const saved = globalThis.fetch;
  const reply = document.getElementById("flow-reply-context");
  const CLIPPED = "y".repeat(200) + "...";        // looks DESC_CLIP-clipped
  const FULL = "y".repeat(4000) + " full tail";
  app.state.flowInterjectRequested = false;
  app.state.flowReplyPromptExpanded = {};
  app.state.flowReplyPromptScroll = {};
  app.state.flowReplyPromptFull = {};
  try {
    let hitUrl = null;
    globalThis.fetch = (url) => {
      hitUrl = String(url);
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ machine_id: "m1", call: { prompt: FULL } }),
      });
    };
    app.renderInterventions({
      status: "running",
      pending_calls: [
        { call_id: "big_call", kind: "call", prompt: CLIPPED, project_root: "/p" },
      ],
    });
    // Collapsed by default → no fetch yet.
    assert.equal(hitUrl, null, "no detail fetch before the body is expanded");
    const toggle = findOne(reply, "flow-reply-prompt-toggle");
    toggle.dispatch("click");                     // expand → triggers the pull
    await flushTicks();
    assert.ok(hitUrl && hitUrl.includes("/api/calls/big_call/detail"),
      "expanding a clipped prompt pulls the full body from the call detail endpoint: " + hitUrl);
    // The full text is cached so a subsequent rebuild mounts it directly.
    assert.equal(app.state.flowReplyPromptFull["big_call"], FULL,
      "the fetched full prompt is cached by call_id");
    const body = findOne(reply, "flow-reply-prompt");
    assert.ok(body.textContent.includes("full tail"),
      "the expanded body shows the untruncated prompt after the pull");
  } finally {
    globalThis.fetch = saved;
  }
});

await checkAsync("reply-context: a short (un-clipped) call prompt never fetches detail", async () => {
  // A prompt at/under DESC_CLIP is carried verbatim, so expanding it must not
  // fire a needless detail pull.
  const saved = globalThis.fetch;
  const reply = document.getElementById("flow-reply-context");
  app.state.flowInterjectRequested = false;
  app.state.flowReplyPromptExpanded = {};
  app.state.flowReplyPromptFull = {};
  try {
    let fetched = false;
    globalThis.fetch = () => { fetched = true; return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) }); };
    app.renderInterventions({
      status: "running",
      pending_calls: [
        { call_id: "small_call", kind: "call", prompt: "short prompt", project_root: "/p" },
      ],
    });
    findOne(reply, "flow-reply-prompt-toggle").dispatch("click");
    await flushTicks();
    assert.equal(fetched, false, "a short prompt is complete on the wire — no detail pull on expand");
  } finally {
    globalThis.fetch = saved;
  }
});

await checkAsync("G3 poll: pollFlowView issues a full history self-heal pull and renders it", async () => {
  const saved = globalThis.fetch;
  const savedMachines = app.state.machines;
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  app.state.flowProgressionMarker = null;
  app.state.periodicSnapshotActive = true;
  app.state.machines = [];   // findFlow → null (flow not yet in state.machines)
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;
  try {
    const calls = installRouterFetch(
      { records: [asstRecord("A", 1, "s1", "discovery")], progress: "p1", delivery: "full" },
      { flow: { flow_id: "F1", current_step: "discovery", current_step_index: 0, status: "running" }, machine_id: "m1" },
    );
    app.pollFlowView();
    await flushTicks();
    // Both legs of the poll fired: a detail pull AND a full conversation pull.
    assert.ok(calls.some((u) => u.includes("/api/flows/")), "poll refreshes the left-side detail");
    const hist = calls.find((u) => u.includes("/api/history/"));
    assert.ok(hist, "poll issues a conversation self-heal pull");
    assert.ok(!hist.includes("after="), "the self-heal is a full (no-after) pull: " + hist);
    // The pulled snapshot is reconciled into the open conversation.
    assert.equal(app.state.flowConversationRecords.length, 1, "self-heal populated the records");
    assert.equal(bubbleNodes(c).length, 1, "self-heal rendered the pulled record");
  } finally {
    app.state.periodicSnapshotActive = false;
    app.state.machines = savedMachines;
    globalThis.fetch = saved;
  }
});

await checkAsync("G3 poll: an unchanged snapshot self-heal skips the DOM rebuild", async () => {
  const saved = globalThis.fetch;
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = null;
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;
  try {
    // Establish the held conversation.
    setFetch({
      records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
      progress: "p0", delivery: "full",
    });
    await app.loadFlowConversation("F1");
    const recsBefore = app.state.flowConversationRecords;
    const stBefore = c.__convState;
    const bubblesBefore = bubbleNodes(c).slice();
    // A silent self-heal whose snapshot is byte-identical to what is held must
    // NOT rebuild: same records ref, same __convState, same DOM nodes.
    setFetch({
      records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
      progress: "p1", delivery: "full",
    });
    await app.loadFlowConversation("F1", { silent: true });
    assert.equal(app.state.flowConversationRecords, recsBefore,
      "unchanged self-heal keeps the same records array (no adopt)");
    assert.equal(c.__convState, stBefore, "unchanged self-heal does not rebuild __convState");
    assert.deepEqual(bubbleNodes(c), bubblesBefore, "unchanged self-heal leaves the DOM untouched");
    // A snapshot that DOES change still rebuilds (self-heals the divergence).
    setFetch({
      records: [
        asstRecord("A", 1, "s1", "discovery"),
        asstRecord("B", 2, "s1", "discovery"),
        asstRecord("C", 3, "s2", "analyze"),
      ],
      progress: "p2", delivery: "full",
    });
    await app.loadFlowConversation("F1", { silent: true });
    assert.equal(app.state.flowConversationRecords.length, 3,
      "a divergent snapshot is adopted and rendered (self-heal)");
    assert.equal(bubbleNodes(c).length, 3);
  } finally {
    globalThis.fetch = saved;
  }
});

check("G3 sameRenderedConversation: identity, length, content, and order sensitivity", () => {
  const A = asstRecord("A", 1, "s1", "discovery");
  const B = asstRecord("B", 2, "s1", "discovery");
  const Bx = asstRecord("B-edited", 2, "s1", "discovery");
  assert.equal(app.sameRenderedConversation([A, B], [A, B]), true, "equal content → same");
  assert.equal(app.sameRenderedConversation([A, B], [A]), false, "different length → different");
  assert.equal(app.sameRenderedConversation([A, B], [A, Bx]), false, "content edit → different");
  assert.equal(app.sameRenderedConversation([A, B], [B, A]), false, "reorder → different");
  const same = [A, B];
  assert.equal(app.sameRenderedConversation(same, same), true, "same reference → same");
});

await checkAsync("G3 convergence: an active periodic poll demotes the progression grace fallback", async () => {
  const saved = globalThis.fetch;
  app.cancelProgressionGrace();
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [];
  app.state.flowConversationAppendSeq = 0;
  app.state.progressionGraceMs = 5;
  // Baseline at discovery so the analyze snapshot below reads as an advance.
  app.state.flowProgressionMarker = {
    flowId: "F1", currentStep: "discovery", currentStepIndex: 0, status: "running",
  };
  setFetch({ records: [], progress: "p", delivery: "full" });
  try {
    // Poll active → the advance updates the marker (activity detection) but must
    // NOT arm the grace loop: the 3s poll is the single self-heal path, so a
    // second full pull on the ~5s grace cadence would be a duplicate.
    app.state.periodicSnapshotActive = true;
    app.maybeRefreshConversationOnProgression({
      flow_id: "F1", current_step: "analyze", current_step_index: 1, status: "running",
    });
    assert.equal(app.state.progressionGraceTimer, null,
      "an active periodic poll must NOT arm the grace fallback");
    assert.equal(app.state.flowProgressionMarker.currentStep, "analyze",
      "the advance is still recorded for activity/stall detection");

    // Poll inactive (a view without it / the DOM-free tests) → the grace loop
    // remains the self-heal path and DOES arm, proving the gate is the switch.
    app.state.periodicSnapshotActive = false;
    app.state.flowProgressionMarker = {
      flowId: "F1", currentStep: "analyze", currentStepIndex: 1, status: "running",
    };
    app.maybeRefreshConversationOnProgression({
      flow_id: "F1", current_step: "commit", current_step_index: 2, status: "running",
    });
    assert.notEqual(app.state.progressionGraceTimer, null,
      "with no periodic poll the grace fallback still arms");
  } finally {
    app.cancelProgressionGrace();
    app.state.periodicSnapshotActive = false;
    globalThis.fetch = saved;
  }
});

await checkAsync("G3 poll: a terminal flow keeps self-healing every tick (never latched off by status)", async () => {
  const saved = globalThis.fetch;
  const savedMachines = app.state.machines;
  app.state.selectedFlowId = "F1";
  app.state.flowConversationRecords = [asstRecord("final", 1, "s9", "commit")];
  app.state.flowConversationProgress = null;
  app.state.machines = [{ flows: [{ flow_id: "F1", status: "completed" }] }];
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;
  try {
    const calls = [];
    globalThis.fetch = (url) => {
      calls.push(String(url));
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({
          records: [asstRecord("final", 1, "s9", "commit")], progress: "p", delivery: "full",
        }),
      });
    };
    // Terminal STATUS never stops the self-heal — it mirrors the left-side detail
    // poll, running a full pull on every tick while the view is open. Re-pulling
    // a static conversation is a cheap silent no-op (no DOM repaint), but the pull
    // itself must keep firing so a late-arriving commit/index result is never
    // frozen out. Each tick therefore issues exactly one history pull.
    for (let i = 0; i < 4; i++) {
      app.selfHealFlowConversation();
      await flushTicks();
    }
    assert.equal(calls.length, 4,
      "a terminal flow issues a self-heal pull on every tick, not just once: " + calls.length);
    assert.ok(calls.every((u) => u.includes("/api/history/")),
      "every self-heal pull hits the history endpoint");
    // G5: the first pull is a bare baseline (no token held yet), but once the
    // server hands back a progress token every subsequent self-heal echoes it —
    // the signature-check pull that replaces the old "re-ship the whole bundle".
    assert.ok(!calls[0].includes("after="),
      "the first self-heal (no token held) is a full baseline pull: " + calls[0]);
    assert.ok(calls.slice(1).every((u) => u.includes("after=")),
      "later self-heal pulls echo the held progress token (signature-check pull)");
  } finally {
    app.state.machines = savedMachines;
    globalThis.fetch = saved;
  }
});

await checkAsync("G3 poll: a commit result landing AFTER the terminal status flip is still self-healed in", async () => {
  const saved = globalThis.fetch;
  const savedMachines = app.state.machines;
  app.state.selectedFlowId = "F1";
  // Status has already flipped completed, but the history cache still holds only
  // the pre-commit snapshot (the daemon/server marked the flow done before the
  // commit/index result was written) AND the WS append carrying it was dropped.
  // This is the exact race the old one-post-terminal-pull latch could not survive.
  app.state.flowConversationRecords = [asstRecord("running-tail", 1, "s9", "commit")];
  app.state.flowConversationProgress = null;
  app.state.machines = [{ flows: [{ flow_id: "F1", status: "completed" }] }];
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;
  try {
    const calls = [];
    // The first several post-terminal pulls return the stale pre-commit snapshot;
    // the history cache only catches up on the fourth pull, now carrying the
    // commit result. Because the self-heal never latches off on terminal status,
    // that later pull still fires and the result is rendered.
    let served = 0;
    globalThis.fetch = (url) => {
      calls.push(String(url));
      served += 1;
      const records = served >= 4
        ? [asstRecord("running-tail", 1, "s9", "commit"),
           asstRecord("Committed abc123: fix", 2, "s9", "commit")]
        : [asstRecord("running-tail", 1, "s9", "commit")];
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ records, progress: "p" + served, delivery: "full" }),
      });
    };
    for (let i = 0; i < 6; i++) {
      app.selfHealFlowConversation();
      await flushTicks();
    }
    assert.equal(app.state.flowConversationRecords.length, 2,
      "the late-arriving commit result was pulled into the conversation");
    assert.ok(
      app.state.flowConversationRecords.some((r) =>
        String(r.content || (r.message && r.message.content) || "").includes("Committed")),
      "the commit result record is present after self-heal");
  } finally {
    app.state.machines = savedMachines;
    globalThis.fetch = saved;
  }
});

// -- history detail view -----------------------------------------------------

await checkAsync("history: reconnect delta appends without clearing detail DOM", async () => {
  app.state.selectedHistoryId = "H1";
  app.state.historyRecords = [];
  app.state.historyProgress = null;
  app.state.historySessions = [{ flow_id: "H1", status: "completed" }];
  const d = document.getElementById("history-detail");
  d.innerHTML = ""; d.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "hA", delivery: "full",
  });
  await app.openHistorySession("H1");             // first selection → full
  assert.equal(app.state.historyProgress, "hA");
  assert.equal(app.state.historyRecords.length, 2);
  const stAfter = d.__convState;
  const firstBubble = bubbleNodes(d)[0];

  setFetch({ records: [asstRecord("C", 3, "s1", "discovery")], progress: "hB", delivery: "delta" });
  await app.openHistorySession("H1", { incremental: true });
  assert.ok(String(__lastFetchUrl).includes("after=hA"), __lastFetchUrl);
  assert.equal(app.state.historyProgress, "hB");
  assert.equal(d.__convState, stAfter);
  assert.ok(bubbleNodes(d).includes(firstBubble), "history detail DOM must not be cleared on a delta");
  assert.equal(app.state.historyRecords.length, 3);
  assert.ok(uniqueKeys(app.state.historyRecords));
});

await checkAsync("history: all-duplicate delta is a render noop", async () => {
  app.state.selectedHistoryId = "H1";
  app.state.historyRecords = [];
  app.state.historyProgress = null;
  app.state.historySessions = [{ flow_id: "H1", status: "completed" }];
  const d = document.getElementById("history-detail");
  d.innerHTML = ""; d.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "r0", delivery: "full",
  });
  await app.openHistorySession("H1");
  const recsBefore = app.state.historyRecords;
  const stBefore = d.__convState;
  const countBefore = bubbleNodes(d).length;

  setFetch({ records: [asstRecord("B", 2, "s1", "discovery")], progress: "r1", delivery: "delta" });
  await app.openHistorySession("H1", { incremental: true });
  assert.equal(app.state.historyProgress, "r1");
  assert.equal(app.state.historyRecords, recsBefore);
  assert.equal(d.__convState, stBefore);
  assert.equal(bubbleNodes(d).length, countBefore);
});

await checkAsync("history: reconnect full fallback replaces the old generation", async () => {
  app.state.selectedHistoryId = "H1";
  app.state.historyRecords = [];
  app.state.historyProgress = null;
  app.state.historySessions = [{ flow_id: "H1", status: "completed" }];
  const d = document.getElementById("history-detail");
  d.innerHTML = ""; d.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "q1", delivery: "full",
  });
  await app.openHistorySession("H1");
  const firstBubble = bubbleNodes(d)[0];
  const stBefore = d.__convState;

  setFetch({
    records: ["X", "Y"].map((x, i) => asstRecord(x, i + 10, "s2", "analyze")),
    progress: "q2", delivery: "full",
  });
  await app.openHistorySession("H1", { incremental: true });
  assert.equal(app.state.historyRecords.length, 2);
  assert.notEqual(d.__convState, stBefore);
  assert.ok(!bubbleNodes(d).includes(firstBubble), "history full fallback must rebuild the DOM");
  assert.deepEqual(app.state.historyRecords.map((r) => r.message.content), ["X", "Y"]);
});

await checkAsync("history: newest overlapping reconnect refresh wins", async () => {
  app.state.selectedHistoryId = "H1";
  app.state.historyRecords = [asstRecord("A", 1, "s1", "discovery")];
  app.state.historyProgress = "q1";
  app.state.historySessions = [{ flow_id: "H1", status: "completed" }];
  const d = document.getElementById("history-detail");
  d.innerHTML = ""; d.__convState = null;
  app.renderConversation(d, app.state.historyRecords, false);

  const requests = setQueuedDeferredFetch();
  const older = app.openHistorySession("H1", { incremental: true });
  const newer = app.openHistorySession("H1", { incremental: true });
  assert.equal(requests.length, 2);

  requests[1].resolve({
    records: [asstRecord("Y", 20, "s3", "implement")],
    progress: "q3",
    delivery: "full",
  });
  await newer;
  requests[0].resolve({
    records: [asstRecord("X", 10, "s2", "analyze")],
    progress: "q2",
    delivery: "full",
  });
  await older;

  assert.deepEqual(app.state.historyRecords.map((r) => r.message.content), ["Y"]);
  assert.equal(app.state.historyProgress, "q3");
});

await checkAsync("history: WS full replacement invalidates an older REST refresh", async () => {
  app.state.selectedHistoryId = "H1";
  app.state.historyRecords = [
    asstRecord("A", 1, "s1", "discovery"),
    asstRecord("B", 2, "s1", "discovery"),
  ];
  app.state.historyProgress = "old-progress";
  app.state.historySessions = [{ flow_id: "H1", status: "completed" }];
  const d = document.getElementById("history-detail");
  d.innerHTML = ""; d.__convState = null;
  app.renderConversation(d, app.state.historyRecords, false);

  const resolveFetch = setDeferredFetch();
  const refresh = app.openHistorySession("H1", { incremental: true });
  app.applyHistoryData({
    flow_id: "H1",
    mode: "full",
    records: [
      asstRecord("X", 10, "s2", "analyze"),
      asstRecord("Y", 11, "s2", "analyze"),
    ],
  });
  const epochAfterFull = app.state.historyEpoch;
  const wsState = d.__convState;
  resolveFetch({
    records: [
      asstRecord("A", 1, "s1", "discovery"),
      asstRecord("B", 2, "s1", "discovery"),
    ],
    progress: "stale-progress",
    delivery: "full",
  });
  await refresh;

  assert.deepEqual(app.state.historyRecords.map((r) => r.message.content), ["X", "Y"]);
  assert.equal(app.state.historyProgress, null);
  assert.equal(app.state.historyEpoch, epochAfterFull);
  assert.equal(d.__convState, wsState, "stale REST response must not repaint");
});

await checkAsync("history: failed reconnect request keeps the existing detail", async () => {
  app.state.selectedHistoryId = "H1";
  app.state.historyRecords = [];
  app.state.historyProgress = null;
  app.state.historySessions = [{ flow_id: "H1", status: "completed" }];
  const d = document.getElementById("history-detail");
  d.innerHTML = ""; d.__convState = null;

  setFetch({ records: [asstRecord("A", 1, "s1", "discovery")], progress: "z0", delivery: "full" });
  await app.openHistorySession("H1");
  const recsBefore = app.state.historyRecords;
  const countBefore = bubbleNodes(d).length;

  setFetch({}, false, 504);
  await app.openHistorySession("H1", { incremental: true });
  assert.equal(app.state.historyRecords, recsBefore);
  assert.equal(bubbleNodes(d).length, countBefore);
});

// -- G3 regression: stale-offset guard + exit/re-enter delta completeness ----
//
// Guards the 872399c history-load regression from the frontend side: a held
// progress token must never be echoed when its backing records were dropped
// (which would render the server's delta tail as the whole conversation = a
// truncated view), and an old session that the user exits and re-enters must
// still walk the delta path on a reconnect — with the rendered records equal to
// the complete on-disk set.

await checkAsync("flow: reconnect with empty held records forces a full load (no stale offset)", async () => {
  app.state.selectedFlowId = "F1";
  // Pathological invariant violation: a token is held but no records back it.
  // The echo-guard must refuse to apply that offset across the cleared bundle.
  app.state.flowConversationRecords = [];
  app.state.flowConversationProgress = "stale-token";
  const c = document.getElementById("flow-conversation");
  c.innerHTML = ""; c.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "fresh", delivery: "full",
  });
  await app.loadFlowConversation("F1", { incremental: true });
  // The stale token was NOT echoed — a full reload was requested instead.
  assert.ok(!String(__lastFetchUrl).includes("after="), __lastFetchUrl);
  // The complete record set is shown (never truncated to a delta tail).
  assert.equal(app.state.flowConversationRecords.length, 2);
  assert.equal(app.state.flowConversationProgress, "fresh");
});

await checkAsync("history: reconnect with empty held records forces a full load (no stale offset)", async () => {
  app.state.selectedHistoryId = "H1";
  app.state.historyRecords = [];
  app.state.historyProgress = "stale-token";
  app.state.historySessions = [{ flow_id: "H1", status: "completed" }];
  const d = document.getElementById("history-detail");
  d.innerHTML = ""; d.__convState = null;

  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "fresh", delivery: "full",
  });
  await app.openHistorySession("H1", { incremental: true });
  assert.ok(!String(__lastFetchUrl).includes("after="), __lastFetchUrl);
  assert.equal(app.state.historyRecords.length, 2);
  assert.equal(app.state.historyProgress, "fresh");
});

await checkAsync("history: exit then re-enter still walks delta on reconnect with complete records", async () => {
  app.state.selectedHistoryId = null;
  app.state.historyRecords = [];
  app.state.historyProgress = null;
  app.state.historySessions = [{ flow_id: "OLD", status: "completed" }];
  const d = document.getElementById("history-detail");
  d.innerHTML = ""; d.__convState = null;

  // Enter the session: full load of the complete record set.
  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "g1", delivery: "full",
  });
  await app.openHistorySession("OLD");
  assert.equal(app.state.historyRecords.length, 2);
  assert.equal(app.state.historyProgress, "g1");

  // Exit the history view (mirror closeHistory's reset of records + progress).
  app.state.selectedHistoryId = null;
  app.state.historyRecords = [];
  app.state.historyProgress = null;

  // Re-enter the SAME old session: a fresh click is a full load (complete),
  // never a stale delta against the previous bundle.
  setFetch({
    records: [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")],
    progress: "g2", delivery: "full",
  });
  await app.openHistorySession("OLD");
  assert.ok(!String(__lastFetchUrl).includes("after="), "fresh re-entry is a full load");
  assert.equal(app.state.historyRecords.length, 2);
  assert.equal(app.state.historyProgress, "g2");

  // A WS-reconnect refresh of the re-entered session DOES walk the delta path
  // (the regression was that old sessions stayed pinned to a full reload): it
  // echoes the held token and appends only the gap, ending with the complete
  // record set — no full reload, no truncation, no duplication.
  setFetch({
    records: [asstRecord("C", 3, "s1", "discovery")],
    progress: "g3", delivery: "delta",
  });
  await app.openHistorySession("OLD", { incremental: true });
  assert.ok(String(__lastFetchUrl).includes("after=g2"), "reconnect echoes the held token (delta)");
  assert.equal(app.state.historyProgress, "g3");
  assert.equal(app.state.historyRecords.length, 3);
  assert.deepEqual(
    app.state.historyRecords.map((r) => r.message.content), ["A", "B", "C"]);
  assert.ok(uniqueKeys(app.state.historyRecords));
});

// -- buildNewFlowBody: New Task POST body (worktree isolation flag) ----------
check("buildNewFlowBody carries all fields and coerces booleans", () => {
  const body = app.buildNewFlowBody({
    machineId: "m1",
    task: "do the thing",
    taskType: "bugfix",
    discover: true,
    worktree: true,
    projectRoot: "/abs/path",
  });
  assert.deepEqual(body, {
    machine_id: "m1",
    task: "do the thing",
    task_type: "bugfix",
    discover: true,
    worktree: true,
    project_root: "/abs/path",
  });
});
check("buildNewFlowBody defaults worktree to false when unchecked", () => {
  const body = app.buildNewFlowBody({
    machineId: "m1",
    task: "t",
    taskType: "feature",
    discover: false,
    worktree: false,
    projectRoot: "/p",
  });
  assert.equal(body.worktree, false);
  assert.equal(body.discover, false);
});
check("buildNewFlowBody coerces truthy/falsy worktree to a real boolean", () => {
  const on = app.buildNewFlowBody({ worktree: 1 });
  assert.strictEqual(on.worktree, true);
  const off = app.buildNewFlowBody({ worktree: undefined });
  assert.strictEqual(off.worktree, false);
});

// -- renderSignature: stable / distinguishing diff-aware render hashing ------
check("renderSignature returns a string", () => {
  assert.equal(typeof app.renderSignature([1, "a", { x: 1 }]), "string");
});
check("renderSignature is stable for the same input", () => {
  const a = app.renderSignature(["flow1", "running", 3]);
  const b = app.renderSignature(["flow1", "running", 3]);
  assert.equal(a, b);
});
check("renderSignature is stable across object key insertion order", () => {
  // Logically-equal objects with different key order must hash identically so
  // key-order jitter never triggers a spurious DOM rebuild.
  const a = app.renderSignature({ status: "running", id: "f1", n: 2 });
  const b = app.renderSignature({ n: 2, id: "f1", status: "running" });
  assert.equal(a, b);
});
check("renderSignature differs when any field changes", () => {
  const base = app.renderSignature(["f1", "running", 3]);
  assert.notEqual(base, app.renderSignature(["f1", "paused", 3]));   // status
  assert.notEqual(base, app.renderSignature(["f1", "running", 4]));  // count
  assert.notEqual(base, app.renderSignature(["f2", "running", 3]));  // id
});
check("renderSignature distinguishes nested-field changes", () => {
  const a = app.renderSignature({ steps: [{ t: "analyze", s: "done" }] });
  const b = app.renderSignature({ steps: [{ t: "analyze", s: "running" }] });
  assert.notEqual(a, b);
});
check("renderSignature distinguishes null / undefined / missing", () => {
  assert.notEqual(app.renderSignature([null]), app.renderSignature([0]));
  assert.notEqual(app.renderSignature([null]), app.renderSignature([]));
  // Tolerates an undefined top-level input without throwing.
  assert.equal(typeof app.renderSignature(undefined), "string");
});

// -- resetRenderSignatures: clears the diff-aware cache ----------------------
check("resetRenderSignatures clears all cached keys", () => {
  app.state.renderSig.machines = "x";
  app.state.renderSig.flows = "y";
  app.resetRenderSignatures();
  assert.deepEqual(Object.keys(app.state.renderSig), []);
});

// -- projectBasename: readable project name from project_root ----------------
check("projectBasename returns the last path segment", () => {
  assert.equal(app.projectBasename("/data/cre/workspace/se3.0"), "se3.0");
  assert.equal(app.projectBasename("/srv/projects/my-app"), "my-app");
});
check("projectBasename strips trailing slashes", () => {
  assert.equal(app.projectBasename("/a/b/"), "b");
  assert.equal(app.projectBasename("/a/b///"), "b");
});
check("projectBasename handles the root path without throwing", () => {
  assert.equal(app.projectBasename("/"), "");
  assert.equal(app.projectBasename("///"), "");
});
check("projectBasename tolerates Windows-style separators", () => {
  assert.equal(app.projectBasename("C:\\work\\proj"), "proj");
  assert.equal(app.projectBasename("C:\\work\\proj\\"), "proj");
});
check("projectBasename returns '' for empty / non-string input", () => {
  assert.equal(app.projectBasename(""), "");
  assert.equal(app.projectBasename(null), "");
  assert.equal(app.projectBasename(undefined), "");
  assert.equal(app.projectBasename(123), "");
  assert.equal(app.projectBasename({}), "");
});

// -- projectDisplayLabel: worktree-aware project label -----------------------
check("projectDisplayLabel returns the basename for ordinary roots", () => {
  // Ordinary (non-worktree) roots must match projectBasename exactly.
  assert.equal(app.projectDisplayLabel("/data/cre/workspace/se3.0"), "se3.0");
  assert.equal(app.projectDisplayLabel("/srv/projects/my-app"), "my-app");
  assert.equal(app.projectDisplayLabel("/a/b/"), "b");
});

check("projectDisplayLabel labels worktree roots as '<name>（worktree）'", () => {
  assert.equal(
    app.projectDisplayLabel(
      "/data/cre/workspace/se3.0/se3/worktrees/" +
        "worktree-bug-discovery-se3-run-webui-se-20260623-101934-c6becdd0",
    ),
    "se3.0 (worktree)",
  );
});

check("projectDisplayLabel tolerates Windows-style worktree paths", () => {
  assert.equal(
    app.projectDisplayLabel("C:\\work\\proj\\se3\\worktrees\\wt-x"),
    "proj (worktree)",
  );
});

check("projectDisplayLabel falls back safely on degenerate input", () => {
  assert.equal(app.projectDisplayLabel(""), "");
  assert.equal(app.projectDisplayLabel(null), "");
  assert.equal(app.projectDisplayLabel(undefined), "");
  assert.equal(app.projectDisplayLabel(123), "");
  assert.equal(app.projectDisplayLabel({}), "");
  assert.equal(app.projectDisplayLabel("/"), "");
});

// -- machinesSignature / flowsSignature: per-field distinguishing (G2) -------

function sampleMachines() {
  return [
    {
      machine_id: "m1",
      hostname: "host-a",
      online: true,
      flows: [
        {
          flow_id: "f1",
          status: "running",
          task_description: "do the thing",
          task_type: "feature",
          progress: 0.5,
          current_step: "implement",
          current_step_index: 3,
          total_steps: 6,
          pending_calls: [],
        },
      ],
    },
    { machine_id: "m2", hostname: "host-b", online: false, flows: [] },
  ];
}

check("machinesSignature is stable for equal input", () => {
  const a = app.machinesSignature(sampleMachines(), "m1");
  const b = app.machinesSignature(sampleMachines(), "m1");
  assert.equal(a, b);
});

check("machinesSignature changes per visible field", () => {
  const base = app.machinesSignature(sampleMachines(), "m1");
  // selected machine change
  assert.notEqual(base, app.machinesSignature(sampleMachines(), "m2"));
  // online flip
  const offline = sampleMachines();
  offline[0].online = false;
  assert.notEqual(base, app.machinesSignature(offline, "m1"));
  // hostname change
  const renamed = sampleMachines();
  renamed[0].hostname = "host-z";
  assert.notEqual(base, app.machinesSignature(renamed, "m1"));
  // flow count change
  const moreFlows = sampleMachines();
  moreFlows[0].flows.push({ flow_id: "f2", status: "init" });
  assert.notEqual(base, app.machinesSignature(moreFlows, "m1"));
  // machine added / removed
  assert.notEqual(base, app.machinesSignature(sampleMachines().slice(0, 1), "m1"));
});

check("machinesSignature ignores fields the list does not paint", () => {
  const base = app.machinesSignature(sampleMachines(), "m1");
  const noisy = sampleMachines();
  noisy[0].some_internal_counter = 99;
  noisy[0].flows[0].progress = 0.9; // not part of the machine-list view
  assert.equal(base, app.machinesSignature(noisy, "m1"));
});

check("flowsSignature is stable for equal input", () => {
  const m = sampleMachines()[0];
  const a = app.flowsSignature(m, "m1", new Set());
  const b = app.flowsSignature(sampleMachines()[0], "m1", new Set());
  assert.equal(a, b);
});

check("flowsSignature changes per visible flow field", () => {
  const m = sampleMachines()[0];
  const base = app.flowsSignature(m, "m1", new Set());
  const mut = (fn) => {
    const x = sampleMachines()[0];
    fn(x.flows[0]);
    return app.flowsSignature(x, "m1", new Set());
  };
  assert.notEqual(base, mut((f) => (f.status = "paused")));      // status
  assert.notEqual(base, mut((f) => (f.progress = 0.9)));          // progress
  assert.notEqual(base, mut((f) => (f.current_step = "verify"))); // current_step
  assert.notEqual(base, mut((f) => (f.current_step_index = 4)));  // index
  assert.notEqual(base, mut((f) => (f.total_steps = 8)));         // total
  assert.notEqual(base, mut((f) => (f.task_type = "bugfix")));    // task_type
  assert.notEqual(base, mut((f) => (f.task_description = "new"))); // task
});

check("flowsSignature changes when only project_root changes", () => {
  // The flow card paints the project_root basename; a change to it (with all
  // other visible fields held constant) must re-key the signature so the card
  // rebuilds with the new project annotation.
  const base = app.flowsSignature(sampleMachines()[0], "m1", new Set());
  const moved = sampleMachines()[0];
  moved.flows[0].project_root = "/data/cre/other-project";
  assert.notEqual(base, app.flowsSignature(moved, "m1", new Set()));
});

check("flowsSignature changes when a pending call appears", () => {
  const base = app.flowsSignature(sampleMachines()[0], "m1", new Set());
  const withCall = sampleMachines()[0];
  withCall.flows[0].pending_calls = [
    { call_id: "c1", context: { flow_id: "f1" } },
  ];
  assert.notEqual(base, app.flowsSignature(withCall, "m1", new Set()));
});

check("flowsSignature changes with resumability and in-flight resume", () => {
  const failed = sampleMachines()[0];
  failed.flows[0].status = "failed";
  const base = app.flowsSignature(failed, "m1", new Set());
  // The same flow with an in-flight resume request must differ so the Resume
  // button can flip to its disabled "Resuming…" state.
  const resuming = app.flowsSignature(failed, "m1", new Set(["f1"]));
  assert.notEqual(base, resuming);
  // Array form (used in some tests) is accepted equivalently.
  assert.equal(resuming, app.flowsSignature(failed, "m1", ["f1"]));
});

check("flowsSignature changes when the selected machine changes", () => {
  const m = sampleMachines()[0];
  assert.notEqual(
    app.flowsSignature(m, "m1", new Set()),
    app.flowsSignature(m, "m2", new Set())
  );
});

check("flowsSignature handles a missing machine (empty state)", () => {
  const a = app.flowsSignature(null, "m1", new Set());
  assert.equal(typeof a, "string");
  // Different selection -> different empty-state signature.
  assert.notEqual(a, app.flowsSignature(null, "m2", new Set()));
});

// -- renderMachines / renderFlows: skip DOM rebuild when data is unchanged ---

check("renderMachines does not rebuild DOM when data is unchanged", () => {
  app.state.machines = sampleMachines();
  app.state.selectedMachineId = "m1";
  app.resetRenderSignatures();

  app.renderMachines();
  const list = document.getElementById("machine-list");
  const firstChildren = list.children.slice();
  assert.ok(firstChildren.length >= 2, "machines should have rendered");

  // Same data again -> the signature matches and the DOM is untouched: the same
  // child node objects survive (no innerHTML="" rebuild).
  app.renderMachines();
  const secondChildren = list.children.slice();
  assert.equal(secondChildren.length, firstChildren.length);
  for (let i = 0; i < firstChildren.length; i++) {
    assert.equal(secondChildren[i], firstChildren[i]);
  }

  // A real change (online flip) -> rebuild, new node objects.
  app.state.machines[0].online = false;
  app.renderMachines();
  const thirdChildren = list.children.slice();
  assert.notEqual(thirdChildren[0], firstChildren[0]);
});

check("renderFlows does not rebuild DOM when data is unchanged", () => {
  app.state.machines = sampleMachines();
  app.state.selectedMachineId = "m1";
  app.resetRenderSignatures();

  app.renderFlows();
  const panel = document.getElementById("flow-list");
  const firstChildren = panel.children.slice();
  assert.ok(firstChildren.length >= 1, "flows should have rendered");

  app.renderFlows();
  const secondChildren = panel.children.slice();
  assert.equal(secondChildren.length, firstChildren.length);
  for (let i = 0; i < firstChildren.length; i++) {
    assert.equal(secondChildren[i], firstChildren[i]);
  }

  // A real change (status) -> rebuild, new node objects.
  app.state.machines[0].flows[0].status = "paused";
  app.renderFlows();
  const thirdChildren = panel.children.slice();
  assert.notEqual(thirdChildren[0], firstChildren[0]);
});

check("renderFlows skips flows without a flow_id (empty-card defense)", () => {
  app.state.machines = [
    {
      machine_id: "m1",
      hostname: "host-a",
      online: true,
      // First entry mimics the archived-root flowless snapshot regression:
      // no flow_id/status. It must not become a card, and must not count
      // toward the non-empty state.
      flows: [
        { project_root: "/p/archived", issue_count: 3 },
        {
          flow_id: "f1",
          status: "running",
          task_description: "real flow",
          project_root: "/p/live",
        },
      ],
    },
  ];
  app.state.selectedMachineId = "m1";
  app.resetRenderSignatures();

  app.renderFlows();
  const panel = document.getElementById("flow-list");
  // Only the flow_id-bearing flow renders a card.
  assert.equal(panel.children.length, 1);

  // When every flow lacks a flow_id, the empty state shows.
  app.state.machines[0].flows = [{ project_root: "/p/archived", issue_count: 3 }];
  app.resetRenderSignatures();
  app.renderFlows();
  assert.equal(panel.children.length, 1);
  assert.equal(panel.children[0].className, "empty");
});

// -- flowSidebarSignature: diff-aware sidebar render guard (G3) --------------
const baseSidebarFlow = () => ({
  task_description: "Do a thing",
  flow_id: "f1",
  status: "running",
  task_type: "feature",
  current_step_index: 2,
  total_steps: 5,
  progress: 0.4,
  current_step: "implement",
  updated_at: 1700000000,
  step_history: [
    { step_type: "analyze", status: "completed", duration: 12 },
    { step_type: "implement", status: "running" },
  ],
});

check("flowSidebarSignature returns a string", () => {
  assert.equal(typeof app.flowSidebarSignature(baseSidebarFlow(), "m1", false), "string");
});
check("flowSidebarSignature is stable for identical inputs", () => {
  const a = app.flowSidebarSignature(baseSidebarFlow(), "m1", false);
  const b = app.flowSidebarSignature(baseSidebarFlow(), "m1", false);
  assert.equal(a, b);
});
check("flowSidebarSignature changes when status changes", () => {
  const base = app.flowSidebarSignature(baseSidebarFlow(), "m1", false);
  const f = baseSidebarFlow(); f.status = "paused"; // also flips resumable
  assert.notEqual(base, app.flowSidebarSignature(f, "m1", false));
});
check("flowSidebarSignature changes when progress changes", () => {
  const base = app.flowSidebarSignature(baseSidebarFlow(), "m1", false);
  const f = baseSidebarFlow(); f.progress = 0.8; f.current_step_index = 4;
  assert.notEqual(base, app.flowSidebarSignature(f, "m1", false));
});
check("flowSidebarSignature changes when step_history status changes", () => {
  const base = app.flowSidebarSignature(baseSidebarFlow(), "m1", false);
  const f = baseSidebarFlow(); f.step_history[1].status = "completed";
  assert.notEqual(base, app.flowSidebarSignature(f, "m1", false));
});
check("flowSidebarSignature changes when a step duration changes", () => {
  const base = app.flowSidebarSignature(baseSidebarFlow(), "m1", false);
  const f = baseSidebarFlow(); f.step_history[0].duration = 99;
  assert.notEqual(base, app.flowSidebarSignature(f, "m1", false));
});
check("flowSidebarSignature reads elapsed when duration is absent", () => {
  const f1 = baseSidebarFlow(); f1.step_history[1].elapsed = 7;
  const f2 = baseSidebarFlow(); // step_history[1] has neither duration nor elapsed
  assert.notEqual(
    app.flowSidebarSignature(f1, "m1", false),
    app.flowSidebarSignature(f2, "m1", false),
  );
});
check("flowSidebarSignature changes when resumability changes", () => {
  // failed status is resumable, running is not — flip resumable independently.
  const running = baseSidebarFlow();              // running → not resumable
  const failed = baseSidebarFlow(); failed.status = "failed"; // → resumable
  assert.notEqual(
    app.flowSidebarSignature(running, "m1", false),
    app.flowSidebarSignature(failed, "m1", false),
  );
});
check("flowSidebarSignature changes when resumeInProgress toggles", () => {
  const f = baseSidebarFlow(); f.status = "failed";
  assert.notEqual(
    app.flowSidebarSignature(f, "m1", false),
    app.flowSidebarSignature(f, "m1", true),
  );
});
check("flowSidebarSignature changes when machineId changes", () => {
  assert.notEqual(
    app.flowSidebarSignature(baseSidebarFlow(), "m1", false),
    app.flowSidebarSignature(baseSidebarFlow(), "m2", false),
  );
});
check("flowSidebarSignature tolerates missing/empty flow", () => {
  assert.equal(typeof app.flowSidebarSignature(null, null, false), "string");
  assert.equal(typeof app.flowSidebarSignature({}, "m1", false), "string");
});
check("flowSidebarSignature changes when only project_root changes", () => {
  // The sidebar Overview now prints a Project row derived from project_root, so
  // a change to it (all else held constant) must re-key the signature.
  const base = app.flowSidebarSignature(baseSidebarFlow(), "m1", false);
  const f = baseSidebarFlow(); f.project_root = "/data/cre/workspace/se3.0";
  assert.notEqual(base, app.flowSidebarSignature(f, "m1", false));
});

// -- interventionsSignature: per-field distinguishing (G4) -------------------
// The reply-panel diff-aware equality function must change its output whenever
// ANY visible dependency of the chip bar / reply-context panel / Interject
// button changes, and stay stable when nothing does. Each assertion below flips
// exactly one field and asserts the signature differs, plus one stability
// assertion for an unchanged repeat.
check("interventionsSignature is stable for identical input", () => {
  const entries = [
    { id: "call:c1", kind: "call", synthetic: false, prompt: "approve?",
      options: ["1"], callId: "c1", phase: null, afterimage: false },
  ];
  const rs = { targetId: "call:c1", pendingSendSettleKey: null,
    flowInterjectRequested: false, isActiveFlow: true, hasRealInterjection: false };
  assert.equal(
    app.interventionsSignature(entries, rs),
    app.interventionsSignature(entries.map((e) => ({ ...e })), { ...rs }),
  );
});
check("interventionsSignature distinguishes every visible entry field", () => {
  const base = [
    { id: "call:c1", kind: "call", synthetic: false, prompt: "approve?",
      options: ["1"], callId: "c1", phase: null, afterimage: false },
  ];
  const rs = { targetId: "call:c1", pendingSendSettleKey: null,
    flowInterjectRequested: false, isActiveFlow: true, hasRealInterjection: false };
  const baseSig = app.interventionsSignature(base, rs);
  const vary = (patch) =>
    app.interventionsSignature([{ ...base[0], ...patch }], rs);
  assert.notEqual(baseSig, vary({ id: "call:c2" }), "id");
  assert.notEqual(baseSig, vary({ kind: "interjection" }), "kind (→label/icon)");
  assert.notEqual(baseSig, vary({ synthetic: true }), "synthetic");
  assert.notEqual(baseSig, vary({ prompt: "deny?" }), "prompt");
  assert.notEqual(baseSig, vary({ options: ["1", "2"] }), "options");
  assert.notEqual(baseSig, vary({ callId: "c9" }), "callId");
  assert.notEqual(baseSig, vary({ phase: "pending" }), "phase");
  assert.notEqual(baseSig, vary({ afterimage: true }), "afterimage");
  // Adding / removing a pending call changes the entries list length.
  assert.notEqual(baseSig, app.interventionsSignature(
    [base[0], { ...base[0], id: "call:c2", callId: "c2" }], rs), "new pending call");
});
check("interventionsSignature distinguishes every reply-state field", () => {
  const entries = [
    { id: "call:c1", kind: "call", synthetic: false, prompt: "approve?",
      options: [], callId: "c1", phase: null, afterimage: false },
  ];
  const rs = { targetId: "call:c1", pendingSendSettleKey: null,
    flowInterjectRequested: false, isActiveFlow: true, hasRealInterjection: false };
  const baseSig = app.interventionsSignature(entries, rs);
  const vary = (patch) => app.interventionsSignature(entries, { ...rs, ...patch });
  // Selected target change (e.g. user picks a different chip).
  assert.notEqual(baseSig, vary({ targetId: "call:c2" }), "targetId");
  // Send going in-flight (pendingSendSettleKey set) must re-disable Send.
  assert.notEqual(baseSig, vary({ pendingSendSettleKey: "c1" }), "pendingSendSettleKey");
  assert.notEqual(baseSig, vary({ flowInterjectRequested: true }), "flowInterjectRequested");
  assert.notEqual(baseSig, vary({ isActiveFlow: false }), "isActiveFlow");
  assert.notEqual(baseSig, vary({ hasRealInterjection: true }), "hasRealInterjection");
});

// -- renderInterventions (DOM): no-change status_update is a zero-DOM skip ----
// The core of the textarea-jank fix: a repeated render with identical data must
// NOT rebuild the chip bar or the reply-context panel, so the large reply
// textarea never reflows. A real data change must still rebuild immediately.
check("renderInterventions: identical data skips DOM rebuild, real change rebuilds", () => {
  // Clean reply-panel state so computeInterventions is deterministic.
  app.state.localInterjections = [];
  app.state.interjectionPhases = {};
  app.state.interjectionConsumedAfterimages = [];
  app.state.flowInterjectRequested = false;
  app.state.flowSyntheticInterjectPending = false;
  app.state.flowReplyTargetId = null;
  app.state.pendingSendSettleKey = null;
  app.resetRenderSignatures();

  const region = document.getElementById("flow-interventions");
  const ctx = document.getElementById("flow-reply-context");
  const flow = (calls) => ({ status: "running", pending_calls: calls });
  const calls = [{ call_id: "c1", kind: "call", prompt: "approve?" }];

  // First render builds the chip + reply-context block from scratch.
  app.renderInterventions(flow(calls));
  assert.equal(region.children.length, 1, "first render builds one chip");
  const chipBefore = region.children[0];
  const ctxChildrenBefore = ctx.childNodes.slice();
  assert.ok(ctxChildrenBefore.length > 0, "reply-context populated on first render");

  // Second render with logically-identical data (a fresh flow object + fresh
  // pending_calls array, mimicking an empty ws status_update) must skip: the
  // chip node and every reply-context child node keep their object identity,
  // proving zero DOM mutation.
  app.renderInterventions(flow([{ call_id: "c1", kind: "call", prompt: "approve?" }]));
  assert.equal(region.children.length, 1, "chip count unchanged on skip");
  assert.strictEqual(region.children[0], chipBefore,
    "chip node identity preserved — chip bar not rebuilt on no-change render");
  const ctxChildrenAfter = ctx.childNodes.slice();
  assert.equal(ctxChildrenAfter.length, ctxChildrenBefore.length,
    "reply-context child count unchanged on skip");
  for (let i = 0; i < ctxChildrenBefore.length; i++) {
    assert.strictEqual(ctxChildrenAfter[i], ctxChildrenBefore[i],
      "reply-context node identity preserved on no-change render (no textarea reflow)");
  }

  // A genuine change — a new pending call arrives — must rebuild immediately.
  app.renderInterventions(flow([
    { call_id: "c1", kind: "call", prompt: "approve?" },
    { call_id: "c2", kind: "cli_confirm", prompt: "press 1", options: ["1"] },
  ]));
  assert.equal(region.children.length, 2, "new pending call rebuilds the chip bar");
  assert.notStrictEqual(region.children[0], chipBefore,
    "rebuild replaces the chip nodes (fresh DOM)");
});

// Each kind of real change individually forces a rebuild from a skipped state.
check("renderInterventions: phase / target / pendingSend changes each rebuild", () => {
  app.state.localInterjections = [];
  app.state.interjectionPhases = {};
  app.state.interjectionConsumedAfterimages = [];
  app.state.flowInterjectRequested = false;
  app.state.flowSyntheticInterjectPending = false;
  app.state.flowReplyTargetId = null;
  app.state.pendingSendSettleKey = null;
  app.resetRenderSignatures();

  const region = document.getElementById("flow-interventions");
  const flow = () => ({
    status: "running",
    pending_calls: [
      { call_id: "c1", kind: "call", prompt: "approve?" },
      { call_id: "c2", kind: "call", prompt: "continue?" },
    ],
  });

  app.renderInterventions(flow());
  let chip0 = region.children[0];
  // Re-render identical → skip (identity preserved).
  app.renderInterventions(flow());
  assert.strictEqual(region.children[0], chip0, "no-change re-render skips");

  // Selected target change rebuilds (a different chip becomes .selected).
  app.state.flowReplyTargetId = "call:c2";
  app.renderInterventions(flow());
  assert.notStrictEqual(region.children[0], chip0, "target change rebuilds");
  chip0 = region.children[0];

  // Re-render identical again → skip.
  app.renderInterventions(flow());
  assert.strictEqual(region.children[0], chip0, "no-change re-render skips again");

  // Send going in-flight (pendingSendSettleKey set) rebuilds so Send disables.
  app.state.pendingSendSettleKey = "c2";
  app.renderInterventions(flow());
  assert.notStrictEqual(region.children[0], chip0, "pendingSendSettleKey change rebuilds");
  assert.equal(document.getElementById("flow-reply-submit").disabled, true,
    "Send disabled while a submission is in flight");
});

console.log(`\n${passed} checks passed.`);
