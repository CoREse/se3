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

  check("G4 successive group states tile in strict timestamp order", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      gsRecord("G3", "queued", 1),
      gsRecord("G3", "running", 2),
      gsRecord("G3", "completed", 3),
    ], false);
    const markers = findAll(container, "group-status-text").map((n) => n.textContent);
    assert.deepEqual(markers, [
      "G3 排队中",
      "G3 正在 worktree 实施中",
      "G3 已完成",
    ]);
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
    // Both markers present, and the assistant bubble survives between them.
    assert.equal(findAll(container, "group-status-marker").length, 2);
    const bubble = findOne(container, "conv-bubble");
    assert.ok(bubble, "interleaved assistant bubble should still render");
  });
}
