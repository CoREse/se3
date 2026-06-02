/*
 * Docked reply-box prompt collapse tests (Group G1).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerReplyBoxPromptCollapseTests({app, check, findOne, findAll})` so the
 * parent harness can drive the same check() reporter and the same `app` module
 * export.
 *
 * Covers the overflow fix for the running-flow console's docked reply box: a
 * pending item with a very long prompt (most notably a discovery_confirm whose
 * prompt embeds a whole refined task description) must NOT grow the bar without
 * limit. The prompt body is therefore default-collapsed behind an expand/
 * collapse trigger, and the expanded body carries the `.flow-reply-prompt`
 * class that style.css height-caps (30vh) + scrolls.
 *
 * Coverage:
 *   (a) buildCollapsiblePrompt yields a trigger + a default-hidden body
 *       (the limited/scrollable `.flow-reply-prompt` class), expands on click,
 *       and collapses on a second click; expanding scrolls into view, collapse
 *       does not.
 *   (b) the full renderInterventions -> updateReplyBox path keeps the prompt
 *       body collapsed by default while header / options stay visible, and the
 *       trigger toggles the same body.
 */
import assert from "node:assert/strict";

export function registerReplyBoxPromptCollapseTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  // A pathologically long single-line prompt — the shape that used to push the
  // textarea / options / Send out of the docked bar.
  const LONG_PROMPT =
    "Proposed task description: " + "x".repeat(4000) + "\n输入 1 确认";

  // ---- (a) buildCollapsiblePrompt unit ------------------------------------
  check("G1 buildCollapsiblePrompt defaults to a collapsed body behind a trigger", () => {
    const wrap = app.buildCollapsiblePrompt(LONG_PROMPT);
    const toggle = findOne(wrap, "flow-reply-prompt-toggle");
    assert.ok(toggle, "expected an expand/collapse trigger button");
    assert.equal(toggle.tagName, "BUTTON");
    const body = findOne(wrap, "flow-reply-prompt");
    assert.ok(body, "expected the prompt body to be mounted");
    // Default collapsed: body hidden, wrapper not yet expanded.
    assert.equal(body.classList.contains("hidden"), true,
      "prompt body must start hidden (collapsed)");
    assert.equal(wrap.classList.contains("expanded"), false);
  });

  check("G1 buildCollapsiblePrompt expands then collapses on successive clicks", () => {
    const wrap = app.buildCollapsiblePrompt(LONG_PROMPT);
    const toggle = findOne(wrap, "flow-reply-prompt-toggle");
    const body = findOne(wrap, "flow-reply-prompt");

    // Expand.
    toggle.dispatch("click");
    assert.equal(body.classList.contains("hidden"), false,
      "body becomes visible on expand");
    assert.equal(wrap.classList.contains("expanded"), true);
    // The expanded body keeps the height-capped/scrollable class so the CSS
    // 30vh + overflow-y rule bounds it.
    assert.equal(body.classList.contains("flow-reply-prompt"), true,
      "expanded body keeps the limited/scrollable class");

    // Collapse.
    toggle.dispatch("click");
    assert.equal(body.classList.contains("hidden"), true,
      "body hides again on the second click");
    assert.equal(wrap.classList.contains("expanded"), false);
  });

  check("G1 buildCollapsiblePrompt scrolls into view on expand, not on collapse", () => {
    const wrap = app.buildCollapsiblePrompt(LONG_PROMPT);
    const toggle = findOne(wrap, "flow-reply-prompt-toggle");
    const body = findOne(wrap, "flow-reply-prompt");

    // Drive requestAnimationFrame synchronously and spy on the body's scroll.
    const savedRaf = globalThis.requestAnimationFrame;
    let scrolls = 0;
    body.scrollIntoView = () => { scrolls += 1; };
    globalThis.requestAnimationFrame = (cb) => cb();
    try {
      toggle.dispatch("click"); // expand -> scrolls
      assert.equal(scrolls, 1, "expanding scrolls the body into view once");
      toggle.dispatch("click"); // collapse -> no scroll
      assert.equal(scrolls, 1, "collapsing must not scroll");
    } finally {
      globalThis.requestAnimationFrame = savedRaf;
    }
  });

  check("G1 buildCollapsiblePrompt renders the prompt body content (no truncation)", () => {
    const wrap = app.buildCollapsiblePrompt("hello prompt body");
    const body = findOne(wrap, "flow-reply-prompt");
    assert.ok(body.textContent.includes("hello prompt body"),
      "prompt markdown content is mounted in the body");
  });

  // ---- (b) full updateReplyBox path ---------------------------------------
  check("G1 updateReplyBox collapses a long prompt by default, keeps controls visible", () => {
    const reply = document.getElementById("flow-reply-context");
    app.state.flowInterjectRequested = false;
    app.renderInterventions({
      status: "running",
      pending_calls: [
        {
          call_id: "dc_long",
          kind: "discovery_confirm",
          prompt: LONG_PROMPT,
        },
      ],
    });
    // The prompt body is present but collapsed (hidden) by default.
    const body = findOne(reply, "flow-reply-prompt");
    assert.ok(body, "prompt body still mounts");
    assert.equal(body.classList.contains("hidden"), true,
      "long prompt is collapsed by default in the docked reply box");
    // The bounded-height controls stay visible: header, the expand trigger,
    // and the synthesized confirm option button.
    assert.ok(findOne(reply, "flow-reply-head"), "header stays visible");
    assert.ok(findOne(reply, "flow-reply-prompt-toggle"),
      "an expand trigger is offered");
    assert.ok(findOne(reply, "flow-reply-options"),
      "discovery_confirm confirm option stays visible");
  });

  check("G1 updateReplyBox trigger expands the collapsed prompt in place", () => {
    const reply = document.getElementById("flow-reply-context");
    app.state.flowInterjectRequested = false;
    app.renderInterventions({
      status: "running",
      pending_calls: [
        { call_id: "k_call", kind: "call", prompt: LONG_PROMPT },
      ],
    });
    const toggle = findOne(reply, "flow-reply-prompt-toggle");
    const body = findOne(reply, "flow-reply-prompt");
    assert.equal(body.classList.contains("hidden"), true);
    toggle.dispatch("click");
    assert.equal(body.classList.contains("hidden"), false,
      "trigger reveals the prompt body");
    // Only one toggle / body pair — no duplicate prompt nodes leak in.
    assert.equal(findAll(reply, "flow-reply-prompt-toggle").length, 1);
    assert.equal(findAll(reply, "flow-reply-prompt").length, 1);
  });

  check("G1 updateReplyBox with no prompt still renders the hint, no trigger", () => {
    const reply = document.getElementById("flow-reply-context");
    app.state.flowInterjectRequested = false;
    app.renderInterventions({
      status: "running",
      pending_calls: [
        { call_id: "k_nohint", kind: "call" },
      ],
    });
    assert.equal(findOne(reply, "flow-reply-prompt-toggle"), null,
      "no prompt -> no expand trigger");
    assert.ok(findOne(reply, "flow-reply-hint"),
      "no prompt -> the kind hint is shown instead");
  });
}
