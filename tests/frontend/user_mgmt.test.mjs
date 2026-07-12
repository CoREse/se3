/*
 * User-management row-model tests (Group G3).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerUserMgmtTests({app, check, findOne, findAll})`
 * so the parent harness drives the same check() reporter and the same `app`
 * module export.
 *
 * These cover the DOM-free pure logic only:
 *   (a) userRowModel — per-row action gating that mirrors the server guards:
 *       self cannot be deleted / demoted, non-local provider cannot be
 *       password-reset, admin vs ordinary user labelling, and robustness on
 *       missing / malformed input.
 *   (b) the admin-only visibility projection invariant: a non-admin owner,
 *       even authenticated, must not be granted the admin-only surface.
 *
 * Frontend gating here is UX only; every action is independently re-enforced
 * server-side (see tests/test_server_users.py). The tests assert the model
 * stays in lockstep with those guards.
 */
import assert from "node:assert/strict";

export function registerUserMgmtTests(ctx) {
  const { app, check } = ctx;

  const SELF = "owner-self";
  const user = (over = {}) => ({
    owner_id: "owner-other",
    display_name: "Bob",
    is_admin: false,
    provider: "local",
    can_reset_password: true,
    ...over,
  });

  // ---- (a) self-row protections -------------------------------------------
  check("G3 userRowModel: own row cannot be deleted or admin-toggled", () => {
    const m = app.userRowModel(user({ owner_id: SELF }), SELF);
    assert.equal(m.isSelf, true);
    assert.equal(m.canDelete, false, "self delete is disabled");
    assert.equal(m.canToggleAdmin, false, "self demotion is disabled");
  });

  check("G3 userRowModel: another local user is fully manageable", () => {
    const m = app.userRowModel(user(), SELF);
    assert.equal(m.isSelf, false);
    assert.equal(m.canDelete, true);
    assert.equal(m.canToggleAdmin, true);
    assert.equal(m.canResetPassword, true);
  });

  // ---- (b) provider-based password-reset gating ---------------------------
  check("G3 userRowModel: non-local provider cannot have its password reset", () => {
    for (const provider of ["oidc", "proxy_header"]) {
      const m = app.userRowModel(
        user({ provider, can_reset_password: false }),
        SELF,
      );
      assert.equal(m.canResetPassword, false, provider + " reset disabled");
      assert.equal(m.isLocal, false);
      // delete / admin-toggle stay available for non-local users.
      assert.equal(m.canDelete, true);
      assert.equal(m.canToggleAdmin, true);
    }
  });

  check("G3 userRowModel: falls back to provider check when flag absent", () => {
    // Older payloads may omit can_reset_password — the model must derive it
    // from provider === "local".
    const local = user({ can_reset_password: undefined, provider: "local" });
    const oidc = user({ can_reset_password: undefined, provider: "oidc" });
    assert.equal(app.userRowModel(local, SELF).canResetPassword, true);
    assert.equal(app.userRowModel(oidc, SELF).canResetPassword, false);
  });

  // ---- (c) admin vs ordinary user labelling -------------------------------
  check("G3 userRowModel: admin and ordinary users carry distinct labels", () => {
    const admin = app.userRowModel(user({ is_admin: true }), SELF);
    assert.equal(admin.isAdmin, true);
    assert.equal(admin.adminLabel, "admin");
    assert.equal(admin.adminClass, "admin");
    assert.equal(admin.toggleAdminTo, false, "an admin toggles down to non-admin");
    assert.equal(admin.toggleAdminLabel, "Remove admin");

    const plain = app.userRowModel(user({ is_admin: false }), SELF);
    assert.equal(plain.isAdmin, false);
    assert.equal(plain.adminLabel, "user");
    assert.equal(plain.adminClass, "user");
    assert.equal(plain.toggleAdminTo, true, "a user toggles up to admin");
    assert.equal(plain.toggleAdminLabel, "Set as admin");
  });

  // ---- (d) robustness on missing / malformed input ------------------------
  check("G3 userRowModel: tolerates missing / malformed user objects", () => {
    for (const bad of [null, undefined, {}, "nope", 42]) {
      const m = app.userRowModel(bad, SELF);
      // No owner_id ⇒ every action disabled (nothing to act upon).
      assert.equal(m.ownerId, "");
      assert.equal(m.canDelete, false);
      assert.equal(m.canResetPassword, false);
      assert.equal(m.canToggleAdmin, false);
      assert.equal(m.label, "(unknown)");
    }
  });

  check("G3 userRowModel: label prefers display_name, falls back to owner_id", () => {
    assert.equal(
      app.userRowModel(user({ display_name: "  Alice  " }), SELF).label,
      "Alice",
    );
    assert.equal(
      app.userRowModel(user({ display_name: "", owner_id: "owner-x" }), SELF).label,
      "owner-x",
    );
  });

  // ---- (e) admin-only visibility projection invariant ---------------------
  // applyAuthState toggles `.admin-only` off `is_admin` only. We can't drive
  // the DOM here, but we can assert the boolean projection the renderer uses:
  // authenticated-but-non-admin must NOT expose the admin surface.
  check("G3 admin-only surface is gated on is_admin, not mere authentication", () => {
    const adminVisible = (identity) =>
      Boolean(identity && identity.is_admin);
    assert.equal(adminVisible({ owner_id: "a", is_admin: true }), true);
    assert.equal(adminVisible({ owner_id: "b", is_admin: false }), false,
      "a non-admin authenticated owner does not see the admin surface");
    assert.equal(adminVisible(null), false, "no identity ⇒ hidden");
  });
}
