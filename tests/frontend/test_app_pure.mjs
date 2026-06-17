I'll read the actual file to see the conflict markers.The working tree already contains both blocks unioned, with a single final `console.log`. Let me verify it's clean and run the test.The working tree already holds the correct union resolution (G2's `machinesSignature`/`flowsSignature`/`renderMachines`/`renderFlows` block followed by G3's `flowSidebarSignature` block, with a single trailing `console.log`), it parses cleanly, and has no conflict markers. This matches the resolved content. Outputting it:

```javascript
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
check("STEP_REPORT_RENDERERS covers the 12 named step types", () => {
  const expected = [
    "analyze", "plan", "implement", "test", "self_check", "verify_spec",
    "update_spec", "spec_gate", "commit", "version_analyze", "summarize",
    "discovery",
  ];
  for (const t of expected) {
    assert.equal(
      typeof app.STEP_REPORT_RENDERERS[t], "function",
      "missing renderer for " + t,
    );
  }
  // Exactly 12 — the prior 11 plus the spec_gate summary renderer (PROPOSE/
  // DESIGN are deprecated and intentionally excluded; DISCOVERY adds a frontend
  // renderer).
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

check("mergeHistoryResponse unrecognised delivery defaults to full", () => {
  const r1 = asstRecord("A1", 1, "s1", "discovery");
  const resp = { records: [r1], progress: "p" };   // no delivery field
  const out = app.mergeHistoryResponse(resp, []);
  assert.equal(out.render, "full");
  assert.equal(out.records.length, 1);
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

// -- Regression A: user Layer 3 stably reaches the original .jsonl envelope --
// A user record carries raw_json=[] and no raw_ndjson, so the shared
// makeRawToggle (无 raw → null) would leave Layer 3 empty. The user side must
// instead fall back to the record's original .jsonl envelope so "查看原始" is
// always reachable — without weakening makeRawToggle's null contract.

// Build a user marker record carrying NO second-layer raw payload (raw_json=[],
// raw_ndjson absent), in the real daemon envelope shape {step_id, step_type,
// message}. normalizeRecord must expose that envelope at norm.raw.envelThe file is too large to echo in full. The working tree already holds the correct union resolution, so I'll stage it and verify the tests pass.The conflict is resolved and staged. Verifying the merged test suite runs clean:The `app.js` source has a stray prose line at the top — it was corrupted during the merge. Let me inspect it.The `app.js` working tree is corrupted — an LLM resolution narrative got written into the file instead of code. Let me assess the damage and recover from git.The working tree `app.js` was clobbered with 11 lines of LLM narrative by a prior agent. The staged index copy is intact. Let me restore it.The staged index app.js is also corrupted (a prior agent ran `git add` on garbage). I must reconstruct app.js by doing a proper 3-way merge of HEAD and MERGE_HEAD. Let me set up the real merge.There's exactly one conflict region in the `module.exports` block. Let me inspect it.The resolution is to keep both export sets. Let me write the resolved region.The resolved app.js parses cleanly with no markers. Let me install it, restore the test file too (verify it's intact), and run the full suite.All 690 Node checks pass. Now let me verify the test file is genuinely the resolved union (not also corrupted) and run the pytest rendering suite.Both conflicts are fully resolved. Let me confirm final git state.Both files are resolved and staged; no unmerged paths remain.

## Summary

The merge had two issues — and a prior agent had corrupted `app.js` by writing its narrative response into the file (the staged index copy was 11 lines of prose instead of ~11k lines of code).

**`src/se3/server/static/app.js`** — Recovered by re-running a clean 3-way merge from the merge base (`8c87bd3`), HEAD (G2), and MERGE_HEAD (G3). The only real conflict was in the `module.exports` block; I kept both export sets:
- G2: `machinesSignature`, `flowsSignature`, `renderMachines`, `renderFlows`
- G3: `flowSidebarSignature`

**`tests/frontend/test_app_pure.mjs`** — Both branches appended distinct test blocks before the final `console.log`; resolved as a union (G2's machine/flow signature suite followed by G3's sidebar signature suite), single trailing `console.log`.

Verification:
- No conflict markers in either file.
- `app.js` passes `node --check`.
- Node suite: **690 checks passed** (includes G2's 13 and G3's 11 new checks).
- `pytest tests/server/test_app_js_rendering.py`: **41 passed**.

The merge is ready to commit (`git commit` to finish — I'll leave that to you per the commit-on-request convention).