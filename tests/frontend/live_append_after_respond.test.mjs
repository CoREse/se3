/*
 * Live-append-after-respond tests (Group G3, symptom A/B alignment).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerLiveAppendAfterRespondTests({app, check, checkAsync, findOne, findAll})`
 * so the parent harness drives the same check() reporter and the same `app`
 * module export.
 *
 * Regression context (issue #193 leftover half — "消息不显示"):
 *
 * Symptom A — after a `respond`/`interject` the daemon-pushed `mode: append`
 * increments were being suppressed server-side (fixed in G1's ws.py change), so
 * the running-flow live view stopped appending until the user re-entered the
 * view. With the server now broadcasting append frames again, the frontend must
 * keep appending them through `applyHistoryData`'s append branch, deduping the
 * batch that overlaps with any REST snapshot the same client pulled
 * concurrently (`dedupeAppendRecords`) and reconciling the optimistic reply echo
 * with the daemon's authoritative `user` record (`reconcileLocalEchoes`) so the
 * reply is shown exactly once and every subsequent record keeps streaming in.
 *
 * Symptom B — a worktree/discovery session's first assistant reply rendered with
 * an empty body. The read-side fix lives in G2 (daemon/history.py); these tests
 * only verify the frontend `normalizeRecord` does not special-case-drop the body
 * of a first worktree assistant record (recovering it from `raw_json` when the
 * top-level `content` is absent, exactly like an ordinary session).
 *
 * All record shapes mirror the REAL daemon envelope: the authoritative
 * `step_type` rides the record envelope (daemon-injected from the jsonl
 * file-name convention), and `message` carries only `{role, content, timestamp}`.
 */
import assert from "node:assert/strict";

export function registerLiveAppendAfterRespondTests(ctx) {
  const { app, check } = ctx;

  // Daemon-shape record builders (envelope step_type, inner message only).
  const asst = (content, ts, stepId, stepType) => ({
    step_id: stepId,
    step_type: stepType,
    message: { role: "assistant", content, timestamp: ts },
  });
  const usr = (content, ts, stepId, stepType) => ({
    step_id: stepId,
    step_type: stepType,
    message: { role: "user", content, timestamp: ts },
  });
  // A partial / stream_progress fragment (assistant, accumulating content).
  const partial = (content, ts, stepId, stepType) => ({
    step_id: stepId,
    step_type: stepType,
    message: { role: "assistant", content, timestamp: ts, partial: true },
  });

  const keys = (recs) => recs.map(app.recordKey);
  const allUnique = (recs) => new Set(keys(recs)).size === recs.length;

  // Reset the running-flow view state + DOM for a flow under test.
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
  function bubbleNodes(c) {
    return c.children.filter((x) => x.__convIdx !== undefined);
  }

  // ----------------------------------------------------------------------- //
  // Symptom A: respond → daemon append keeps streaming, echo shown once.     //
  // ----------------------------------------------------------------------- //

  check("G3 respond: optimistic echo reconciled away by the daemon append, reply shown once", () => {
    const flowId = "flow-respond-A1";
    freshFlow(flowId, [asst("question?", 1, "s1", "discovery")]);

    // User confirms the pending call — appendLocalReply splices a tagged echo.
    app.appendLocalReply(flowId, { kind: "call", callId: "c1" }, "1");
    assert.equal(app.state.flowConversationRecords.length, 2,
      "echo appended optimistically");

    // The daemon now pushes (mode:append) the authoritative user record for the
    // same reply PLUS the next agent assistant output (which previously never
    // arrived because the server suppressed the append broadcast).
    app.applyHistoryData({
      flow_id: flowId,
      mode: "append",
      records: [
        usr("1", 2, "s1", "discovery"),
        asst("continuing after confirmation", 3, "s1", "discovery"),
      ],
    });

    const recs = app.state.flowConversationRecords;
    // echo removed, authoritative user kept, plus the new assistant output.
    const userBodies = recs
      .filter((r) => !r.__localEcho)
      .map(app.normalizeRecord)
      .filter((n) => n.role === "user")
      .map((n) => n.content);
    assert.deepEqual(userBodies, ["1"], "exactly one authoritative user reply");
    assert.equal(recs.filter((r) => r.__localEcho).length, 0,
      "optimistic echo reconciled away");
    const asstBodies = recs.map(app.normalizeRecord)
      .filter((n) => n.role === "assistant").map((n) => n.content);
    assert.deepEqual(asstBodies, ["question?", "continuing after confirmation"],
      "the post-respond agent output kept streaming in");
    assert.ok(allUnique(recs), "no duplicate recordKey after reconcile");
  });

  check("G3 respond: consecutive post-respond appends keep accumulating (no re-entry needed)", () => {
    const flowId = "flow-respond-A2";
    const c = freshFlow(flowId, [asst("q", 1, "s1", "discovery")]);

    app.appendLocalReply(flowId, { kind: "call", callId: "c1" }, "1");
    // First append: authoritative user reply.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [usr("1", 2, "s1", "discovery")],
    });
    // Several subsequent independent appends (agent output, then the user's next
    // free-form message, then more agent output) — each must keep appending.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [asst("step 2 output", 3, "s2", "analyze")],
    });
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [usr("please also check X", 4, "s2", "analyze")],
    });
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [asst("checked X", 5, "s2", "analyze")],
    });

    const recs = app.state.flowConversationRecords;
    const bodies = recs.map(app.normalizeRecord).map((n) => n.content);
    assert.deepEqual(bodies,
      ["q", "1", "step 2 output", "please also check X", "checked X"],
      "every post-respond record appended in order, none dropped, none duplicated");
    assert.ok(allUnique(recs));
    // The DOM grew to one bubble per record (no freeze, no re-entry needed).
    assert.equal(bubbleNodes(c).length, 5,
      "live DOM kept appending without a view re-entry");
  });

  check("G3 respond: REST pull + WS broadcast overlap of the same batch renders once", () => {
    const flowId = "flow-respond-A3";
    freshFlow(flowId, [asst("q", 1, "s1", "discovery")]);

    app.appendLocalReply(flowId, { kind: "call", callId: "c1" }, "1");

    // The client that issued respond also pulled a REST snapshot which already
    // contains the authoritative reply + the next output (it landed after the
    // server cache write but before the WS broadcast). Model that by feeding the
    // overlapping records into state via one append first...
    const authUser = usr("1", 2, "s1", "discovery");
    const nextOut = asst("after confirm", 3, "s1", "discovery");
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: [authUser, nextOut] });
    const lenAfterFirst = app.state.flowConversationRecords.length;

    // ...then the WS broadcast for the SAME batch arrives. dedupeAppendRecords
    // must filter it out entirely so nothing renders twice.
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: [authUser, nextOut] });
    assert.equal(app.state.flowConversationRecords.length, lenAfterFirst,
      "the overlapping WS rebroadcast must not append duplicates");
    assert.ok(allUnique(app.state.flowConversationRecords),
      "no duplicate recordKey from the REST/WS overlap");
  });

  check("G3 respond: accumulating partial fragments after respond are NOT deduped", () => {
    const flowId = "flow-respond-A4";
    freshFlow(flowId, []);

    app.appendLocalReply(flowId, { kind: "call", callId: "c1" }, "1");
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [usr("1", 2, "s1", "discovery")],
    });

    // The next turn streams partial fragments whose content grows each push —
    // same stepId/role/ts but different content, so recordKey differs and they
    // must each survive dedupe (otherwise the live stream would stall).
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [partial("🔧 Read foo", 3, "s2", "analyze")],
    });
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [partial("🔧 Read foo\n✅ Read ✓", 3, "s2", "analyze")],
    });
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [partial("🔧 Read foo\n✅ Read ✓\nthinking…", 3, "s2", "analyze")],
    });

    const partials = app.state.flowConversationRecords
      .filter((r) => app.normalizeRecord(r).partial);
    assert.equal(partials.length, 3,
      "each accumulating partial fragment retained (not falsely deduped)");
    assert.ok(allUnique(app.state.flowConversationRecords));
  });

  // ----------------------------------------------------------------------- //
  // Regression: respond → a fresh reply whose recordKey collides with a       //
  // FAR-BACK old record must NOT be deduped away (the persistent live stall).  //
  // ----------------------------------------------------------------------- //

  check("G1 respond: fresh reply colliding on recordKey with a far-back record still appends (no stall)", () => {
    const flowId = "flow-collision-G1";
    // Seed a long conversation whose EARLY part holds an old "1" reply, then pad
    // with enough later records to push it well out of the recent tail window.
    const initial = [usr("1", 100, "s1", "discovery")];
    for (let i = 0; i < 80; i++) initial.push(asst("filler " + i, 200 + i, "s2", "analyze"));
    const c = freshFlow(flowId, initial);
    const base = app.state.flowConversationRecords.length;
    const domBase = bubbleNodes(c).length;

    // A discovery continuation reuses step_id s1; the operator presses "1" again
    // and the daemon's authoritative record lands at the SAME step/second, so its
    // recordKey collides with the far-back old "1". Pre-fix the whole-array dedupe
    // filtered it (fresh empty → applyHistoryData short-circuits) and the record
    // never reached state or DOM — the user's "nothing shows after respond" bug.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [usr("1", 100, "s1", "discovery")],
    });
    assert.equal(app.state.flowConversationRecords.length, base + 1,
      "the colliding fresh reply must still append (regression: it was suppressed)");
    assert.equal(bubbleNodes(c).length, domBase + 1,
      "the colliding fresh reply must reach the DOM, not stall");

    // And the agent's subsequent output keeps streaming, no re-entry needed.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [asst("output after the reply", 300, "s1", "discovery")],
    });
    assert.equal(app.state.flowConversationRecords.length, base + 2,
      "post-reply agent output keeps streaming live");
    assert.equal(bubbleNodes(c).length, domBase + 2,
      "post-reply agent output reaches the DOM");
  });

  check("G3 respond: post-respond agent output + a repeated user reply keep live-appending (collision-safe)", () => {
    const flowId = "flow-respond-A5";
    // Seed an earlier "1" reply far back so the later repeated "1" reply's
    // recordKey could collide with it — the bounded window must still let it land.
    const initial = [usr("1", 100, "s1", "discovery")];
    for (let i = 0; i < 80; i++) initial.push(asst("filler " + i, 200 + i, "s2", "analyze"));
    const c = freshFlow(flowId, initial);
    const base = app.state.flowConversationRecords.length;

    // respond: optimistic echo for a NEW "1" reply (rank 1 — one prior auth "1").
    app.appendLocalReply(flowId, { kind: "call", callId: "c1" }, "1");
    // The daemon's authoritative record for that reply collides with the old "1".
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [usr("1", 100, "s1", "discovery")],
    });
    // The agent keeps producing; then the user sends a free-form follow-up.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [asst("agent continues", 300, "s1", "discovery")],
    });
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [usr("please continue", 301, "s1", "discovery")],
    });

    const recs = app.state.flowConversationRecords;
    // The reply's own optimistic echo is reconciled away by its authoritative copy.
    assert.equal(recs.filter((r) => r.__localEcho).length, 0,
      "the reply's own echo reconciled away once its authoritative copy landed");
    // The tail carries the authoritative reply, the agent output, and the follow-up,
    // all live, in chronological order, none dropped.
    const tail = recs.slice(base).map(app.normalizeRecord).map((n) => n.content);
    assert.deepEqual(tail, ["1", "agent continues", "please continue"],
      "every post-respond record appended live in chronological order");
    // The DOM grew by exactly the three new records (no freeze, no re-entry).
    assert.equal(bubbleNodes(c).length, base + 3,
      "live DOM kept appending the post-respond records without a view re-entry");
  });

  // ----------------------------------------------------------------------- //
  // Symptom B: worktree first assistant record body is not normalize-dropped. //
  // ----------------------------------------------------------------------- //

  check("G3 worktree: first assistant record with top-level content normalizes non-empty", () => {
    // The plainest worktree first-record shape once G2 reads the body: a normal
    // assistant record carrying its content. normalizeRecord must keep it.
    const norm = app.normalizeRecord(
      asst("First discovery reply for the worktree session.", 1, "01_discovery_ab12", "discovery"));
    assert.equal(norm.role, "assistant");
    assert.equal(norm.content, "First discovery reply for the worktree session.");
    assert.equal(norm.stepType, "discovery");
  });

  check("G3 worktree: first assistant body recovered from raw_json (no top-level content)", () => {
    // A worktree/discovery first record may carry its text only inside the
    // stream-json `raw_json` envelope (no top-level `content`). normalizeRecord
    // must recover it rather than render an empty bubble (symptom B's empty body).
    const rec = {
      step_id: "01_discovery_ab12",
      step_type: "discovery",
      message: {
        role: "assistant",
        timestamp: 1,
        raw_json: [
          { type: "assistant", message: { content: [{ type: "text", text: "worktree first body" }] } },
        ],
      },
    };
    const norm = app.normalizeRecord(rec);
    assert.equal(norm.role, "assistant");
    assert.equal(norm.content, "worktree first body",
      "first worktree assistant body recovered, not dropped to empty");
  });

  check("G3 worktree: sidecar-merged first + following records render in full, in order", () => {
    // After a worktree merge-back the first record arrives in the primary jsonl
    // and the following ones via the `*.jsonl.from-<branch>` sidecar (G2). To the
    // frontend they are ordinary append records sharing the logical step; the
    // live view must render the first body AND keep appending the rest.
    const flowId = "flow-worktree-B";
    freshFlow(flowId, []);
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [asst("worktree first body", 1, "01_discovery_ab12", "discovery")],
    });
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [
        usr("looks good, continue", 2, "01_discovery_ab12", "discovery"),
        asst("worktree second body", 3, "01_discovery_ab12", "discovery"),
      ],
    });
    const bodies = app.state.flowConversationRecords
      .map(app.normalizeRecord).map((n) => n.content);
    assert.deepEqual(bodies,
      ["worktree first body", "looks good, continue", "worktree second body"],
      "worktree first body present and subsequent records appended in order");
    assert.ok(bodies.every((b) => b !== ""), "no empty worktree body");
    assert.ok(allUnique(app.state.flowConversationRecords));
  });

  // ----------------------------------------------------------------------- //
  // Regression two (G2): answering a DISCOVERY pause must un-freeze the live  //
  // view — the post-answer `running` step_started anchor (and the records     //
  // that follow it) must reach the live append channel and supersede the      //
  // frozen "已暂停" row, without exit/re-entry forcing a full rebuild.         //
  //                                                                           //
  // Root cause: recordKey omitted the lifecycle `status`, so a `paused`       //
  // step_status and the resumed `running` step_started for the SAME step at   //
  // the SAME wall-clock second hashed identically. dedupeAppendRecords then   //
  // dropped the running anchor as a "duplicate", so the live append never     //
  // delivered the anchor that removeSupersededStatusRows needs to顶掉 the      //
  // paused row — the view stayed frozen on 已暂停 until a full re-entry.        //
  // ----------------------------------------------------------------------- //

  // Envelope-less lifecycle anchors, matching chat_history's on-disk shape
  // (record_step_started / record_step_status write flat `type`-tagged dicts
  // with NO role/content, exactly like the waiting_for_lock record builder).
  const startedRow = (stepId, stepType, ts) => ({
    type: "step_started", step_id: stepId, step_type: stepType,
    status: "running", timestamp: ts,
  });
  const pausedRow = (stepId, stepType, ts) => ({
    type: "step_status", step_id: stepId, step_type: stepType,
    status: "paused", timestamp: ts,
  });
  const statusRows = (c) => c.children.filter((x) => x.__convStatusRow);

  check("G2 recordKey distinguishes paused vs running anchors at the same second", () => {
    // The exact collision that froze the view: same stepId / step-event role /
    // null attempt / empty content / same second-granularity timestamp, differing
    // ONLY in status. They must hash to distinct keys so the resumed running
    // anchor is never deduped against the frozen paused anchor.
    const paused = pausedRow("01_discovery_ab12", "discovery", 5);
    const running = startedRow("01_discovery_ab12", "discovery", 5);
    assert.notEqual(app.recordKey(paused), app.recordKey(running),
      "paused and running anchors of the same step/second must not collide");
    // And a true duplicate (same status) still collides, so genuine dedupe holds.
    assert.equal(app.recordKey(paused), app.recordKey(pausedRow("01_discovery_ab12", "discovery", 5)),
      "two identical paused anchors still share one key (dedupe preserved)");
  });

  check("G2 dedupeAppendRecords does not mask the running anchor colliding with a frozen paused row", () => {
    const paused = pausedRow("01_discovery_ab12", "discovery", 5);
    const running = startedRow("01_discovery_ab12", "discovery", 5);
    // Pre-fix the whole-window dedupe (status-blind key) filtered `running` out
    // entirely; it must now survive as a genuinely new record.
    const fresh = app.dedupeAppendRecords([paused], [running]);
    assert.equal(fresh.length, 1, "the resumed running anchor must not be deduped away");
    assert.equal(app.normalizeRecord(fresh[0]).status, "running");
  });

  check("G2 post-answer running anchor supersedes the frozen 已暂停 row via the live append", () => {
    const flowId = "flow-g2-resume";
    // The live view is frozen on the discovery pause: an assistant question plus
    // the paused lifecycle anchor are the last things rendered.
    const c = freshFlow(flowId, [
      asst("Which framework should I use?", 4, "01_discovery_ab12", "discovery"),
      pausedRow("01_discovery_ab12", "discovery", 5),
    ]);
    // Exactly one status row, reading 已暂停.
    let rows = statusRows(c);
    assert.equal(rows.length, 1, "starts frozen on a single paused anchor");
    assert.ok(rows[0].classList.contains("step-status-paused"));

    // The operator answers; the daemon-resumed second process re-runs discovery
    // and the daemon pushes (mode:append) the resumed running anchor at the SAME
    // second as the pause, followed by the next assistant turn.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [
        startedRow("01_discovery_ab12", "discovery", 5),
        asst("Great, proceeding with the chosen framework.", 6, "01_discovery_ab12", "discovery"),
      ],
    });

    // The paused row is superseded by the running anchor (one truthful status),
    // and the post-answer assistant turn streamed in — no re-entry needed.
    rows = statusRows(c);
    assert.equal(rows.length, 1, "the 已暂停 row is superseded, not stacked");
    assert.ok(rows[0].classList.contains("step-status-running"),
      "the surviving anchor reads 进行中 after the resume");
    const asstBodies = app.state.flowConversationRecords
      .map(app.normalizeRecord).filter((n) => n.role === "assistant").map((n) => n.content);
    assert.deepEqual(asstBodies,
      ["Which framework should I use?", "Great, proceeding with the chosen framework."],
      "the post-answer assistant turn kept streaming live");
    assert.ok(allUnique(app.state.flowConversationRecords));
  });

  check("G2 multi-round discovery: each round's paused/running anchors stay distinct, latest wins", () => {
    const flowId = "flow-g2-multiround";
    const stepId = "01_discovery_ab12";
    const c = freshFlow(flowId, [
      asst("Round 1 question?", 4, stepId, "discovery"),
      pausedRow(stepId, "discovery", 5),
    ]);
    // Operator answers round 1; resume re-runs discovery which asks ANOTHER
    // clarifying question and pauses again. The resumed `running` anchor lands at
    // the SAME second as round-1's pause (the daemon-resume collision the fix
    // targets); round-2's pause arrives a few seconds later, all on the reused
    // discovery step_id.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [
        startedRow(stepId, "discovery", 5),
        asst("Round 2 question?", 6, stepId, "discovery"),
        pausedRow(stepId, "discovery", 8),
      ],
    });
    // Round-2 paused is a genuinely-new record (not masked by round-1 paused),
    // and the region collapses to ONE current status anchor (the latest paused).
    const rows = statusRows(c);
    assert.equal(rows.length, 1, "still exactly one status anchor for the region");
    assert.ok(rows[0].classList.contains("step-status-paused"),
      "the region settles on the latest (round-2) paused anchor");
    const asstBodies = app.state.flowConversationRecords
      .map(app.normalizeRecord).filter((n) => n.role === "assistant").map((n) => n.content);
    assert.deepEqual(asstBodies, ["Round 1 question?", "Round 2 question?"],
      "both rounds' questions are visible, none dropped");
    assert.ok(allUnique(app.state.flowConversationRecords));
  });

  check("G2 a running-anchor-only post-answer batch is applied (not short-circuited as all-duplicate)", () => {
    const flowId = "flow-g2-anchor-only";
    const stepId = "01_discovery_ab12";
    const c = freshFlow(flowId, [pausedRow(stepId, "discovery", 5)]);
    assert.equal(statusRows(c).length, 1);
    // The daemon's first post-answer tick may carry ONLY the running anchor
    // (the assistant output is still streaming). Pre-fix this batch was entirely
    // a recordKey duplicate of the paused row, so applyHistoryData short-circuited
    // (`if (!fresh.length) return;`) and the supersede never ran. It must now
    // apply and flip 已暂停 → 进行中 on this very tick.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [startedRow(stepId, "discovery", 5)],
    });
    const rows = statusRows(c);
    assert.equal(rows.length, 1, "still one status anchor");
    assert.ok(rows[0].classList.contains("step-status-running"),
      "the running anchor alone is enough to un-freeze the paused row");
  });

  check("G2 full-rebuild and live-append converge on the same superseded result", () => {
    const stepId = "01_discovery_ab12";
    const records = [
      asst("Q?", 4, stepId, "discovery"),
      pausedRow(stepId, "discovery", 5),
      startedRow(stepId, "discovery", 5),
      asst("A.", 6, stepId, "discovery"),
    ];
    // Full rebuild (exit/re-enter): all records in one render pass.
    const full = freshFlow("flow-g2-full", records.slice());
    const fullRows = statusRows(full);
    // Live append: paused first, then the rest as a delta.
    const live = freshFlow("flow-g2-live", records.slice(0, 2));
    app.state.selectedFlowId = "flow-g2-live";
    app.state.flowConversationRecords = records.slice(0, 2);
    app.applyHistoryData({
      flow_id: "flow-g2-live", mode: "append", records: records.slice(2),
    });
    const liveRows = statusRows(live);
    assert.equal(fullRows.length, 1);
    assert.equal(liveRows.length, 1,
      "live append converges to the same single anchor as the full rebuild");
    assert.ok(fullRows[0].classList.contains("step-status-running"));
    assert.ok(liveRows[0].classList.contains("step-status-running"),
      "both paths settle on 进行中, never frozen on 已暂停");
  });
}


