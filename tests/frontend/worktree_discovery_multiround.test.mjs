/*
 * Worktree multi-round discovery reconcile tests (Group G3).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerWorktreeDiscoveryMultiroundTests({app, check})`
 * so the parent harness drives the same check() reporter and `app` export.
 *
 * Regression context ("discovery 步骤第一轮之后的聊天记录消失" in worktree runs):
 *
 * A worktree flow's discovery history can be surfaced by the daemon from more
 * than one physical .jsonl file — the worktree's own primary file plus a
 * ``.from-<branch>`` merge-back sidecar (and, transiently, a cross-root copy).
 * Before G1 the daemon folded every physical file under ONE logical step_id and
 * each file's line ordinals restarted at 0, so a sidecar's ``stepId#ordinal``
 * collided with the primary's — the frontend keyed its idempotent reconcile on
 * ``stepId#ordinal`` and dropped (or in-place-overwrote) the 2nd+ round as a
 * "duplicate". G1 now emits a DISTINCT, stable step_id per physical file (the
 * sidecar keeps its ``.from-<branch>`` marker), so ``stepId#ordinal`` is again
 * globally unique. These tests pin that the frontend, which consumes that
 * disambiguated step_id verbatim through `recordKey`, renders EVERY round and
 * that `dedupeSnapshotDiscovery` de-dups only a byte-identical clone — never a
 * legitimately-different record that happens to reuse a physical ordinal.
 *
 * Record shapes mirror the REAL daemon envelope: the authoritative `step_type`
 * and the stable `ordinal` ride the record envelope (daemon-injected), and the
 * inner `message` carries only {role, content, timestamp}.
 */
import assert from "node:assert/strict";

export function registerWorktreeDiscoveryMultiroundTests(ctx) {
  const { app, check } = ctx;
  const {
    recordKey,
    recordOrdinal,
    reconcileAppendRecords,
    dedupeSnapshotDiscovery,
  } = app;

  // Daemon-shape discovery record: envelope step_id + ordinal, inner message.
  const disc = (stepId, ordinal, role, content, ts) => ({
    step_id: stepId,
    step_type: "discovery",
    ordinal,
    message: { role, content, timestamp: ts },
  });

  // Reset the running-flow view + DOM, mirroring the live-append harness.
  function freshFlow(flowId, initial = []) {
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = initial.slice();
    app.state.flowConversationProgress = null;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    if (initial.length) app.renderConversation(c, app.state.flowConversationRecords, false);
    return c;
  }
  const bubbleNodes = (c) => c.children.filter((x) => x.__convIdx !== undefined);

  // --------------------------------------------------------------------- //
  // Task 1: cross-source records that share a physical ordinal keep DISTINCT
  // recordKeys (the daemon disambiguates the step_id) — both render, neither
  // overwrites the other.
  // --------------------------------------------------------------------- //
  check("worktree discovery: primary + sidecar share ordinal 0 but keep distinct recordKeys", () => {
    const primary = disc("01_discovery_ab12", 0, "assistant", "primary body", 10);
    const sidecar = disc("01_discovery_ab12.from-worktree__b", 0, "assistant", "sidecar body", 11);
    // Same physical ordinal, different source ⇒ NON-colliding key.
    assert.notEqual(recordKey(primary), recordKey(sidecar));
    assert.equal(recordOrdinal(primary), 0);
    assert.equal(recordOrdinal(sidecar), 0);

    const { records } = reconcileAppendRecords([primary], [sidecar]);
    // Both survive — the sidecar is NOT treated as a duplicate of the primary.
    assert.equal(records.length, 2);
    assert.equal(records[0].message.content, "primary body");
    assert.equal(records[1].message.content, "sidecar body");
  });

  check("worktree discovery: 2nd+ round records append (not dropped) through the live reconcile", () => {
    // A single worktree discovery file grows across rounds: round 1 lands
    // ordinals 0..1, round 2 lands ordinals 2..3 (monotonic within one file).
    const r1 = [
      disc("01_discovery_ab12", 0, "user", "round1 question", 1),
      disc("01_discovery_ab12", 1, "assistant", "round1 answer", 2),
    ];
    const r2 = [
      disc("01_discovery_ab12", 2, "user", "round2 question", 3),
      disc("01_discovery_ab12", 3, "assistant", "round2 answer", 4),
    ];
    const c = freshFlow("flow-wt-discovery", r1);
    assert.equal(bubbleNodes(c).length, 2, "round 1 rendered");

    // The daemon pushes round 2 as a `mode: append` batch.
    const rec = reconcileAppendRecords(app.state.flowConversationRecords, r2);
    assert.equal(rec.fresh.length, 2, "both round-2 records are fresh, none dropped");
    app.state.flowConversationRecords = rec.records;
    app.renderConversation(c, app.state.flowConversationRecords, true);

    // Every round is present — the 2nd round did NOT disappear.
    assert.equal(app.state.flowConversationRecords.length, 4);
    assert.equal(bubbleNodes(c).length, 4, "both rounds visible after append");
    const keys = app.state.flowConversationRecords.map(recordKey);
    assert.equal(new Set(keys).size, 4, "every round keeps a distinct recordKey");
  });

  // --------------------------------------------------------------------- //
  // Task 2: dedupeSnapshotDiscovery compares CONTENT before dropping — a
  // shared stepId#ordinal alone is not proof of a clone.
  // --------------------------------------------------------------------- //
  check("dedupeSnapshotDiscovery: same stepId#ordinal but DIFFERENT content — both kept", () => {
    // A pathological ordinal reuse (which G1 aims to prevent, but the frontend
    // must not depend on): two discovery records collide on stepId#ordinal yet
    // carry different content. They are NOT clones — dropping either would
    // silently lose a legitimate round.
    const a = disc("01_discovery_ab12", 0, "assistant", "round1 body", 10);
    const b = disc("01_discovery_ab12", 0, "assistant", "round2 body — different", 20);
    assert.equal(recordKey(a), recordKey(b), "keys collide (shared stepId#ordinal)");

    const out = dedupeSnapshotDiscovery([a, b]);
    assert.equal(out.length, 2, "different content ⇒ neither dropped");
    assert.deepEqual(
      out.map((r) => r.message.content),
      ["round1 body", "round2 body — different"],
    );
  });

  check("dedupeSnapshotDiscovery: a byte-identical clone IS still de-duped", () => {
    // The genuine split-root clone case: same key AND same content ⇒ one bubble.
    const c1 = disc("01_discovery_ab12", 0, "assistant", "identical body", 10);
    const c2 = disc("01_discovery_ab12", 0, "assistant", "identical body", 10);
    assert.equal(recordKey(c1), recordKey(c2));

    const out = dedupeSnapshotDiscovery([c1, c2]);
    assert.equal(out.length, 1, "true clone collapses to one");
    assert.equal(out[0].message.content, "identical body");
  });

  check("dedupeSnapshotDiscovery: cross-source rounds (distinct step_ids) all survive", () => {
    // The realistic post-G1 merged snapshot: primary file rounds plus a sidecar
    // round, each with its own disambiguated step_id — none share a key, so the
    // guard is a pure pass-through and every round renders.
    const records = [
      disc("01_discovery_ab12", 0, "user", "q1", 1),
      disc("01_discovery_ab12", 1, "assistant", "a1", 2),
      disc("01_discovery_ab12", 2, "assistant", "a2 round2", 3),
      disc("01_discovery_ab12.from-worktree__b", 0, "assistant", "sidecar round", 4),
    ];
    const out = dedupeSnapshotDiscovery(records);
    assert.equal(out.length, 4, "no legitimate round is dropped");
    assert.equal(out, records, "no drop ⇒ same array reference (no-op)");
  });
}
