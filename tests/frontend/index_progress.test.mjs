/*
 * Code-index update-progress marker tests (Group G3).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerIndexProgressTests({app, check, findOne, findAll})` so the parent
 * harness can drive the same check() reporter and the same `app` module export.
 *
 * The commit step rebuilds se3/code-index.md before staging, re-summarising
 * every touched source node; that rebuild emits one `type:'index_progress'`
 * NDJSON line per file/dir node (via chat_history.record_index_progress). The
 * web console renders these as a single live "Updating code-index: <path> (i/N)"
 * progress line that updates in place as the counts climb.
 *
 * Coverage:
 *   (a) normalizeRecord recognizes `type:'index_progress'`, mapping
 *       path / node-kind / done / total / phase / stepType('commit').
 *   (b) indexProgressLabel + indexProgressState pure helpers.
 *   (c) the marker renders as an `.index-progress-marker` with the right
 *       `.status-<state>` class, successive records of one step converge to a
 *       SINGLE in-place line (not stacked bubbles), the LATEST record (by
 *       ts/idx) wins — so a step retry's second rebuild shows its own live
 *       progress rather than freezing on the first rebuild's ✓ — and a
 *       non-index_progress record renders unaffected.
 */
import assert from "node:assert/strict";

export function registerIndexProgressTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  const ipRecord = (path, done, total, ts, opts = {}) => ({
    step_id: opts.stepId || "09_commit_abcd1234",
    step_type: "commit",
    message: {
      type: "index_progress",
      role: "system",
      step_type: "commit",
      path: path,
      kind: opts.kind || "file",
      done: done,
      total: total,
      phase: opts.phase || "file",
      timestamp: ts,
    },
  });

  // ---- (a) normalizeRecord -------------------------------------------------
  check("G3 normalizeRecord recognizes index_progress and maps fields", () => {
    const norm = app.normalizeRecord(ipRecord("src/tianluo/cli.py", 3, 12, 42));
    assert.equal(norm.kind, "index_progress");
    assert.equal(norm.role, "index-progress");
    assert.equal(norm.path, "src/tianluo/cli.py");
    assert.equal(norm.indexKind, "file");
    assert.equal(norm.done, 3);
    assert.equal(norm.total, 12);
    assert.equal(norm.phase, "file");
    assert.equal(norm.stepType, "commit");
    assert.equal(norm.timestamp, 42);
  });

  check("G3 normalizeRecord defaults stepType to commit when absent", () => {
    const norm = app.normalizeRecord({
      message: { type: "index_progress", path: "a.py", done: 1, total: 2 },
    });
    assert.equal(norm.stepType, "commit");
    assert.equal(norm.path, "a.py");
  });

  check("G3 normalizeRecord coerces non-numeric done/total to 0", () => {
    const norm = app.normalizeRecord({
      message: { type: "index_progress", path: "a.py", done: null, total: "x" },
    });
    assert.equal(norm.done, 0);
    assert.equal(norm.total, 0);
  });

  // ---- (b) pure helpers ----------------------------------------------------
  check("G3 indexProgressLabel formats path with (done/total)", () => {
    assert.equal(
      app.indexProgressLabel("src/tianluo/cli.py", 3, 12),
      "Updating code-index: src/tianluo/cli.py (3/12)",
    );
  });

  check("G3 indexProgressLabel drops the suffix when total is unknown", () => {
    assert.equal(app.indexProgressLabel("a.py", 0, 0), "Updating code-index: a.py");
  });

  check("G3 indexProgressLabel degrades a missing path to an ellipsis", () => {
    assert.equal(app.indexProgressLabel("", 1, 3), "Updating code-index: … (1/3)");
    assert.equal(app.indexProgressLabel(null, 0, 0), "Updating code-index: …");
  });

  check("G3 indexProgressState flips to completed only when done>=total>0", () => {
    assert.equal(app.indexProgressState(3, 12), "running");
    assert.equal(app.indexProgressState(12, 12), "completed");
    assert.equal(app.indexProgressState(13, 12), "completed");
    // total 0 is an unknown/empty build, never "completed".
    assert.equal(app.indexProgressState(0, 0), "running");
  });

  // ---- (c) render path -----------------------------------------------------
  check("G3 index_progress renders an .index-progress-marker with status class", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [ipRecord("src/tianluo/cli.py", 3, 12, 1)], false);
    const marker = findOne(container, "index-progress-marker");
    assert.ok(marker, "expected an .index-progress-marker row");
    assert.ok(marker.classList.contains("status-running"),
      "an in-progress marker should carry the .status-running class");
    const text = findOne(marker, "index-progress-text");
    assert.ok(text && text.textContent === "Updating code-index: src/tianluo/cli.py (3/12)",
      `marker text should be the progress label, got ${text && text.textContent}`);
    // Affordance-free: no fold / raw / chip on the marker.
    assert.equal(findAll(marker, "msg-chip").length, 0,
      "marker must not contain a chip");
  });

  check("G3 completed marker carries the .status-completed class", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [ipRecord("dir/", 12, 12, 1, { kind: "dir", phase: "dir" })], false);
    const marker = findOne(container, "index-progress-marker");
    assert.ok(marker.classList.contains("status-completed"),
      "a done>=total marker should carry the .status-completed class");
  });

  check("G3 successive records of one step converge to one in-place line", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      ipRecord("a.py", 1, 3, 1),
      ipRecord("b.py", 2, 3, 2),
      ipRecord("c.py", 3, 3, 3),
    ], false);
    // The three records of the SAME commit step fold to ONE line — the latest /
    // terminal state — instead of stacking three progress rows.
    const lines = findAll(container, "index-progress-text").map((n) => n.textContent);
    assert.deepEqual(lines, ["Updating code-index: c.py (3/3)"]);
    assert.equal(findAll(container, "index-progress-marker").length, 1);
    const marker = findOne(container, "index-progress-marker");
    assert.ok(marker.classList.contains("status-completed"),
      "the surviving line must reflect the completed (3/3) state");
  });

  check("G3 the terminal marker wins even if records arrive out of order", () => {
    const container = document.createElement("div");
    // Terminal (3/3) arrives with a LATER ts, then a stale running record.
    app.renderConversation(container, [
      ipRecord("a.py", 1, 3, 1),
      ipRecord("c.py", 3, 3, 3),
      ipRecord("b.py", 2, 3, 2),
    ], false);
    assert.equal(findAll(container, "index-progress-marker").length, 1);
    const line = findOne(container, "index-progress-text");
    assert.equal(line.textContent, "Updating code-index: c.py (3/3)",
      "the terminal (done>=total) marker must supersede the running ones");
  });

  check("G3 a second rebuild under the same step_id shows its live progress, not the first's ✓", () => {
    const container = document.createElement("div");
    // Attempt 1 completed (2/2, terminal). The step is retried after source
    // files changed and ensure_code_index_fresh appends a fresh rebuild's
    // running records (later ts) to the SAME {step_id}.jsonl. The surviving
    // line must track the NEW rebuild's live count, not freeze on the earlier
    // rebuild's completed "✓ (2/2)" marker.
    app.renderConversation(container, [
      ipRecord("a.py", 1, 2, 1),
      ipRecord("b.py", 2, 2, 2),   // attempt 1 terminal (done>=total)
      ipRecord("a.py", 1, 3, 3),   // attempt 2 starts, running, later ts
    ], false);
    assert.equal(findAll(container, "index-progress-marker").length, 1);
    const marker = findOne(container, "index-progress-marker");
    const line = findOne(marker, "index-progress-text");
    assert.equal(line.textContent, "Updating code-index: a.py (1/3)",
      "the newer rebuild's running record must supersede the earlier terminal one");
    assert.ok(marker.classList.contains("status-running"),
      "the line must reflect the second rebuild's in-progress state");
  });

  check("G3 index_progress markers do not disturb an interleaved assistant bubble", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      ipRecord("a.py", 1, 2, 1),
      {
        step_id: "09_commit_abcd1234",
        step_type: "commit",
        message: { role: "assistant", content: "committing now", timestamp: 2 },
      },
      ipRecord("b.py", 2, 2, 3),
    ], false);
    // The two markers converge to one line, and the assistant bubble survives.
    assert.equal(findAll(container, "index-progress-marker").length, 1);
    const bubble = findOne(container, "conv-bubble");
    assert.ok(bubble && bubble.textContent.includes("committing now"),
      "interleaved assistant content must survive the marker convergence");
  });

  check("G3 markers of different steps stay independent, never folded together", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      ipRecord("a.py", 2, 2, 1, { stepId: "09_commit_aaaa1111" }),
      ipRecord("b.py", 2, 2, 2, { stepId: "13_commit_bbbb2222" }),
    ], false);
    // Distinct step_ids → distinct keys → two independent lines.
    assert.equal(findAll(container, "index-progress-marker").length, 2,
      "markers under different step_ids must not collapse together");
  });

  check("G3 incremental append of further records stays one in-place line", () => {
    const container = document.createElement("div");
    // First batch: the rebuild starts (1/3).
    app.renderConversation(container, [ipRecord("a.py", 1, 3, 1)], false);
    assert.equal(findAll(container, "index-progress-marker").length, 1);
    // Second batch arrives via incremental append (append=true, cumulative).
    app.renderConversation(container, [
      ipRecord("a.py", 1, 3, 1),
      ipRecord("b.py", 2, 3, 2),
      ipRecord("c.py", 3, 3, 3),
    ], true);
    assert.equal(findAll(container, "index-progress-marker").length, 1,
      "after the live append the step still shows exactly one line");
    const line = findOne(container, "index-progress-text");
    assert.equal(line.textContent, "Updating code-index: c.py (3/3)");
  });

  check("G3 a non-index_progress record renders unaffected alongside markers", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      ipRecord("a.py", 1, 1, 1),
      {
        step_id: "09_commit_abcd1234",
        step_type: "commit",
        message: { role: "assistant", content: "done", timestamp: 2 },
      },
    ], false);
    const bubble = findOne(container, "conv-bubble");
    assert.ok(bubble && bubble.textContent.includes("done"),
      "the ordinary chat bubble must render normally");
    assert.equal(findAll(container, "index-progress-marker").length, 1);
  });
}
