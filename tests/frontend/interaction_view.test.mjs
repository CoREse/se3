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
// Defect 2: optimistic-echo dedup (reconcileLocalEchoes / comparableUserText)
// ---------------------------------------------------------------------------
//
// `appendLocalReply` splices an optimistic `__localEcho` user record so a
// submitted reply shows instantly. The daemon later pushes its authoritative
// copy of the same reply with a different step_id / timestamp (hence a
// different recordKey), so the identity-based merge dedup cannot pair them and
// the desktop reply renders twice. `reconcileLocalEchoes` removes the echo once
// the authoritative record arrives, keeping the reply exactly once without
// losing or reordering any record.

const TEMPLATE_PREFIX_END = "<!--SE3:TEMPLATE_END-->";
const USER_CONTENT_BEGIN = "<!--SE3:USER_CONTENT-->";
const USER_CONTENT_END = "<!--SE3:USER_CONTENT_END-->";

function echoRecord(text, ts, priorAuth) {
  const rec = {
    step_id: "interaction",
    __localEcho: true,
    __localEchoText: text,
    message: { role: "user", content: text, timestamp: ts, step_type: "Reply response" },
  };
  if (priorAuth != null) rec.__localEchoPriorAuth = priorAuth;
  return rec;
}
function daemonUser(text, ts, stepId) {
  return {
    step_id: stepId || "03_discovery_abc",
    step_type: "discovery",
    message: { role: "user", content: text, timestamp: ts },
  };
}
function assistant(text, ts) {
  return { step_id: "03_discovery_abc", step_type: "discovery",
    message: { role: "assistant", content: text, timestamp: ts } };
}
function countUserRecords(records) {
  return records.filter((r) => app.normalizeRecord(r).role === "user").length;
}

// -- comparableUserText: trim + marker-literal extraction --------------------
eq(app.comparableUserText("  hello world  "), "hello world",
   "comparableUserText trims surrounding whitespace");
eq(app.comparableUserText(
     `boilerplate${TEMPLATE_PREFIX_END}${USER_CONTENT_BEGIN}\nhello world\n${USER_CONTENT_END}\nsuffix`),
   "hello world",
   "comparableUserText extracts the literal user-content section from a marker-wrapped prompt");
eq(app.comparableUserText(123), "", "comparableUserText degrades non-string to empty");

// -- positive: echo + matching daemon record dedups to one user bubble -------
{
  const merged = [echoRecord("hello", 2000), daemonUser("hello", "2026-06-08T10:00:00Z")];
  const out = app.reconcileLocalEchoes(merged);
  eq(countUserRecords(out), 1, "echo + matching daemon record collapse to a single user record");
  eq(out.length, 1, "the optimistic echo is removed, leaving only the daemon record");
  eq(out[0].__localEcho === true, false, "the surviving record is the authoritative daemon record, not the echo");
  eq(out !== merged, true, "a changed result is a new array (forces a full re-render)");
}

// -- marker-wrapped daemon record still matches the echo's literal text ------
{
  const wrapped = `pre${TEMPLATE_PREFIX_END}${USER_CONTENT_BEGIN}\nplease continue\n${USER_CONTENT_END}\npost`;
  const merged = [echoRecord("please continue", 2000), daemonUser(wrapped, "2026-06-08T10:00:00Z")];
  const out = app.reconcileLocalEchoes(merged);
  eq(out.length, 1, "marker-wrapped daemon record dedups the echo (literal compare)");
  eq(out[0].__localEcho === true, false, "marker case keeps the authoritative daemon record");
}

// -- negative: no matching daemon record → echo is NOT removed ---------------
{
  const merged = [assistant("thinking", 1000), echoRecord("hello", 2000), daemonUser("different reply", "2026-06-08T10:00:00Z")];
  const out = app.reconcileLocalEchoes(merged);
  eq(out.length, 3, "with no content match the echo is preserved (no reply lost)");
  eq(countUserRecords(out), 2, "both distinct user replies survive");
  eq(out === merged, true, "an unchanged result returns the same array reference (keeps cheap append path)");
}

// -- daemon record never dropped; other records & order preserved ------------
{
  const a = assistant("A1", 1000);
  const e = echoRecord("hello", 2000);
  const d = daemonUser("hello", "2026-06-08T10:00:03Z");
  const out = app.reconcileLocalEchoes([a, e, d]);
  eq(out.length, 2, "only the echo is removed; the daemon user record stays");
  eq(out[0] === a, true, "preceding assistant record is preserved in place");
  eq(out[1] === d, true, "the authoritative daemon record is preserved (never dropped)");
}

// -- guard: a lone echo with no authoritative twin is never removed ----------
{
  const merged = [echoRecord("solo", 2000)];
  const out = app.reconcileLocalEchoes(merged);
  eq(out.length, 1, "a single pending echo with no daemon record is kept");
  eq(out === merged, true, "lone echo returns the same array reference (no change)");
}

// -- duplicate reply text: new echo survives until ITS OWN daemon record lands
// Regression: repeated identical replies (e.g. "yes" / "continue"). The first
// "yes" already has its authoritative daemon record; the user sends "yes" again.
// A reconcile pass triggered by an unrelated append must NOT remove the new echo
// just because the earlier "yes" is in the conversation — the new echo's own
// daemon copy has not arrived yet (priorAuth=1, authCount=1).
{
  const d1 = daemonUser("yes", "2026-06-08T10:00:00Z");
  const e2 = echoRecord("yes", 3000, 1); // created when 1 authoritative "yes" existed
  const merged = [d1, e2];
  const out = app.reconcileLocalEchoes(merged);
  eq(out.length, 2, "new duplicate echo is kept while only its OWN daemon record is missing");
  eq(countUserRecords(out), 2, "both the earlier daemon 'yes' and the pending echo survive");
  eq(out === merged, true, "no change → same array reference (no flicker, cheap append path)");
}

// -- duplicate reply text: echo removed once its own daemon record arrives -----
{
  const d1 = daemonUser("yes", "2026-06-08T10:00:00Z", "03_a");
  const e2 = echoRecord("yes", 3000, 1);
  const d2 = daemonUser("yes", "2026-06-08T10:01:00Z", "05_b");
  const out = app.reconcileLocalEchoes([d1, e2, d2]);
  eq(out.length, 2, "once the second daemon 'yes' lands, the echo collapses away");
  eq(countUserRecords(out), 2, "exactly two authoritative 'yes' bubbles remain");
  eq(out.some((r) => r.__localEcho === true), false, "no optimistic echo survives");
}

// -- two simultaneous duplicate echoes, one daemon record: remove only one -----
// Both echoes were created before any authoritative "go" existed, so they carry
// distinct ranks by creation order: e1 rank 0 (no prior copy), e2 rank 1 (one
// pending echo, e1, already present). One daemon "go" has arrived → exactly one
// echo (the earliest, rank 0) is removed.
{
  const e1 = echoRecord("go", 2000, 0);
  const e2 = echoRecord("go", 3000, 1);
  const d1 = daemonUser("go", "2026-06-08T10:00:00Z");
  const out = app.reconcileLocalEchoes([e1, d1, e2]);
  eq(countUserRecords(out), 2, "one daemon record clears exactly one of two pending echoes");
  eq(out.includes(d1), true, "the authoritative daemon record is always kept");
  eq(out.filter((r) => r.__localEcho === true).length, 1, "one pending echo still awaits its own daemon copy");
  eq(out[0] === e1, false, "the earliest echo (whose daemon copy landed) is the one removed");
  eq(out.includes(e2), true, "the later pending echo (rank 1) is preserved");
}

// -- three simultaneous identical echoes, daemon records arrive one at a time ---
// Regression for the cumulative-removal bug: sending the same reply (e.g. "yes")
// three times rapidly creates echo1/echo2/echo3 with ranks 0/1/2. Each daemon
// record must clear exactly ONE more echo — never sweep away all remaining
// pending echoes at once, which would make a not-yet-confirmed reply transiently
// vanish (漏显) until its own daemon record lands.
{
  const e1 = echoRecord("yes", 1000, 0);
  const e2 = echoRecord("yes", 2000, 1);
  const e3 = echoRecord("yes", 3000, 2);

  // After daemon #1: echo1 removed; echo2, echo3 + 1 authoritative remain (3 bubbles).
  const d1 = daemonUser("yes", "2026-06-08T10:00:00Z", "03_a");
  const pass1 = app.reconcileLocalEchoes([e1, e2, e3, d1]);
  eq(countUserRecords(pass1), 3, "after the first daemon record, 3 user bubbles remain (echo2, echo3, daemon1)");
  eq(pass1.filter((r) => r.__localEcho === true).length, 2, "two pending echoes still await their own daemon copies");
  eq(pass1.includes(e1), false, "the earliest echo (rank 0) is removed by the first daemon record");

  // After daemon #2: only echo2 additionally removed; echo3 + 2 authoritative remain (3 bubbles).
  const d2 = daemonUser("yes", "2026-06-08T10:01:00Z", "05_b");
  const pass2 = app.reconcileLocalEchoes([e2, e3, d1, d2]);
  eq(countUserRecords(pass2), 3, "after the second daemon record, 3 user bubbles remain (echo3, daemon1, daemon2)");
  eq(pass2.filter((r) => r.__localEcho === true).length, 1, "the third reply's echo stays visible until its own daemon copy lands");
  eq(pass2.includes(e3), true, "the still-pending third echo (rank 2) is NOT removed by the second daemon record");

  // After daemon #3: the last echo collapses away; 3 authoritative bubbles remain.
  const d3 = daemonUser("yes", "2026-06-08T10:02:00Z", "07_c");
  const pass3 = app.reconcileLocalEchoes([e3, d1, d2, d3]);
  eq(countUserRecords(pass3), 3, "after the third daemon record, exactly 3 authoritative bubbles remain");
  eq(pass3.some((r) => r.__localEcho === true), false, "no optimistic echo survives once all daemon copies arrive");
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\ninteraction_view: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
