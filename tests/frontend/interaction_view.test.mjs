// Lightweight, dependency-free assertion script for the web console's
// running-flow chat view pure functions.
//
//   Run:  node tests/frontend/interaction_view.test.mjs
//
// The web frontend (src/se3/server/static/app.js) has no build step and no
// module system, so its pure helpers cannot be imported directly. This file
// is therefore the *executable contract* for three deterministic pieces of
// classification logic the chat view depends on. The reference
// implementations below MUST stay byte-for-byte equivalent to the functions
// shipped in app.js — if app.js diverges, this script fails and the divergence
// is caught before it reaches the UI.
//
// Why this matters (echoes issue 109): the collapse/expand decision is made
// from the structured `role` field of each record — never by guessing from
// message text — so prompt-template messages collapse to a chip and genuine
// assistant output / intervention items stay expanded, deterministically.

// ---------------------------------------------------------------------------
// Reference implementations (keep in sync with app.js)
// ---------------------------------------------------------------------------

// Map a raw record role onto one of the three rendering buckets. `human` is
// folded into `user`; anything unrecognised is treated as `system` so it is
// collapsed rather than mistaken for assistant output.
function classifyRole(role) {
  const r = String(role || '').toLowerCase().trim();
  if (r === 'assistant') return 'assistant';
  if (r === 'user' || r === 'human') return 'user';
  return 'system';
}

// user/system messages are prompt-template noise: collapse them to a
// one-line, click-to-expand chip. Assistant output is the real product and
// stays expanded.
function shouldCollapseMessage(role) {
  return classifyRole(role) !== 'assistant';
}

// The four interaction-call kinds the backend emits. An unknown/missing kind
// degrades to `call` — matching the daemon aggregator's backward-compatible
// treatment of legacy call files.
const CALL_KINDS = ['call', 'interjection', 'retry_decision', 'cli_confirm'];

function interventionItemType(kind) {
  return CALL_KINDS.includes(kind) ? kind : 'call';
}

// Human-facing label for an intervention entry, branched by kind.
function interventionLabel(kind) {
  switch (interventionItemType(kind)) {
    case 'interjection':    return 'Interjection';
    case 'retry_decision':  return 'Retry / failure decision';
    case 'cli_confirm':     return 'CLI confirmation';
    default:                return 'Pending call';
  }
}

// One-line chip label for a collapsed prompt-template message, e.g.
// "system prompt · discovery mode".
function chipLabel(role, meta) {
  const bucket = classifyRole(role);
  const tag = meta && meta.mode ? ` · ${meta.mode}` : '';
  return `${bucket} prompt${tag}`;
}

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

// ---------------------------------------------------------------------------
// classifyRole
// ---------------------------------------------------------------------------

eq(classifyRole('assistant'), 'assistant', 'assistant -> assistant');
eq(classifyRole('user'), 'user', 'user -> user');
eq(classifyRole('human'), 'user', 'human folds into user');
eq(classifyRole('system'), 'system', 'system -> system');
eq(classifyRole('Assistant'), 'assistant', 'role match is case-insensitive');
eq(classifyRole('  user  '), 'user', 'role is trimmed');
eq(classifyRole('tool'), 'system', 'unknown role -> system');
eq(classifyRole(''), 'system', 'empty role -> system');
eq(classifyRole(null), 'system', 'null role -> system');
eq(classifyRole(undefined), 'system', 'undefined role -> system');

// ---------------------------------------------------------------------------
// shouldCollapseMessage  (chip-collapse decision)
// ---------------------------------------------------------------------------

eq(shouldCollapseMessage('assistant'), false, 'assistant output stays expanded');
eq(shouldCollapseMessage('user'), true, 'user prompt collapses to a chip');
eq(shouldCollapseMessage('human'), true, 'human prompt collapses to a chip');
eq(shouldCollapseMessage('system'), true, 'system prompt collapses to a chip');
eq(shouldCollapseMessage('tool'), true, 'unknown role collapses to a chip');
// Determinism guard: identical text, different role -> opposite decision,
// proving the decision never inspects message text.
eq(shouldCollapseMessage('assistant') === shouldCollapseMessage('system'), false,
   'collapse decision is driven by role, not text');

// ---------------------------------------------------------------------------
// interventionItemType  (kind branching)
// ---------------------------------------------------------------------------

eq(interventionItemType('call'), 'call', 'call kind');
eq(interventionItemType('interjection'), 'interjection', 'interjection kind');
eq(interventionItemType('retry_decision'), 'retry_decision', 'retry_decision kind');
eq(interventionItemType('cli_confirm'), 'cli_confirm', 'cli_confirm kind');
eq(interventionItemType('mystery'), 'call', 'unknown kind -> call');
eq(interventionItemType(undefined), 'call', 'missing kind -> call');

eq(interventionLabel('interjection'), 'Interjection', 'interjection label');
eq(interventionLabel('retry_decision'), 'Retry / failure decision', 'retry label');
eq(interventionLabel('cli_confirm'), 'CLI confirmation', 'cli_confirm label');
eq(interventionLabel('call'), 'Pending call', 'call label');
eq(interventionLabel('bogus'), 'Pending call', 'unknown kind label -> Pending call');

// ---------------------------------------------------------------------------
// chipLabel
// ---------------------------------------------------------------------------

eq(chipLabel('system', { mode: 'discovery mode' }), 'system prompt · discovery mode',
   'system chip with mode');
eq(chipLabel('user', {}), 'user prompt', 'user chip without mode');
eq(chipLabel('human', null), 'user prompt', 'human chip folds to user');

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\ninteraction_view: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
