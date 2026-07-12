/*
 * Live-append step-transition tests (Group G1, discovery→analyze freeze).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerLiveAppendStepTransitionTests({app, check, findOne, findAll})` so the
 * parent harness drives the same check() reporter and the same `app` export.
 *
 * Regression context — the long-standing "running-flow console freezes when the
 * flow transitions discovery → analyze" bug. After the operator confirms the
 * discovery plan and the engine steps from `discovery` into `analyze`, the
 * daemon keeps pushing `mode: append` increments (the discovery `step_completed`
 * terminal, the analyze `step_started` running anchor, and analyze's first
 * assistant turns). The live view MUST keep rendering them so the operator sees
 * analyze proceed WITHOUT having to leave and re-enter the session (a full
 * `mode: full` reload). The two failure modes this guards:
 *
 *   1. A transition / multi-round-discovery batch wrongly filtered empty by
 *      `dedupeAppendRecords` (a coarse recordKey collision against the recent
 *      tail), making `applyHistoryData` short-circuit (`if (!fresh.length)
 *      return;`) and the cursor stall — so every subsequent append is dropped.
 *   2. The post-transition records never reaching the DOM / state, so the live
 *      stream diverges from what a full reload (`mode: full`) would show.
 *
 * The contract these tests pin: the incremental (append) path and the
 * one-shot full-rebuild (`mode: full`) path converge on the SAME rendered
 * conversation — no loss, no duplication, no freeze.
 *
 * Record shapes mirror the REAL daemon envelope: the authoritative `step_type`
 * rides the record envelope (daemon-injected from the jsonl file-name
 * convention), `message` carries only `{role, content, timestamp}`, and the
 * lifecycle anchors (step_started / step_status / step_completed) are flat
 * `type`-tagged dicts exactly as chat_history writes them on disk.
 */
import assert from "node:assert/strict";

export function registerLiveAppendStepTransitionTests(ctx) {
  const { app, check } = ctx;

  // ----- daemon-shape record builders ------------------------------------- //
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
  // Flat lifecycle anchors (chat_history.record_step_started / _step_status).
  const startedRow = (stepId, stepType, ts) => ({
    type: "step_started", step_id: stepId, step_type: stepType,
    status: "running", timestamp: ts,
  });
  const pausedRow = (stepId, stepType, ts) => ({
    type: "step_status", step_id: stepId, step_type: stepType,
    status: "paused", timestamp: ts,
  });
  const completedRow = (stepId, stepType, ts) => ({
    type: "step_completed", step_id: stepId, step_type: stepType,
    data: { step: { step_id: stepId, step_type: stepType, status: "completed", outputs: {} } },
    timestamp: ts,
  });
  // step_output is the non-terminal usage record emitted on each PAUSED /
  // REVISION_NEEDED round of a step (it shares the step's step_id, carries no
  // top-level lifecycle `status`, and rides the same channel as the terminal
  // report). Discovery emits one per round; the terminal step_completed lands
  // when the step finishes.
  const outputRow = (stepId, stepType, ts) => ({
    type: "step_output", step_id: stepId, step_type: stepType,
    data: { step: { step_id: stepId, step_type: stepType, status: "non_terminal", outputs: {} } },
    timestamp: ts,
  });

  const keys = (recs) => recs.map(app.recordKey);
  const allUnique = (recs) => new Set(keys(recs)).size === recs.length;
  const bodies = (recs) => recs.map(app.normalizeRecord).map((n) => n.content);
  const asstBodies = (recs) =>
    recs.map(app.normalizeRecord).filter((n) => n.role === "assistant").map((n) => n.content);

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
  const statusRows = (c) => c.children.filter((x) => x.__convStatusRow);

  // The canonical discovery→analyze record stream. Discovery runs multiple
  // rounds (running → paused → running, REUSING one step_id), the operator
  // answers each round with a short reply (`1` then `按1确定`) where the resume
  // running anchor lands at the SAME wall-clock second as the answer/pause, then
  // discovery COMPLETES and analyze starts and produces its first assistant
  // turns. Built once and replayed two ways (full vs incremental).
  const DISCOVERY = "01_discovery_ab12";
  const ANALYZE = "02_analyze_cd34";
  function transitionSequence() {
    return [
      startedRow(DISCOVERY, "discovery", 1),                  // 0 discovery RUNNING
      asst("Round 1 — which option?", 2, DISCOVERY, "discovery"), // 1
      pausedRow(DISCOVERY, "discovery", 3),                   // 2 paused for input
      usr("1", 3, DISCOVERY, "discovery"),                    // 3 answer (same second)
      startedRow(DISCOVERY, "discovery", 3),                  // 4 resume (same second!)
      asst("Round 2 — confirm the plan?", 4, DISCOVERY, "discovery"), // 5
      pausedRow(DISCOVERY, "discovery", 5),                   // 6 paused again
      usr("按1确定", 5, DISCOVERY, "discovery"),               // 7 answer (same second, distinct text)
      startedRow(DISCOVERY, "discovery", 5),                  // 8 resume (same second!)
      completedRow(DISCOVERY, "discovery", 6),                // 9 discovery COMPLETED (terminal)
      startedRow(ANALYZE, "analyze", 7),                      // 10 analyze RUNNING (new step_id)
      asst("Analyzing the spec…", 8, ANALYZE, "analyze"),     // 11
      asst("Analysis complete.", 9, ANALYZE, "analyze"),      // 12
    ];
  }
  // How the daemon dribbles the same stream out as incremental append batches.
  function transitionBatches(seq) {
    return [
      seq.slice(0, 2),    // discovery running + round-1 question
      seq.slice(2, 3),    // paused
      seq.slice(3, 5),    // answer + resume (same-second collision pair)
      seq.slice(5, 6),    // round-2 question
      seq.slice(6, 7),    // paused
      seq.slice(7, 9),    // answer + resume (same-second collision pair)
      seq.slice(9, 12),   // *** THE TRANSITION BATCH: completed + analyze start + first turn ***
      seq.slice(12),      // analyze second turn
    ];
  }

  // ----------------------------------------------------------------------- //
  // 1. No freeze: the transition batch and everything after keeps streaming. //
  // ----------------------------------------------------------------------- //

  check("G1 transition: discovery→analyze keeps live-appending after the transition (no freeze)", () => {
    const flowId = "flow-transition-1";
    const c = freshFlow(flowId, []);
    const seq = transitionSequence();
    for (const batch of transitionBatches(seq)) {
      app.applyHistoryData({ flow_id: flowId, mode: "append", records: batch });
    }
    const recs = app.state.flowConversationRecords;
    // Every assistant turn — across BOTH steps — streamed in, in order, none lost.
    assert.deepEqual(asstBodies(recs), [
      "Round 1 — which option?",
      "Round 2 — confirm the plan?",
      "Analyzing the spec…",
      "Analysis complete.",
    ], "every assistant turn across the discovery→analyze transition rendered live");
    // The analyze region's content reached the DOM (the freeze symptom is that
    // nothing post-transition appears until a full re-entry).
    const domBodies = bubbleNodes(c)
      .map((b) => b.__convStepType);
    assert.ok(domBodies.includes("analyze"),
      "analyze step bubbles reached the DOM without a view re-entry");
    assert.ok(allUnique(recs), "no duplicate recordKey across the transition");
  });

  check("G1 transition: incremental append converges on the same result as a full reload", () => {
    const seq = transitionSequence();

    // Full reload path (exit + re-enter → one mode:full push of the whole stream).
    const full = freshFlow("flow-transition-full", []);
    app.applyHistoryData({ flow_id: "flow-transition-full", mode: "full", records: seq.slice() });
    const fullBodies = bodies(app.state.flowConversationRecords);
    const fullStatusRows = statusRows(full).length;
    const fullBubbles = bubbleNodes(full).length;

    // Incremental append path.
    const live = freshFlow("flow-transition-live", []);
    app.state.selectedFlowId = "flow-transition-live";
    for (const batch of transitionBatches(seq)) {
      app.applyHistoryData({ flow_id: "flow-transition-live", mode: "append", records: batch });
    }
    const liveBodies = bodies(app.state.flowConversationRecords);
    const liveStatusRows = statusRows(live).length;
    const liveBubbles = bubbleNodes(live).length;

    assert.deepEqual(liveBodies, fullBodies,
      "live-append record content equals the full-reload content (no loss, no dup)");
    assert.equal(liveStatusRows, fullStatusRows,
      "live-append settles on the same number of status anchors as a full reload");
    assert.equal(liveBubbles, fullBubbles,
      "live-append DOM bubble count equals the full-reload bubble count");
  });

  check("G1 transition: discovery status rows are superseded once it completes; analyze shows running", () => {
    const flowId = "flow-transition-2";
    const c = freshFlow(flowId, []);
    const seq = transitionSequence();
    for (const batch of transitionBatches(seq)) {
      app.applyHistoryData({ flow_id: flowId, mode: "append", records: batch });
    }
    const rows = statusRows(c);
    // discovery: its non-terminal anchors (进行中 / 已暂停) are superseded by the
    // step_completed terminal report → it contributes ZERO status rows. analyze:
    // one running anchor. So exactly one status row remains and it reads 进行中.
    assert.equal(rows.length, 1,
      "only the analyze running anchor remains; discovery's are superseded by its report");
    assert.ok(rows[0].classList.contains("step-status-running"),
      "the surviving anchor is analyze's In progress anchor");
    assert.equal(rows[0].__convStepType, "analyze",
      "the surviving status row belongs to the analyze region");
  });

  // ----------------------------------------------------------------------- //
  // 2. The transition batch re-delivered (REST snapshot ∩ WS broadcast       //
  //    overlap) renders exactly once.                                        //
  // ----------------------------------------------------------------------- //

  check("G1 transition: a re-delivered transition batch is deduped to a single render", () => {
    const flowId = "flow-transition-3";
    const seq = transitionSequence();
    const c = freshFlow(flowId, []);
    // Stream everything up to (but excluding) the transition batch.
    const batches = transitionBatches(seq);
    for (let i = 0; i < 6; i++) {
      app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[i] });
    }
    const before = app.state.flowConversationRecords.length;
    const domBefore = bubbleNodes(c).length;

    // The transition batch arrives — once via the REST pull, then the SAME batch
    // re-broadcast over WS. dedupeAppendRecords must filter the second copy.
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[6] });
    const afterFirst = app.state.flowConversationRecords.length;
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[6] });
    assert.equal(app.state.flowConversationRecords.length, afterFirst,
      "the re-delivered transition batch must not append duplicates");
    assert.ok(afterFirst > before, "the transition batch did genuinely append once");
    assert.ok(bubbleNodes(c).length > domBefore,
      "the transition batch reached the DOM exactly once");
    assert.ok(allUnique(app.state.flowConversationRecords),
      "no duplicate recordKey after the REST/WS overlap of the transition batch");

    // And streaming continues after the overlap (the final analyze turn).
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[7] });
    assert.ok(asstBodies(app.state.flowConversationRecords).includes("Analysis complete."),
      "post-transition analyze output keeps streaming after the overlap dedup");
  });

  // ----------------------------------------------------------------------- //
  // 3. Distinct short replies (『1』/『按1确定』) at the same wall-clock second  //
  //    are NOT collapsed by the dedup (they carry distinct content).         //
  // ----------------------------------------------------------------------- //

  check("G1 transition: a terminal step_completed is not deduped against a same-second step_output", () => {
    // During discovery's last round the daemon may push a non-terminal
    // step_output (round usage) and the terminal step_completed within the SAME
    // wall-clock second on the SAME step_id. Both are role:"step-event" with no
    // top-level lifecycle `status` and empty content, so a status-only recordKey
    // hashes them identically — the terminal report would then be deduped away,
    // the discovery region would never show its completion (and its status
    // anchors would never be superseded). recordKey must distinguish them by the
    // event `kind`.
    const out = outputRow(DISCOVERY, "discovery", 6);
    const done = completedRow(DISCOVERY, "discovery", 6);
    assert.notEqual(app.recordKey(out), app.recordKey(done),
      "step_output and step_completed of the same step/second must not collide on recordKey");
    const fresh = app.dedupeAppendRecords([out], [done]);
    assert.equal(fresh.length, 1,
      "the terminal step_completed must survive the dedup against a prior step_output");
    assert.equal(app.normalizeRecord(fresh[0]).kind, "step_completed");

    // End-to-end: the terminal report reaches the DOM and supersedes the
    // discovery status anchors even when a same-second step_output precedes it.
    const flowId = "flow-transition-output";
    const c = freshFlow(flowId, [
      startedRow(DISCOVERY, "discovery", 1),
      asst("Round 1 — which option?", 2, DISCOVERY, "discovery"),
      pausedRow(DISCOVERY, "discovery", 6),
    ]);
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [outputRow(DISCOVERY, "discovery", 6), completedRow(DISCOVERY, "discovery", 6)],
    });
    assert.equal(c.children.filter((x) => x.__convTerminalRow).length, 1,
      "the discovery step_completed report card rendered (not deduped away)");
    assert.equal(statusRows(c).length, 0,
      "the discovery status anchors are superseded once its terminal report lands");
    assert.ok(allUnique(app.state.flowConversationRecords));
  });

  check("G1 transition: distinct short replies at the same second both survive the dedup", () => {
    const flowId = "flow-transition-4";
    freshFlow(flowId, []);
    // Two genuinely-distinct answers emitted within the same wall-clock second.
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [usr("1", 5, DISCOVERY, "discovery")],
    });
    app.applyHistoryData({
      flow_id: flowId, mode: "append",
      records: [usr("按1确定", 5, DISCOVERY, "discovery")],
    });
    const userBodies = app.state.flowConversationRecords
      .map(app.normalizeRecord).filter((n) => n.role === "user").map((n) => n.content);
    assert.deepEqual(userBodies, ["1", "按1确定"],
      "two distinct same-second replies both kept — only content-identical duplicates collapse");
    assert.ok(allUnique(app.state.flowConversationRecords));
  });

  // ----------------------------------------------------------------------- //
  // 4. Cursor / state coherence: a genuinely-all-duplicate append short      //
  //    circuits WITHOUT stalling the render cursor, so the very next genuine  //
  //    append still renders (the precise "freeze after a duplicate" failure). //
  // ----------------------------------------------------------------------- //

  check("G1 transition: an all-duplicate append short-circuits but does NOT freeze the next genuine append", () => {
    const flowId = "flow-transition-5";
    const seq = transitionSequence();
    const c = freshFlow(flowId, []);
    const batches = transitionBatches(seq);
    // Stream up through the transition batch.
    for (let i = 0; i < 7; i++) {
      app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[i] });
    }
    const lenBefore = app.state.flowConversationRecords.length;
    const domBefore = bubbleNodes(c).length;
    const cursorBefore = c.__convState && c.__convState.count;

    // A pure-duplicate re-push of the transition batch: must short-circuit with
    // NO state change AND NO cursor movement (state.flowConversationRecords and
    // __convState.count stay in lock-step — the task-4 coherence invariant).
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[6] });
    assert.equal(app.state.flowConversationRecords.length, lenBefore,
      "all-duplicate append leaves state unchanged");
    assert.equal(c.__convState.count, cursorBefore,
      "all-duplicate append leaves the render cursor unchanged (no stall, no skip)");
    assert.equal(c.__convState.count, app.state.flowConversationRecords.length,
      "render cursor and held-record count stay in lock-step after the short-circuit");

    // The NEXT genuinely-new append (the final analyze turn) MUST still render —
    // i.e. the short-circuit did not poison the cursor.
    app.applyHistoryData({ flow_id: flowId, mode: "append", records: batches[7] });
    assert.equal(app.state.flowConversationRecords.length, lenBefore + batches[7].length,
      "the next genuine append still lands after an all-duplicate short-circuit");
    assert.equal(bubbleNodes(c).length, domBefore + batches[7].length,
      "the next genuine append reaches the DOM (no freeze)");
    assert.equal(c.__convState.count, app.state.flowConversationRecords.length,
      "render cursor caught up to the held-record count");
  });
}
