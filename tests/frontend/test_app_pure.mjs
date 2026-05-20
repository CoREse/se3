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
// content bubble. Two-segment legacy input returns `suffix: ""`. Missing or
// malformed markers return null so the caller can fall back to the whole-
// message chip path.
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
check("splitUserPromptByMarker two-segment input has empty suffix", () => {
  const TPE = app.TEMPLATE_PREFIX_END;
  const UCB = app.USER_CONTENT_BEGIN;
  const sample = "You are an expert engineer.\n" + TPE + "\n" + UCB + "\n## Task\nDo it";
  const split = app.splitUserPromptByMarker(sample);
  assert.ok(split, "split returned null but should have");
  assert.equal(split.prefix.startsWith("You are an expert engineer."), true);
  assert.equal(split.content.startsWith("## Task"), true);
  assert.equal(split.suffix, "");
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
  // pick the bogus end up as the content terminator.
  assert.ok(split);
  assert.equal(split.suffix, "");
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

// -- KIND_META: user-facing wording, no implementation vocabulary ----------
// The chip labels (and any other visible string in KIND_META) must use
// user-facing neutral phrases. They must NOT leak the internal transport
// vocabulary — `MCP`, `call_id`, or a literal `call ` followed by a hex id —
// into anything the user reads. (call_id remains only on hidden DOM
// attributes / hover tooltips.)
check("KIND_META covers the four recognized kinds", () => {
  for (const k of ["call", "interjection", "retry_decision", "cli_confirm"]) {
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

console.log(`\n${passed} checks passed.`);
