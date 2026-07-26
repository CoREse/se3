/*
 * Node assertion tests for the web console's auth / owner-narrowing pure
 * helpers (Group G9).
 *
 * Run manually:  node tests/test_server_authz_frontend.mjs
 *
 * These are the DOM-free, isomorphically-testable functions the login gate,
 * the 401-interception, the owner-scoped machine view, and the daemon-key
 * panel are built on. They are intentionally exercised without a browser so
 * the chromium e2e path (which fails here for lack of libnspr4.so — see the
 * `test_headless_browser_env` memory) is avoided entirely. A small pytest
 * bridge (`test_server_authz_frontend.py`) pulls this suite into the pytest
 * run.
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const app = require(
  path.join(here, "..", "src", "tianluo", "server", "static", "app.js"),
);

let passed = 0;
function check(name, fn) {
  fn();
  passed += 1;
  console.log("  ok -", name);
}

// ---------------------------------------------------------------------------
// nextAuthState — the login state machine
// ---------------------------------------------------------------------------

check("G9 AUTH_STATES enumerates the three coarse states", () => {
  assert.deepEqual(
    new Set(Object.values(app.AUTH_STATES)),
    new Set(["unknown", "login", "authed"]),
  );
});

check("G9 success events transition to authed", () => {
  for (const ev of ["me_ok", "login_ok", "breakglass_ok"]) {
    assert.equal(
      app.nextAuthState(app.AUTH_STATES.UNKNOWN, ev),
      app.AUTH_STATES.AUTHED,
      `event ${ev} should authenticate`,
    );
    assert.equal(
      app.nextAuthState(app.AUTH_STATES.LOGIN, ev),
      app.AUTH_STATES.AUTHED,
    );
  }
});

check("G9 deauth events transition to the login gate", () => {
  for (const ev of ["me_401", "unauthorized", "logout"]) {
    assert.equal(
      app.nextAuthState(app.AUTH_STATES.AUTHED, ev),
      app.AUTH_STATES.LOGIN,
      `event ${ev} should drop to login`,
    );
    assert.equal(
      app.nextAuthState(app.AUTH_STATES.UNKNOWN, ev),
      app.AUTH_STATES.LOGIN,
    );
  }
});

check("G9 unknown events are idempotent and keep the current state", () => {
  assert.equal(
    app.nextAuthState(app.AUTH_STATES.AUTHED, "noise"),
    app.AUTH_STATES.AUTHED,
  );
  assert.equal(
    app.nextAuthState(app.AUTH_STATES.LOGIN, "noise"),
    app.AUTH_STATES.LOGIN,
  );
  // A bogus current value normalizes back to UNKNOWN rather than propagating.
  assert.equal(app.nextAuthState("garbage", "noise"), app.AUTH_STATES.UNKNOWN);
});

check("G9 a full login→logout→relogin cycle lands correctly", () => {
  let s = app.AUTH_STATES.UNKNOWN;
  s = app.nextAuthState(s, "me_401"); // boot, no session
  assert.equal(s, app.AUTH_STATES.LOGIN);
  s = app.nextAuthState(s, "login_ok");
  assert.equal(s, app.AUTH_STATES.AUTHED);
  s = app.nextAuthState(s, "unauthorized"); // session expired mid-session
  assert.equal(s, app.AUTH_STATES.LOGIN);
  s = app.nextAuthState(s, "breakglass_ok");
  assert.equal(s, app.AUTH_STATES.AUTHED);
  s = app.nextAuthState(s, "logout");
  assert.equal(s, app.AUTH_STATES.LOGIN);
});

// ---------------------------------------------------------------------------
// ownerLabel — top-bar identity rendering
// ---------------------------------------------------------------------------

check("G9 ownerLabel prefers display_name", () => {
  assert.equal(
    app.ownerLabel({ display_name: "Alice", owner_id: "o-1" }),
    "Alice",
  );
});

check("G9 ownerLabel falls back to owner_id", () => {
  assert.equal(app.ownerLabel({ display_name: "", owner_id: "o-7" }), "o-7");
});

check("G9 ownerLabel marks an admin", () => {
  assert.equal(
    app.ownerLabel({ display_name: "Root", is_admin: true }),
    "Root (admin)",
  );
});

check("G9 ownerLabel is empty for no identity", () => {
  assert.equal(app.ownerLabel(null), "");
  assert.equal(app.ownerLabel(undefined), "");
});

// ---------------------------------------------------------------------------
// isUnauthorizedStatus — 401 interception predicate
// ---------------------------------------------------------------------------

check("G9 isUnauthorizedStatus is true only for 401", () => {
  assert.equal(app.isUnauthorizedStatus(401), true);
  for (const s of [200, 202, 403, 404, 500, 0, undefined, null]) {
    assert.equal(app.isUnauthorizedStatus(s), false, `status ${s}`);
  }
});

// ---------------------------------------------------------------------------
// canOwnerControlMachine / visibleMachinesForOwner — owner narrowing
// ---------------------------------------------------------------------------

const ALICE = { owner_id: "o-alice", is_admin: false };
const ADMIN = { owner_id: "o-admin", is_admin: true };
const MACHINES = [
  { machine_id: "m1", owner_id: "o-alice" },
  { machine_id: "m2", owner_id: "o-bob" },
  { machine_id: "m3", owner_id: null }, // unbound
];

check("G9 a regular owner controls only its own machines", () => {
  assert.equal(app.canOwnerControlMachine(MACHINES[0], ALICE), true);
  assert.equal(app.canOwnerControlMachine(MACHINES[1], ALICE), false);
  // An unbound machine is not "theirs".
  assert.equal(app.canOwnerControlMachine(MACHINES[2], ALICE), false);
});

check("G9 an admin controls every machine, including unbound", () => {
  for (const m of MACHINES) {
    assert.equal(app.canOwnerControlMachine(m, ADMIN), true);
  }
});

check("G9 no identity controls nothing (fail-closed)", () => {
  assert.equal(app.canOwnerControlMachine(MACHINES[0], null), false);
});

check("G9 visibleMachinesForOwner narrows to the owner's machines", () => {
  const visible = app.visibleMachinesForOwner(MACHINES, ALICE);
  assert.deepEqual(visible.map((m) => m.machine_id), ["m1"]);
  // Bob's machine and the cross-owner control entry never surface for Alice.
  assert.ok(!visible.some((m) => m.owner_id === "o-bob"));
});

check("G9 visibleMachinesForOwner gives an admin the full list", () => {
  const visible = app.visibleMachinesForOwner(MACHINES, ADMIN);
  assert.equal(visible.length, 3);
});

check("G9 visibleMachinesForOwner is empty without an identity", () => {
  assert.deepEqual(app.visibleMachinesForOwner(MACHINES, null), []);
  assert.deepEqual(app.visibleMachinesForOwner(null, ALICE), []);
});

// ---------------------------------------------------------------------------
// daemonKeyRowModel — daemon-key panel row view model
// ---------------------------------------------------------------------------

check("G9 daemonKeyRowModel marks an active key", () => {
  const m = app.daemonKeyRowModel({
    key_id: "k1",
    label: "laptop",
    revoked: false,
  });
  assert.equal(m.keyId, "k1");
  assert.equal(m.label, "laptop");
  assert.equal(m.revoked, false);
  assert.equal(m.statusLabel, "Active");
  assert.equal(m.statusClass, "active");
});

check("G9 daemonKeyRowModel marks a revoked key (via revoked flag or timestamp)", () => {
  const byFlag = app.daemonKeyRowModel({ key_id: "k2", revoked: true });
  assert.equal(byFlag.revoked, true);
  assert.equal(byFlag.statusLabel, "Revoked");
  assert.equal(byFlag.statusClass, "revoked");

  const byTs = app.daemonKeyRowModel({ key_id: "k3", revoked_at: 1700000000 });
  assert.equal(byTs.revoked, true);
});

check("G9 daemonKeyRowModel supplies an unlabeled fallback", () => {
  assert.equal(app.daemonKeyRowModel({ key_id: "k4", label: "" }).label, "(unlabeled)");
  assert.equal(app.daemonKeyRowModel({ key_id: "k5" }).label, "(unlabeled)");
});

check("G9 daemonKeyRowModel tolerates a non-object input", () => {
  const m = app.daemonKeyRowModel(null);
  assert.equal(m.keyId, "");
  assert.equal(m.label, "(unlabeled)");
  assert.equal(m.revoked, false);
});

console.log(`\n${passed} checks passed`);
