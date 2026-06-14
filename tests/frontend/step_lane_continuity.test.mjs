/*
 * Continuous step-lane background tests (Group G2).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerStepLaneContinuityTests({app, check, findOne,
 * findAll})` so the parent harness drives the same check() reporter.
 *
 * This is a CSS-only feature (方案A: 背景连续、块仍分明): the per-step lane colour,
 * formerly painted as a per-record island background, is now carried on a
 * --step-lane custom property and rendered through a z-index:-1 ::before underlay
 * that overflows ±7px into the .history-detail gap:14px so adjacent same-step
 * records' lanes meet edge-to-edge and read as one continuous band. The
 * assertions are read-only string/rule checks against style.css.
 *
 * Coverage:
 *   (a) The 13 per-type rules carry --step-lane (with their original colour /
 *       alpha) and keep their border-left-color, and no longer paint `background`
 *       directly on the record box.
 *   (b) A shared ::before underlay rule exists with top:-7px / bottom:-7px and
 *       z-index:-1, consuming background-color:var(--step-lane).
 *   (c) Boundary / container-edge overflow-suppression rules exist (first block
 *       no upward overflow, last block no downward overflow, container first /
 *       last child no overflow past the edge).
 *   (d) The geometry rule still excludes .step-status-row / .group-status-marker.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STYLE_CSS = path.join(
  HERE, "..", "..", "src", "se3", "server", "static", "style.css");
const CSS = fs.readFileSync(STYLE_CSS, "utf8");

// The 13 content step types and their original lane colour/alpha (must be
// preserved byte-for-byte, only moved from `background:` to `--step-lane:`).
const PER_TYPE = [
  ["discovery", "rgba(91, 143, 214, 0.07)"],
  ["analyze", "rgba(63, 169, 191, 0.07)"],
  ["plan", "rgba(138, 130, 224, 0.08)"],
  ["confirm", "rgba(111, 134, 201, 0.07)"],
  ["implement", "rgba(87, 168, 106, 0.07)"],
  ["test", "rgba(63, 174, 154, 0.07)"],
  ["self_check", "rgba(200, 154, 70, 0.08)"],
  ["verify_spec", "rgba(208, 130, 78, 0.08)"],
  ["update_spec", "rgba(84, 174, 196, 0.07)"],
  ["spec_gate", "rgba(194, 161, 63, 0.08)"],
  ["version_analyze", "rgba(168, 127, 196, 0.08)"],
  ["commit", "rgba(121, 168, 63, 0.07)"],
  ["summarize", "rgba(130, 139, 152, 0.09)"],
];

// Extract the body `{...}` of the first rule whose selector contains `needle`.
function ruleBody(css, needle) {
  const at = css.indexOf(needle);
  assert.notEqual(at, -1, `missing rule containing: ${needle}`);
  const open = css.indexOf("{", at);
  assert.notEqual(open, -1, `no '{' after: ${needle}`);
  const close = css.indexOf("}", open);
  assert.notEqual(close, -1, `no '}' after: ${needle}`);
  return css.slice(open + 1, close);
}

export function registerStepLaneContinuityTests(ctx) {
  const { check } = ctx;

  // ---- (a) per-type rules carry --step-lane, keep border-left, drop bg ------
  for (const [type, colour] of PER_TYPE) {
    check(`G2 step-type-${type} carries --step-lane and keeps border-left`, () => {
      const sel =
        `.history-detail .conv-record.step-type-${type}:not(.step-status-row):not(.group-status-marker)`;
      const body = ruleBody(CSS, sel);
      assert.ok(
        body.includes(`--step-lane: ${colour}`),
        `${type} rule must set --step-lane: ${colour}`);
      assert.ok(
        body.includes(`border-left-color: var(--step-${type})`),
        `${type} rule must keep its border-left-color`);
      // No longer paints a plain `background` on the record box itself.
      assert.ok(
        !/(^|[\s;{])background\s*:/.test(body),
        `${type} rule must not paint background directly (lane moved to ::before)`);
    });
  }

  // ---- (b) shared ::before underlay with ±7px overflow and z-index:-1 -------
  check("G2 step-type ::before underlay overflows ±7px under content", () => {
    const body = ruleBody(
      CSS,
      `.history-detail .conv-record[class*="step-type-"]:not(.step-status-row):not(.group-status-marker)::before`);
    assert.ok(body.includes("top: -7px"), "::before must overflow top: -7px");
    assert.ok(body.includes("bottom: -7px"), "::before must overflow bottom: -7px");
    assert.ok(body.includes("left: 0"), "::before must span left: 0");
    assert.ok(body.includes("right: 0"), "::before must span right: 0");
    assert.ok(/z-index:\s*-1/.test(body), "::before must sit at z-index: -1 (below content)");
    assert.ok(
      body.includes("background-color: var(--step-lane)"),
      "::before must paint background-color: var(--step-lane)");
    assert.ok(body.includes('content: ""'), "::before must set content");
    assert.ok(/position:\s*absolute/.test(body), "::before must be absolutely positioned");
  });

  check("G2 step-type record establishes a positioning context", () => {
    const body = ruleBody(
      CSS,
      `.history-detail .conv-record[class*="step-type-"]:not(.step-status-row):not(.group-status-marker) {`);
    assert.ok(/position:\s*relative/.test(body),
      "content step-type record must be position: relative for the ::before underlay");
  });

  // ---- (b2) visibility contract: record must form a STACKING CONTEXT --------
  // Pins the fix for the self-check critical issue. The lane is painted by a
  // z-index:-1 ::before underlay. If .conv-record only has position:relative
  // (no stacking context), that negative-z pseudo resolves against the nearest
  // ancestor stacking context (.history-view / #flow-view) and is drawn BEHIND
  // the opaque `background: var(--bg)` of the intervening panes
  // (.history-detail-pane / .flow-main) — so the lane renders nothing. The
  // record MUST therefore form its own stacking context (isolation: isolate,
  // or an explicit non-auto z-index) so the underlay paints behind the
  // record's own content but ABOVE the pane background and stays visible.
  check("G2 step-type record forms a stacking context so the underlay is visible", () => {
    const body = ruleBody(
      CSS,
      `.history-detail .conv-record[class*="step-type-"]:not(.step-status-row):not(.group-status-marker) {`);
    const formsStackingContext =
      /isolation:\s*isolate/.test(body) ||
      // An explicit non-auto z-index on a positioned element also forms one.
      /z-index:\s*-?\d/.test(body);
    assert.ok(
      formsStackingContext,
      "content step-type record must form a stacking context (isolation: isolate " +
      "or an explicit z-index) so the z-index:-1 lane underlay paints above the " +
      "ancestor pane's opaque var(--bg) background instead of being hidden behind it");
  });

  // The intervening panes really do carry an opaque background — this is the
  // ancestor that would hide an un-contained underlay. Lock it so the test
  // above keeps its rationale: if these ever become transparent the stacking
  // requirement could be relaxed, but today it is mandatory.
  check("G2 intervening panes carry an opaque var(--bg) background", () => {
    const detailPane = ruleBody(CSS, ".history-detail-pane");
    assert.ok(/background:\s*var\(--bg\)/.test(detailPane),
      ".history-detail-pane must carry the opaque var(--bg) background the underlay must paint above");
  });

  // ---- (c) boundary / container-edge suppression ---------------------------
  // The base ::before underlay rule carries
  // `[class*="step-type-"]:not(.step-status-row):not(.group-status-marker)`,
  // giving it specificity (0,5,1). The four boundary-suppression rules below
  // must WIN the cascade over it to reset the ±7px overflow at step/container
  // edges. Two things make them win and BOTH must be locked, or the reset can
  // silently regress (a single-`.history-detail` selector is only (0,3,1), so
  // its top:0/bottom:0 would lose to the base ::before and the lane would bleed
  // across the .history-step-header into the neighbouring step):
  //   1. equal-or-greater specificity — achieved by tripling `.history-detail`
  //      (`.history-detail.history-detail.history-detail …`) to reach (0,5,1);
  //   2. later source order than the base ::before rule, to win the tie.
  // The single-`.history-detail` substring is found inside the tripled selector
  // regardless, so the needles below carry the full tripled prefix and we also
  // assert each rule appears AFTER the base ::before rule in source order.
  const TRIPLE = ".history-detail.history-detail.history-detail";
  const baseBeforeSel =
    `.history-detail .conv-record[class*="step-type-"]:not(.step-status-row):not(.group-status-marker)::before`;
  const baseBeforeAt = CSS.indexOf(baseBeforeSel);
  assert.notEqual(baseBeforeAt, -1, "base ::before underlay rule must exist");

  function boundaryRule(needle, edge) {
    // Must use the tripled prefix (specificity (0,5,1) >= base (0,5,1)).
    const at = CSS.indexOf(needle);
    assert.notEqual(
      at, -1,
      `boundary rule must use the specificity-tripled prefix: ${needle}`);
    // Must come AFTER the base ::before rule so it wins the specificity tie.
    assert.ok(
      at > baseBeforeAt,
      `boundary rule must appear later in source order than the base ::before ` +
      `rule to win the cascade tie: ${needle}`);
    const body = ruleBody(CSS, needle);
    assert.ok(
      new RegExp(`${edge}:\\s*0`).test(body),
      `boundary rule must reset ${edge} to 0: ${needle}`);
  }

  check("G2 step first block does not overflow upward across the header", () => {
    boundaryRule(`${TRIPLE} .history-step-header + .conv-record::before`, "top");
  });
  check("G2 step last block does not overflow downward across the next header", () => {
    boundaryRule(`${TRIPLE} .conv-record:has(+ .history-step-header)::before`, "bottom");
  });
  check("G2 container first/last child does not overflow past the edge", () => {
    boundaryRule(`${TRIPLE} > .conv-record:first-child::before`, "top");
    boundaryRule(`${TRIPLE} > .conv-record:last-child::before`, "bottom");
  });

  // Pin that the tripling is load-bearing: a single-`.history-detail` boundary
  // selector (specificity (0,3,1)) would LOSE the cascade to the base ::before
  // rule (0,5,1). Assert the boundary rules do NOT degrade to a bare
  // single-`.history-detail` form, so a future edit dropping the tripling fails.
  check("G2 boundary rules keep the load-bearing specificity tripling", () => {
    // Strip /* ... */ comments first: the explanatory comment above the rules
    // deliberately quotes the single-`.history-detail` anti-pattern as an
    // example, which would otherwise trip the "must be tripled" scan below.
    const CSS_NC = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
    for (const needle of [
      " .history-step-header + .conv-record::before",
      " .conv-record:has(+ .history-step-header)::before",
      " > .conv-record:first-child::before",
      " > .conv-record:last-child::before",
    ]) {
      // Every occurrence of this boundary tail must be preceded by the tripled
      // prefix — never a bare single `.history-detail`.
      let from = 0;
      let found = false;
      for (;;) {
        const at = CSS_NC.indexOf(needle, from);
        if (at === -1) break;
        found = true;
        const before = CSS_NC.slice(0, at);
        assert.ok(
          before.endsWith(TRIPLE),
          `boundary rule '${needle.trim()}' must be prefixed by the tripled ` +
          `'${TRIPLE}' (specificity (0,5,1)); a single '.history-detail' ` +
          `(0,3,1) would lose the cascade to the base ::before underlay`);
        from = at + needle.length;
      }
      assert.ok(found, `boundary rule tail must exist: ${needle.trim()}`);
    }
  });

  // ---- (d) status / DAG markers stay excluded ------------------------------
  check("G2 lane geometry still excludes status / group markers", () => {
    // Both the position:relative base rule and the ::before rule must carry the
    // :not(.step-status-row):not(.group-status-marker) exclusion.
    const baseSel =
      `.history-detail .conv-record[class*="step-type-"]:not(.step-status-row):not(.group-status-marker) {`;
    const beforeSel =
      `.history-detail .conv-record[class*="step-type-"]:not(.step-status-row):not(.group-status-marker)::before`;
    assert.notEqual(CSS.indexOf(baseSel), -1,
      "base geometry rule must exclude .step-status-row/.group-status-marker");
    assert.notEqual(CSS.indexOf(beforeSel), -1,
      "::before geometry rule must exclude .step-status-row/.group-status-marker");
  });
}
