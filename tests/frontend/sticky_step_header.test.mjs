/*
 * Viewport-driven sticky floating step-header tests (Group G5).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`, including the getBoundingClientRect /
 * scrollTo / scrollTop geometry stubs) is installed. Exposes
 * `registerStickyStepHeaderTests({app, check, findOne, findAll})` so the parent
 * harness drives the same check() reporter and the same `app` module export.
 *
 * Coverage:
 *   (a) computeStickyStep — the DOM-free viewport→step judgement: up/down
 *       scroll returns the correct step, a header AT the top hides the float
 *       (mutual exclusion with the original header), empty/single/boundary
 *       inputs degrade sanely, and the returned step reflects ONLY the viewport
 *       top (never the executing step).
 *   (b) stickyScrollTarget — the click-to-locate target offset (pure).
 *   (c) DOM wiring via ensureStickyHeaderMounted — float mounts, hidden at the
 *       top, shows the right label after scrolling down, switches to the
 *       previous step on scroll up, hides when an original header reaches the
 *       top, and a click smooth-scrolls the original header to the top + hides
 *       the float without touching any record/state.
 *   (d) Running-flow view (scroller === content) and history view (content
 *       nested inside a separate scroller) share the SAME logic and behave
 *       identically.
 */
import assert from "node:assert/strict";

export function registerStickyStepHeaderTests(ctx) {
  const { app, check, findOne } = ctx;

  // -- builders ------------------------------------------------------------
  // A `.history-step-header` separator row with a `.history-step-title`, whose
  // getBoundingClientRect top is pinned to its content offset (mount happens at
  // scrollTop 0, so measured offset == content offset).
  function makeHeader(label, top) {
    const h = document.createElement("div");
    h.className = "history-step-header";
    const t = document.createElement("h5");
    t.className = "history-step-title";
    t.textContent = label;
    h.appendChild(t);
    h.__rect = { top, left: 0, right: 100, bottom: top + 28, width: 100, height: 28 };
    return h;
  }
  // The visible scroll viewport height. The sticky float hides as soon as an
  // original header is visible ANYWHERE in this viewport (the reveal band is the
  // viewport height, per stickyRevealPx), so the float only shows while scrolled
  // into a step region TALLER than the viewport. The DOM fixtures therefore use
  // a small viewport and headers spaced far apart (≫ VIEWPORT) so each step
  // region is "tall" and the float meaningfully appears mid-region.
  const VIEWPORT = 100;
  function buildContent(headers) {
    const content = document.createElement("div");
    content.__rect = { top: 0, left: 0, right: 100, bottom: VIEWPORT, width: 100, height: VIEWPORT };
    for (const h of headers) content.appendChild(h);
    return content;
  }
  // offsets array shorthand for the pure-function tests.
  const offs = (...tops) => tops.map((top, i) => ({ index: i, top, label: "S" + i }));

  // ======================================================================
  // (a) computeStickyStep — pure viewport→step judgement
  // ======================================================================
  check("G5 computeStickyStep returns null for empty/invalid input", () => {
    assert.equal(app.computeStickyStep([], 0), null);
    assert.equal(app.computeStickyStep(null, 50), null);
    assert.equal(app.computeStickyStep(undefined, 50), null);
  });

  check("G5 computeStickyStep hides the float when the viewport top is above the first header", () => {
    // Single header at the very top: at scrollTop 0 the original header is
    // visible → no floating duplicate.
    assert.equal(app.computeStickyStep(offs(0), 0), null);
    // First header below the viewport top (content above it) → still hidden.
    assert.equal(app.computeStickyStep(offs(40, 200), 0), null);
  });

  check("G5 computeStickyStep returns the step whose content sits at the viewport top", () => {
    const o = offs(0, 100, 300);
    // Scrolled 50px down: header0 has scrolled off, its step owns the top.
    let r = app.computeStickyStep(o, 50);
    assert.ok(r && r.index === 0, `expected step 0, got ${r && r.index}`);
    assert.equal(r.label, "S0");
    // Between header1 (100) and header2 (300): step 1 owns the top.
    r = app.computeStickyStep(o, 150);
    assert.ok(r && r.index === 1, `expected step 1, got ${r && r.index}`);
    // Past the last header: step 2 owns the top.
    r = app.computeStickyStep(o, 350);
    assert.ok(r && r.index === 2, `expected step 2, got ${r && r.index}`);
  });

  check("G5 computeStickyStep hides the float when an original header is exactly at the top", () => {
    const o = offs(0, 100, 300);
    // Mutual exclusion: each header AT the viewport top → original visible →
    // float hidden, even though an earlier header has scrolled off.
    assert.equal(app.computeStickyStep(o, 100), null);
    assert.equal(app.computeStickyStep(o, 300), null);
    // The 1px tolerance absorbs fractional scroll positions around a header.
    assert.equal(app.computeStickyStep(o, 100.4), null);
    assert.equal(app.computeStickyStep(o, 299.6), null);
  });

  check("G5 computeStickyStep switches to the previous step when scrolling up", () => {
    const o = offs(0, 100, 300);
    // Down at 150 → step 1. Scroll up so step 0's content re-enters the top.
    assert.equal(app.computeStickyStep(o, 150).index, 1);
    const up = app.computeStickyStep(o, 90);
    assert.ok(up && up.index === 0,
      `scrolling up must immediately fall back to the previous step, got ${up && up.index}`);
  });

  check("G5 computeStickyStep clamps negative / NaN scrollTop to the top", () => {
    const o = offs(0, 100);
    assert.equal(app.computeStickyStep(o, -50), null);
    assert.equal(app.computeStickyStep(o, NaN), null);
  });

  check("G5 computeStickyStep ignores non-finite header offsets without throwing", () => {
    const o = [{ index: 0, top: 0, label: "A" }, { index: 1, top: NaN, label: "B" },
      { index: 2, top: 200, label: "C" }];
    // At 250 the last finite header (200) owns the top; the NaN entry is skipped.
    const r = app.computeStickyStep(o, 250);
    assert.ok(r && r.index === 2 && r.label === "C");
  });

  check("G5 computeStickyStep hides the float once a header enters the reveal band", () => {
    const o = offs(0, 100, 300);
    // Pure contract: `revealPx` is the band below the viewport top within which
    // a visible header hides the float. The DOM layer passes the visible
    // viewport height; here we exercise the mechanic with explicit values.
    // Default (no reveal arg) → only a ~1px tolerance hides: at 90 the next
    // header (100, 10px below) is still outside the band, so step 0 shows.
    assert.ok(app.computeStickyStep(o, 90) && app.computeStickyStep(o, 90).index === 0);
    // With a reveal band of 28, a header 10px below the top is "visible" within
    // the band → mutual exclusion hides the float.
    assert.equal(app.computeStickyStep(o, 90, 28), null,
      "an original header within the reveal band hides the float");
    // A header outside the band (300 is 150px below at y=150, band 28) does NOT hide.
    const r = app.computeStickyStep(o, 150, 28);
    assert.ok(r && r.index === 1,
      "a header beyond the reveal band keeps the float showing");
    // A larger band (viewport-height-like) hides the same header: at y=150 the
    // header 300 is 150px below but within a 200px band → float hidden.
    assert.equal(app.computeStickyStep(o, 150, 200), null,
      "a visible header within a viewport-sized band hides the float");
    // A reveal arg <= STICKY_EPS is treated as the bare tolerance (no widening).
    assert.ok(app.computeStickyStep(o, 90, 0) && app.computeStickyStep(o, 90, 0).index === 0);
  });

  // ======================================================================
  // (b) stickyScrollTarget — click-to-locate target offset (pure)
  // ======================================================================
  check("G5 stickyScrollTarget returns the header's content offset, null out of range", () => {
    const o = offs(0, 120, 360);
    assert.equal(app.stickyScrollTarget(o, 1), 120);
    assert.equal(app.stickyScrollTarget(o, 2), 360);
    assert.equal(app.stickyScrollTarget(o, -1), null);
    assert.equal(app.stickyScrollTarget(o, 9), null);
    assert.equal(app.stickyScrollTarget(null, 0), null);
  });

  // ======================================================================
  // (c) DOM wiring via ensureStickyHeaderMounted (running-flow view)
  // ======================================================================
  // In the running-flow view the conversation element IS its own scroller, so
  // scroller === content. Mount with scrollTop 0 so the measured header offsets
  // equal their content offsets.
  function mountFlow(headers) {
    const conv = buildContent(headers);
    conv.scrollTop = 0;
    app.ensureStickyHeaderMounted(conv, conv);
    return conv;
  }
  const floatOf = (scroller) =>
    findOne(scroller, "conv-sticky-header") || scroller.__convStickyFloat;
  const floatLabel = (floatEl) => {
    const t = findOne(floatEl, "conv-sticky-header__title");
    return t ? t.textContent : null;
  };
  const isHidden = (floatEl) => floatEl.classList.contains("hidden");

  // Tall-step header offsets: regions are 400px tall, far bigger than the 100px
  // VIEWPORT, so a step's next header is off-screen while scrolled into its
  // middle and the float meaningfully shows.
  const DISCOVERY_TOP = 0, ANALYZE_TOP = 400, IMPLEMENT_TOP = 800;

  check("G5 ensureStickyHeaderMounted creates a hidden float at the top of the scroll area", () => {
    const conv = mountFlow([makeHeader("DISCOVERY", DISCOVERY_TOP), makeHeader("ANALYZE", ANALYZE_TOP)]);
    const floatEl = floatOf(conv);
    assert.ok(floatEl, "a floating step header is mounted");
    // It is the FIRST child so the sticky anchor pins to the viewport top.
    assert.strictEqual(conv.firstChild, floatEl, "float is the scroller's first child");
    // At scrollTop 0 the original DISCOVERY header is visible → float hidden.
    assert.equal(isHidden(floatEl), true, "float hidden while the original header is at the top");
  });

  check("G5 scrolling deep into a step shows the float with that step's label", () => {
    const conv = mountFlow([makeHeader("DISCOVERY", DISCOVERY_TOP), makeHeader("ANALYZE", ANALYZE_TOP),
      makeHeader("IMPLEMENT", IMPLEMENT_TOP)]);
    const floatEl = floatOf(conv);
    // Deep inside ANALYZE (400..800), viewport 500..600: DISCOVERY & IMPLEMENT
    // headers are both off-screen → the float shows ANALYZE.
    conv.scrollTop = 500;
    conv.dispatch("scroll");
    assert.equal(isHidden(floatEl), false, "float shows once no original header is visible");
    assert.equal(floatLabel(floatEl), "ANALYZE", "float reflects the step at the viewport top");
  });

  check("G5 scrolling up switches the float to the previous step immediately", () => {
    const conv = mountFlow([makeHeader("DISCOVERY", DISCOVERY_TOP), makeHeader("ANALYZE", ANALYZE_TOP),
      makeHeader("IMPLEMENT", IMPLEMENT_TOP)]);
    const floatEl = floatOf(conv);
    conv.scrollTop = 500; conv.dispatch("scroll");
    assert.equal(floatLabel(floatEl), "ANALYZE");
    // Scroll back up deep into DISCOVERY (0..400), viewport 200..300: ANALYZE's
    // header (400) is off-screen below → float switches to DISCOVERY.
    conv.scrollTop = 200; conv.dispatch("scroll");
    assert.equal(isHidden(floatEl), false);
    assert.equal(floatLabel(floatEl), "DISCOVERY",
      "scrolling up must switch the float to the previous step");
  });

  check("G5 float hides when the original header re-enters the viewport top", () => {
    const conv = mountFlow([makeHeader("DISCOVERY", DISCOVERY_TOP), makeHeader("ANALYZE", ANALYZE_TOP)]);
    const floatEl = floatOf(conv);
    conv.scrollTop = 500; conv.dispatch("scroll");
    assert.equal(isHidden(floatEl), false);
    // Land the ANALYZE original header exactly at the top → float hidden.
    conv.scrollTop = ANALYZE_TOP; conv.dispatch("scroll");
    assert.equal(isHidden(floatEl), true,
      "float and the original header are mutually exclusive");
  });

  check("G5 clicking the float smooth-scrolls the original header to the top and hides the float", () => {
    const records = [{ message: { role: "user", content: "x" } }]; // sentinel for no-state-mutation
    const before = JSON.stringify(records);
    const conv = mountFlow([makeHeader("DISCOVERY", DISCOVERY_TOP), makeHeader("ANALYZE", ANALYZE_TOP),
      makeHeader("IMPLEMENT", IMPLEMENT_TOP)]);
    const floatEl = floatOf(conv);
    conv.scrollTop = 550; conv.dispatch("scroll");
    assert.equal(floatLabel(floatEl), "ANALYZE");
    // Click the floating banner: scroll ANALYZE's original header (offset 400)
    // to the top and hide the float.
    const inner = findOne(floatEl, "conv-sticky-header__inner");
    inner.dispatch("click");
    assert.equal(conv.scrollTop, ANALYZE_TOP, "click scrolls the original header to the top");
    assert.equal(isHidden(floatEl), true, "float hides after the click locates the header");
    // The interaction is pure navigation: it never touches conversation records
    // or any flow/step state.
    assert.equal(JSON.stringify(records), before, "click must not mutate any records/state");
  });

  check("G5 the float re-measures after a reflow shifts later headers", () => {
    const d = makeHeader("DISCOVERY", DISCOVERY_TOP);
    const a = makeHeader("ANALYZE", ANALYZE_TOP);
    const im = makeHeader("IMPLEMENT", IMPLEMENT_TOP);
    const conv = mountFlow([d, a, im]);
    const floatEl = floatOf(conv);
    conv.scrollTop = 500; conv.dispatch("scroll");
    assert.equal(floatLabel(floatEl), "ANALYZE");

    // Simulate expanding a long message inside the DISCOVERY region: every
    // header below it slides down by 300px. The mount-time cached offsets are
    // now stale — only a re-measure reflects the true positions.
    a.__rect = { top: 700, left: 0, right: 100, bottom: 728, width: 100, height: 28 };
    im.__rect = { top: 1100, left: 0, right: 100, bottom: 1128, width: 100, height: 28 };

    // At the SAME scrollTop the viewport top now falls in DISCOVERY (ANALYZE
    // moved to 700, off-screen below 500..600). A re-measuring update reflects
    // that; a stale cache would wrongly keep showing ANALYZE.
    conv.dispatch("scroll");
    assert.equal(floatLabel(floatEl), "DISCOVERY",
      "the float must reflect the post-reflow header positions, not the cache");

    // Click-to-locate must also use the fresh positions: with ANALYZE active at
    // a deeper scroll, the click scrolls to its NEW offset (700), not the stale 400.
    conv.scrollTop = 900; conv.dispatch("scroll");
    assert.equal(floatLabel(floatEl), "ANALYZE");
    const inner = findOne(floatEl, "conv-sticky-header__inner");
    inner.dispatch("click");
    assert.equal(conv.scrollTop, 700,
      "click locates the re-measured header offset, not the stale cached one");
  });

  check("G5 the float hides as soon as an original header is visible in the viewport", () => {
    // Issue-3 core: the float must hide whenever an original header is visible
    // anywhere in the scroll viewport — not only once it reaches the float
    // banner's own (small) height of the top.
    const conv = mountFlow([makeHeader("DISCOVERY", DISCOVERY_TOP), makeHeader("ANALYZE", ANALYZE_TOP),
      makeHeader("IMPLEMENT", IMPLEMENT_TOP)]);
    const floatEl = floatOf(conv);
    // ANALYZE (400) is 50px below the top (viewport 350..450) → VISIBLE → the
    // float must be hidden even though the header is well outside any tiny
    // banner band. This is the exact divergence the fix closes.
    conv.scrollTop = 350; conv.dispatch("scroll");
    assert.equal(isHidden(floatEl), true,
      "a visible original header 50px below the top hides the float");
    // Scroll so ANALYZE is off-screen below (150px below, viewport 250..350) →
    // no header visible → float shows the active step (DISCOVERY).
    conv.scrollTop = 250; conv.dispatch("scroll");
    assert.equal(isHidden(floatEl), false);
    assert.equal(floatLabel(floatEl), "DISCOVERY",
      "an off-screen next header keeps the float showing the active step");
  });

  check("G5 an empty conversation (no headers) keeps the float hidden", () => {
    const conv = mountFlow([]);
    const floatEl = floatOf(conv);
    assert.ok(floatEl);
    assert.equal(isHidden(floatEl), true);
    conv.scrollTop = 200; conv.dispatch("scroll");
    assert.equal(isHidden(floatEl), true, "no headers → nothing to float");
  });

  check("G5 a full re-render (cleared scroller) re-mounts the same float", () => {
    const conv = mountFlow([makeHeader("DISCOVERY", DISCOVERY_TOP), makeHeader("ANALYZE", ANALYZE_TOP)]);
    const first = floatOf(conv);
    // Simulate renderConversation's full rebuild wiping the container.
    conv.innerHTML = "";
    conv.appendChild(makeHeader("DISCOVERY", DISCOVERY_TOP));
    conv.appendChild(makeHeader("ANALYZE", ANALYZE_TOP));
    conv.scrollTop = 0;
    app.ensureStickyHeaderMounted(conv, conv);
    const again = floatOf(conv);
    assert.strictEqual(again, first, "the same float node is re-used across rebuilds");
    assert.strictEqual(conv.firstChild, again, "float re-mounts as the first child");
    conv.scrollTop = 500; conv.dispatch("scroll");
    assert.equal(floatLabel(again), "ANALYZE", "float still tracks scrolling after a rebuild");
  });

  // ======================================================================
  // (d) History view parity — content nested inside a separate scroller
  // ======================================================================
  check("G5 history view (nested scroller) shares the identical sticky behavior", () => {
    // The history detail (`content`) lives inside a separate scrolling pane
    // (`scroller`), unlike the flow view where they are the same element. The
    // SAME ensureStickyHeaderMounted drives both. Tall regions (500px) ≫ the
    // 100px pane viewport so the float shows mid-region.
    const content = buildContent([makeHeader("TEST", 0), makeHeader("COMMIT", 500),
      makeHeader("SUMMARY", 1000)]);
    const scroller = document.createElement("div");
    scroller.__rect = { top: 0, left: 0, right: 100, bottom: VIEWPORT, width: 100, height: VIEWPORT };
    scroller.scrollTop = 0;
    scroller.appendChild(content);
    app.ensureStickyHeaderMounted(scroller, content);
    const floatEl = scroller.__convStickyFloat;
    assert.ok(floatEl, "history float mounts on the pane scroller");
    assert.strictEqual(scroller.firstChild, floatEl, "float is the pane's first child");
    // Hidden at the top, shows the active step deep in a region, switches on up.
    assert.equal(floatEl.classList.contains("hidden"), true);
    scroller.scrollTop = 600; scroller.dispatch("scroll");  // deep in COMMIT
    assert.equal(floatLabel(floatEl), "COMMIT");
    scroller.scrollTop = 1100; scroller.dispatch("scroll"); // past SUMMARY
    assert.equal(floatLabel(floatEl), "SUMMARY");
    scroller.scrollTop = 200; scroller.dispatch("scroll");  // back deep in TEST
    assert.equal(floatLabel(floatEl), "TEST");
    // Click-to-locate works the same as the flow view.
    const inner = findOne(floatEl, "conv-sticky-header__inner");
    scroller.scrollTop = 600; scroller.dispatch("scroll"); // COMMIT active
    inner.dispatch("click");
    assert.equal(scroller.scrollTop, 500, "history click locates COMMIT's header to the top");
    assert.equal(floatEl.classList.contains("hidden"), true);
  });
}
