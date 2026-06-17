/*
 * G4 — end-to-end console-consistency capstone (frontend half).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerConsoleE2EConsistencyTests({app, check, findOne, findAll})`.
 *
 * This is the FRONTEND half of the cross-layer end-to-end bridge that locks the
 * long-standing running-flow *freeze* regression. The records replayed here are
 * NOT hand-authored: they are the GOLDEN FIXTURE produced by the Python test
 * `tests/test_server_history_live_append_broadcast.py`, which drives the REAL
 * daemon `DaemonHistoryReader` over a real on-disk
 * `se3/history/<flow>/<step>.jsonl` + `engine.json` evolution and pipes every
 * incremental delta through the REAL server `_handle_message` (cache write +
 * `/ws/ui` broadcast). The fixture therefore captures exactly the bytes a
 * subscribed live console receives:
 *
 *   daemon incremental read → server broadcast → THIS frontend consumer.
 *
 * For BOTH freeze-triggering scenarios — the discovery→analyze confirmation
 * transition and a step-failure → manual-retry — this test proves the
 * incremental (live `mode: append`) render path converges on the full-reload
 * (`mode: full` GET /api/history snapshot) render path: no loss, no duplication,
 * no freeze — WITHOUT the operator having to leave and re-enter the session.
 *
 * If the daemon record shape or the scenario scripts change, regenerate the
 * fixture with `SE3_REGEN_GOLDEN=1 pytest
 * tests/test_server_history_live_append_broadcast.py` (the Python golden-check
 * test fails until you do).
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export function registerConsoleE2EConsistencyTests(ctx) {
  const { app, check } = ctx;

  const here = path.dirname(fileURLToPath(import.meta.url));
  const fixturePath = path.join(here, "fixtures", "console_e2e_frames.json");
  const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));

  const keys = (recs) => recs.map(app.recordKey);
  const allUnique = (recs) => new Set(keys(recs)).size === recs.length;
  const bodies = (recs) => recs.map(app.normalizeRecord).map((n) => n.content);
  const asstBodies = (recs) =>
    recs.map(app.normalizeRecord).filter((n) => n.role === "assistant").map((n) => n.content);

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
  const statusRows = (c) => c.children.filter((x) => x.__convStatusRow);

  // Replay the captured daemon→server broadcast frames into the live consumer.
  function replayLive(flowId, frames) {
    const c = freshFlow(flowId);
    for (const frame of frames) {
      app.applyHistoryData({ flow_id: flowId, mode: frame.mode, records: frame.records });
    }
    return c;
  }

  // The authoritative full reload (exit + re-enter → one mode:full snapshot).
  function replayFull(flowId, snapshot) {
    const c = freshFlow(flowId);
    app.applyHistoryData({ flow_id: flowId, mode: "full", records: snapshot });
    return c;
  }

  for (const [name, sc] of Object.entries(fixture)) {
    check(`G4 e2e (${name}): live daemon→server→frontend stream converges with the full snapshot`, () => {
      const live = replayLive(`e2e-${name}-live`, sc.frames);
      const liveRecs = app.state.flowConversationRecords;
      const liveBodies = bodies(liveRecs);
      const liveBubbles = bubbleNodes(live).length;
      const liveStatus = statusRows(live).length;
      assert.ok(allUnique(liveRecs), "no duplicate recordKey across the live stream (no dup)");

      const full = replayFull(`e2e-${name}-full`, sc.snapshot);
      const fullBodies = bodies(app.state.flowConversationRecords);

      // The incremental live render equals the full-reload render — the precise
      // "you must reload to see the rest" freeze is impossible if these agree.
      assert.deepEqual(liveBodies, fullBodies,
        "incremental live content equals the full-snapshot content (no loss, no freeze)");
      assert.equal(liveBubbles, bubbleNodes(full).length,
        "live DOM bubble count equals the full-reload bubble count");
      assert.equal(liveStatus, statusRows(full).length,
        "live settles on the same status-anchor count as a full reload");
    });

    check(`G4 e2e (${name}): a re-delivered final live frame is deduped (REST/WS overlap, no freeze)`, () => {
      const flowId = `e2e-${name}-overlap`;
      const c = freshFlow(flowId);
      // Stream every captured frame.
      for (const frame of sc.frames) {
        app.applyHistoryData({ flow_id: flowId, mode: frame.mode, records: frame.records });
      }
      const lenBefore = app.state.flowConversationRecords.length;
      const cursorBefore = c.__convState && c.__convState.count;

      // The final live append is re-delivered (the snapshot ∩ WS broadcast race):
      // it must be deduped to a no-op WITHOUT stalling the render cursor.
      const last = sc.frames[sc.frames.length - 1];
      app.applyHistoryData({ flow_id: flowId, mode: "append", records: last.records });
      assert.equal(app.state.flowConversationRecords.length, lenBefore,
        "the re-delivered final frame appended no duplicate");
      assert.equal(c.__convState.count, cursorBefore,
        "the duplicate short-circuit left the render cursor in lock-step (no stall)");
      assert.ok(allUnique(app.state.flowConversationRecords),
        "no duplicate recordKey after the REST/WS overlap");
    });
  }

  // ---- scenario-specific content presence (the post-trigger continuation) -- //

  check("G4 e2e (transition): post-confirmation analyze output renders live in the DOM", () => {
    const c = replayLive("e2e-transition-content", fixture.transition.frames);
    const recs = app.state.flowConversationRecords;
    assert.ok(asstBodies(recs).includes("Analyzing the spec…"),
      "the analyze step's first turn streamed in after the discovery→analyze transition");
    assert.ok(asstBodies(recs).includes("Analysis complete."),
      "analyze keeps streaming live after the transition");
    const stepTypes = bubbleNodes(c).map((b) => b.__convStepType);
    assert.ok(stepTypes.includes("analyze"),
      "analyze step bubbles reached the DOM without a view re-entry (no freeze)");
  });

  check("G4 e2e (retry): post-failure retry output renders live in the DOM", () => {
    const c = replayLive("e2e-retry-content", fixture.retry.frames);
    const recs = app.state.flowConversationRecords;
    // The doomed attempt and the similar-looking retry draft both survive, plus
    // the fresh success turn — the retry was not mistaken for a duplicate.
    assert.deepEqual(asstBodies(recs), [
      "Drafting the spec update…",
      "Drafting the spec update…",
      "Spec update applied.",
    ], "the retry's similar-content draft + success turn streamed live, none deduped away");
    assert.ok(bubbleNodes(c).length > 0, "retry output reached the DOM (no freeze)");
  });
}
