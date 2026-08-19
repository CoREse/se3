/*
 * Merged-snapshot clone de-dup tests (originally the G3 worktree discovery
 * guard, since generalized to every step).
 *
 * Loaded by tests/frontend/test_app_pure.mjs. Exposes
 * `registerSnapshotDiscoveryDedupTests({app, check})` so the parent harness
 * drives the same check() reporter and the same `app` module export.
 *
 * Context: the worktree-mode read path can surface a flow whose history is
 * split across the main-repo root (where discovery ran before the worktree
 * fork) and the worktree root (the later steps plus its OWN copy of discovery).
 * The daemon merges the two roots and de-dups at the physical step-file layer,
 * but the frontend keeps a belt-and-suspenders guard: a `mode: full` snapshot
 * (which `mergeHistoryResponse` adopts wholesale, NOT covered by
 * `dedupeAppendRecords`) must not render a doubled bubble.
 *
 * The guard used to be scoped strictly to discovery. It no longer is: a live
 * worktree flow's plan/confirm records rendered FOUR times each because the
 * server's history cache accumulated clones from re-delivered appends and
 * served them in every full snapshot. That is fixed at the source (the
 * `(step_id, ordinal)` append reconcile in server/state.py), and this backstop
 * now covers a full snapshot from ANY step and ANY source. Its collapse rule is
 * unchanged and deliberately narrow — same `recordKey` AND byte-identical
 * content — so a same-key/different-content record is still never dropped.
 */
import assert from "node:assert/strict";

export function registerSnapshotDiscoveryDedupTests(ctx) {
  const { app } = ctx;
  const { dedupeSnapshotClones, mergeHistoryResponse, recordKey } = app;

  // Daemon-shape record builders (authoritative step_type on the envelope, the
  // inner `message` carrying only {role, content, timestamp}).
  const disc = (content, ts) => ({
    step_id: "01_discovery_ab12",
    step_type: "discovery",
    message: { role: "assistant", content, timestamp: ts },
  });
  const discUser = (content, ts) => ({
    step_id: "01_discovery_ab12",
    step_type: "discovery",
    message: { role: "user", content, timestamp: ts },
  });
  const analyze = (content, ts) => ({
    step_id: "02_analyze_cd34",
    step_type: "analyze",
    message: { role: "assistant", content, timestamp: ts },
  });
  const plan = (content, ts) => ({
    step_id: "03_plan_ef56",
    step_type: "plan",
    message: { role: "assistant", content, timestamp: ts },
  });

  ctx.check(
    "dedupeSnapshotClones: duplicate discovery records collapse to one, order preserved",
    () => {
      const records = [
        discUser("请帮我修复 bug", 1),
        disc("discovery body", 2),
        // worktree root's own copy of the SAME discovery records (same recordKey)
        discUser("请帮我修复 bug", 1),
        disc("discovery body", 2),
        analyze("analyze body", 3),
        plan("plan body", 4),
      ];
      const out = dedupeSnapshotClones(records);
      // discovery appears exactly once each; analyze/plan untouched.
      assert.equal(out.length, 4);
      const keys = out.map(recordKey);
      assert.equal(new Set(keys).size, keys.length, "no duplicate recordKey");
      // Order is preserved: discovery user, discovery asst, analyze, plan.
      assert.deepEqual(
        out.map((r) => r.step_type),
        ["discovery", "discovery", "analyze", "plan"],
      );
    },
  );

  ctx.check(
    "dedupeSnapshotClones: no duplicates returns the SAME array reference (no-op)",
    () => {
      const records = [disc("a", 1), analyze("b", 2), plan("c", 3)];
      assert.equal(dedupeSnapshotClones(records), records);
    },
  );

  // Numbered (daemon-stamped `ordinal`) builders: only these can produce two
  // records that SHARE a recordKey while carrying DIFFERENT content, which is
  // the case the guard must never collapse.
  const numbered = (stepId, stepType, ordinal, content, ts) => ({
    step_id: stepId,
    step_type: stepType,
    ordinal,
    message: { role: "assistant", content, timestamp: ts },
  });

  ctx.check(
    "dedupeSnapshotClones: byte-identical clones of a NON-discovery step collapse too",
    () => {
      // The live-worktree defect shape: the server's cached bundle had absorbed
      // repeated overlapping append drains, so its full snapshot carried each
      // plan / confirm record four times over. The guard is no longer scoped to
      // discovery, so every step's true clone collapses to one bubble.
      const records = [
        disc("d", 1),
        numbered("05_plan_aa11", "plan", 0, "plan step_completed", 5),
        numbered("05_plan_aa11", "plan", 0, "plan step_completed", 5),
        numbered("05_plan_aa11", "plan", 0, "plan step_completed", 5),
        numbered("05_plan_aa11", "plan", 0, "plan step_completed", 5),
        numbered("06_confirm_bb22", "confirm", 0, "confirm?", 6),
        numbered("06_confirm_bb22", "confirm", 0, "confirm?", 6),
      ];
      const out = dedupeSnapshotClones(records);
      assert.deepEqual(
        out.map((r) => r.step_type),
        ["discovery", "plan", "confirm"],
      );
      const keys = out.map(recordKey);
      assert.equal(new Set(keys).size, keys.length, "no duplicate recordKey");
    },
  );

  ctx.check(
    "dedupeSnapshotClones: un-numbered clones of a later step collapse as well",
    () => {
      // Legacy (pre-ordinal) records key off their content signature, so two
      // byte-identical analyze records ARE indistinguishable clones — the old
      // discovery-only scope let them render twice.
      const records = [disc("d", 1), analyze("same", 5), analyze("same", 5)];
      const out = dedupeSnapshotClones(records);
      assert.deepEqual(
        out.map((r) => r.step_type),
        ["discovery", "analyze"],
      );
    },
  );

  ctx.check(
    "dedupeSnapshotClones: same recordKey, DIFFERENT content — both kept, any step",
    () => {
      // A retried step rewrote its physical jsonl line, so one number carries
      // two contents. They are not clones; dropping either would lose a real
      // record. (The discovery counterpart of this is pinned in
      // worktree_discovery_multiround.test.mjs.)
      const a = numbered("05_plan_aa11", "plan", 0, "attempt 1 body", 5);
      const b = numbered("05_plan_aa11", "plan", 0, "attempt 2 body", 7);
      assert.equal(recordKey(a), recordKey(b), "keys collide (same stepId#ordinal)");

      const out = dedupeSnapshotClones([a, b]);
      assert.equal(out.length, 2, "different content ⇒ neither dropped");
      assert.deepEqual(
        out.map((r) => r.message.content),
        ["attempt 1 body", "attempt 2 body"],
      );
    },
  );

  ctx.check(
    "dedupeSnapshotClones: numbered records with no clones return the SAME array",
    () => {
      const records = [
        numbered("05_plan_aa11", "plan", 0, "a", 1),
        numbered("05_plan_aa11", "plan", 1, "b", 2),
        numbered("06_implement_cc33", "implement", 0, "c", 3),
      ];
      assert.equal(dedupeSnapshotClones(records), records);
    },
  );

  ctx.check(
    "mergeHistoryResponse full path: a clone-carrying snapshot renders each record once",
    () => {
      // End-to-end through the real full-adoption path: a server bundle that
      // had absorbed four overlapping drains delivers each plan/confirm record
      // four times; the view must hold exactly one of each.
      const dup = (rec, times) => Array.from({ length: times }, () => rec);
      const response = {
        delivery: "full",
        progress: "tok-3",
        records: [
          discUser("请帮我修复 bug", 1),
          ...dup(numbered("05_plan_aa11", "plan", 0, "plan done", 5), 4),
          ...dup(numbered("06_confirm_bb22", "confirm", 0, "ok?", 6), 4),
          ...dup(numbered("06_confirm_bb22", "confirm", 1, "yes", 7), 4),
        ],
      };
      const result = mergeHistoryResponse(response, [], []);
      assert.equal(result.render, "full");
      assert.equal(result.records.length, 4);
      const keys = result.records.map(recordKey);
      assert.equal(new Set(keys).size, keys.length, "no duplicate recordKey");
    },
  );

  ctx.check(
    "mergeHistoryResponse full path: split-root snapshot renders discovery once",
    () => {
      // A `mode: full` (delivery omitted ⇒ full) response carrying the split
      // main+worktree merge with a duplicate discovery pair.
      const response = {
        delivery: "full",
        progress: "tok-1",
        records: [
          discUser("请帮我修复 bug", 1),
          disc("discovery body", 2),
          discUser("请帮我修复 bug", 1),
          disc("discovery body", 2),
          analyze("analyze body", 3),
        ],
      };
      const result = mergeHistoryResponse(response, [], []);
      assert.equal(result.render, "full");
      const keys = result.records.map(recordKey);
      assert.equal(new Set(keys).size, keys.length, "no duplicate recordKey");
      const discCount = result.records.filter(
        (r) => r.step_type === "discovery",
      ).length;
      assert.equal(discCount, 2, "one discovery user + one discovery assistant");
      // The subsequent step still renders.
      assert.ok(
        result.records.some((r) => r.step_type === "analyze"),
        "analyze step preserved",
      );
    },
  );

  ctx.check(
    "mergeHistoryResponse delta path is unaffected by the snapshot clone guard",
    () => {
      // The delta/append path is governed by dedupeAppendRecords, not the
      // snapshot clone guard; a fresh record on a clean held array still
      // appends.
      const response = {
        delivery: "delta",
        progress: "tok-2",
        records: [analyze("new analyze tail", 9)],
      };
      const result = mergeHistoryResponse(response, [disc("d", 1)], [disc("d", 1)]);
      assert.equal(result.render, "delta");
      assert.equal(result.records.length, 2);
    },
  );
}
