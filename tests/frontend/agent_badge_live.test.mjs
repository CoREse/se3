/*
 * Live accumulating-bubble agent/model badge tests (Group G3).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerAgentBadgeLiveTests({app, check, findOne, findAll})` so the parent
 * harness can drive the same check() reporter and the same `app` module export.
 *
 * Coverage (running-flow-console "Live Per-Turn Stream Accumulation" + the G3
 * task contract):
 *   (a) the FIRST streaming fragment that carries an agent_name immediately
 *       shows an agent badge at the top of the accumulating bubble;
 *   (b) a LATER fragment that carries model_name upgrades the SAME badge in
 *       place to "agent · model" — no new bubble, no second badge;
 *   (c) a fragment that carries no agent_name renders no badge and no
 *       placeholder (backward-compatible with legacy streams);
 *   (d) the whole turn renders as a single accumulating bubble through the
 *       integration path (renderConversation) — the upgrade does not spawn a
 *       second headed bubble.
 *
 * The chromium e2e same-source case (render_in_browser.mjs) is deselected in
 * this headless environment (missing libnspr4.so); this node-stub suite covers
 * the same assertions.
 */
import assert from "node:assert/strict";

export function registerAgentBadgeLiveTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  // A stream_progress (partial) fragment, mirroring what record_stream_progress
  // writes and the daemon forwards inside the `message` envelope. agent/model
  // are optional — omitting them models a legacy stream with no agent metadata.
  const frag = (agent, model, content, ts, attempt = 0) => {
    const message = {
      type: "stream_progress",
      partial: true,
      role: "assistant",
      content: content,
      timestamp: ts,
      attempt: attempt,
    };
    if (agent != null) message.agent_name = agent;
    if (model != null) message.model_name = model;
    return { step_id: "01_discovery_abcd1234", step_type: "discovery", message };
  };

  // ---- (a) first fragment with agent shows the badge ----------------------
  check("G3 buildPartialBubble shows agent badge from the first fragment", () => {
    const norm = app.normalizeRecord(frag("dclaude", null, "thinking…", 1));
    const row = app.buildPartialBubble(norm);
    const badge = findOne(row, "agent-badge");
    assert.ok(badge, "expected an .agent-badge on the first fragment");
    assert.equal(badge.textContent, "dclaude",
      "agent-only badge should read just the agent name");
  });

  check("G3 agent badge sits above the inline-process content", () => {
    const norm = app.normalizeRecord(frag("dclaude", null, "hi", 1));
    const row = app.buildPartialBubble(norm);
    const bubble = findOne(row, "conv-bubble");
    assert.ok(bubble, "expected a .conv-bubble");
    // The badge must be the bubble's first child, above the inline container,
    // consistent with the final assistant bubble form.
    const first = bubble.children[0];
    assert.ok(first && first.classList.contains("agent-badge"),
      "the agent badge should be the first child of the bubble");
  });

  // ---- (b) later fragment with model upgrades the badge in place ----------
  check("G3 later fragment with model upgrades badge to agent · model", () => {
    const row = app.buildPartialBubble(
      app.normalizeRecord(frag("dclaude", null, "a", 1)));
    // A later fragment now carries the parsed model name.
    app.appendPartialFragment(row, app.normalizeRecord(frag("dclaude", "claude-opus-4-8", "b", 2)));
    const badges = findAll(row, "agent-badge");
    assert.equal(badges.length, 1, "must remain a single badge after upgrade");
    assert.equal(badges[0].textContent, "dclaude · claude-opus-4-8",
      "badge should upgrade in place to agent · model");
  });

  check("G3 badge retains agent/model when a later fragment drops them", () => {
    // Once agent/model are known, a subsequent bare fragment (no metadata) must
    // not clear the badge — the bubble keeps the most-complete value seen.
    const row = app.buildPartialBubble(
      app.normalizeRecord(frag("dclaude", "claude-opus-4-8", "a", 1)));
    app.appendPartialFragment(row, app.normalizeRecord(frag(null, null, "b", 2)));
    const badges = findAll(row, "agent-badge");
    assert.equal(badges.length, 1);
    assert.equal(badges[0].textContent, "dclaude · claude-opus-4-8");
  });

  check("G3 agent change clears the previous agent's model", () => {
    // attempt A shows "A · model-A"; a rotation reuses this accumulating bubble
    // and B's fragment carries only agent_name. The stale model-A MUST be
    // dropped — the badge shows "B", never "B · model-A".
    const row = app.buildPartialBubble(
      app.normalizeRecord(frag("A", "model-A", "a", 1)));
    app.appendPartialFragment(row, app.normalizeRecord(frag("B", null, "b", 2)));
    const badges = findAll(row, "agent-badge");
    assert.equal(badges.length, 1, "must remain a single badge after rotation");
    assert.equal(badges[0].textContent, "B",
      "agent change must clear the stale model and show the new agent alone");
  });

  check("G3 agent change adopts the new agent's own model when present", () => {
    // If B's fragment carries both agent_name and model_name, the badge shows
    // B's own model — never the prior agent's stale model.
    const row = app.buildPartialBubble(
      app.normalizeRecord(frag("A", "model-A", "a", 1)));
    app.appendPartialFragment(row, app.normalizeRecord(frag("B", "model-B", "b", 2)));
    const badges = findAll(row, "agent-badge");
    assert.equal(badges.length, 1);
    assert.equal(badges[0].textContent, "B · model-B",
      "the rotated agent must show its own model, not the previous agent's");
  });

  // ---- (c) missing agent_name renders no badge / no placeholder -----------
  check("G3 fragment without agent_name renders no badge and no placeholder", () => {
    const row = app.buildPartialBubble(app.normalizeRecord(frag(null, null, "x", 1)));
    assert.equal(findAll(row, "agent-badge").length, 0,
      "legacy stream (no agent_name) must render no badge");
  });

  check("G3 model-only-but-no-agent still renders no badge", () => {
    // A model name without an agent name is not enough to show a badge
    // (formatAgentBadgeText returns null without an agent).
    const row = app.buildPartialBubble(app.normalizeRecord(frag(null, "claude-opus-4-8", "x", 1)));
    assert.equal(findAll(row, "agent-badge").length, 0);
  });

  // ---- (d) integration: single accumulating bubble through renderConversation
  check("G3 agent→model upgrade stays a single bubble (renderConversation)", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      frag("dclaude", null, "first chunk", 1),
      frag("dclaude", "claude-opus-4-8", "second chunk", 2),
    ], false);
    // Exactly one accumulating assistant bubble, with one upgraded badge.
    const bubbles = findAll(container, "conv-bubble");
    assert.equal(bubbles.length, 1, "the turn must render as a single bubble");
    const badges = findAll(container, "agent-badge");
    assert.equal(badges.length, 1, "single badge after the in-place upgrade");
    assert.equal(badges[0].textContent, "dclaude · claude-opus-4-8");
  });

  check("G3 legacy partial stream renders one bubble with no badge", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      frag(null, null, "first chunk", 1),
      frag(null, null, "second chunk", 2),
    ], false);
    assert.equal(findAll(container, "conv-bubble").length, 1);
    assert.equal(findAll(container, "agent-badge").length, 0,
      "no badge and no placeholder for a metadata-less legacy stream");
  });
}
