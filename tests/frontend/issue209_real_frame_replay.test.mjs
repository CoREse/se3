/*
 * Issue #209 — frontend real-frame replay guard (G3 task 2).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerIssue209RealFrameReplayTests({app, check, findOne, findAll})`.
 *
 * Unlike the hand-authored live_append_step_transition / _retry_after_error
 * scenarios and the regenerable golden console_e2e_frames fixture, the frames
 * replayed here are the EXACT real frame sequence G1 captured for issue #209 —
 * `tests/frontend/fixtures/issue_209/daemon_frames.json` — produced by the real
 * `DaemonHistoryReader.read_active_flows` over the real on-disk records of a
 * `se3 run` that ran discovery→analyze→plan (plan failed), exercising BOTH #209
 * triggers:
 *
 *   - frame 0 `disc-stream-1`  — mode:full,   3 records (discovery)
 *   - frame 1 `disc-paused`    — mode:append, 3 records (discovery + paused)
 *   - frame 2 `resume-burst`   — mode:append, 14 records (discovery resume
 *     step_started/step_completed + ALL of analyze AND plan, incl. plan's
 *     step_failed)
 *
 * G1 localized the #209 freeze to **daemon push-loop starvation under load**,
 * NOT the frontend: replaying these real frames through the production
 * `applyHistoryData` / `dedupeAppendRecords` shows the live incremental-append
 * path converges on the full-reload (`mode: full`) path — no loss, no dup, no
 * freeze. This guard pins that the frontend stays correct on the real #209
 * frames, so a future frontend change cannot silently reintroduce a
 * frame-handling defect that would be misattributed as the #209 freeze. (The
 * fail-before/pass-after regression lock for the actual root cause lives at the
 * daemon layer in tests/test_issue209_live_append_regression.py.)
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export function registerIssue209RealFrameReplayTests(ctx) {
  const { app, check } = ctx;

  const here = path.dirname(fileURLToPath(import.meta.url));
  const fixturePath = path.join(here, "fixtures", "issue_209", "daemon_frames.json");
  const frames = JSON.parse(fs.readFileSync(fixturePath, "utf8")).frames;
  // The authoritative full reload is the in-order concatenation of every
  // frame's records (= the on-disk record sequence the daemon read).
  const fullSnapshot = frames.flatMap((f) => f.records);

  const keys = (recs) => recs.map(app.recordKey);
  const allUnique = (recs) => new Set(keys(recs)).size === recs.length;
  const bodies = (recs) => recs.map(app.normalizeRecord).map((n) => n.content);
  const asstBodies = (recs) =>
    recs.map(app.normalizeRecord).filter((n) => n.role === "assistant").map((n) => n.content);
  const stepTypes = (recs) =>
    new Set(recs.map(app.normalizeRecord).map((n) => n.stepType));

  function freshFlow(flowId) {
    app.state.selectedFlowId = flowId;
    app.state.flowConversationRecords = [];
    app.state.flowConversationProgress = null;
    const c = document.getElementById("flow-conversation");
    c.innerHTML = "";
    c.__convState = null;
    return c;
  }
  const bubbleNodes = (c) => c.children.filter((x) => x.__convIdx !== undefined);

  function replayLive(flowId) {
    const c = freshFlow(flowId);
    for (const frame of frames) {
      app.applyHistoryData({ flow_id: flowId, mode: frame.mode, records: frame.records });
    }
    return c;
  }

  // -- 1. live incremental replay converges on the full-reload snapshot ----- //

  check("#209 real frames: live incremental replay equals the full reload (no loss/dup/freeze)", () => {
    const live = replayLive("i209-live");
    const liveRecs = app.state.flowConversationRecords;
    const liveBodies = bodies(liveRecs);
    const liveBubbles = bubbleNodes(live).length;

    const full = freshFlow("i209-full");
    app.applyHistoryData({ flow_id: "i209-full", mode: "full", records: fullSnapshot.slice() });
    const fullBodies = bodies(app.state.flowConversationRecords);
    const fullBubbles = bubbleNodes(full).length;

    assert.deepEqual(liveBodies, fullBodies,
      "live-append content equals the full-reload content on the real #209 frames");
    assert.equal(liveBubbles, fullBubbles,
      "live-append DOM bubble count equals the full-reload bubble count");
    assert.ok(allUnique(liveRecs), "no duplicate recordKey across the real-frame replay");
  });

  // -- 2. the discovery→analyze transition AND plan failure reach the view -- //

  check("#209 real frames: analyze + plan surface live after the discovery→analyze transition", () => {
    const c = replayLive("i209-transition");
    const recs = app.state.flowConversationRecords;
    const types = stepTypes(recs);
    assert.ok(types.has("analyze"), "analyze records surfaced into the live conversation");
    assert.ok(types.has("plan"), "plan records surfaced into the live conversation");
    // analyze content reached the DOM (the freeze symptom is nothing
    // post-transition appearing until a full re-entry).
    const domTypes = new Set(bubbleNodes(c).map((b) => b.__convStepType));
    assert.ok(domTypes.has("analyze"), "analyze bubbles reached the DOM without a view re-entry");
    // The plan step_failed terminal (the error trigger) is present.
    assert.ok(
      recs.map(app.normalizeRecord).some((n) => n.kind === "step_failed"),
      "the plan step_failed terminal (the #209 error trigger) reached the conversation");
    // Every assistant turn across all three steps streamed in (none lost).
    assert.ok(asstBodies(recs).length >= 6,
      "all assistant turns across discovery/analyze/plan streamed in");
  });

  // -- 3. the resume-burst transition batch re-delivered dedupes to one render //

  check("#209 real frames: a re-delivered resume-burst batch is deduped to a single render", () => {
    const flowId = "i209-overlap";
    const c = freshFlow(flowId);
    // Stream discovery's full + paused frames, then the resume burst once.
    app.applyHistoryData({ flow_id: flowId, mode: frames[0].mode, records: frames[0].records });
    app.applyHistoryData({ flow_id: flowId, mode: frames[1].mode, records: frames[1].records });
    const before = app.state.flowConversationRecords.length;
    app.applyHistoryData({ flow_id: flowId, mode: frames[2].mode, records: frames[2].records });
    const afterFirst = app.state.flowConversationRecords.length;
    assert.ok(afterFirst > before, "the resume-burst batch genuinely appended once");

    // The SAME resume-burst re-broadcast (REST snapshot ∩ WS overlap) must dedupe.
    app.applyHistoryData({ flow_id: flowId, mode: frames[2].mode, records: frames[2].records });
    assert.equal(app.state.flowConversationRecords.length, afterFirst,
      "the re-delivered resume-burst batch must not append duplicates");
    assert.ok(allUnique(app.state.flowConversationRecords),
      "no duplicate recordKey after the REST/WS overlap of the resume burst");
    assert.ok(
      stepTypes(app.state.flowConversationRecords).has("analyze"),
      "analyze still present exactly once after the overlap dedup");
  });
}
