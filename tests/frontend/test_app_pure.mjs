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
check("running flow with no calls gets a synthetic interjection entry", () => {
  const entries = app.computeInterventions({ status: "running", pending_calls: [] });
  assert.equal(entries.length, 1);
  assert.equal(entries[0].kind, "interjection");
  assert.equal(entries[0].synthetic, true);
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
  // 2 calls + 1 synthetic interjection (no interjection kind present).
  assert.equal(entries.length, 3);
  assert.equal(entries[0].callId, "c1");
  assert.equal(entries[1].kind, "cli_confirm");
  assert.deepEqual(entries[1].options, ["1", "2"]);
  assert.equal(entries[2].kind, "interjection");
});
check("explicit interjection call suppresses the synthetic one", () => {
  const entries = app.computeInterventions({
    status: "running",
    pending_calls: [{ call_id: "i1", kind: "interjection", prompt: "ctrl-c" }],
  });
  assert.equal(entries.length, 1);
  assert.equal(entries[0].callId, "i1");
  assert.equal(entries[0].synthetic, false);
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

console.log(`\n${passed} checks passed.`);
