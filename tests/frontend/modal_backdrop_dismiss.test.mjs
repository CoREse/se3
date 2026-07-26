/*
 * Static-source tests guarding that the web console's modal windows can no
 * longer be dismissed by clicking the backdrop (the area outside the modal
 * card) — they now close ONLY via their × / cancel buttons.
 *
 * Background: every modal used to register a backdrop click listener of the
 * form `<modal>.addEventListener("click", e => { if (e.target.id === "<modal>")
 * close...() })`. An accidental click outside the card wiped everything the
 * user had typed. Those 7 listeners were removed; this suite reads the app.js
 * source text and asserts:
 *   (a) none of the 7 target modal ids still carry a backdrop dismiss listener;
 *   (b) every modal keeps its × / cancel close-button binding;
 *   (c) the mobile flow-sidebar drawer backdrop and the nav-menu outside-click
 *       dismiss are untouched (different semantics, intentionally preserved).
 *
 * Run manually:  node tests/frontend/modal_backdrop_dismiss.test.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const APP_JS = path.join(here, "..", "..", "src", "tianluo", "server", "static", "app.js");
const js = readFileSync(APP_JS, "utf-8");

let passed = 0;
function check(name, fn) {
  fn();
  passed += 1;
  console.log("  ok -", name);
}

// The 7 modals whose backdrop dismiss was removed.
const BACKDROP_MODALS = [
  "issue-modal",
  "issue-action-modal",
  "end-session-modal",
  "issue-launch-modal",
  "keys-modal",
  "users-modal",
  "new-task-modal",
];

// ---------------------------------------------------------------------------
// (a) No backdrop dismiss listener survives for any of the 7 modals.
// ---------------------------------------------------------------------------
for (const id of BACKDROP_MODALS) {
  check(`no backdrop dismiss listener for ${id}`, () => {
    // The removed pattern guarded close() on `e.target.id === "<modal>"`.
    assert.ok(
      !js.includes(`e.target.id === "${id}"`),
      `app.js still references e.target.id === "${id}" — backdrop dismiss must be removed`,
    );
  });
}

check("no `e.target.id ===` backdrop pattern remains at all", () => {
  // Every backdrop dismiss used this exact comparison shape; none should be
  // left after the removal.
  assert.ok(
    !js.includes("e.target.id ==="),
    "app.js still contains an `e.target.id ===` backdrop dismiss comparison",
  );
});

// ---------------------------------------------------------------------------
// (b) Each modal keeps its explicit × / cancel close-button binding.
// ---------------------------------------------------------------------------
const CLOSE_BUTTON_BINDINGS = [
  // [needle, human label]
  ['$("issue-modal-close").addEventListener("click", closeIssueModal)', "issue-modal ×"],
  ['$("issue-action-cancel").addEventListener("click", closeIssueActionModal)', "issue-action cancel"],
  ['$("issue-action-close").addEventListener("click", closeIssueActionModal)', "issue-action ×"],
  ['endCancel.addEventListener("click", closeEndSessionModal)', "end-session cancel"],
  ['endClose.addEventListener("click", closeEndSessionModal)', "end-session ×"],
  ['issueLaunchCancel.addEventListener("click", closeIssueLaunchModal)', "issue-launch cancel"],
  ['issueLaunchClose.addEventListener("click", closeIssueLaunchModal)', "issue-launch ×"],
  ['$("keys-close").addEventListener("click", closeKeys)', "keys ×"],
  ['$("users-close").addEventListener("click", closeUsers)', "users ×"],
  ['$("new-task-close").addEventListener("click", closeNewTask)', "new-task ×"],
];

for (const [needle, label] of CLOSE_BUTTON_BINDINGS) {
  check(`close-button binding retained: ${label}`, () => {
    assert.ok(js.includes(needle), `app.js is missing the ${label} close binding`);
  });
}

// ---------------------------------------------------------------------------
// (b') Confirm/submit bindings are untouched.
// ---------------------------------------------------------------------------
check("confirm/submit bindings unchanged", () => {
  for (const needle of [
    '$("issue-form").addEventListener("submit", submitIssueForm)',
    '$("issue-action-confirm").addEventListener("click", confirmIssueAction)',
    'endConfirm.addEventListener("click", confirmEndSession)',
    'issueLaunchConfirm.addEventListener("click", confirmIssueLaunch)',
  ]) {
    assert.ok(js.includes(needle), `app.js is missing the confirm binding: ${needle}`);
  }
});

// ---------------------------------------------------------------------------
// (c) The mobile sidebar-drawer backdrop and nav-menu outside-click dismiss
//     are intentionally preserved — they are different semantics.
// ---------------------------------------------------------------------------
check("flow-sidebar-backdrop drawer dismiss preserved", () => {
  assert.ok(
    js.includes('$("flow-sidebar-backdrop")') &&
      js.includes('sidebarBackdrop.addEventListener("click", closeFlowSidebar)'),
    "the mobile sidebar drawer backdrop dismiss must be preserved",
  );
});

check("nav-menu outside-click dismiss preserved", () => {
  // The document-level outside click that collapses the nav menu stays.
  assert.ok(
    js.includes("if (!isNavMenuOpen()) return;") && js.includes("closeNavMenu();"),
    "the nav-menu outside-click dismiss must be preserved",
  );
});

// ---------------------------------------------------------------------------
// Done
// ---------------------------------------------------------------------------
console.log(`\n  ${passed} checks passed.`);
