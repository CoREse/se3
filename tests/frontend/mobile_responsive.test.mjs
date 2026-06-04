/*
 * Mobile-responsive pure-helper tests (Group G7).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerMobileResponsiveTests({app, check, findOne, findAll})` so the parent
 * harness drives the same check() reporter and the same `app` module export.
 *
 * The mobile-portrait pass (G1–G6) is overwhelmingly CSS that only takes effect
 * inside the @media (max-width: 600px) breakpoint; the only JavaScript it adds
 * are a handful of DOM-free state-transition helpers that flip a class
 * (hamburger open/close, main-list machines↔flows panel switch, History
 * list↔detail switch, flow-view sidebar drawer open/close). Those helpers are
 * the testable surface here — pure functions with no browser dependency, so we
 * assert their state machines directly. The visual rules themselves are covered
 * by the static-source guards in tests/test_frontend_mobile.py.
 */
import assert from "node:assert/strict";

export function registerMobileResponsiveTests(ctx) {
  const { app, check } = ctx;

  // -- navMenuNextState (G2 topbar overflow menu) ---------------------------
  // The hamburger is a plain toggle: the next state is simply the negation of
  // the current open flag. Two toggles return to the start (involution).
  check("G7 navMenuNextState toggles the open flag", () => {
    assert.equal(app.navMenuNextState(false), true);
    assert.equal(app.navMenuNextState(true), false);
  });
  check("G7 navMenuNextState is an involution (toggle twice → start)", () => {
    assert.equal(app.navMenuNextState(app.navMenuNextState(false)), false);
    assert.equal(app.navMenuNextState(app.navMenuNextState(true)), true);
  });
  check("G7 navMenuNextState coerces truthy/falsy inputs to a boolean", () => {
    assert.equal(app.navMenuNextState(undefined), true);
    assert.equal(app.navMenuNextState(null), true);
    assert.equal(app.navMenuNextState(0), true);
    assert.equal(app.navMenuNextState(1), false);
    assert.equal(app.navMenuNextState("open"), false);
  });

  // -- flowSidebarNextState (G4 running-console off-canvas drawer) -----------
  // Mirrors navMenuNextState: the sidebar drawer is a plain open/close toggle.
  check("G7 flowSidebarNextState toggles the open flag", () => {
    assert.equal(app.flowSidebarNextState(false), true);
    assert.equal(app.flowSidebarNextState(true), false);
  });
  check("G7 flowSidebarNextState is an involution", () => {
    assert.equal(app.flowSidebarNextState(app.flowSidebarNextState(true)), true);
  });
  check("G7 flowSidebarNextState coerces non-boolean inputs", () => {
    assert.equal(app.flowSidebarNextState(undefined), true);
    assert.equal(app.flowSidebarNextState("x"), false);
  });

  // -- listPanelState (G3 main-list Machines↔Flows panel switch) ------------
  // The main list collapses to a single visible panel on a phone. The state is
  // "machines" (default) or "flows"; selecting a machine reveals flows, the
  // back button / a reset returns to machines, and an unknown action is inert
  // (the current panel is preserved, defaulting to "machines").
  check("G7 listPanelState: select-machine → flows", () => {
    assert.equal(app.listPanelState("machines", "select-machine"), "flows");
    assert.equal(app.listPanelState("flows", "select-machine"), "flows");
  });
  check("G7 listPanelState: back / reset → machines", () => {
    assert.equal(app.listPanelState("flows", "back"), "machines");
    assert.equal(app.listPanelState("flows", "reset"), "machines");
    assert.equal(app.listPanelState("machines", "reset"), "machines");
  });
  check("G7 listPanelState: unknown action preserves the current panel", () => {
    assert.equal(app.listPanelState("flows", "noop"), "flows");
    assert.equal(app.listPanelState("machines", "noop"), "machines");
    // A bogus current value normalizes to the safe "machines" default.
    assert.equal(app.listPanelState("garbage", "noop"), "machines");
    assert.equal(app.listPanelState(undefined, undefined), "machines");
  });
  check("G7 listPanelState: full select→back round trip returns to machines", () => {
    let panel = "machines";
    panel = app.listPanelState(panel, "select-machine");
    assert.equal(panel, "flows");
    panel = app.listPanelState(panel, "back");
    assert.equal(panel, "machines");
  });

  // -- historyPanelState (G5 History list↔detail panel switch) --------------
  // Isomorphic to the main-list switch but over list/detail: selecting a
  // session reveals the detail, back / reset return to the list.
  check("G7 historyPanelState: select-session → detail", () => {
    assert.equal(app.historyPanelState("list", "select-session"), "detail");
    assert.equal(app.historyPanelState("detail", "select-session"), "detail");
  });
  check("G7 historyPanelState: back / reset → list", () => {
    assert.equal(app.historyPanelState("detail", "back"), "list");
    assert.equal(app.historyPanelState("detail", "reset"), "list");
    assert.equal(app.historyPanelState("list", "reset"), "list");
  });
  check("G7 historyPanelState: unknown action preserves the current panel", () => {
    assert.equal(app.historyPanelState("detail", "noop"), "detail");
    assert.equal(app.historyPanelState("list", "noop"), "list");
    assert.equal(app.historyPanelState("garbage", "noop"), "list");
    assert.equal(app.historyPanelState(undefined, undefined), "list");
  });
  check("G7 historyPanelState: full select→back round trip returns to list", () => {
    let panel = "list";
    panel = app.historyPanelState(panel, "select-session");
    assert.equal(panel, "detail");
    panel = app.historyPanelState(panel, "back");
    assert.equal(panel, "list");
  });

  // -- cross-helper invariant: the two panel switches never cross-leak -------
  // list uses machines/flows, history uses list/detail; their vocabularies are
  // disjoint so a stray action on one never yields the other's panel name.
  check("G7 panel switches keep disjoint vocabularies", () => {
    const listVals = new Set();
    const histVals = new Set();
    for (const cur of ["machines", "flows", "list", "detail", "x"]) {
      for (const act of ["select-machine", "select-session", "back", "reset", "noop"]) {
        listVals.add(app.listPanelState(cur, act));
        histVals.add(app.historyPanelState(cur, act));
      }
    }
    assert.deepEqual([...listVals].sort(), ["flows", "machines"]);
    assert.deepEqual([...histVals].sort(), ["detail", "list"]);
  });
}
