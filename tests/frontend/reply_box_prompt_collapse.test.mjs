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

  // ---- (c) expand-persist-across-rerender regression tests -----------------
  // Regression: after the user manually expands the prompt body, a subsequent
  // renderInterventions → updateReplyBox cycle (triggered by STATUS_UPDATE /
  // ws push) must NOT reset it back to collapsed.

  check("G1 expanded prompt survives a re-render (renderInterventions re-render)", () => {
    const reply = document.getElementById("flow-reply-context");
    const pendingCall = {
      call_id: "persist_1",
      kind: "discovery_confirm",
      prompt: LONG_PROMPT,
    };

    // First render — collapsed by default.
    app.state.flowInterjectRequested = false;
    app.renderInterventions({
      status: "running",
      pending_calls: [pendingCall],
    });
    const body1 = findOne(reply, "flow-reply-prompt");
    const toggle1 = findOne(reply, "flow-reply-prompt-toggle");
    assert.ok(body1, "prompt body mounts on first render");
    assert.equal(body1.classList.contains("hidden"), true,
      "starts collapsed");

    // User manually expands.
    toggle1.dispatch("click");
    assert.equal(body1.classList.contains("hidden"), false,
      "body visible after user click");
    assert.equal(app.state.flowReplyPromptExpanded["call:persist_1"], true,
      "expand state persisted to state");

    // Simulate a STATUS_UPDATE / ws push triggering re-render.
    app.renderInterventions({
      status: "running",
      pending_calls: [pendingCall],
    });
    const body2 = findOne(reply, "flow-reply-prompt");
    const wrap2 = findOne(reply, "flow-reply-prompt-wrap");
    assert.ok(body2, "prompt body re-mounts after re-render");
    assert.equal(body2.classList.contains("hidden"), false,
      "body must still be visible after re-render (not reset to collapsed)");
    assert.equal(wrap2.classList.contains("expanded"), true,
      "wrapper keeps expanded class after re-render");
    const toggle2 = findOne(reply, "flow-reply-prompt-toggle");
    assert.equal(toggle2.textContent, "▾ 收起消息详情",
      "trigger text shows collapse label after re-render");
  });

  check("G1 collapsed prompt stays collapsed across re-render", () => {
    const reply = document.getElementById("flow-reply-context");
    const pendingCall = {
      call_id: "stay_collapsed",
      kind: "call",
      prompt: "some prompt text",
    };

    // Render — collapsed by default, user does NOT expand.
    app.state.flowInterjectRequested = false;
    app.state.flowReplyPromptExpanded = {};
    app.renderInterventions({
      status: "running",
      pending_calls: [pendingCall],
    });
    const body1 = findOne(reply, "flow-reply-prompt");
    assert.equal(body1.classList.contains("hidden"), true,
      "starts collapsed");

    // Re-render without user interaction.
    app.renderInterventions({
      status: "running",
      pending_calls: [pendingCall],
    });
    const body2 = findOne(reply, "flow-reply-prompt");
    assert.equal(body2.classList.contains("hidden"), true,
      "remains collapsed after re-render");
  });

  check("G1 different intervention ids expand independently", () => {
    const reply = document.getElementById("flow-reply-context");
    app.state.flowReplyPromptExpanded = {};

    // Render with two pending calls — only the first is selected by default.
    app.state.flowInterjectRequested = false;
    app.renderInterventions({
      status: "running",
      pending_calls: [
        { call_id: "chip_a", kind: "call", prompt: "prompt A" },
        { call_id: "chip_b", kind: "call", prompt: "prompt B" },
      ],
    });
    // Expand the currently-selected (first) chip's prompt.
    const toggle = findOne(reply, "flow-reply-prompt-toggle");
    toggle.dispatch("click");
    assert.equal(app.state.flowReplyPromptExpanded["call:chip_a"], true,
      "first chip expanded");
    assert.equal(app.state.flowReplyPromptExpanded["call:chip_b"], undefined,
      "second chip not touched");

    // Select the second chip — its prompt should start collapsed.
    app.state.flowReplyTargetId = "call:chip_b";
    app.renderInterventions({
      status: "running",
      pending_calls: [
        { call_id: "chip_a", kind: "call", prompt: "prompt A" },
        { call_id: "chip_b", kind: "call", prompt: "prompt B" },
      ],
    });
    const body2 = findOne(reply, "flow-reply-prompt");
    assert.equal(body2.classList.contains("hidden"), true,
      "second chip starts collapsed even though first is expanded");
  });

  check("G1 collapsing an expanded prompt also persists the state", () => {
    const reply = document.getElementById("flow-reply-context");
    app.state.flowReplyPromptExpanded = {};
    const pendingCall = {
      call_id: "collapse_persist",
      kind: "call",
      prompt: "prompt text",
    };

    // Render + expand.
    app.state.flowInterjectRequested = false;
    app.renderInterventions({
      status: "running",
      pending_calls: [pendingCall],
    });
    const toggle1 = findOne(reply, "flow-reply-prompt-toggle");
    toggle1.dispatch("click"); // expand
    assert.equal(app.state.flowReplyPromptExpanded["call:collapse_persist"], true);

    // Collapse.
    const toggle2 = findOne(reply, "flow-reply-prompt-toggle");
    toggle2.dispatch("click"); // collapse
    assert.equal(app.state.flowReplyPromptExpanded["call:collapse_persist"], false,
      "collapse also writes back to state");

    // Re-render — should stay collapsed.
    app.renderInterventions({
      status: "running",
      pending_calls: [pendingCall],
    });
    const body = findOne(reply, "flow-reply-prompt");
    assert.equal(body.classList.contains("hidden"), true,
      "stays collapsed after re-render following user collapse");
  });

  check("G1 buildCollapsiblePrompt with opts.expanded=true starts expanded", () => {
    const wrap = app.buildCollapsiblePrompt("hello", { expanded: true });
    const body = findOne(wrap, "flow-reply-prompt");
    const toggle = findOne(wrap, "flow-reply-prompt-toggle");
    assert.equal(body.classList.contains("hidden"), false,
      "body is visible when opts.expanded=true");
    assert.equal(wrap.classList.contains("expanded"), true,
      "wrapper has expanded class");
    assert.equal(toggle.textContent, "▾ 收起消息详情",
      "trigger text shows collapse label");
  });

  check("G1 buildCollapsiblePrompt onToggle callback fires on user click", () => {
    let lastToggle = null;
    const wrap = app.buildCollapsiblePrompt("hello", {
      onToggle(v) { lastToggle = v; },
    });
    const toggle = findOne(wrap, "flow-reply-prompt-toggle");
    assert.equal(lastToggle, null, "callback not called yet");
    toggle.dispatch("click"); // expand
    assert.equal(lastToggle, true, "callback receives true on expand");
    toggle.dispatch("click"); // collapse
    assert.equal(lastToggle, false, "callback receives false on collapse");
  });

  check("G1 buildCollapsiblePrompt without opts keeps backward-compatible defaults", () => {
    const wrap = app.buildCollapsiblePrompt("hello");
    const body = findOne(wrap, "flow-reply-prompt");
    const toggle = findOne(wrap, "flow-reply-prompt-toggle");
    assert.equal(body.classList.contains("hidden"), true,
      "default: collapsed");
    assert.equal(wrap.classList.contains("expanded"), false,
      "default: no expanded class");
    assert.equal(toggle.textContent, "▸ 展开消息详情",
      "default: trigger shows expand label");
    // Click should still work without onToggle.
    toggle.dispatch("click");
    assert.equal(body.classList.contains("hidden"), false,
      "click still works without opts");
  });
}
