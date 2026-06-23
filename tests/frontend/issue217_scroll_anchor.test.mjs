/*
 * Element-anchored scroll-preservation tests (issue #217 / issue #209 jump fix).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`, whose `getBoundingClientRect` is driven
 * by a settable content-space `__rect` minus the summed ancestor `scrollTop`)
 * is installed. Exposes `registerIssue217ScrollAnchorTests({app, check, ...})`
 * so the parent harness drives the same `check()` reporter and `app` export.
 *
 * Context: issue #209's progression-triggered SILENT rebuild does a from-scratch
 * `renderConversation(append=false)`. Re-laying-out the same records can give the
 * content ABOVE the reader's viewport a different total height, so restoring an
 * absolute pixel `scrollTop` scrolled the conversation up a large stretch — the
 * bug issue #217 reports. The fix anchors on the bubble the reader is looking at
 * (`captureScrollAnchor`) and, after the rebuild, moves `scrollTop` so that same
 * bubble (matched by recordKey across the old/new arrays) returns to the same
 * viewport offset (`restoreScrollAnchor`), absorbing any height change above it.
 *
 * These tests pin:
 *   (1) when content ABOVE the anchored bubble grows taller, the bubble stays at
 *       the SAME viewport offset (scrollTop grows to match) — the exact scenario
 *       the old absolute-pixel restore could not express and which jumped;
 *   (2) the anchor follows the record by recordKey, NOT absolute index, so a
 *       record inserted ahead of the anchor still re-finds the right bubble;
 *   (3) captureScrollAnchor returns null when there is no usable geometry;
 *   (4) restoreScrollAnchor falls back to the absolute scrollTop when the anchor
 *       is null, the recordKey is missing, or the bubble has no geometry.
 *
 * Per the recorded env note, the chromium headless e2e is unavailable on this
 * host (missing libnspr4.so); this node-stub suite covers the equivalent logic.
 */
import assert from "node:assert/strict";

export function registerIssue217ScrollAnchorTests(ctx) {
  const { app, check } = ctx;

  const asstRecord = (content, ts, stepId, stepType) => ({
    step_id: stepId,
    step_type: stepType,
    message: { role: "assistant", content, timestamp: ts },
  });

  // Reset the flow-conversation container and render `records` into it from
  // scratch (the same append=false rebuild the silent path performs).
  function freshContainer(records) {
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    c.scrollTop = 0;
    // The container is the reference frame: its own scrollTop is never
    // subtracted from its own rect, so top 0 anchors the viewport at content 0.
    c.__rect = { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
    app.renderConversation(c, records, false);
    return c;
  }

  // Assign a content-space vertical layout to the rendered bubbles: each bubble
  // (in __convIdx order) is stacked at the next `top`, with the given height.
  // `__rect` is content space; the stub subtracts the container's scrollTop on
  // read, mirroring a real browser. Returns the cumulative content height.
  function layoutBubbles(container, heights) {
    const bubbles = container.children
      .filter((c) => c.__convIdx !== undefined)
      .sort((a, b) => a.__convIdx - b.__convIdx);
    assert.equal(bubbles.length, heights.length,
      "every record must have produced exactly one bubble");
    let top = 0;
    for (let i = 0; i < bubbles.length; i++) {
      const h = heights[i];
      bubbles[i].__rect = {
        top, left: 0, right: 0, bottom: top + h, width: 0, height: h,
      };
      top += h;
    }
    container.scrollHeight = top;
    return top;
  }

  // The current viewport offset of the bubble at `convIdx` (its top edge minus
  // the container's viewport top), as the reader perceives it.
  function viewportOffset(container, convIdx) {
    const bubble = container.children.find((c) => c.__convIdx === convIdx);
    return bubble.getBoundingClientRect().top
      - container.getBoundingClientRect().top;
  }

  // -- (1) content above the anchor grows taller → offset preserved -----------
  check("anchor: bubble stays at the same viewport offset when content above grows", () => {
    const records = [
      asstRecord("AAA", 1, "s1", "discovery"),
      asstRecord("BBB", 2, "s1", "discovery"),
      asstRecord("CCC", 3, "s1", "discovery"),
    ];
    const c = freshContainer(records);
    // OLD layout: three 100px bubbles. Reader scrolled so the 2nd bubble sits at
    // the viewport top (scrollTop 100 of a 300px-tall body, 100px viewport).
    layoutBubbles(c, [100, 100, 100]);
    c.clientHeight = 100;
    c.scrollTop = 100;
    assert.equal(viewportOffset(c, 1), 0,
      "precondition: the 2nd bubble is anchored at the viewport top");

    const anchor = app.captureScrollAnchor(c, records);
    assert.ok(anchor, "an anchor must be captured for a scrolled-up reader");
    assert.equal(anchor.recordKey, app.recordKey(records[1]),
      "the anchor must identify the topmost visible bubble's record");
    assert.equal(anchor.viewportOffset, 0);

    // SILENT rebuild: same records, but the FIRST bubble now lays out at 250px
    // (e.g. markdown reflowed taller) — the content above the anchor grew 150px.
    const preserveScrollTop = c.scrollTop; // the absolute fallback (= 100)
    app.renderConversation(c, records, false);
    layoutBubbles(c, [250, 100, 100]);

    app.restoreScrollAnchor(c, records, anchor, preserveScrollTop);

    // The anchored bubble is back at offset 0 — NOT yanked down 150px as the old
    // absolute-pixel restore (scrollTop clamped to 100) would have left it.
    assert.equal(viewportOffset(c, 1), 0,
      "the anchored bubble must stay at the same viewport offset");
    assert.equal(c.scrollTop, 250,
      "scrollTop must grow with the content above the anchor (250, not the stale 100)");
    assert.notEqual(c.scrollTop, preserveScrollTop,
      "the absolute-pixel fallback (100) would have jumped the view — the anchor must beat it");
  });

  // -- (2) anchor follows recordKey, not absolute index ----------------------
  check("anchor: re-finds the bubble by recordKey when a record is inserted ahead", () => {
    const records = [
      asstRecord("AAA", 1, "s1", "discovery"),
      asstRecord("BBB", 2, "s1", "discovery"),
      asstRecord("CCC", 3, "s1", "discovery"),
    ];
    const c = freshContainer(records);
    layoutBubbles(c, [100, 100, 100]);
    c.clientHeight = 100;
    c.scrollTop = 100; // 2nd bubble (BBB) at the viewport top
    const anchor = app.captureScrollAnchor(c, records);
    assert.equal(anchor.recordKey, app.recordKey(records[1]));

    // SILENT rebuild whose NEW array has a record inserted at the FRONT, so BBB
    // moves from index 1 to index 2. An index-based restore would grab the wrong
    // bubble; the recordKey match must follow BBB to its new index.
    const newRecords = [
      asstRecord("ZZZ", 0, "s1", "discovery"), // inserted ahead
      records[0],
      records[1], // BBB, now at index 2
      records[2],
    ];
    const preserveScrollTop = c.scrollTop;
    app.renderConversation(c, newRecords, false);
    layoutBubbles(c, [100, 100, 100, 100]); // BBB now at content top 200

    app.restoreScrollAnchor(c, newRecords, anchor, preserveScrollTop);

    // BBB (new index 2, content top 200) must sit at viewport offset 0 again.
    assert.equal(viewportOffset(c, 2), 0,
      "the anchor must follow BBB to its shifted index and pin it at offset 0");
    assert.equal(c.scrollTop, 200);
  });

  // -- (3) captureScrollAnchor returns null with no usable geometry ----------
  check("anchor: capture returns null when geometry is unavailable", () => {
    const records = [asstRecord("AAA", 1, "s1", "discovery")];
    const c = freshContainer(records);
    // No __rect assigned to the bubbles → all-zero rects → no visible bubble.
    c.clientHeight = 100;
    c.scrollTop = 50;
    assert.equal(app.captureScrollAnchor(c, records), null,
      "all-zero geometry must yield no anchor so the caller can fall back");
    // Empty records also yields null without throwing.
    assert.equal(app.captureScrollAnchor(c, []), null);
  });

  // -- (4) restore falls back to the absolute scrollTop when unusable --------
  check("anchor: restore falls back to the clamped absolute scrollTop", () => {
    const records = [
      asstRecord("AAA", 1, "s1", "discovery"),
      asstRecord("BBB", 2, "s1", "discovery"),
    ];
    const c = freshContainer(records);
    c.scrollHeight = 1000;

    // (a) null anchor → set the clamped fallback (the original preserveScrollTop
    // behaviour: Math.min(600, 1000) === 600).
    c.scrollTop = 0;
    app.restoreScrollAnchor(c, records, null, 600);
    assert.equal(c.scrollTop, 600, "a null anchor must restore the absolute fallback");

    // (b) anchor whose recordKey is absent from the new records → fallback.
    c.scrollTop = 0;
    app.restoreScrollAnchor(
      c, records, { recordKey: "no-such-key", viewportOffset: 0 }, 600);
    assert.equal(c.scrollTop, 600, "a vanished record must restore the absolute fallback");

    // (c) fallback is clamped to the (possibly smaller) new content height.
    c.scrollTop = 0;
    c.scrollHeight = 400;
    app.restoreScrollAnchor(c, records, null, 600);
    assert.equal(c.scrollTop, 400, "the fallback must clamp to the new content height");
  });
}
