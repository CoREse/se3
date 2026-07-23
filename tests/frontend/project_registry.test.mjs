/*
 * Registered-project management tests (Group G4).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerProjectRegistryTests({app, check, checkAsync,
 * findOne, findAll})` so the parent harness drives the same reporter and the
 * same `app` module export.
 *
 * Coverage:
 *   (a) projectRegistryRowModel — stale/active projections over the RAW
 *       registry mirror (a vanished path must stay visible and be flagged, it
 *       is exactly what the operator opened the dialog to clean up), plus
 *       defensiveness against missing / malformed entries.
 *   (b) buildAddProjectBody — trimming and the up-front empty / non-absolute
 *       rejections, returned as data rather than thrown.
 *   (c) projectErrorKey — the daemon's stable error_code → i18n key map and
 *       the generic fallback for a code a newer daemon invents.
 *   (d) renderProjects under the DOM stub — empty state, multi-row rendering,
 *       stale + active badges.
 *   (e) the two-stage removal: the row button only opens the confirmation and
 *       issues NO request; the confirm step is what sends the DELETE.
 *   (f) addProject's client-side guard, its POST body, and error_code →
 *       localized copy rendering.
 *   (g) the machine-row entry button: stopPropagation so the row's own
 *       "select this machine" gesture is not hijacked.
 *   (h) syncProjectsFromSnapshot — the STATUS_UPDATE repaint that makes an
 *       add/remove land in an open dialog without a reload.
 *
 * Every action here is independently re-enforced server-side (owner gate +
 * daemon-side validation; see tests/server/test_project_registry_api.py and
 * tests/daemon/test_project_registry.py) — the frontend logic is UX only.
 */
import assert from "node:assert/strict";

export async function registerProjectRegistryTests(ctx) {
  const { app, check, checkAsync, findAll, findOne } = ctx;
  const state = app.state;

  // ---- fetch harness -------------------------------------------------------
  // Records every request app.js makes so a "must not fetch" assertion is a
  // real observation rather than an absence of side effects.
  function installFetch(responder) {
    const calls = [];
    const saved = globalThis.fetch;
    globalThis.fetch = async (url, init) => {
      calls.push({ url: String(url), init: init || {} });
      const spec = responder(String(url), init || {}) || {};
      const status = spec.status === undefined ? 200 : spec.status;
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => (spec.body === undefined ? {} : spec.body),
      };
    };
    return { calls, restore: () => { globalThis.fetch = saved; } };
  }

  const listBody = (projects) => ({
    machine_id: "m1",
    projects,
    count: projects.length,
  });

  function resetDialogState() {
    state.projectMachineId = "m1";
    state.projectEntries = [];
    state.projectRemoveTarget = null;
    document.getElementById("project-error").classList.add("hidden");
    document.getElementById("project-remove-error").classList.add("hidden");
    document.getElementById("project-remove-modal").classList.add("hidden");
    document.getElementById("project-add-path").value = "";
  }

  // ==========================================================================
  // (a) projectRegistryRowModel
  // ==========================================================================
  check("G4 projectRegistryRowModel: a vanished path is flagged stale", () => {
    const m = app.projectRegistryRowModel({
      path: "/home/u/gone", exists: false, active: false,
    });
    assert.equal(m.path, "/home/u/gone");
    assert.equal(m.stale, true, "exists:false must surface as stale");
    assert.equal(m.active, false);
    // A stale entry is still removable — deleting it is the whole point.
    assert.equal(m.canRemove, true);
  });

  check("G4 projectRegistryRowModel: a live path is not stale", () => {
    const m = app.projectRegistryRowModel({
      path: "/home/u/proj", exists: true, active: true,
    });
    assert.equal(m.stale, false);
    assert.equal(m.active, true);
    assert.equal(m.canRemove, true, "an active root stays removable — the "
      + "live-flow refusal is the daemon's call, not the mirror's");
  });

  check("G4 projectRegistryRowModel: absent `exists` is read as present", () => {
    // An older daemon predating the field must not have every entry painted
    // as missing (that would invite deleting live roots).
    const m = app.projectRegistryRowModel({ path: "/home/u/proj" });
    assert.equal(m.stale, false);
    assert.equal(m.active, false);
  });

  check("G4 projectRegistryRowModel: path is trimmed and labelled", () => {
    const m = app.projectRegistryRowModel({ path: "  /home/u/proj  " });
    assert.equal(m.path, "/home/u/proj");
    assert.equal(m.name, "proj");
  });

  check("G4 projectRegistryRowModel: a worktree root folds to its project", () => {
    const m = app.projectRegistryRowModel({
      path: "/home/u/proj/se3/worktrees/impl-g4", exists: true,
    });
    assert.equal(m.name, "proj (worktree)");
  });

  check("G4 projectRegistryRowModel: tolerates missing / malformed entries", () => {
    for (const bad of [null, undefined, {}, "nope", 42, { path: 7 }]) {
      const m = app.projectRegistryRowModel(bad);
      assert.equal(m.path, "");
      assert.equal(m.stale, false);
      assert.equal(m.active, false);
      // Nothing to act upon ⇒ no remove button is offered.
      assert.equal(m.canRemove, false);
    }
  });

  // ==========================================================================
  // (b) buildAddProjectBody
  // ==========================================================================
  check("G4 buildAddProjectBody: trims and wraps an absolute path", () => {
    const r = app.buildAddProjectBody("  /home/u/proj  ");
    assert.equal(r.ok, true);
    assert.equal(r.projectRoot, "/home/u/proj");
    assert.deepEqual(r.body, { project_root: "/home/u/proj" });
  });

  check("G4 buildAddProjectBody: empty / whitespace / non-string is rejected", () => {
    for (const bad of ["", "   ", "\t\n", null, undefined, 42, {}]) {
      const r = app.buildAddProjectBody(bad);
      assert.equal(r.ok, false);
      assert.equal(r.reason, "empty");
      // Rejection is DATA, never an exception — the caller localizes it.
      assert.equal(r.body, undefined);
    }
  });

  check("G4 buildAddProjectBody: a relative path is rejected up front", () => {
    for (const rel of ["proj", "./proj", "../proj", "~/proj", "se3/proj"]) {
      const r = app.buildAddProjectBody(rel);
      assert.equal(r.ok, false, `${rel} must be rejected`);
      assert.equal(r.reason, "not_absolute");
    }
  });

  check("G4 buildAddProjectBody: Windows absolute forms are accepted", () => {
    // The daemon owns the definition of "absolute" on its own filesystem; this
    // guard must only reject what is unambiguously relative.
    for (const abs of ["C:\\proj", "d:/proj", "\\\\host\\share\\proj"]) {
      const r = app.buildAddProjectBody(abs);
      assert.equal(r.ok, true, `${abs} must be accepted`);
      assert.equal(r.body.project_root, abs);
    }
  });

  // ==========================================================================
  // (c) projectErrorKey
  // ==========================================================================
  check("G4 projectErrorKey: every daemon error_code maps to its own key", () => {
    const expected = {
      invalid_path: "projects.errInvalidPath",
      not_found: "projects.errNotFound",
      not_a_directory: "projects.errNotADirectory",
      live_flow: "projects.errLiveFlow",
      not_registered: "projects.errNotRegistered",
      // A failed registry rewrite must NOT share the not_registered copy: the
      // entry is still there and the operator's move is to retry.
      registry_error: "projects.errRegistryError",
      invalid_operation: "projects.errInvalidOperation",
      unsupported: "projects.errUnsupported",
    };
    for (const [code, key] of Object.entries(expected)) {
      assert.equal(app.projectErrorKey(code), key);
    }
    // Distinct codes must not collide onto one key.
    const keys = Object.values(expected);
    assert.equal(new Set(keys).size, keys.length);
  });

  check("G4 projectErrorKey: an unknown / absent code falls back", () => {
    for (const bad of ["what_is_this", "", "   ", null, undefined, 42, {}]) {
      assert.equal(app.projectErrorKey(bad), "projects.errGeneric");
    }
  });

  // ==========================================================================
  // (c2) applyProjectAdded / applyProjectRemoved — the post-write projections
  // that stand in for the not-yet-refreshed STATUS_UPDATE mirror
  // ==========================================================================
  check("G4 applyProjectAdded: inserts in daemon order without mutating input", () => {
    const before = [{ path: "/a", exists: true, active: false }];
    const after = app.applyProjectAdded(before, "/b");
    assert.deepEqual(after, [
      { path: "/a", exists: true, active: false },
      { path: "/b", exists: true, active: true },
    ]);
    assert.deepEqual(before, [{ path: "/a", exists: true, active: false }],
      "the caller's array is left alone");
    // Sorted by path, matching the daemon's own ordering.
    assert.deepEqual(
      app.applyProjectAdded([{ path: "/z" }], "/a").map((e) => e.path), ["/a", "/z"]);
  });

  check("G4 applyProjectAdded: re-adding a stale row revives it in place", () => {
    const after = app.applyProjectAdded(
      [{ path: "/gone", exists: false, active: false }], "/gone");
    assert.deepEqual(after, [{ path: "/gone", exists: true, active: true }]);
  });

  check("G4 applyProjectAdded: a blank root is a no-op", () => {
    const before = [{ path: "/a", exists: true, active: false }];
    assert.deepEqual(app.applyProjectAdded(before, "   "), before);
    assert.deepEqual(app.applyProjectAdded(before, null), before);
    assert.deepEqual(app.applyProjectAdded(undefined, "/a"),
      [{ path: "/a", exists: true, active: true }]);
  });

  check("G4 applyProjectRemoved: drops only the matching row", () => {
    const before = [{ path: "/a" }, { path: "/b" }];
    assert.deepEqual(app.applyProjectRemoved(before, "/a"), [{ path: "/b" }]);
    assert.deepEqual(app.applyProjectRemoved(before, "/nope"), before);
    assert.deepEqual(app.applyProjectRemoved(before, ""), before);
    assert.deepEqual(before, [{ path: "/a" }, { path: "/b" }],
      "the caller's array is left alone");
  });

  // ==========================================================================
  // (d) renderProjects — DOM stub
  // ==========================================================================
  check("G4 renderProjects: empty registry paints the empty state", () => {
    resetDialogState();
    app.renderProjects();
    const list = document.getElementById("project-list");
    assert.equal(findAll(list, "project-row").length, 0);
    const empty = findOne(list, "empty") || (list.children[0] || null);
    assert.ok(empty, "an empty-state node is rendered");
    assert.ok(empty.classList.contains("empty"));
  });

  check("G4 renderProjects: renders one row per entry with badges", () => {
    resetDialogState();
    state.projectEntries = [
      { path: "/home/u/a", exists: true, active: true },
      { path: "/home/u/b", exists: false, active: false },
      { path: "/home/u/c", exists: true, active: false },
    ];
    app.renderProjects();
    const list = document.getElementById("project-list");
    const rows = findAll(list, "project-row");
    assert.equal(rows.length, 3);
    assert.deepEqual(
      findAll(list, "project-row-path").map((n) => n.textContent),
      ["/home/u/a", "/home/u/b", "/home/u/c"],
    );
    // The vanished entry — and only it — carries the stale marking.
    assert.equal(rows[0].classList.contains("stale"), false);
    assert.equal(rows[1].classList.contains("stale"), true);
    assert.equal(rows[2].classList.contains("stale"), false);
    assert.equal(findAll(list, "project-badge-stale").length, 1);
    // The polled entry — and only it — carries the active badge.
    assert.equal(findAll(list, "project-badge-active").length, 1);
    assert.ok(findOne(rows[0], "project-badge-active"));
    // Every row offers a remove button.
    assert.equal(findAll(list, "project-remove-btn").length, 3);
  });

  check("G4 renderProjects: an entry with no path is skipped", () => {
    resetDialogState();
    state.projectEntries = [{ path: "" }, { path: "/home/u/ok" }, null];
    app.renderProjects();
    const list = document.getElementById("project-list");
    assert.equal(findAll(list, "project-row").length, 1);
  });

  // ==========================================================================
  // (e) two-stage removal
  // ==========================================================================
  await checkAsync("G4 remove is two-stage: the row button sends no request", async () => {
    resetDialogState();
    state.projectEntries = [{ path: "/home/u/a", exists: true, active: false }];
    app.renderProjects();
    const harness = installFetch(() => ({ status: 200, body: {} }));
    try {
      const btn = findOne(document.getElementById("project-list"), "project-remove-btn");
      assert.ok(btn, "the row carries a remove button");
      btn.dispatch("click");
      // The first click ONLY opens the confirmation.
      assert.deepEqual(harness.calls, [], "no request may leave on the first click");
      assert.equal(state.projectRemoveTarget, "/home/u/a");
      assert.equal(
        document.getElementById("project-remove-modal").classList.contains("hidden"),
        false,
        "the confirmation modal is opened",
      );
      assert.equal(
        document.getElementById("project-remove-path").textContent,
        "/home/u/a",
      );
    } finally {
      harness.restore();
    }
  });

  await checkAsync("G4 confirming the removal sends the DELETE and drops the row", async () => {
    resetDialogState();
    state.projectEntries = [
      { path: "/home/u/a", exists: true, active: false },
      { path: "/home/u/b", exists: true, active: false },
    ];
    app.confirmRemoveProject("/home/u/a");
    // The mirror is deliberately stale here — it still lists the removed root,
    // which is exactly what the server would answer before the daemon's fast
    // push lands. A re-read would paint the row straight back.
    const harness = installFetch((url, init) => {
      if ((init.method || "GET") === "DELETE") {
        return { status: 200, body: { status: "removed", project_root: "/home/u/a" } };
      }
      return { status: 200, body: listBody([
        { path: "/home/u/a", exists: true, active: false },
        { path: "/home/u/b", exists: true, active: false },
      ]) };
    });
    try {
      await app.removeProject();
      const del = harness.calls.filter((c) => (c.init.method || "GET") === "DELETE");
      assert.equal(del.length, 1);
      assert.ok(del[0].url.startsWith("/api/machines/m1/projects?project_root="));
      assert.ok(
        del[0].url.includes(encodeURIComponent("/home/u/a")),
        "the path travels URL-encoded in the query",
      );
      // Confirmation closes and the row disappears from the local list.
      assert.equal(
        document.getElementById("project-remove-modal").classList.contains("hidden"),
        true,
      );
      assert.equal(state.projectRemoveTarget, null);
      assert.equal(
        harness.calls.filter((c) => (c.init.method || "GET") === "GET").length,
        0,
        "no re-read: the mirror still predates the write",
      );
      assert.deepEqual(state.projectEntries,
        [{ path: "/home/u/b", exists: true, active: false }]);
      assert.equal(
        findAll(document.getElementById("project-list"), "project-row").length, 1);
    } finally {
      harness.restore();
    }
  });

  await checkAsync("G4 a live-flow refusal renders localized copy, dialog stays open", async () => {
    resetDialogState();
    app.confirmRemoveProject("/home/u/a");
    const harness = installFetch(() => ({
      status: 409,
      body: {
        detail: "Project /home/u/a has a running flow; stop it before removing",
        error_code: "live_flow",
      },
    }));
    try {
      await app.removeProject();
      const errBox = document.getElementById("project-remove-error");
      assert.equal(errBox.classList.contains("hidden"), false, "the error is shown");
      // No dictionary is loaded in the node harness, so tf() yields the
      // per-key fallback — here the daemon's prose. What matters is that the
      // code was recognized (it routed through the key map) and that the
      // confirmation stayed open so the operator can react.
      assert.ok(errBox.textContent.includes("running flow"));
      assert.equal(
        document.getElementById("project-remove-modal").classList.contains("hidden"),
        false,
        "a refused removal keeps the confirmation open",
      );
      assert.equal(state.projectRemoveTarget, "/home/u/a");
    } finally {
      harness.restore();
    }
  });

  // ==========================================================================
  // (f) addProject
  // ==========================================================================
  await checkAsync("G4 addProject: a relative path is refused without a request", async () => {
    resetDialogState();
    document.getElementById("project-add-path").value = "relative/path";
    const harness = installFetch(() => ({ status: 201, body: {} }));
    try {
      await app.addProject({ preventDefault() {} });
      assert.deepEqual(harness.calls, [], "the client guard fires before any fetch");
      const errBox = document.getElementById("project-error");
      assert.equal(errBox.classList.contains("hidden"), false);
      assert.ok(errBox.textContent.length > 0);
    } finally {
      harness.restore();
    }
  });

  await checkAsync("G4 addProject: posts the trimmed path and shows it at once", async () => {
    resetDialogState();
    document.getElementById("project-add-path").value = "  /home/u/new  ";
    // The GET stub answers with the PRE-write mirror (the realistic race: the
    // daemon acks the command before it fast-pushes the new snapshot), so a
    // re-read here would repaint the dialog without the new project.
    const harness = installFetch((url, init) => {
      if ((init.method || "GET") === "POST") {
        return { status: 201, body: { status: "registered", project_root: "/home/u/new" } };
      }
      return { status: 200, body: listBody([]) };
    });
    try {
      await app.addProject({ preventDefault() {} });
      const post = harness.calls.filter((c) => (c.init.method || "GET") === "POST");
      assert.equal(post.length, 1);
      assert.equal(post[0].url, "/api/machines/m1/projects");
      assert.deepEqual(JSON.parse(post[0].init.body), { project_root: "/home/u/new" });
      assert.equal(document.getElementById("project-add-path").value, "");
      assert.equal(
        harness.calls.filter((c) => (c.init.method || "GET") === "GET").length,
        0,
        "no re-read: the mirror still predates the write",
      );
      // The daemon's echoed normalized root is painted locally.
      assert.deepEqual(state.projectEntries,
        [{ path: "/home/u/new", exists: true, active: true }]);
      assert.equal(
        findAll(document.getElementById("project-list"), "project-row").length, 1);
    } finally {
      harness.restore();
    }
  });

  await checkAsync("G4 addProject: the daemon's normalized root wins over the input", async () => {
    resetDialogState();
    // A worktree spelling folds back to its main root on the daemon; the row
    // must carry what was actually registered, not what was typed, or the
    // follow-up snapshot would look like a second unrelated entry.
    document.getElementById("project-add-path").value = "/home/u/proj-wt";
    const harness = installFetch(() => ({
      status: 201, body: { status: "registered", project_root: "/home/u/proj" },
    }));
    try {
      await app.addProject({ preventDefault() {} });
      assert.deepEqual(state.projectEntries,
        [{ path: "/home/u/proj", exists: true, active: true }]);
    } finally {
      harness.restore();
    }
  });

  await checkAsync("G4 addProject: a daemon error_code drives the message", async () => {
    resetDialogState();
    document.getElementById("project-add-path").value = "/home/u/missing";
    const harness = installFetch(() => ({
      status: 404,
      body: { detail: "Path does not exist: /home/u/missing", error_code: "not_found" },
    }));
    try {
      await app.addProject({ preventDefault() {} });
      const errBox = document.getElementById("project-error");
      assert.equal(errBox.classList.contains("hidden"), false);
      assert.ok(errBox.textContent.includes("/home/u/missing"));
      // The submit button is re-enabled so the operator can correct and retry.
      assert.equal(document.getElementById("project-add-submit").disabled, false);
    } finally {
      harness.restore();
    }
  });

  await checkAsync("G4 addProject: a network failure has its own message", async () => {
    resetDialogState();
    document.getElementById("project-add-path").value = "/home/u/new";
    const saved = globalThis.fetch;
    globalThis.fetch = async () => { throw new Error("network down"); };
    try {
      await app.addProject({ preventDefault() {} });
      const errBox = document.getElementById("project-error");
      assert.equal(errBox.classList.contains("hidden"), false);
      assert.ok(errBox.textContent.length > 0);
      assert.equal(document.getElementById("project-add-submit").disabled, false);
    } finally {
      globalThis.fetch = saved;
    }
  });

  await checkAsync("G4 loadProjects: a failed GET degrades to an empty list + error", async () => {
    resetDialogState();
    state.projectEntries = [{ path: "/stale/from/before" }];
    const harness = installFetch(() => ({ status: 503, body: { detail: "offline" } }));
    try {
      await app.loadProjects();
      assert.deepEqual(state.projectEntries, []);
      assert.equal(
        document.getElementById("project-error").classList.contains("hidden"), false);
      assert.equal(
        findAll(document.getElementById("project-list"), "project-row").length, 0);
    } finally {
      harness.restore();
    }
  });

  // ==========================================================================
  // (g) machine-row entry button
  // ==========================================================================
  await checkAsync("G4 the machine-row entry button opens the dialog without selecting", async () => {
    const savedMachines = state.machines;
    const savedSelected = state.selectedMachineId;
    const harness = installFetch(() => ({ status: 200, body: listBody([]) }));
    try {
      app.resetRenderSignatures();
      state.machines = [
        { machine_id: "m1", hostname: "alpha", online: true, flows: [] },
        { machine_id: "m2", hostname: "beta", online: false, flows: [] },
      ];
      state.selectedMachineId = "m1";
      app.renderMachines();
      const buttons = findAll(document.getElementById("machine-list"), "machine-projects-btn");
      assert.equal(buttons.length, 2, "every machine row carries the entry button");

      // Drive the listener with an event that records stopPropagation, which
      // the stub's own dispatch() cannot supply.
      let stopped = 0;
      const listener = buttons[1]._listeners.click[0];
      listener({ stopPropagation() { stopped += 1; }, preventDefault() {} });
      assert.equal(stopped, 1, "the row's select gesture must not also fire");
      assert.equal(state.selectedMachineId, "m1", "selection is unchanged");
      assert.equal(state.projectMachineId, "m2", "the dialog is scoped to that machine");
      assert.equal(
        document.getElementById("project-modal").classList.contains("hidden"), false);
      app.closeProjects();
    } finally {
      harness.restore();
      state.machines = savedMachines;
      state.selectedMachineId = savedSelected;
      app.resetRenderSignatures();
    }
  });

  // ==========================================================================
  // (h) STATUS_UPDATE repaint
  // ==========================================================================
  check("G4 syncProjectsFromSnapshot repaints from the machine mirror", () => {
    resetDialogState();
    const savedMachines = state.machines;
    try {
      state.machines = [{
        machine_id: "m1",
        registered_projects: [
          { path: "/home/u/a", exists: true, active: true },
          { path: "/home/u/gone", exists: false, active: false },
        ],
      }];
      app.syncProjectsFromSnapshot();
      const list = document.getElementById("project-list");
      assert.equal(findAll(list, "project-row").length, 2);
      assert.equal(findAll(list, "project-badge-stale").length, 1);

      // A machine whose snapshot predates the field degrades to an empty list
      // rather than keeping the previous machine's rows on screen.
      state.machines = [{ machine_id: "m1" }];
      app.syncProjectsFromSnapshot();
      assert.equal(findAll(document.getElementById("project-list"), "project-row").length, 0);
    } finally {
      state.machines = savedMachines;
      resetDialogState();
    }
  });
}
