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
}
