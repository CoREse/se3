/*
 * Issue management pure-helper tests (Group G7).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerIssueManagementTests({app, check, findOne, findAll})`
 * so the parent harness drives the same check() reporter and the same `app`
 * module export.
 *
 * These cover the DOM-free pure logic only:
 *   (a) issueDisplayTitle — title derivation from explicit title, description
 *       first line, or "untitled" fallback.
 *   (b) issueSlug — filesystem-safe slug generation.
 *   (c) filterIssues — filtering by showClosed, source, type.
 *   (d) issueTypes — collecting unique types for the filter dropdown.
 *   (e) issuesPanelState — narrow-screen panel switch (list ↔ detail).
 *   (f) issueStatusClass / issuePriorityClass — CSS class mapping.
 *   (g) KNOWN_ISSUE_TYPES — constant correctness.
 *   (h) issueMachineId — machine_id key contract (REST API → modal).
 *   (i) buildIssueCreateBody — create request body construction.
 *   (j) buildIssueEditBody — edit request body with dirty-field tracking.
 *   (k) buildIssueActionBody — close/reopen request body construction.
 */
import assert from "node:assert/strict";

export function registerIssueManagementTests(ctx) {
  const { app, check } = ctx;

  // ---- (a) issueDisplayTitle -----------------------------------------------

  check("G7 issueDisplayTitle: prefers explicit title", () => {
    assert.equal(
      app.issueDisplayTitle({ title: "Fix login bug", description: "desc" }),
      "Fix login bug",
    );
  });

  check("G7 issueDisplayTitle: trims whitespace from title", () => {
    assert.equal(
      app.issueDisplayTitle({ title: "  Fix login bug  ", description: "desc" }),
      "Fix login bug",
    );
  });

  check("G7 issueDisplayTitle: falls back to description first line", () => {
    assert.equal(
      app.issueDisplayTitle({ title: "", description: "First line\nSecond line" }),
      "First line",
    );
    assert.equal(
      app.issueDisplayTitle({ title: null, description: "First line\nSecond" }),
      "First line",
    );
    assert.equal(
      app.issueDisplayTitle({ description: "Only one line" }),
      "Only one line",
    );
  });

  check("G7 issueDisplayTitle: truncates long description first line to 80 chars", () => {
    const long = "A".repeat(120);
    assert.equal(app.issueDisplayTitle({ description: long }).length, 80);
  });

  check("G7 issueDisplayTitle: skips blank description lines", () => {
    assert.equal(
      app.issueDisplayTitle({ description: "\n\n  \nReal content\nMore" }),
      "Real content",
    );
  });

  check("G7 issueDisplayTitle: returns 'untitled' when nothing usable", () => {
    assert.equal(app.issueDisplayTitle({}), "untitled");
    assert.equal(app.issueDisplayTitle(null), "untitled");
    assert.equal(app.issueDisplayTitle({ title: "", description: "" }), "untitled");
    assert.equal(app.issueDisplayTitle({ title: "   ", description: "  \n  " }), "untitled");
  });

  // ---- (b) issueSlug -------------------------------------------------------

  check("G7 issueSlug: normalizes title to lowercase hyphenated slug", () => {
    assert.equal(app.issueSlug("Fix Login Bug"), "fix-login-bug");
    assert.equal(app.issueSlug("Hello  World"), "hello-world");
  });

  check("G7 issueSlug: strips leading/trailing hyphens", () => {
    assert.equal(app.issueSlug("--fix--"), "fix");
    assert.equal(app.issueSlug("!!!"), "untitled");
  });

  check("G7 issueSlug: collapses non-alphanumeric runs", () => {
    assert.equal(app.issueSlug("a@b#c"), "a-b-c");
  });

  check("G7 issueSlug: returns 'untitled' for empty/invalid input", () => {
    assert.equal(app.issueSlug(""), "untitled");
    assert.equal(app.issueSlug(null), "untitled");
    assert.equal(app.issueSlug("   "), "untitled");
    assert.equal(app.issueSlug("!!!@@@"), "untitled");
  });

  // ---- (c) filterIssues ----------------------------------------------------

  const issues = [
    { id: "1", status: "open", source: "human", type: "bug" },
    { id: "2", status: "in-progress", source: "system", type: "feature" },
    { id: "3", status: "resolved", source: "human", type: "bug" },
    { id: "4", status: "closed", source: "system", type: "task" },
    { id: "5", status: "won't-fix", source: "human", type: "bug" },
  ];

  check("G7 filterIssues: default (no closed) shows only open/in-progress", () => {
    const result = app.filterIssues(issues, { showClosed: false, sourceFilter: "", typeFilter: "" });
    assert.equal(result.length, 2);
    assert.equal(result[0].id, "1");
    assert.equal(result[1].id, "2");
  });

  check("G7 filterIssues: showClosed includes all statuses", () => {
    const result = app.filterIssues(issues, { showClosed: true, sourceFilter: "", typeFilter: "" });
    assert.equal(result.length, 5);
  });

  check("G7 filterIssues: sourceFilter='human'", () => {
    const result = app.filterIssues(issues, { showClosed: true, sourceFilter: "human", typeFilter: "" });
    assert.equal(result.length, 3);
    assert.ok(result.every((i) => i.source === "human"));
  });

  check("G7 filterIssues: typeFilter='bug'", () => {
    const result = app.filterIssues(issues, { showClosed: true, sourceFilter: "", typeFilter: "bug" });
    assert.equal(result.length, 3);
    assert.ok(result.every((i) => i.type === "bug"));
  });

  check("G7 filterIssues: combined filters", () => {
    const result = app.filterIssues(issues, { showClosed: false, sourceFilter: "human", typeFilter: "bug" });
    assert.equal(result.length, 1);
    assert.equal(result[0].id, "1");
  });

  check("G7 filterIssues: tolerates null/invalid entries", () => {
    const mixed = [null, undefined, "nope", { id: "1", status: "open" }, 42];
    const result = app.filterIssues(mixed, { showClosed: false, sourceFilter: "", typeFilter: "" });
    assert.equal(result.length, 1);
    assert.equal(result[0].id, "1");
  });

  check("G7 filterIssues: returns empty for non-array input", () => {
    assert.deepEqual(app.filterIssues(null, {}), []);
    assert.deepEqual(app.filterIssues(undefined, {}), []);
    assert.deepEqual(app.filterIssues("nope", {}), []);
  });

  // ---- (d) issueTypes ------------------------------------------------------

  check("G7 issueTypes: collects unique sorted types", () => {
    const types = app.issueTypes(issues);
    assert.deepEqual(types, ["bug", "feature", "task"]);
  });

  check("G7 issueTypes: handles missing/null types", () => {
    const mixed = [
      { type: "bug" },
      { type: "" },
      { type: null },
      {},
      { type: "bug" },
    ];
    assert.deepEqual(app.issueTypes(mixed), ["bug"]);
  });

  check("G7 issueTypes: returns empty for non-array", () => {
    assert.deepEqual(app.issueTypes(null), []);
    assert.deepEqual(app.issueTypes(undefined), []);
  });

  // ---- (e) issuesPanelState ------------------------------------------------

  check("G7 issuesPanelState: default is list", () => {
    assert.equal(app.issuesPanelState(undefined, "unknown"), "list");
  });

  check("G7 issuesPanelState: select-issue moves to detail", () => {
    assert.equal(app.issuesPanelState("list", "select-issue"), "detail");
  });

  check("G7 issuesPanelState: back returns to list", () => {
    assert.equal(app.issuesPanelState("detail", "back"), "list");
  });

  check("G7 issuesPanelState: reset returns to list", () => {
    assert.equal(app.issuesPanelState("detail", "reset"), "list");
  });

  check("G7 issuesPanelState: preserves detail on unknown action", () => {
    assert.equal(app.issuesPanelState("detail", "something"), "detail");
  });

  // ---- (f) issueStatusClass / issuePriorityClass ---------------------------

  check("G7 issueStatusClass: maps all statuses", () => {
    assert.equal(app.issueStatusClass("open"), "badge-open");
    assert.equal(app.issueStatusClass("in-progress"), "badge-in-progress");
    assert.equal(app.issueStatusClass("resolved"), "badge-resolved");
    assert.equal(app.issueStatusClass("won't-fix"), "badge-wontfix");
    assert.equal(app.issueStatusClass("closed"), "badge-closed");
    assert.equal(app.issueStatusClass("unknown"), "badge-open");
    assert.equal(app.issueStatusClass(null), "badge-open");
  });

  check("G7 issuePriorityClass: maps all priorities", () => {
    assert.equal(app.issuePriorityClass("critical"), "priority-critical");
    assert.equal(app.issuePriorityClass("high"), "priority-high");
    assert.equal(app.issuePriorityClass("medium"), "priority-medium");
    assert.equal(app.issuePriorityClass("low"), "priority-low");
    assert.equal(app.issuePriorityClass(""), "priority-none");
    assert.equal(app.issuePriorityClass(null), "priority-none");
  });

  // ---- (g) KNOWN_ISSUE_TYPES -----------------------------------------------

  check("G7 KNOWN_ISSUE_TYPES: is a non-empty array of strings", () => {
    assert.ok(Array.isArray(app.KNOWN_ISSUE_TYPES));
    assert.ok(app.KNOWN_ISSUE_TYPES.length > 0);
    for (const t of app.KNOWN_ISSUE_TYPES) {
      assert.equal(typeof t, "string");
      assert.ok(t.trim().length > 0, "type must be non-empty");
    }
  });

  check("G7 KNOWN_ISSUE_TYPES: contains expected types", () => {
    for (const expected of ["bug", "feature", "enhancement", "idea", "task"]) {
      assert.ok(
        app.KNOWN_ISSUE_TYPES.includes(expected),
        `missing expected type: ${expected}`,
      );
    }
  });

  // ---- (h) issueMachineId ---------------------------------------------------
  // Pins the key contract between GET /api/issues responses (machine_id) and
  // what the edit/close/reopen modals read.

  check("G7 issueMachineId: reads machine_id from REST API response", () => {
    assert.equal(app.issueMachineId({ machine_id: "m-abc" }), "m-abc");
  });

  check("G7 issueMachineId: falls back to legacy _machine_id", () => {
    assert.equal(app.issueMachineId({ _machine_id: "m-legacy" }), "m-legacy");
  });

  check("G7 issueMachineId: prefers machine_id over _machine_id", () => {
    assert.equal(
      app.issueMachineId({ machine_id: "canonical", _machine_id: "legacy" }),
      "canonical",
    );
  });

  check("G7 issueMachineId: returns empty for null/missing", () => {
    assert.equal(app.issueMachineId(null), "");
    assert.equal(app.issueMachineId({}), "");
    assert.equal(app.issueMachineId({ machine_id: "" }), "");
  });

  // ---- (i) buildIssueCreateBody ---------------------------------------------

  check("G7 buildIssueCreateBody: always carries machine_id and project_root", () => {
    const body = app.buildIssueCreateBody("desc", "m1", "/proj");
    assert.equal(body.description, "desc");
    assert.equal(body.machine_id, "m1");
    assert.equal(body.project_root, "/proj");
    assert.equal(body.title, undefined);
  });

  check("G7 buildIssueCreateBody: includes optional fields when truthy", () => {
    const body = app.buildIssueCreateBody("desc", "m1", "/proj", "My Title", "bug", "high");
    assert.equal(body.title, "My Title");
    assert.equal(body.type, "bug");
    assert.equal(body.priority, "high");
  });

  check("G7 buildIssueCreateBody: omits falsy optional fields", () => {
    const body = app.buildIssueCreateBody("desc", "m1", "/proj", "", "", "");
    assert.equal("title" in body, false);
    assert.equal("type" in body, false);
    assert.equal("priority" in body, false);
    assert.equal("tags" in body, false);
  });

  check("G7 buildIssueCreateBody: includes tags when non-empty", () => {
    const body = app.buildIssueCreateBody("desc", "m1", "/proj", "", "", "", ["ui", "perf"]);
    assert.deepEqual(body.tags, ["ui", "perf"]);
  });

  check("G7 buildIssueCreateBody: omits tags when empty", () => {
    const body = app.buildIssueCreateBody("desc", "m1", "/proj", "", "", "", []);
    assert.equal("tags" in body, false);
  });

  check("G7 buildIssueCreateBody: never emits a scope field", () => {
    const body = app.buildIssueCreateBody("desc", "m1", "/proj", "t", "bug", "high", ["ui"]);
    assert.equal("scope" in body, false);
  });

  // ---- (j) buildIssueEditBody -----------------------------------------------

  check("G7 buildIssueEditBody: includes only dirty fields plus routing keys", () => {
    const dirty = new Set(["issue-title"]);
    const body = app.buildIssueEditBody("new desc", "m1", "/proj", dirty, {
      title: "Updated",
      type: "bug",
      priority: "low",
    });
    // description was NOT dirty — must not appear (the snapshot value is only a
    // DESC_CLIP preview, so PATCHing it back would truncate the stored body).
    assert.equal("description" in body, false);
    assert.equal(body.machine_id, "m1");
    assert.equal(body.project_root, "/proj");
    assert.equal(body.title, "Updated");
    // type and priority were NOT dirty — must not appear
    assert.equal("type" in body, false);
    assert.equal("priority" in body, false);
    assert.equal("tags" in body, false);
  });

  check("G7 buildIssueEditBody: includes description only when it is dirty", () => {
    const dirty = new Set(["issue-description"]);
    const body = app.buildIssueEditBody("edited body", "m1", "/proj", dirty, {
      title: "T",
    });
    assert.equal(body.description, "edited body");
    // title was NOT dirty — must not appear
    assert.equal("title" in body, false);
  });

  check("G7 buildIssueEditBody: omits machine_id/project_root when empty", () => {
    const body = app.buildIssueEditBody("desc", "", "", new Set(), {});
    assert.equal("machine_id" in body, false);
    assert.equal("project_root" in body, false);
  });

  check("G7 buildIssueEditBody: empty string for dirty field clears value", () => {
    const dirty = new Set(["issue-title", "issue-priority"]);
    const body = app.buildIssueEditBody("desc", "m1", "/proj", dirty, {
      title: "",
      priority: "",
    });
    assert.equal(body.title, "");
    assert.equal(body.priority, "");
  });

  check("G7 buildIssueEditBody: never emits a scope field even if formValues has one", () => {
    const dirty = new Set(["issue-title"]);
    const body = app.buildIssueEditBody("desc", "m1", "/proj", dirty, {
      title: "Updated",
      scope: "out_of_scope",
    });
    assert.equal("scope" in body, false);
  });

  check("G7 buildIssueEditBody: includes tags when dirty", () => {
    const dirty = new Set(["issue-tags"]);
    const body = app.buildIssueEditBody("desc", "m1", "/proj", dirty, {
      tags: ["ui", "docs"],
    });
    assert.deepEqual(body.tags, ["ui", "docs"]);
  });

  check("G7 buildIssueEditBody: sends empty array when tags dirty but empty", () => {
    const dirty = new Set(["issue-tags"]);
    const body = app.buildIssueEditBody("desc", "m1", "/proj", dirty, {
      tags: [],
    });
    assert.deepEqual(body.tags, []);
  });

  // ---- (k) buildIssueActionBody ---------------------------------------------

  check("G7 buildIssueActionBody: includes routing keys when present", () => {
    const body = app.buildIssueActionBody("m1", "/proj");
    assert.equal(body.machine_id, "m1");
    assert.equal(body.project_root, "/proj");
    assert.equal("reason" in body, false);
  });

  check("G7 buildIssueActionBody: includes reason when truthy", () => {
    const body = app.buildIssueActionBody("m1", "/proj", "duplicate");
    assert.equal(body.reason, "duplicate");
  });

  check("G7 buildIssueActionBody: omits empty routing keys", () => {
    const body = app.buildIssueActionBody("", "");
    assert.equal("machine_id" in body, false);
    assert.equal("project_root" in body, false);
  });

  // ---- (l) parseTagsFromString / formatTagsForInput --------------------------

  check("G7 parseTagsFromString: splits comma-separated tags", () => {
    assert.deepEqual(app.parseTagsFromString("ui, perf, docs"), ["ui", "perf", "docs"]);
  });

  check("G7 parseTagsFromString: trims whitespace", () => {
    assert.deepEqual(app.parseTagsFromString("  ui ,  perf  , docs "), ["ui", "perf", "docs"]);
  });

  check("G7 parseTagsFromString: filters empty entries", () => {
    assert.deepEqual(app.parseTagsFromString("ui,,perf,"), ["ui", "perf"]);
  });

  check("G7 parseTagsFromString: returns empty array for falsy input", () => {
    assert.deepEqual(app.parseTagsFromString(""), []);
    assert.deepEqual(app.parseTagsFromString(null), []);
    assert.deepEqual(app.parseTagsFromString(undefined), []);
  });

  check("G7 formatTagsForInput: joins tags with comma-space", () => {
    assert.equal(app.formatTagsForInput(["ui", "perf", "docs"]), "ui, perf, docs");
  });

  check("G7 formatTagsForInput: returns empty string for empty/invalid input", () => {
    assert.equal(app.formatTagsForInput([]), "");
    assert.equal(app.formatTagsForInput(null), "");
    assert.equal(app.formatTagsForInput(undefined), "");
  });

  // ---- (m) issueCompositeKey -------------------------------------------------
  // Pins the composite-key contract that prevents cross-project id collisions
  // in the issues panel selection/detail lookup.

  check("G7 issueCompositeKey: combines machine_id, project_root, and id", () => {
    const key = app.issueCompositeKey({
      machine_id: "m1",
      project_root: "/proj",
      id: "001",
    });
    assert.equal(key, "m1::/proj::001");
  });

  check("G7 issueCompositeKey: different projects produce different keys", () => {
    const keyA = app.issueCompositeKey({
      machine_id: "m1",
      project_root: "/projA",
      id: "001",
    });
    const keyB = app.issueCompositeKey({
      machine_id: "m1",
      project_root: "/projB",
      id: "001",
    });
    assert.notEqual(keyA, keyB);
  });

  check("G7 issueCompositeKey: falls back to _machine_id", () => {
    const key = app.issueCompositeKey({
      _machine_id: "legacy",
      project_root: "/proj",
      id: "002",
    });
    assert.equal(key, "legacy::/proj::002");
  });

  check("G7 issueCompositeKey: handles null/missing gracefully", () => {
    assert.equal(app.issueCompositeKey(null), "");
    assert.equal(app.issueCompositeKey({}), "::::");
    assert.equal(app.issueCompositeKey({ id: "001" }), "::::001");
  });

  // ---- (m) selectTypeDropdownOptions ----------------------------------------
  // Locks in that the dropdown options come from the unfiltered type universe
  // (allIssueTypes) when available, rather than from the already-narrowed
  // filtered list (state.issues).  Without this preference, selecting a type
  // filter removes all other types from the dropdown — the exact regression
  // the fetchAllIssueTypes + refreshIssueTypeFilter fix prevents.

  check("G7 selectTypeDropdownOptions: prefers allIssueTypes over issues", () => {
    // Simulates: user has type filter active, state.issues contains only
    // "bug" entries, but allIssueTypes has the full universe.
    const allIssueTypes = ["bug", "feature", "task"];
    const filteredIssues = [{ type: "bug" }, { type: "bug" }];
    const result = app.selectTypeDropdownOptions(allIssueTypes, filteredIssues);
    assert.deepEqual(result, ["bug", "feature", "task"]);
  });

  check("G7 selectTypeDropdownOptions: falls back to issueTypes(issues) when allIssueTypes is empty", () => {
    const result = app.selectTypeDropdownOptions([], [{ type: "bug" }, { type: "feature" }]);
    assert.deepEqual(result, ["bug", "feature"]);
  });

  check("G7 selectTypeDropdownOptions: falls back when allIssueTypes is null", () => {
    const result = app.selectTypeDropdownOptions(null, [{ type: "enhancement" }]);
    assert.deepEqual(result, ["enhancement"]);
  });

  check("G7 selectTypeDropdownOptions: falls back when allIssueTypes is undefined", () => {
    const result = app.selectTypeDropdownOptions(undefined, [{ type: "idea" }]);
    assert.deepEqual(result, ["idea"]);
  });

  check("G7 selectTypeDropdownOptions: returns empty when both are empty", () => {
    assert.deepEqual(app.selectTypeDropdownOptions([], []), []);
    assert.deepEqual(app.selectTypeDropdownOptions(null, null), []);
  });

  // ---- (n) fetchIssuesCoalesceDecision ----------------------------------------
  // Regression tests for the request-coalescing state machine that previously
  // caused the issues list to never receive data under fast STATUS_UPDATEs
  // (every in-flight response was discarded as "stale" because the old code
  // bumped _issuesFetchSeq on every call instead of deferring when in-flight).

  check("G7 fetchIssuesCoalesceDecision: defers when in-flight", () => {
    const result = app.fetchIssuesCoalesceDecision({ inFlight: true, seq: 3 });
    assert.equal(result.action, "defer");
    assert.equal(result.refreshPending, true);
    // seq must NOT be bumped on defer — that was the root cause of the
    // starvation bug (bumping seq discarded every in-flight response).
    assert.equal("seq" in result, false);
  });

  check("G7 fetchIssuesCoalesceDecision: proceeds when idle", () => {
    const result = app.fetchIssuesCoalesceDecision({ inFlight: false, seq: 3 });
    assert.equal(result.action, "proceed");
    assert.equal(result.seq, 4);
  });

  check("G7 fetchIssuesCoalesceDecision: proceeds from seq 0", () => {
    const result = app.fetchIssuesCoalesceDecision({ inFlight: false, seq: 0 });
    assert.equal(result.action, "proceed");
    assert.equal(result.seq, 1);
  });

  // ---- (o) fetchIssuesFinallyDecision -----------------------------------------
  // Regression tests for the finally-block ordering: render first, then
  // re-dispatch.  The old code that reversed this order (or used per-call
  // seq bumping) caused perpetual loading.

  check("G7 fetchIssuesFinallyDecision: applies when seq matches", () => {
    const result = app.fetchIssuesFinallyDecision(5, { fetchSeq: 5, refreshPending: false });
    assert.equal(result.applyResponse, true);
    assert.equal(result.reDispatch, false);
  });

  check("G7 fetchIssuesFinallyDecision: discards stale response", () => {
    const result = app.fetchIssuesFinallyDecision(3, { fetchSeq: 5, refreshPending: false });
    assert.equal(result.applyResponse, false);
    assert.equal(result.reDispatch, false);
  });

  check("G7 fetchIssuesFinallyDecision: re-dispatches when refresh pending", () => {
    const result = app.fetchIssuesFinallyDecision(5, { fetchSeq: 5, refreshPending: true });
    assert.equal(result.applyResponse, true);
    assert.equal(result.reDispatch, true);
  });

  check("G7 fetchIssuesFinallyDecision: re-dispatches even for stale response", () => {
    // A stale response still triggers re-dispatch if refresh was pending —
    // the re-dispatch will pick up the newer data.
    const result = app.fetchIssuesFinallyDecision(3, { fetchSeq: 5, refreshPending: true });
    assert.equal(result.applyResponse, false);
    assert.equal(result.reDispatch, true);
  });

  check("G7 fetchIssuesFinallyDecision: no re-dispatch when no refresh pending", () => {
    const result = app.fetchIssuesFinallyDecision(5, { fetchSeq: 5, refreshPending: false });
    assert.equal(result.reDispatch, false);
  });

  // ---- (p) fetchIssues starvation regression scenario -------------------------
  // Simulates the exact scenario that caused the prior bug: multiple rapid
  // calls while one request is in-flight.  Under the old code, every call
  // bumped seq, so the in-flight response always saw seq !== _issuesFetchSeq
  // and was discarded.  Under the new coalescing logic, the second call
  // defers (sets refreshPending), the first call's response is applied (seq
  // matches), and then a re-dispatch happens.

  check("G7 fetchIssues coalesce: rapid calls do not starve the list", () => {
    // First call — idle, proceeds with seq 1.
    const call1 = app.fetchIssuesCoalesceDecision({ inFlight: false, seq: 0 });
    assert.equal(call1.action, "proceed");
    assert.equal(call1.seq, 1);

    // Second call while first is in-flight — defers.
    const call2 = app.fetchIssuesCoalesceDecision({ inFlight: true, seq: 1 });
    assert.equal(call2.action, "defer");
    assert.equal(call2.refreshPending, true);

    // First call completes — seq matches, apply + re-dispatch.
    const fin1 = app.fetchIssuesFinallyDecision(1, { fetchSeq: 1, refreshPending: true });
    assert.equal(fin1.applyResponse, true);
    assert.equal(fin1.reDispatch, true);

    // Re-dispatch — idle now, proceeds with seq 2.
    const call3 = app.fetchIssuesCoalesceDecision({ inFlight: false, seq: 1 });
    assert.equal(call3.action, "proceed");
    assert.equal(call3.seq, 2);

    // Re-dispatch completes — seq matches, no more refresh pending.
    const fin2 = app.fetchIssuesFinallyDecision(2, { fetchSeq: 2, refreshPending: false });
    assert.equal(fin2.applyResponse, true);
    assert.equal(fin2.reDispatch, false);
  });

  // ---- (p2) allIssueTypesApplyDecision stale-response guard -------------------
  // Frequent STATUS_UPDATEs start overlapping fetchAllIssueTypes requests. Without
  // a sequence guard, a slower older response can complete last and overwrite a
  // newer project-root universe (dropping a just-added project and resetting the
  // selected project). The guard applies a response only when its seq is still the
  // latest.

  check("G1 allIssueTypesApplyDecision: applies the latest response", () => {
    assert.equal(app.allIssueTypesApplyDecision(5, 5), true);
  });

  check("G1 allIssueTypesApplyDecision: discards a stale (older) response", () => {
    // An older request (seq 3) completes after a newer one (seq 5) started.
    assert.equal(app.allIssueTypesApplyDecision(3, 5), false);
  });

  check("G1 allIssueTypesApplyDecision: overlapping requests keep only newest", () => {
    // Two requests start: request A gets seq 1, request B gets seq 2 (latest).
    const seqA = 1;
    const seqB = 2;
    const currentSeq = seqB;
    // B (newest) completes first and is applied.
    assert.equal(app.allIssueTypesApplyDecision(seqB, currentSeq), true);
    // A (older) completes later and is discarded, so it cannot clobber B's
    // newer universe / selection.
    assert.equal(app.allIssueTypesApplyDecision(seqA, currentSeq), false);
  });

  // ---- (q) issueProjectRoots ---------------------------------------------------
  // Derives the project-root dropdown options from an unfiltered issue set.
  // Mirrors the pattern of issueTypes (dedup + sort) but for project_root
  // strings. The dropdown options come from an unfiltered universe so that
  // selecting a project does not collapse the dropdown.

  check("G3 issueProjectRoots: deduplicates project_root from issues", () => {
    const issues = [
      { project_root: "/projA" },
      { project_root: "/projB" },
      { project_root: "/projA" }, // duplicate
    ];
    const result = app.issueProjectRoots(issues);
    assert.deepEqual(result, ["/projA", "/projB"]);
  });

  check("G3 issueProjectRoots: preserves first-seen order (stable dedup)", () => {
    const issues = [
      { project_root: "/projB" },
      { project_root: "/projA" },
      { project_root: "/projB" },
    ];
    const result = app.issueProjectRoots(issues);
    assert.deepEqual(result, ["/projB", "/projA"]);
  });

  check("G3 issueProjectRoots: skips issues with missing project_root", () => {
    const issues = [
      { project_root: "/projA" },
      { project_root: null },
      { project_root: undefined },
      { project_root: "" },
      { project_root: "   " },
      {}, // no project_root key at all
      { project_root: "/projB" },
    ];
    const result = app.issueProjectRoots(issues);
    assert.deepEqual(result, ["/projA", "/projB"]);
  });

  check("G3 issueProjectRoots: returns empty for non-array input", () => {
    assert.deepEqual(app.issueProjectRoots(null), []);
    assert.deepEqual(app.issueProjectRoots(undefined), []);
    assert.deepEqual(app.issueProjectRoots("nope"), []);
    assert.deepEqual(app.issueProjectRoots(42), []);
  });

  check("G3 issueProjectRoots: returns empty for empty array", () => {
    assert.deepEqual(app.issueProjectRoots([]), []);
  });

  check("G3 issueProjectRoots: skips null/invalid entries in the array", () => {
    const issues = [
      null,
      undefined,
      "nope",
      42,
      { project_root: "/projA" },
    ];
    const result = app.issueProjectRoots(issues);
    assert.deepEqual(result, ["/projA"]);
  });

  check("G3 issueProjectRoots: trims whitespace from project_root", () => {
    const issues = [
      { project_root: "  /projA  " },
    ];
    const result = app.issueProjectRoots(issues);
    assert.deepEqual(result, ["/projA"]);
  });

  check("G3 issueProjectRoots: treats trimmed duplicates as same", () => {
    const issues = [
      { project_root: "/projA" },
      { project_root: "  /projA  " },
    ];
    const result = app.issueProjectRoots(issues);
    assert.deepEqual(result, ["/projA"]);
  });

  // ---- (r) pickDefaultIssueProjectRoot -----------------------------------------
  // Determines which project_root the dropdown should select. Defaults to
  // "" (全部项目) on first load or when the current selection vanished.

  check("G3 pickDefaultIssueProjectRoot: returns empty string for null/undefined/empty allProjectRoots", () => {
    assert.equal(app.pickDefaultIssueProjectRoot(null, "/projA"), "");
    assert.equal(app.pickDefaultIssueProjectRoot(undefined, "/projA"), "");
    assert.equal(app.pickDefaultIssueProjectRoot([], "/projA"), "");
  });

  check("G3 pickDefaultIssueProjectRoot: preserves currentSelected when still present", () => {
    const roots = ["/projA", "/projB"];
    assert.equal(app.pickDefaultIssueProjectRoot(roots, "/projB"), "/projB");
  });

  check("G3 pickDefaultIssueProjectRoot: falls back to empty string when currentSelected vanished", () => {
    const roots = ["/projA", "/projB"];
    assert.equal(app.pickDefaultIssueProjectRoot(roots, "/projC"), "");
  });

  check("G3 pickDefaultIssueProjectRoot: defaults to empty string when currentSelected is null/undefined", () => {
    const roots = ["/projA", "/projB"];
    assert.equal(app.pickDefaultIssueProjectRoot(roots, null), "");
    assert.equal(app.pickDefaultIssueProjectRoot(roots, undefined), "");
  });

  check("G3 pickDefaultIssueProjectRoot: defaults to empty string when currentSelected is empty string", () => {
    const roots = ["/projA", "/projB"];
    assert.equal(app.pickDefaultIssueProjectRoot(roots, ""), "");
  });

  check("G3 pickDefaultIssueProjectRoot: single project still defaults to empty string", () => {
    // Unlike the history view which auto-selects the only bucket, the
    // issue project dropdown defaults to "全部项目" because the user
    // most commonly wants to see all issues regardless of project.
    const roots = ["/projA"];
    assert.equal(app.pickDefaultIssueProjectRoot(roots, null), "");
  });

  // ---- (l) issueLaunchModel — start-flow-from-issue gating (G4) -------------

  check("G4 issueLaunchModel: open issue is launchable", () => {
    const m = app.issueLaunchModel({ id: "001", status: "open" });
    assert.equal(m.canLaunch, true);
    assert.equal(m.reason, "");
  });

  check("G4 issueLaunchModel: missing status defaults to open and is launchable", () => {
    assert.equal(app.issueLaunchModel({ id: "001" }).canLaunch, true);
  });

  check("G4 issueLaunchModel: in-progress is disabled with a reason", () => {
    const m = app.issueLaunchModel({ id: "001", status: "in-progress" });
    assert.equal(m.canLaunch, false);
    assert.ok(m.reason.length > 0);
  });

  check("G4 issueLaunchModel: resolved / won't-fix / closed are all disabled", () => {
    for (const status of ["resolved", "won't-fix", "closed"]) {
      const m = app.issueLaunchModel({ id: "001", status });
      assert.equal(m.canLaunch, false, `status ${status} should be disabled`);
      assert.ok(m.reason.length > 0, `status ${status} should carry a reason`);
    }
  });

  check("G4 issueLaunchModel: status matching is case-insensitive and trimmed", () => {
    assert.equal(app.issueLaunchModel({ status: "  OPEN  " }).canLaunch, true);
    assert.equal(app.issueLaunchModel({ status: "In-Progress" }).canLaunch, false);
  });

  check("G4 issueLaunchModel: an unknown status is disabled with a generic reason", () => {
    const m = app.issueLaunchModel({ id: "001", status: "weird" });
    assert.equal(m.canLaunch, false);
    assert.ok(m.reason.includes("weird"));
  });

  check("G4 issueLaunchModel: a non-object is disabled", () => {
    assert.equal(app.issueLaunchModel(null).canLaunch, false);
    assert.equal(app.issueLaunchModel(undefined).canLaunch, false);
  });

  // ---- (m) buildIssueFlowBody — POST /api/flows from-issue body (G4) --------

  check("G4 buildIssueFlowBody: carries issue id, machine/project and discover", () => {
    const iss = {
      id: "042",
      machine_id: "m1",
      project_root: "/proj",
      status: "open",
    };
    const body = app.buildIssueFlowBody(iss, false);
    assert.equal(body.from_issue_id, "042");
    assert.equal(body.machine_id, "m1");
    assert.equal(body.project_root, "/proj");
    assert.equal(body.discover, false);
    // task content is intentionally empty — the daemon sources it from the issue.
    assert.equal(body.task, "");
  });

  check("G4 buildIssueFlowBody: threads the discover flag through", () => {
    const iss = { id: "042", machine_id: "m1", project_root: "/proj" };
    assert.equal(app.buildIssueFlowBody(iss, true).discover, true);
    // Non-boolean discover is coerced.
    assert.equal(app.buildIssueFlowBody(iss, 1).discover, true);
    assert.equal(app.buildIssueFlowBody(iss, undefined).discover, false);
  });

  check("G4 buildIssueFlowBody: honors the _machine_id REST key fallback", () => {
    // issueMachineId reads machine_id OR _machine_id (the aggregated REST shape).
    const iss = { id: "042", _machine_id: "mX", project_root: "/proj" };
    assert.equal(app.buildIssueFlowBody(iss, false).machine_id, "mX");
  });

  check("G4 buildIssueFlowBody: coerces numeric id to string and tolerates a bare issue", () => {
    const body = app.buildIssueFlowBody({ id: 7 }, false);
    assert.equal(body.from_issue_id, "7");
    assert.equal(body.machine_id, "");
    assert.equal(body.project_root, "");
  });

  check("G1 buildIssueFlowBody: threads the worktree flag through", () => {
    const iss = { id: "042", machine_id: "m1", project_root: "/proj" };
    // Explicit true rides into the body as worktree:true.
    assert.equal(app.buildIssueFlowBody(iss, false, true).worktree, true);
    // Non-boolean worktree is coerced (parallel to discover's handling).
    assert.equal(app.buildIssueFlowBody(iss, false, 1).worktree, true);
    assert.equal(app.buildIssueFlowBody(iss, false, undefined).worktree, false);
  });

  check("G1 buildIssueFlowBody: omitting the worktree arg defaults to false", () => {
    // Backward compatibility — the legacy two-arg call still yields worktree:false.
    const iss = { id: "042", machine_id: "m1", project_root: "/proj" };
    assert.equal(app.buildIssueFlowBody(iss, true).worktree, false);
  });
}
