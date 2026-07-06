/*
 * discovery→analyze boundary scroll-anchor tests (issue #260, group G5).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`, whose getBoundingClientRect is driven by a
 * settable content-space `__rect` minus the summed ancestor scrollTop) is
 * installed. Exposes `registerDiscoveryAnalyzeScrollAnchorTests({app, check,
 * checkAsync, ...})` so the parent harness drives the same check() reporter and
 * `app` export — mirroring issue217_scroll_anchor / progression_refresh.
 *
 * Context: at the discovery→analyze boundary the WS increment stalls, so content
 * lands without an auto-scroll (or a large chunk arrives between the isNearBottom
 * measure and the scroll). The progression fallback then fires a SILENT full
 * rebuild whose stickiness used to be decided by the FROZEN-DOM isNearBottom —
 * which now reads scrollHeight-scrollTop-clientHeight>80 and MISJUDGES a
 * bottom-follower as scrolled-up. The rebuild took the anchor branch, pinned the
 * old tail, and jumped the view up (symptom (a)). The fix reads the persistent
 * `flowConversationFollowingBottom` intent — driven only by real scroll /
 * scroll-to-bottom signals — so a follower who merely drifted from a stalled
 * append still sticks to the bottom, while a reader who genuinely scrolled up
 * keeps their element-anchored viewport offset.
 *
 * These tests pin, reusing the issue217 `__rect` / layoutBubbles / viewportOffset
 * stub convention:
 *   (1) BOTTOM-FOLLOW: the frozen DOM misjudges (gap>80) but the intent flag is
 *       true → the silent rebuild sticks to the NEW bottom (no up-jump);
 *   (2) NON-BOTTOM: a scrolled-up reader (flag false) keeps the anchored bubble
 *       at the same viewport offset when the analyze label appends below it.
 *
 * Per the recorded env note, the chromium headless e2e is unavailable on this
 * host (missing libnspr4.so); this node-stub suite covers the equivalent logic.
 */
import assert from "node:assert/strict";

export async function registerDiscoveryAnalyzeScrollAnchorTests(ctx) {
  const { app, check, checkAsync } = ctx;

  const asst = (content, ts, stepId, stepType) => ({
    step_id: stepId,
    step_type: stepType,
    message: { role: "assistant", content, timestamp: ts },
  });

  // A one-shot fetch that always answers with `payload` (the silent full pull is
  // a single no-`after` GET). Mirrors installCountingFetch in progression_refresh.
  function installFetch(payload) {
    globalThis.fetch = () => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve(payload),
    });
  }

  // Assign a content-space vertical stack to the rendered bubbles (issue217's
  // stub): each bubble (in __convIdx order) sits at the next top with the given
  // height; `__rect` is content space and the stub subtracts scrollTop on read.
  function layoutBubbles(container, heights) {
    const bubbles = container.children
      .filter((c) => c.__convIdx !== undefined)
      .sort((a, b) => a.__convIdx - b.__convIdx);
    assert.equal(bubbles.length, heights.length,
      "every record must have produced exactly one bubble");
    let top = 0;
    for (let i = 0; i < bubbles.length; i++) {
      const h = heights[i];
      bubbles[i].__rect = { top, left: 0, right: 0, bottom: top + h, width: 0, height: h };
      top += h;
    }
    container.scrollHeight = top;
    return top;
  }

  // The viewport offset of the bubble at `convIdx` (its top edge minus the
  // container's viewport top), as the reader perceives it.
  function viewportOffset(container, convIdx) {
    const bubble = container.children.find((c) => c.__convIdx === convIdx);
    return bubble.getBoundingClientRect().top - container.getBoundingClientRect().top;
  }

  // -- (1) bottom-follower sticks despite the frozen-DOM misjudge --------------
  await checkAsync("discovery→analyze: a bottom-follower sticks to the new bottom after a silent rebuild (frozen-DOM misjudge)", async () => {
    const saved = globalThis.fetch;
    try {
      const c = document.getElementById("flow-conversation");
      c.innerHTML = ""; c.__convState = null;
      c.__rect = { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
      app.state.selectedFlowId = "F1";
      app.state.flowConversationRecords = [];
      app.state.flowConversationProgress = null;

      // Open at the end of discovery: three streamed bubbles, the last long.
      const discovery = [
        asst("disco 1", 1, "s1", "discovery"),
        asst("disco 2", 2, "s1", "discovery"),
        asst("disco 3 — long final turn", 3, "s1", "discovery"),
      ];
      installFetch({ records: discovery, progress: "d0", delivery: "full" });
      await app.loadFlowConversation("F1");

      // Frozen DOM at the boundary: the last append drifted the viewport off the
      // exact bottom (a stalled increment / a large chunk landing without an
      // auto-scroll), so isNearBottom MISJUDGES scrolled-up: gap 120 > 80.
      layoutBubbles(c, [100, 100, 300]);            // total 500
      c.clientHeight = 100;
      c.scrollTop = 280;                            // 500 - 280 - 100 = 120 > 80
      // But the reader never scrolled up — they are still a bottom-follower.
      app.state.flowConversationFollowingBottom = true;

      // The silent progression rebuild pulls discovery + the lone analyze label.
      installFetch({
        records: discovery.concat([asst("[analyze]", 4, "s2", "analyze")]),
        progress: "a0", delivery: "full",
      });
      await app.loadFlowConversation("F1", { silent: true });

      assert.equal(app.state.flowConversationRecords.length, 4,
        "the analyze record must have merged into the conversation");
      // Followed to the new bottom — NOT anchored back to the old drifted tail.
      assert.equal(c.scrollTop, c.scrollHeight,
        "a bottom-follower must stick to the new bottom, not jump up to the anchored old tail");
      assert.notEqual(c.scrollTop, 280,
        "the view must not stay pinned at the pre-rebuild scrollTop (the up-jump)");
    } finally {
      globalThis.fetch = saved;
    }
  });

  // -- (2) scrolled-up reader keeps their anchored viewport offset -------------
  check("discovery→analyze: a scrolled-up reader keeps the anchored bubble's viewport offset when analyze appends below", () => {
    const c = document.getElementById("flow-conversation");
    c.innerHTML = ""; c.__convState = null; c.scrollTop = 0;
    c.__rect = { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };

    const discovery = [
      asst("disco 1", 1, "s1", "discovery"),
      asst("disco 2", 2, "s1", "discovery"),
      asst("disco 3", 3, "s1", "discovery"),
    ];
    app.renderConversation(c, discovery, false);
    layoutBubbles(c, [100, 100, 100]);              // total 300
    c.clientHeight = 100;
    c.scrollTop = 100;                              // 2nd bubble at the viewport top
    assert.equal(viewportOffset(c, 1), 0, "precondition: the 2nd bubble anchors the viewport top");

    // The reader deliberately scrolled up: the scroll handler drops the intent,
    // so the silent path takes the element-anchor branch (silent && !stick).
    app.state.flowConversationFollowingBottom = false;
    const anchor = app.captureScrollAnchor(c, discovery);
    assert.ok(anchor, "a scrolled-up reader yields an anchor");
    assert.equal(anchor.recordKey, app.recordKey(discovery[1]));
    const preserveScrollTop = c.scrollTop;

    // Silent rebuild: discovery + the analyze label appended at the TAIL. Content
    // ABOVE the anchor is unchanged, so the anchored bubble must not move.
    const rebuilt = discovery.concat([asst("[analyze]", 4, "s2", "analyze")]);
    app.renderConversation(c, rebuilt, false);
    layoutBubbles(c, [100, 100, 100, 40]);          // analyze label below the anchor
    app.restoreScrollAnchor(c, rebuilt, anchor, preserveScrollTop);

    assert.equal(viewportOffset(c, 1), 0,
      "the anchored discovery bubble stays at the same viewport offset when analyze appends below");
    assert.equal(c.scrollTop, 100,
      "scrollTop is unchanged — no jump for a scrolled-up reader");
  });
}
