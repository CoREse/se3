// Lightweight, dependency-free assertion script for the web console's
// running-flow chat view classification contract.
//
//   Run:  node tests/frontend/interaction_view.test.mjs
//
// This file imports the *real* pure helpers exported by the web frontend
// (src/se3/server/static/app.js) rather than reimplementing them. app.js has
// no build step and no module system in the browser, but it appends a
// `module.exports` block guarded by `typeof module !== "undefined"` so Node
// can `require()` the same source the browser ships. Testing the shipped
// functions directly means a divergence in app.js is caught here — a
// reimplemented reference copy could silently drift out of sync, so it is
// deliberately avoided.
//
// Why this matters (echoes issues 109 / 110): the collapse/expand decision is
// made from the structured `role` field of each record — never by guessing
// from message text — so prompt-template messages collapse to a chip and
// genuine assistant output / intervention items stay expanded,
// deterministically.

import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const app = require(
  path.join(here, "..", "..", "src", "se3", "server", "static", "app.js"),
);

// ---------------------------------------------------------------------------
// Tiny assertion harness
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;

function eq(actual, expected, label) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    passed += 1;
  } else {
    failed += 1;
    console.error(`  FAIL: ${label}\n    expected ${e}\n    got      ${a}`);
  }
}

// `collapseDecision` is the chat view's actual collapse rule: a raw record is
// first normalized (which folds `human` into `user`) and the normalized role
// drives `isCollapsibleRole`. Both halves come straight from app.js.
function collapseDecision(rawRecord) {
  const norm = app.normalizeRecord(rawRecord);
  return app.isCollapsibleRole(norm.role);
}

// ---------------------------------------------------------------------------
// normalizeRecord: role classification is structural, not text-based
// ---------------------------------------------------------------------------

eq(app.normalizeRecord({ message: { role: "assistant", content: "x" } }).role,
   "assistant", "assistant role preserved");
eq(app.normalizeRecord({ message: { role: "user", content: "x" } }).role,
   "user", "user role preserved");
eq(app.normalizeRecord({ message: { role: "human", content: "x" } }).role,
   "user", "human folds into user");
eq(app.normalizeRecord({ message: { role: "system", content: "x" } }).role,
   "system", "system role preserved");
eq(app.normalizeRecord({ message: { role: "Assistant", content: "x" } }).role,
   "assistant", "role classification is case-insensitive");

// ---------------------------------------------------------------------------
// isCollapsibleRole + collapseDecision (chip-collapse rule)
// ---------------------------------------------------------------------------

eq(app.isCollapsibleRole("user"), true, "user prompt collapses to a chip");
eq(app.isCollapsibleRole("system"), true, "system prompt collapses to a chip");
eq(app.isCollapsibleRole("assistant"), false, "assistant output stays expanded");

eq(collapseDecision({ message: { role: "assistant", content: "hello" } }),
   false, "assistant record stays expanded");
eq(collapseDecision({ message: { role: "user", content: "hello" } }),
   true, "user record collapses");
eq(collapseDecision({ message: { role: "human", content: "hello" } }),
   true, "human record collapses (folded to user)");
eq(collapseDecision({ message: { role: "system", content: "hello" } }),
   true, "system record collapses");

// Determinism guard: identical content, different role -> opposite decision,
// proving the decision never inspects message text.
eq(collapseDecision({ message: { role: "assistant", content: "SAME" } })
     === collapseDecision({ message: { role: "system", content: "SAME" } }),
   false, "collapse decision is driven by role, not text");

// ---------------------------------------------------------------------------
// normalizeKind: interaction-call kind branching
// ---------------------------------------------------------------------------

eq(app.normalizeKind("call"), "call", "call kind");
eq(app.normalizeKind("interjection"), "interjection", "interjection kind");
eq(app.normalizeKind("retry_decision"), "retry_decision", "retry_decision kind");
eq(app.normalizeKind("cli_confirm"), "cli_confirm", "cli_confirm kind");
eq(app.normalizeKind("discovery_confirm"), "discovery_confirm", "discovery_confirm kind");
eq(app.normalizeKind("mystery"), "call", "unknown kind degrades to call");
eq(app.normalizeKind(undefined), "call", "missing kind degrades to call");

// ---------------------------------------------------------------------------
// chipLabel: one-line label for a collapsed prompt-template message
// ---------------------------------------------------------------------------

eq(app.chipLabel({ role: "system", stepType: "discovery" }),
   "system prompt · discovery", "system chip with step context");
eq(app.chipLabel({ role: "user", stepType: "" }),
   "user prompt", "user chip without context");

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\ninteraction_view: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
