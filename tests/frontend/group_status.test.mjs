/*
 * Per-group DAG status marker tests (Group G4).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerGroupStatusTests({app, check, findOne, findAll})` so the parent
 * harness can drive the same check() reporter and the same `app` module
 * export.
 *
 * Coverage:
 *   (a) normalizeRecord recognizes `type:'group_status'`, normalizing
 *       group_id / status / stepType('implement') / timestamp.
 *   (b) groupStatusLabel maps every status (queued/running/completed/failed/
 *       skipped) to its label, with unknown-status + missing-id fallbacks.
 *   (c) the marker renders as a `.group-status-marker` with the right
 *       `.status-<status>` class, and successive states of the same group are
 *       tiled in strict timestamp order without disturbing surrounding chat
 *       bubbles (no fold / raw / chip affordances).
 */
import assert from "node:assert/strict";

export function registerGroupStatusTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  const gsRecord = (groupId, status, ts, stepId = "07_implement_abcd1234") => ({
    step_id: stepId,
    step_type: "implement",
    message: {
      type: "group_status",
      role: "system",
      group_id: groupId,
      status: status,
      step_type: "implement",
      timestamp: ts,
    },
  });

  // ---- (a) normalizeRecord -------------------------------------------------
  check("G4 normalizeRecord recognizes group_status and maps fields", () => {
    const norm = app.normalizeRecord(gsRecord("G3", "running", 42));
    assert.equal(norm.kind, "group_status");
    assert.equal(norm.role, "group-status");
    assert.equal(norm.groupId, "G3");
    assert.equal(norm.status, "running");
    assert.equal(norm.stepType, "implement");
    assert.equal(norm.timestamp, 42);
  });

  check("G4 normalizeRecord defaults stepType to implement when absent", () => {
    const norm = app.normalizeRecord({
      message: { type: "group_status", group_id: "G1", status: "queued" },
    });
    assert.equal(norm.stepType, "implement");
    assert.equal(norm.groupId, "G1");
  });

  check("G4 normalizeRecord coerces numeric group_id / status to strings", () => {
    const norm = app.normalizeRecord({
      message: { type: "group_status", group_id: 2, status: "completed" },
    });
    assert.equal(norm.groupId, "2");
    assert.equal(typeof norm.groupId, "string");
  });

  // ---- (b) groupStatusLabel mapping ---------------------------------------
  check("G4 groupStatusLabel covers every status", () => {
    assert.equal(app.groupStatusLabel("G1", "queued"), "G1 排队中");
    assert.equal(app.groupStatusLabel("G2", "running"), "G2 正在 worktree 实施中");
    assert.equal(app.groupStatusLabel("G3", "completed"), "G3 已完成");
    assert.equal(app.groupStatusLabel("G4", "failed"), "G4 失败");
    assert.equal(app.groupStatusLabel("G5", "skipped"), "G5 已跳过");
  });

  check("G4 groupStatusLabel is case-insensitive on status", () => {
    assert.equal(app.groupStatusLabel("G1", "RUNNING"), "G1 正在 worktree 实施中");
  });

  check("G4 groupStatusLabel falls back on unknown status / missing id", () => {
    // Unknown status keeps its raw token rather than dropping it.
    assert.equal(app.groupStatusLabel("G1", "merging"), "G1 merging");
    // Missing group id degrades to "?" rather than a dangling label.
    assert.equal(app.groupStatusLabel("", "running"), "? 正在 worktree 实施中");
    assert.equal(app.groupStatusLabel(null, null), "?");
  });

  // ---- (c) render path -----------------------------------------------------
  check("G4 group_status renders a .group-status-marker with status class", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [gsRecord("G3", "running", 1)], false);
    const marker = findOne(container, "group-status-marker");
    assert.ok(marker, "expected a .group-status-marker row");
    assert.ok(marker.classList.contains("status-running"),
      "marker should carry the .status-running class");
    const text = findOne(marker, "group-status-text");
    assert.ok(text && text.textContent === "G3 正在 worktree 实施中",
      `marker text should be the running label, got ${text && text.textContent}`);
    // No fold / raw / chip affordances on the marker.
    assert.equal(findAll(marker, "msg-chip").length, 0,
      "marker must not contain a chip");
  });

  check("G4 successive group states converge to one in-place card (terminal)", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      gsRecord("G3", "queued", 1),
      gsRecord("G3", "running", 2),
      gsRecord("G3", "completed", 3),
    ], false);
    // The (step_id, group_id) composite key folds the three records to ONE
    // card — the terminal/latest state — instead of stacking three.
    const markers = findAll(container, "group-status-text").map((n) => n.textContent);
    assert.deepEqual(markers, ["G3 已完成"]);
    assert.equal(findAll(container, "group-status-marker").length, 1);
  });

  check("G4 group_status markers do not disturb an interleaved assistant bubble", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      gsRecord("G1", "running", 1),
      {
        step_id: "07_implement_abcd1234",
        step_type: "implement",
        message: { role: "assistant", content: "working on it", timestamp: 2 },
      },
      gsRecord("G1", "completed", 3),
    ], false);
    // The two same-group markers converge to one card, and the assistant
    // bubble in between survives the convergence.
    assert.equal(findAll(container, "group-status-marker").length, 1);
    const bubble = findOne(container, "conv-bubble");
    assert.ok(bubble, "interleaved assistant bubble should still render");
    assert.ok(bubble.textContent.includes("working on it"),
      "interleaved assistant content must survive the group-status convergence");
  });

  // ---- (d) agent / model badge --------------------------------------------
  // record_group_status (G2 production side) attaches optional agent_name and,
  // once parsed from the worktree LLM's NDJSON init/system metadata, model_name.
  const gsRecordAM = (groupId, status, ts, agent, model) => {
    const r = gsRecord(groupId, status, ts);
    if (agent != null) r.message.agent_name = agent;
    if (model != null) r.message.model_name = model;
    return r;
  };

  check("G4 normalizeRecord extracts agent_name / model_name on group_status", () => {
    const norm = app.normalizeRecord(gsRecordAM("G3", "running", 1, "dclaude", "claude-opus-4-8"));
    assert.equal(norm.agentName, "dclaude");
    assert.equal(norm.modelName, "claude-opus-4-8");
  });

  check("G4 normalizeRecord leaves agent/model null on legacy group_status", () => {
    const norm = app.normalizeRecord(gsRecord("G3", "running", 1));
    assert.equal(norm.agentName, null);
    assert.equal(norm.modelName, null);
  });

  check("G4 running marker shows the agent-only badge", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [gsRecordAM("G3", "running", 1, "dclaude", null)], false);
    const marker = findOne(container, "group-status-marker");
    const badge = findOne(marker, "agent-badge");
    assert.ok(badge, "expected an agent-badge on the marker");
    assert.equal(badge.textContent, "dclaude");
    // The marker stays affordance-free even with a badge.
    assert.equal(findAll(marker, "msg-chip").length, 0);
  });

  check("G4 running marker shows agent · model once the model is known", () => {
    const container = document.createElement("div");
    app.renderConversation(
      container, [gsRecordAM("G3", "running", 1, "dclaude", "claude-opus-4-8")], false);
    const badge = findOne(findOne(container, "group-status-marker"), "agent-badge");
    assert.ok(badge, "expected an agent-badge on the marker");
    assert.equal(badge.textContent, "dclaude · claude-opus-4-8");
  });

  check("G4 successive records upgrade the group in place from agent → agent · model", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      gsRecordAM("G3", "running", 1, "dclaude", null),
      gsRecordAM("G3", "running", 2, "dclaude", "claude-opus-4-8"),
      gsRecordAM("G3", "completed", 3, "dclaude", "claude-opus-4-8"),
    ], false);
    // Only ONE card survives the convergence, and because the latest record
    // carries the accumulated agent/model, that card shows agent · model — the
    // in-place badge upgrade with no stacked duplicates.
    const badges = findAll(container, "agent-badge").map((b) => b.textContent);
    assert.deepEqual(badges, ["dclaude · claude-opus-4-8"]);
    const labels = findAll(container, "group-status-text").map((n) => n.textContent);
    assert.deepEqual(labels, ["G3 已完成"]);
  });

  check("G4 retry / rotation reports the latest attempt's agent, never a stale one", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      gsRecordAM("G3", "running", 1, "dclaude", null),
      gsRecordAM("G3", "running", 2, "claude", "claude-opus-4-8"),
    ], false);
    // The two same-group running records converge to one card showing the
    // latest (rotated) agent — never the stale earlier one.
    const badges = findAll(container, "agent-badge").map((b) => b.textContent);
    assert.deepEqual(badges, ["claude · claude-opus-4-8"]);
  });

  check("G4 legacy group_status without agent renders no badge / placeholder", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [gsRecord("G3", "running", 1)], false);
    const marker = findOne(container, "group-status-marker");
    assert.equal(findAll(marker, "agent-badge").length, 0,
      "no badge should render when agent_name is absent");
    // The status text itself is unaffected — no dangling separator / placeholder.
    const text = findOne(marker, "group-status-text");
    assert.equal(text.textContent, "G3 正在 worktree 实施中");
  });

  check("G4 final content salvage is unaffected by status markers carrying agent/model", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      gsRecordAM("G1", "running", 1, "dclaude", "claude-opus-4-8"),
      {
        step_id: "07_implement_abcd1234",
        step_type: "implement",
        message: { role: "assistant", content: "salvaged group content", timestamp: 2 },
      },
      gsRecordAM("G1", "completed", 3, "dclaude", "claude-opus-4-8"),
    ], false);
    // The two same-group markers converge to one card; the salvaged assistant
    // content in between is untouched.
    assert.equal(findAll(container, "group-status-marker").length, 1);
    const bubble = findOne(container, "conv-bubble");
    assert.ok(bubble && bubble.textContent.includes("salvaged group content"),
      "salvaged assistant content should still render alongside the marker");
  });

  // ---- (e) (step_id, group_id) composite convergence ----------------------
  // The core of the regression fix: one card per (step_id, group_id), updated
  // in place; distinct groups stay independent; terminal supersedes running;
  // and incremental append keeps it to one card.

  check("G4 three running records of one (step_id, group_id) collapse to one card", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      gsRecordAM("G3", "running", 1, null, null),
      gsRecordAM("G3", "running", 2, "dclaude", null),
      gsRecordAM("G3", "running", 3, "dclaude", "claude-opus-4-8"),
    ], false);
    assert.equal(findAll(container, "group-status-marker").length, 1,
      "the three running records must converge to a single card");
    const badge = findOne(findOne(container, "group-status-marker"), "agent-badge");
    assert.ok(badge && badge.textContent === "dclaude · claude-opus-4-8",
      "the surviving card carries the latest agent · model");
  });

  check("G4 different group_ids keep independent cards, never folded together", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      gsRecord("G1", "running", 1),
      gsRecord("G2", "running", 2),
      gsRecord("G3", "running", 3),
    ], false);
    const labels = findAll(container, "group-status-text").map((n) => n.textContent);
    assert.deepEqual(labels, [
      "G1 正在 worktree 实施中",
      "G2 正在 worktree 实施中",
      "G3 正在 worktree 实施中",
    ]);
    assert.equal(findAll(container, "group-status-marker").length, 3,
      "G1/G2/G3 share a step_id but must each keep their own card");
  });

  check("G4 same group_id under different step_ids does not fold across steps", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      gsRecord("G1", "completed", 1, "07_implement_aaaa1111"),
      gsRecord("G1", "completed", 2, "11_implement_bbbb2222"),
    ], false);
    // Same group_id but distinct step_ids → distinct composite keys → two cards.
    assert.equal(findAll(container, "group-status-marker").length, 2,
      "a reused group_id under a different step_id must not collapse the cards");
  });

  check("G4 completed terminal card supersedes the group's earlier running card", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      gsRecord("G2", "running", 1),
      gsRecord("G2", "completed", 2),
    ], false);
    const markers = findAll(container, "group-status-text").map((n) => n.textContent);
    assert.deepEqual(markers, ["G2 已完成"]);
    const marker = findOne(container, "group-status-marker");
    assert.ok(marker.classList.contains("status-completed"),
      "the surviving card must be the terminal completed one");
  });

  check("G4 incremental append of further same-group records stays one card", () => {
    const container = document.createElement("div");
    // First batch: group starts running (no model yet).
    app.renderConversation(container, [
      gsRecordAM("G3", "running", 1, "dclaude", null),
    ], false);
    assert.equal(findAll(container, "group-status-marker").length, 1);
    // Second batch arrives via incremental append (append=true, cumulative
    // records) carrying the model then the terminal state.
    app.renderConversation(container, [
      gsRecordAM("G3", "running", 1, "dclaude", null),
      gsRecordAM("G3", "running", 2, "dclaude", "claude-opus-4-8"),
      gsRecordAM("G3", "completed", 3, "dclaude", "claude-opus-4-8"),
    ], true);
    assert.equal(findAll(container, "group-status-marker").length, 1,
      "after the live append the group still shows exactly one card");
    const labels = findAll(container, "group-status-text").map((n) => n.textContent);
    assert.deepEqual(labels, ["G3 已完成"]);
    const badge = findOne(findOne(container, "group-status-marker"), "agent-badge");
    assert.equal(badge.textContent, "dclaude · claude-opus-4-8");
  });
}
