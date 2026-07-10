/*
 * G5 differential-protocol tests: HISTORY_INDEX delta merge, the silent
 * self-heal's not_modified / delta signature-check handling, and issue-detail
 * lazy-loading of the untruncated description.
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerHistoryIndexMergeTests({app, check, checkAsync})`.
 *
 * These pin the G5 traffic-reduction protocol from the frontend side:
 *   * a full `history_index` push is the baseline; a `history_index_delta`
 *     merges by flow_id (upsert / remove) onto it, keeping the aggregated index
 *     decoupled from the total flow count;
 *   * the periodic silent self-heal echoes the held token + signature and, on a
 *     `delivery:"not_modified"` reply, does nothing (no records adopt, no DOM
 *     rebuild) — the idle-poll win — while a `delivery:"delta"` still folds the
 *     tail in and a `delivery:"full"` still rebuilds from a real divergence
 *     (the #209 freeze defence);
 *   * an issue's truncated description preview is replaced in place by the full
 *     body pulled on demand, and a failed pull keeps the preview + shows a hint.
 */
import assert from "node:assert/strict";

export async function registerHistoryIndexMergeTests(ctx) {
  const { app, check, checkAsync } = ctx;

  const meta = (flowId, updatedAt, extra) => ({
    flow_id: flowId,
    project_root: "/repo",
    status: "running",
    updated_at: updatedAt,
    ...(extra || {}),
  });

  // -- applyHistoryIndexDelta: upsert / remove / sort ------------------------
  check("history_index_delta: upsert inserts a new flow, replaces an existing one", () => {
    app.state.historySessions = [meta("A", "2026-07-01T00:00:00"), meta("B", "2026-07-02T00:00:00")];
    app.state.historyIndexConfirmed = false;
    // Upsert a brand-new flow C and replace B's row (status change).
    app.applyHistoryIndexDelta(
      [meta("C", "2026-07-03T00:00:00"), meta("B", "2026-07-02T00:00:00", { status: "completed" })],
      [],
    );
    const byId = new Map(app.state.historySessions.map((s) => [s.flow_id, s]));
    assert.equal(byId.size, 3, "A, B, C are all present after the upsert");
    assert.ok(byId.has("C"), "the new flow C was inserted");
    assert.equal(byId.get("B").status, "completed", "B's row was replaced in place");
    assert.equal(app.state.historyIndexConfirmed, true, "a delta settles the confirmed state");
  });

  check("history_index_delta: removed ids drop their rows", () => {
    app.state.historySessions = [meta("A", "2026-07-01T00:00:00"), meta("B", "2026-07-02T00:00:00")];
    app.applyHistoryIndexDelta([], ["A"]);
    const ids = app.state.historySessions.map((s) => s.flow_id);
    assert.deepEqual(ids, ["B"], "the removed flow A is gone; B survives");
  });

  check("history_index_delta: merged index is re-sorted updated_at DESC (active flow rises)", () => {
    app.state.historySessions = [meta("A", "2026-07-01T00:00:00"), meta("B", "2026-07-02T00:00:00")];
    // A just became active — its updated_at now leads. The delta must reorder it
    // to the front, exactly as a fresh full index would.
    app.applyHistoryIndexDelta([meta("A", "2026-07-09T00:00:00")], []);
    const ids = app.state.historySessions.map((s) => s.flow_id);
    assert.deepEqual(ids, ["A", "B"], "the freshly-active flow sorts to the top");
  });

  check("history_index_delta: a delta before any baseline seeds a partial view", () => {
    app.state.historySessions = [];
    app.applyHistoryIndexDelta([meta("Z", "2026-07-05T00:00:00")], []);
    assert.deepEqual(app.state.historySessions.map((s) => s.flow_id), ["Z"],
      "the delta seeds a partial index the next full push will correct");
  });

  // -- silent self-heal: not_modified / delta / full three-state -------------
  const asstRecord = (content, ts, stepId, stepType) => ({
    step_id: stepId,
    step_type: stepType,
    message: { role: "assistant", content, timestamp: ts },
  });

  function seedOpenFlow(records, progress, signature) {
    app.state.selectedFlowId = "F1";
    app.state.flowConversationRecords = records;
    app.state.flowConversationProgress = progress;
    app.state.flowConversationSignature = signature;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = ""; c.__convState = null;
    app.renderConversation(c, records, false);
    return c;
  }

  function withFetch(payload, ok, status) {
    const saved = globalThis.fetch;
    const calls = [];
    globalThis.fetch = (url) => {
      calls.push(String(url));
      return Promise.resolve({
        ok: ok !== false, status: status || 200,
        json: () => Promise.resolve(payload),
      });
    };
    return { calls, restore: () => { globalThis.fetch = saved; } };
  }

  await checkAsync("silent self-heal: not_modified echoes token+sig and does NOT rebuild", async () => {
    const held = [asstRecord("A", 1, "s1", "discovery"), asstRecord("B", 2, "s1", "discovery")];
    const c = seedOpenFlow(held, "tok0", "sig0");
    const stBefore = c.__convState;
    const f = withFetch({ delivery: "not_modified", records: [], progress: "tok1", signature: "sig0" });
    try {
      await app.loadFlowConversation("F1", { silent: true });
      // The pull echoed BOTH the held token and signature.
      assert.ok(f.calls[0].includes("after=tok0"), "not_modified pull echoes the token: " + f.calls[0]);
      assert.ok(f.calls[0].includes("sig=sig0"), "not_modified pull echoes the signature: " + f.calls[0]);
      // Nothing repainted: same records ref, same __convState.
      assert.equal(app.state.flowConversationRecords, held, "not_modified keeps the same records array");
      assert.equal(c.__convState, stBefore, "not_modified does not rebuild __convState");
      // The refreshed token is still stored for the next poll; signature holds.
      assert.equal(app.state.flowConversationProgress, "tok1", "the fresh token is stored");
      assert.equal(app.state.flowConversationSignature, "sig0", "the signature is retained");
    } finally {
      f.restore();
    }
  });

  await checkAsync("silent self-heal: delta appends the tail without a full rebuild", async () => {
    const held = [asstRecord("A", 1, "s1", "discovery")];
    const c = seedOpenFlow(held, "tokD", "sigD");
    const f = withFetch({
      delivery: "delta",
      records: [asstRecord("B", 2, "s1", "discovery")],
      progress: "tokD2", signature: "sigD2",
    });
    try {
      await app.loadFlowConversation("F1", { silent: true });
      assert.ok(f.calls[0].includes("after=tokD"), "delta pull echoes the token");
      assert.equal(app.state.flowConversationRecords.length, 2, "the delta tail was folded in");
      const bodies = app.state.flowConversationRecords
        .map(app.normalizeRecord).map((n) => n.content);
      assert.deepEqual(bodies, ["A", "B"], "the appended record is B after A");
      assert.equal(app.state.flowConversationSignature, "sigD2", "the fresh signature is stored");
    } finally {
      f.restore();
    }
  });

  await checkAsync("silent self-heal: full rebuilds from a real divergence (#209 defence)", async () => {
    // The held view diverged (a dropped/rewritten increment); the server can no
    // longer serve a delta and answers full — the freeze defence still rebuilds.
    const held = [asstRecord("STALE", 1, "s1", "discovery")];
    seedOpenFlow(held, "tokF", "sigF");
    const f = withFetch({
      delivery: "full",
      records: [
        asstRecord("real0", 1, "s1", "discovery"),
        asstRecord("real1", 2, "s2", "analyze"),
      ],
      progress: "tokF2", signature: "sigF2",
    });
    try {
      await app.loadFlowConversation("F1", { silent: true });
      const bodies = app.state.flowConversationRecords
        .map(app.normalizeRecord).map((n) => n.content);
      assert.deepEqual(bodies, ["real0", "real1"],
        "a full delivery rebuilt the authoritative conversation from the divergence");
    } finally {
      f.restore();
    }
  });

  // -- issue-detail lazy-load ------------------------------------------------
  check("descriptionLikelyTruncated: only a >200-char preview counts as clipped", () => {
    assert.equal(app.descriptionLikelyTruncated("short"), false);
    assert.equal(app.descriptionLikelyTruncated("x".repeat(200)), false);
    assert.equal(app.descriptionLikelyTruncated("x".repeat(201)), true);
    assert.equal(app.descriptionLikelyTruncated(null), false);
  });

  const makeNodes = () => {
    const descNode = document.createElement("div");
    descNode.textContent = "PREVIEW…";
    const note = document.createElement("div");
    note.className = "issue-detail-desc-note hidden";
    return { descNode, note };
  };

  await checkAsync("issue detail lazy-load: full description replaces the preview on success", async () => {
    const iss = { id: "7", project_root: "/repo", machine_id: "m1" };
    app.state.selectedIssueId = ""; // set by helper below
    const key = "m1::/repo::7";
    app.state.selectedIssueId = key;
    const { descNode, note } = makeNodes();
    const f = withFetch({ issue: { description: "THE FULL BODY" } });
    try {
      await app.loadIssueFullDescription(iss, descNode, note);
      assert.ok(f.calls[0].includes("/api/issues/7/detail"), "hits the detail endpoint: " + f.calls[0]);
      assert.ok(f.calls[0].includes("machine_id=m1"), "passes the owning machine id");
      assert.equal(descNode.textContent, "THE FULL BODY", "the preview was replaced with the full body");
      assert.ok(note.className.includes("hidden"), "no error note is shown on success");
    } finally {
      f.restore();
    }
  });

  await checkAsync("issue detail lazy-load: a failed pull keeps the preview and shows the hint", async () => {
    const iss = { id: "7", project_root: "/repo", machine_id: "m1" };
    app.state.selectedIssueId = "m1::/repo::7";
    const { descNode, note } = makeNodes();
    const f = withFetch({}, false, 503);
    try {
      await app.loadIssueFullDescription(iss, descNode, note);
      assert.equal(descNode.textContent, "PREVIEW…", "the truncated preview survives a failed pull");
      assert.ok(!note.className.includes("hidden"), "the retry hint is revealed on failure");
    } finally {
      f.restore();
    }
  });

  await checkAsync("issue detail lazy-load: a late response for a deselected issue is dropped", async () => {
    const iss = { id: "7", project_root: "/repo", machine_id: "m1" };
    // The user selected a DIFFERENT issue while the pull was in flight.
    app.state.selectedIssueId = "m1::/repo::99";
    const { descNode, note } = makeNodes();
    const f = withFetch({ issue: { description: "STALE FULL BODY" } });
    try {
      await app.loadIssueFullDescription(iss, descNode, note);
      assert.equal(descNode.textContent, "PREVIEW…",
        "a stale full body must not overwrite a different issue's pane");
    } finally {
      f.restore();
    }
  });
}
