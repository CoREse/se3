/*
 * Worktree-mode merged-snapshot discovery de-dup tests (Group G3).
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
 * `dedupeAppendRecords`) must not render a doubled discovery bubble. The guard
 * is scoped strictly to discovery records — later steps and the recordKey
 * identity of the rest of the conversation are untouched.
 */
import assert from "node:assert/strict";

export function registerSnapshotDiscoveryDedupTests(ctx) {
  const { app } = ctx;
  const { dedupeSnapshotDiscovery, mergeHistoryResponse, recordKey } = app;

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
    "dedupeSnapshotDiscovery: duplicate discovery records collapse to one, order preserved",
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
      const out = dedupeSnapshotDiscovery(records);
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
    "dedupeSnapshotDiscovery: no duplicates returns the SAME array reference (no-op)",
    () => {
      const records = [disc("a", 1), analyze("b", 2), plan("c", 3)];
      assert.equal(dedupeSnapshotDiscovery(records), records);
    },
  );

  ctx.check(
    "dedupeSnapshotDiscovery: only discovery is de-duped — a coincidental analyze dup is kept",
    () => {
      // Two analyze records that happen to hash to the same recordKey must NOT
      // be dropped — the guard is intentionally discovery-only.
      const records = [
        disc("d", 1),
        analyze("same", 5),
        analyze("same", 5),
      ];
      const out = dedupeSnapshotDiscovery(records);
      assert.equal(out.length, 3);
      assert.deepEqual(
        out.map((r) => r.step_type),
        ["discovery", "analyze", "analyze"],
      );
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
    "mergeHistoryResponse delta path is unaffected by the discovery guard",
    () => {
      // The delta/append path is governed by dedupeAppendRecords, not the
      // snapshot discovery guard; a fresh discovery record on a clean held
      // array still appends.
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
